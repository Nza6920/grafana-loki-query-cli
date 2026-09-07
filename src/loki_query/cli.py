from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
import os
from pathlib import Path
import sys
from typing import Any, TextIO

from .client import AuthenticationError, Opener, QueryError, default_opener, query_range
from .config import ConfigurationError, default_config_path, load_config
from .output import write_entries
from .time_range import TimeRangeError, resolve_time_range


EXIT_CONFIG = 2
EXIT_AUTH = 3
EXIT_QUERY = 4


class _InstalledVersionAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        try:
            installed_version = version("loki-query")
        except PackageNotFoundError:
            parser.error("distribution metadata is unavailable; install loki-query")
        print(f"{parser.prog} {installed_version}")
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loki-query",
        description="Query Loki logs through a Grafana datasource proxy.",
    )
    parser.add_argument(
        "--version",
        action=_InstalledVersionAction,
        nargs=0,
        help="show program's version number and exit",
    )
    parser.add_argument(
        "--config",
        type=str,
        help="Configuration file (default: environment or platform config path).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    query_parser = subparsers.add_parser(
        "query",
        help="Run a Loki query_range request.",
    )
    query_parser.add_argument("--profile", required=True, help="Named target profile.")
    range_group = query_parser.add_mutually_exclusive_group()
    range_group.add_argument("--since", help="Relative duration such as 15m or 2h.")
    range_group.add_argument("--start", help="RFC 3339 range start.")
    query_parser.add_argument("--end", help="RFC 3339 range end (default: now).")
    query_parser.add_argument(
        "--limit",
        type=_limit,
        default=100,
        help="Maximum log entries (1-5000; default: 100).",
    )
    query_parser.add_argument(
        "--output",
        choices=("human", "raw", "jsonl"),
        default="human",
        help="Output format (default: human).",
    )
    query_parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=30.0,
        help="HTTP timeout in seconds (default: 30).",
    )
    query_parser.add_argument("logql", help="Complete LogQL query, or - to read stdin.")
    profiles_parser = subparsers.add_parser(
        "profiles", help="Inspect configured profiles."
    )
    profiles_subparsers = profiles_parser.add_subparsers(
        dest="profiles_command", required=True
    )
    profiles_subparsers.add_parser("list", help="List configured profile names.")
    config_parser = subparsers.add_parser(
        "config", help="Inspect and validate configuration."
    )
    config_subparsers = config_parser.add_subparsers(
        dest="config_command", required=True
    )
    config_subparsers.add_parser("path", help="Print the default configuration path.")
    config_subparsers.add_parser("validate", help="Validate the configuration file.")
    return parser


def _limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("limit must be an integer") from error
    if not 1 <= parsed <= 5000:
        raise argparse.ArgumentTypeError("limit must be between 1 and 5000")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timeout must be a number") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("timeout must be positive")
    return parsed


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    opener: Opener = default_opener,
    now: datetime | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    environment = environ if environ is not None else os.environ
    input_stream = stdin if stdin is not None else sys.stdin
    output_stream = stdout if stdout is not None else sys.stdout
    error_stream = stderr if stderr is not None else sys.stderr
    try:
        return _dispatch(
            args,
            environment=environment,
            input_stream=input_stream,
            output_stream=output_stream,
            error_stream=error_stream,
            opener=opener,
            now=now or datetime.now(UTC),
        )
    except (ConfigurationError, TimeRangeError) as error:
        print(f"error: {error}", file=error_stream)
        return EXIT_CONFIG
    except AuthenticationError as error:
        print(f"error: {error}", file=error_stream)
        return EXIT_AUTH
    except QueryError as error:
        print(f"error: {error}", file=error_stream)
        return EXIT_QUERY


def _dispatch(
    args: argparse.Namespace,
    *,
    environment: Mapping[str, str],
    input_stream: TextIO,
    output_stream: TextIO,
    error_stream: TextIO,
    opener: Opener,
    now: datetime,
) -> int:
    selected_config_path = (
        Path(args.config).expanduser()
        if args.config
        else default_config_path(environ=environment)
    )
    if args.command == "config" and args.config_command == "path":
        print(selected_config_path, file=output_stream)
    elif args.command == "config" and args.config_command == "validate":
        config = load_config(selected_config_path)
        count = len(config.profiles)
        noun = "profile" if count == 1 else "profiles"
        print(f"Configuration is valid ({count} {noun}).", file=output_stream)
    elif args.command == "profiles" and args.profiles_command == "list":
        config = load_config(selected_config_path)
        for profile_name in sorted(config.profiles):
            print(profile_name, file=output_stream)
    elif args.command == "query":
        if args.end and not args.start:
            raise ConfigurationError(
                "--end requires --start and cannot be combined with --since."
            )
        config = load_config(selected_config_path)
        profile = config.profiles.get(args.profile)
        if profile is None:
            raise ConfigurationError(f"Unknown profile: {args.profile!r}.")
        token = environment.get(profile.token_env)
        if not token:
            print(
                f"error: token environment variable {profile.token_env!r} is not set.",
                file=error_stream,
            )
            return EXIT_AUTH
        query = input_stream.read() if args.logql == "-" else args.logql
        if not query.strip():
            raise ConfigurationError("LogQL query must not be empty.")
        start_ns, end_ns = resolve_time_range(
            since=args.since,
            start=args.start,
            end=args.end,
            now=now,
        )
        entries = query_range(
            profile=profile,
            token=token,
            query=query.strip(),
            start_ns=start_ns,
            end_ns=end_ns,
            limit=args.limit,
            timeout=args.timeout,
            opener=opener,
        )
        write_entries(entries, args.output, output_stream)
    return 0
