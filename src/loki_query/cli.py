from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
import os
import math
from pathlib import Path
import sys
import time
from typing import Any, NoReturn, Protocol, TextIO
from urllib.request import Request

from .client import (
    AuthenticationError,
    HttpAttemptBudgetError,
    Opener,
    QueryError, QueryResourceError, QueryTimeoutError,
    DEFAULT_RESPONSE_BYTES, MAX_RESPONSE_BYTES, DEFAULT_RETAINED_BYTES, MAX_RETAINED_BYTES,
    ReadableResponse,
    Sleeper,
    default_opener,
    query_range,
    query_endpoint, query_error_reason,
)
from .config import ConfigurationError, default_config_path, load_config
from .persistence import WorkDirectory
from .output import (write_result, BudgetedStream, OutputBudget, set_retrieval_status,
                     DEFAULT_RECORD_BYTES, MAX_RECORD_BYTES, DEFAULT_OUTPUT_BYTES, MAX_OUTPUT_BYTES)
from .reporting import log_report, validate_report_destination, write_log_report
from .presentation import present_logs
from .scanning import ScanOptions, ScanOutcome, scan_range
from .time_range import TimeRangeError, resolve_time_range
from .trace import (annotate_trace, configure_trace_parser, run_trace,
                    validate_trace_resume, write_trace_outcome)


EXIT_CONFIG = 2
EXIT_AUTH = 3
EXIT_QUERY = 4


class _ParserExit(Exception):
    """An expected parser outcome, converted to a return code by main."""

    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


class _TextWriter(Protocol):
    def write(self, text: str, /) -> object: ...


def build_parser(
    *, stdout: TextIO | None = None, stderr: TextIO | None = None
) -> argparse.ArgumentParser:
    output_stream = stdout if stdout is not None else sys.stdout
    error_stream = stderr if stderr is not None else sys.stderr

    class InstalledVersionAction(argparse.Action):
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
            print(f"{parser.prog} {installed_version}", file=output_stream)
            parser.exit()

    class InvocationParser(argparse.ArgumentParser):
        def print_help(self, file: _TextWriter | None = None) -> None:
            super().print_help(output_stream if file is None else file)

        def print_usage(self, file: _TextWriter | None = None) -> None:
            super().print_usage(output_stream if file is None else file)

        def error(self, message: str) -> NoReturn:
            self.print_usage(error_stream)
            self.exit(2, f"{self.prog}: error: {message}\n")

        def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
            if message:
                error_stream.write(message)
            raise _ParserExit(status)

    parser = InvocationParser(
        prog="loki-query",
        description="Query Loki logs and metrics through a Grafana datasource proxy.",
    )
    parser.add_argument(
        "--version",
        action=InstalledVersionAction,
        nargs=0,
        help="show program's version number and exit",
    )
    parser.add_argument(
        "--config",
        type=str,
        help="Configuration file (default: environment or platform config path).",
    )
    subparsers = parser.add_subparsers(
        dest="command", required=True, parser_class=InvocationParser
    )
    query_parser = subparsers.add_parser(
        "query",
        help="Run a Loki query_range request.",
    )
    query_parser.add_argument("--profile", required=True, help="Named target profile.")
    query_parser.add_argument(
        "--query-type",
        choices=("log", "metric"),
        default="log",
        help="Declared query result type (default: log).",
    )
    range_group = query_parser.add_mutually_exclusive_group()
    range_group.add_argument("--since", help="Relative duration such as 15m or 2h.")
    range_group.add_argument("--start", help="RFC 3339 range start.")
    query_parser.add_argument("--end", help="RFC 3339 range end (default: now).")
    query_parser.add_argument(
        "--limit",
        type=_limit,
        default=None,
        help="Maximum entries per request (1-5000; default: 100, scan: 1000).",
    )
    query_parser.add_argument(
        "--direction",
        choices=("backward", "forward"),
        default=None,
        help="Log result direction (default: backward).",
    )
    query_parser.add_argument(
        "--step",
        type=_non_empty_step,
        help="Metric query step in Loki seconds or duration form.",
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
    query_parser.add_argument("--max-record-bytes", type=_record_bytes, default=DEFAULT_RECORD_BYTES,
                               help="Complete UTF-8 record bytes (1-8388608; default: 4194304 / 4 MiB).")
    query_parser.add_argument("--max-output-bytes", type=_output_bytes, default=DEFAULT_OUTPUT_BYTES,
                               help="Combined stdout/stderr bytes (1-33554432; default: 1048576 / 1 MiB).")
    query_parser.add_argument("--scan", action="store_true", help="Adaptively scan dense or timed-out intervals with bounded execution.")
    query_parser.add_argument("--max-http-attempts", type=_positive_int, help="HTTP attempt ceiling including retries (scan default: 60); explicit cap emits stderr accounting.")
    query_parser.add_argument("--max-elapsed-seconds", type=_positive_float, help="Scan elapsed budget (default: 300).")
    query_parser.add_argument("--max-records", type=_positive_int, help="Scan retained record budget (default: 10000).")
    query_parser.add_argument("--work-dir", help="Persist scan progress and evidence in this explicit directory.")
    query_parser.add_argument("--report", help="Write log completeness JSON to this file.")
    query_parser.add_argument("logql", help="Complete LogQL query, or - to read stdin.")
    trace_parser = subparsers.add_parser("trace", help="Query exact trace IDs and show a timeline.")
    configure_trace_parser(
        trace_parser,
        limit_type=_limit, timeout_type=_positive_float, budget_type=_positive_int,
    )
    export_parser = subparsers.add_parser("export", help="Export a saved scan without configuration, credentials, or HTTP.")
    export_parser.add_argument("--work-dir", required=True, help="Saved scan directory.")
    export_parser.add_argument("--output", choices=("human", "raw", "jsonl"), default="human")
    export_parser.add_argument("--service-field", default="app", help="Trace service label for human output.")
    export_parser.add_argument("--report", help="Write saved completeness JSON to this file.")
    resume_parser = subparsers.add_parser("resume", help="Continue only unresolved saved scan intervals.")
    resume_parser.add_argument("--work-dir", required=True, help="Saved scan directory.")
    resume_parser.add_argument("--profile", required=True, help="Explicit original target profile.")
    resume_parser.add_argument("--limit", type=_limit, default=1000, help="Entries per request (default: 1000).")
    resume_parser.add_argument("--max-http-attempts", type=_positive_int, help="HTTP attempt ceiling including retries (default: 60); explicit cap emits stderr accounting.")
    resume_parser.add_argument("--max-elapsed-seconds", type=_positive_float, default=300.0)
    resume_parser.add_argument("--max-records", type=_positive_int, default=10000)
    resume_parser.add_argument("--timeout", type=_positive_float, default=30.0)
    resume_parser.add_argument("--output", choices=("human", "raw", "jsonl"), default="human")
    resume_parser.add_argument("--service-field", default="app", help="Trace service label for human output.")
    resume_parser.add_argument("--trace-id", help="Assert the saved trace ID without changing scope.")
    resume_parser.add_argument("--source", choices=("json", "label"), help="Assert the saved trace source.")
    resume_parser.add_argument("--selector", help="Assert the saved trace selector.")
    resume_parser.add_argument("--field", dest="fields", action="append", help="Assert the saved trace field mapping.")
    resume_parser.add_argument("--path", dest="paths", action="append", help="Assert the saved trace JSON Pointer mapping.")
    resume_parser.add_argument("--report", help="Write current and cumulative completeness JSON.")
    profiles_parser = subparsers.add_parser(
        "profiles", help="Inspect configured profiles."
    )
    profiles_subparsers = profiles_parser.add_subparsers(
        dest="profiles_command", required=True, parser_class=InvocationParser
    )
    profiles_subparsers.add_parser("list", help="List configured profile names.")
    config_parser = subparsers.add_parser(
        "config", help="Inspect and validate configuration."
    )
    config_subparsers = config_parser.add_subparsers(
        dest="config_command", required=True, parser_class=InvocationParser
    )
    config_subparsers.add_parser("path", help="Print the default configuration path.")
    config_subparsers.add_parser("validate", help="Validate the configuration file.")
    trace_parser.add_argument("--max-record-bytes", type=_record_bytes, default=DEFAULT_RECORD_BYTES,
                              help="Complete rendered record bytes (1-8388608; default: 4194304 / 4 MiB).")
    trace_parser.add_argument("--max-output-bytes", type=_output_bytes, default=DEFAULT_OUTPUT_BYTES,
                              help="Combined stdout/stderr bytes (1-33554432; default: 1048576 / 1 MiB).")
    for archive_parser in (export_parser, resume_parser):
        archive_parser.add_argument("--max-record-bytes", type=_record_bytes, default=DEFAULT_RECORD_BYTES,
                                    help="Complete rendered record bytes (1-8388608; default: 4194304 / 4 MiB).")
        archive_parser.add_argument("--max-output-bytes", type=_output_bytes, default=DEFAULT_OUTPUT_BYTES,
                                    help="Combined stdout/stderr bytes (1-33554432; default: 1048576 / 1 MiB).")
    for data_parser in (query_parser, trace_parser, resume_parser, export_parser):
        data_parser.add_argument("--max-response-bytes", type=lambda value: _byte_budget(value, MAX_RESPONSE_BYTES),
                                 default=DEFAULT_RESPONSE_BYTES, help="Actual HTTP body bytes (1-67108864; default: 33554432 / 32 MiB).")
        data_parser.add_argument("--max-retained-bytes", type=lambda value: _byte_budget(value, MAX_RETAINED_BYTES),
                                 default=DEFAULT_RETAINED_BYTES, help="Retained canonical UTF-8 bytes (1-268435456; default: 67108864 / 64 MiB).")
    return parser


def _limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("limit must be an integer") from error
    if not 1 <= parsed <= 5000:
        raise argparse.ArgumentTypeError("limit must be between 1 and 5000")
    return parsed


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("budget must be an integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("budget must be positive")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timeout must be a number") from error
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("value must be finite and positive")
    return parsed


def _byte_budget(value: str, maximum: int) -> int:
    parsed = _positive_int(value)
    if parsed > maximum:
        raise argparse.ArgumentTypeError(f"byte budget must be between 1 and {maximum}")
    return parsed


def _record_bytes(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > MAX_RECORD_BYTES:
        raise argparse.ArgumentTypeError("record byte budget must be between 1 and 8388608")
    return parsed


def _output_bytes(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > MAX_OUTPUT_BYTES:
        raise argparse.ArgumentTypeError("output byte budget must be between 1 and 33554432")
    return parsed


def _non_empty_step(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("step must not be empty")
    return value


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    opener: Opener = default_opener,
    sleeper: Sleeper = time.sleep,
    now: datetime | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """Write command output to the supplied streams and return its exit code.

    Help, version, and expected input failures return normally. Unexpected
    execution failures propagate to the caller.
    """
    environment = environ if environ is not None else os.environ
    input_stream = stdin if stdin is not None else sys.stdin
    output_stream = stdout if stdout is not None else sys.stdout
    error_stream = stderr if stderr is not None else sys.stderr
    parser = build_parser(stdout=output_stream, stderr=error_stream)
    try:
        args = parser.parse_args(argv)
    except _ParserExit as outcome:
        return outcome.status
    budget = (OutputBudget(args.max_record_bytes, args.max_output_bytes,
                           max_response_bytes=args.max_response_bytes, max_retained_bytes=args.max_retained_bytes)
              if args.command in ("query", "trace", "export", "resume") else None)
    if budget is not None:
        budget.ensure_status_capacity()
    original_error_stream = error_stream
    if budget is not None:
        output_stream = BudgetedStream(output_stream, budget)
        error_stream = BudgetedStream(error_stream, budget)
    attempts = 0
    cap = getattr(args, "max_http_attempts", None)

    def bounded_opener(request: Request, timeout: float) -> ReadableResponse:
        nonlocal attempts
        if cap is not None and attempts >= cap:
            raise HttpAttemptBudgetError("HTTP attempt budget exhausted.")
        attempts += 1
        return opener(request, timeout)

    def bounded_sleeper(seconds: float) -> None:
        if cap is not None and attempts >= cap:
            raise HttpAttemptBudgetError("HTTP attempt budget exhausted before retry wait.")
        sleeper(seconds)

    try:
        exit_code = _dispatch(
            args,
            environment=environment,
            input_stream=input_stream,
            output_stream=output_stream,
            error_stream=error_stream,
            opener=bounded_opener,
            sleeper=bounded_sleeper,
            now=now or datetime.now(UTC),
            monotonic=monotonic,
        )
        return EXIT_QUERY if budget is not None and budget.losses and exit_code == 0 else exit_code
    except (ConfigurationError, TimeRangeError) as error:
        print(f"error: {error}", file=error_stream)
        return EXIT_CONFIG
    except AuthenticationError as error:
        set_retrieval_status(error_stream, "failed")
        if budget is not None:
            budget.mark_loss("authentication_failed")
        print(f"error: {error}", file=error_stream)
        return EXIT_AUTH
    except QueryError as error:
        resource_loss = isinstance(error, (QueryResourceError, QueryTimeoutError))
        set_retrieval_status(error_stream, "incomplete" if resource_loss else "failed")
        if budget is not None:
            reason = query_error_reason(error, fallback="query_failed")
            if reason not in budget.losses:
                budget.mark_loss(reason)
        print(f"error: {error}", file=error_stream)
        return EXIT_QUERY
    except BaseException:
        set_retrieval_status(error_stream, "failed")
        if budget is not None:
            budget.mark_loss("execution_interrupted")
        raise
    finally:
        accounting = f"HTTP attempt accounting: used={attempts} cap={cap}\n" if cap is not None else ""
        if budget is not None:
            budget.finish(original_error_stream, accounting,
                          leading_newline=isinstance(error_stream, BudgetedStream) and error_stream.needs_newline)
        elif accounting:
            error_stream.write(accounting)


def _dispatch(
    args: argparse.Namespace,
    *,
    environment: Mapping[str, str],
    input_stream: TextIO,
    output_stream: TextIO,
    error_stream: TextIO,
    opener: Opener,
    sleeper: Sleeper,
    now: datetime,
    monotonic: Callable[[], float],
) -> int:
    selected_config_path = (
        Path(args.config).expanduser()
        if args.config
        else default_config_path(environ=environment)
    )
    if args.command == "export":
        validate_report_destination(args.report)
        work = WorkDirectory(args.work_dir)
        work.validate_report(args.report)
        _, saved = work.load(args.max_retained_bytes)
        saved.report['current_losses'] = {}
        saved.report['usage']['current'] = {**saved.report['usage']['current'],
                                            'skipped_entries': 0, 'skipped_series': 0}
        saved.report.pop('evidence', None)
        if 'trace' in saved.report:
            if not args.service_field.strip():
                raise ConfigurationError('Service field must not be empty.')
            return write_trace_outcome(saved, args, output_stream, error_stream, token="")
        write_result(saved.result, args.output, output_stream)
        write_log_report(saved.report, args.report, error_stream, token="")
        return saved.exit_code
    if args.command == "resume":
        validate_report_destination(args.report)
        with WorkDirectory(args.work_dir).writer() as work:
            work.validate_report(args.report)
            identity, saved = work.load(args.max_retained_bytes)
            config = load_config(selected_config_path)
            profile = config.profiles.get(args.profile)
            if profile is None:
                raise ConfigurationError(f"Unknown profile: {args.profile!r}.")
            target = {"endpoint": query_endpoint(profile), "datasource_uid": profile.datasource_uid}
            if identity['profile'] != profile.name or identity['target'] != target:
                raise ConfigurationError("Saved investigation profile or target does not match; use a fresh investigation for changed scope.")
            metadata = identity.get('trace')
            validate_trace_resume(args, metadata)
            if not args.service_field.strip():
                raise ConfigurationError('Service field must not be empty.')
            def save_progress(value: ScanOutcome) -> None:
                if metadata is not None:
                    annotate_trace(value, metadata)
                work.save(value, identity=identity)
            resume_token = environment.get(profile.token_env, "")
            if any(interval.reasons for interval in saved.intervals) and not resume_token:
                raise AuthenticationError(f"token environment variable {profile.token_env!r} is not set.")
            outcome = scan_range(
                profile=profile, token=resume_token, query=identity['query'],
                start_ns=int(identity['start_ns']), end_ns=int(identity['end_ns']),
                direction=identity['direction'],
                options=ScanOptions(limit=args.limit, max_http_attempts=args.max_http_attempts or 60,
                    max_elapsed_seconds=args.max_elapsed_seconds, max_records=args.max_records,
                    timeout=args.timeout, max_response_bytes=args.max_response_bytes,
                    max_retained_bytes=args.max_retained_bytes),
                opener=opener, sleeper=sleeper, monotonic=monotonic, seed=saved,
                progress=save_progress,
            )
            if metadata is not None:
                annotate_trace(outcome, metadata)
                code = write_trace_outcome(outcome, args, output_stream, error_stream, token=resume_token)
            else:
                write_result(outcome.result, args.output, output_stream)
                write_log_report(outcome.report, args.report, error_stream, token=resume_token)
                code = outcome.exit_code
            work.save(outcome, identity=identity)
            return code
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
    elif args.command == "trace":
        return run_trace(args, config_path=selected_config_path, environment=environment,
                         stdout=output_stream, stderr=error_stream, opener=opener,
                         sleeper=sleeper, now=now, monotonic=monotonic)
    elif args.command == "query":
        if args.end and not args.start:
            raise ConfigurationError(
                "--end requires --start and cannot be combined with --since."
            )
        scan_flags = [flag for flag, value in (
            ("--max-elapsed-seconds", args.max_elapsed_seconds),
            ("--max-records", args.max_records),
            ("--work-dir", args.work_dir),
        ) if value is not None]
        if args.scan:
            scan_flags.append("--scan")
        if scan_flags and args.query_type == "metric":
            raise ConfigurationError(f"{', '.join(scan_flags)} cannot be used with metric queries.")
        if scan_flags and not args.scan:
            raise ConfigurationError(f"{', '.join(scan_flags)} requires --scan.")
        if args.query_type == "metric":
            invalid_flags = [
                flag
                for flag, value in (
                    ("--limit", args.limit),
                    ("--direction", args.direction),
                    ("--report", args.report),
                )
                if value is not None
            ]
            if invalid_flags:
                raise ConfigurationError(
                    f"{', '.join(invalid_flags)} cannot be used with metric queries."
                )
        elif args.step is not None:
            raise ConfigurationError("--step cannot be used with log queries.")
        validate_report_destination(args.report)
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
            set_retrieval_status(error_stream, "failed")
            if isinstance(error_stream, BudgetedStream):
                error_stream.budget.mark_loss("authentication_failed")
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
        if args.scan:
            def execute_scan(progress: Callable[[ScanOutcome], None] | None = None) -> ScanOutcome:
                return scan_range(
                    profile=profile, token=token, query=query.strip(), start_ns=start_ns, end_ns=end_ns,
                    direction=args.direction or "backward",
                    options=ScanOptions(limit=args.limit or 1000,
                        max_http_attempts=args.max_http_attempts or 60,
                        max_elapsed_seconds=args.max_elapsed_seconds or 300.0,
                        max_records=args.max_records or 10000, timeout=args.timeout, max_response_bytes=args.max_response_bytes,
                    max_retained_bytes=args.max_retained_bytes),
                    opener=opener, sleeper=sleeper, monotonic=monotonic, progress=progress,
                )

            if args.work_dir is not None:
                with WorkDirectory(args.work_dir).writer() as work:
                    work.validate_report(args.report)
                    work.require_new()
                    outcome = execute_scan(work.save)
                    presented = present_logs(outcome.result, outcome.report, args.output,
                                             output_stream, error_stream, destination=args.report, token=token)
                    work.save(replace(outcome, report=presented))
            else:
                outcome = execute_scan()
                present_logs(outcome.result, outcome.report, args.output,
                             output_stream, error_stream, destination=args.report, token=token)
            return outcome.exit_code
        started = monotonic()
        attempts = 0

        def counted_opener(request: Request, timeout: float) -> ReadableResponse:
            nonlocal attempts
            attempts += 1
            return opener(request, timeout)

        try:
            result = query_range(
                profile=profile,
                token=token,
                query=query.strip(),
                start_ns=start_ns,
                end_ns=end_ns,
                query_type=args.query_type,
                limit=100 if args.query_type == "log" and args.limit is None else args.limit,
                direction=(
                    "backward"
                    if args.query_type == "log" and args.direction is None
                    else args.direction
                ),
                step=args.step,
                timeout=args.timeout,
                opener=counted_opener,
                sleeper=sleeper, monotonic=monotonic,
                max_response_bytes=args.max_response_bytes, max_retained_bytes=args.max_retained_bytes,
            )
        except QueryError as error:
            if args.query_type == "log":
                report = log_report(
                    profile=profile, query=query.strip(), start_ns=start_ns, end_ns=end_ns,
                    direction=args.direction or "backward", limit=args.limit or 100,
                    result=None, attempts=attempts, timeout=args.timeout,
                    elapsed_seconds=max(0.0, monotonic() - started),
                    failure_reason=query_error_reason(error),
                    max_http_attempts=min(3, args.max_http_attempts or 3),
                    max_response_bytes=args.max_response_bytes, max_retained_bytes=args.max_retained_bytes,
                )
                present_logs(None, report, args.output, output_stream, error_stream,
                             destination=args.report, token=token)
            raise
        if args.query_type == "log":
            report = log_report(
                profile=profile, query=query.strip(), start_ns=start_ns, end_ns=end_ns,
                direction=args.direction or "backward", limit=args.limit or 100,
                result=result, attempts=attempts, timeout=args.timeout, elapsed_seconds=max(0.0, monotonic() - started),
                max_http_attempts=min(3, args.max_http_attempts or 3),
                    max_response_bytes=args.max_response_bytes, max_retained_bytes=args.max_retained_bytes,
            )
            present_logs(result, report, args.output, output_stream, error_stream,
                         destination=args.report, token=token)
        else:
            set_retrieval_status(error_stream, "incomplete" if result.skipped.incomplete else "complete",
                                 parse_loss=result.skipped.incomplete)
            write_result(result, args.output, output_stream)
        if result.skipped.incomplete:
            if args.query_type == "log":
                return EXIT_QUERY
            skipped = result.skipped
            print(
                "warning: incomplete results; "
                f"skipped {skipped.skipped_series} series, "
                f"{skipped.skipped_entries} log entries, and "
                f"{skipped.skipped_samples} metric samples.",
                file=error_stream,
            )
            return EXIT_QUERY
    return 0
