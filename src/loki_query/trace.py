from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any, TextIO
from urllib.request import Request

from .client import AuthenticationError, LogEntry, Opener, QueryError, QueryResult, ReadableResponse, Sleeper, query_range
from .config import ConfigurationError, load_config
from .output import format_timestamp_utc, write_result
from .reporting import log_report, validate_report_destination, write_log_report
from .time_range import resolve_time_range

DEFAULT_TRACE_MAPPING = (('trace_id',), ('traceId',))

def configure_trace_parser(
    parser: argparse.ArgumentParser, *, limit_type: Callable[[str], int],
    timeout_type: Callable[[str], float],
) -> None:
    parser.add_argument('trace_id', help='Complete case-sensitive trace ID.')
    parser.add_argument('--profile', required=True, help='Named target profile.')
    parser.add_argument('--selector', required=True, help='One Loki stream selector; no pipeline.')
    ranges = parser.add_mutually_exclusive_group(required=True)
    ranges.add_argument('--since', help='Relative duration such as 15m.')
    ranges.add_argument('--start', help='RFC 3339 range start.')
    parser.add_argument('--end', help='RFC 3339 range end (default: now).')
    parser.add_argument('--limit', type=limit_type, default=100, help='Maximum candidates (1-5000; default: 100).')
    parser.add_argument('--direction', choices=('backward', 'forward'), default='backward')
    parser.add_argument('--output', choices=('human', 'raw', 'jsonl'), default='human')
    parser.add_argument('--service-field', default='app', help='Returned service label (default: app).')
    parser.add_argument('--field', dest='fields', metavar='NAME', action='append',
                        help='Literal top-level JSON field; repeat to match any selected field.')
    parser.add_argument('--path', dest='paths', metavar='POINTER', action='append',
                        help='JSON Pointer to nested object members; repeat to match any selected path.')
    parser.add_argument('--timeout', type=timeout_type, default=30.0)
    parser.add_argument('--report', help='Write trace completeness JSON to this file.')


def validate_selector(selector: str) -> None:
    # Only a single lexical selector is allowed; Loki validates matcher semantics.
    text = selector.strip()
    if not text.startswith('{'):
        raise ConfigurationError('--selector requires a single stream selector without a pipeline.')
    quote = ''
    escaped = False
    for index, character in enumerate(text[1:], start=1):
        if quote:
            if escaped:
                escaped = False
            elif character == '\\' and quote == '"':
                escaped = True
            elif character == quote:
                quote = ''
        elif character in ('"', '`'):
            quote = character
        elif character == '}':
            if index == len(text) - 1:
                return
            break
        elif character in ('{', '#'):
            break
    raise ConfigurationError('--selector requires a single stream selector without a pipeline.')

def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Ambiguous duplicate JSON key.')
        result[key] = value
    return result

def _invalid_constant(value: str) -> Any:
    raise ValueError('Non-JSON numeric constant.')

def resolve_json_mapping(
    fields: list[str] | None, paths: list[str] | None,
) -> tuple[tuple[str, ...], ...]:
    if fields is None and paths is None:
        return DEFAULT_TRACE_MAPPING
    for field in fields or []:
        if not field or any(ord(character) < 32 or ord(character) == 127 for character in field):
            raise ConfigurationError('--field requires a nonempty literal key without control characters.')
    mapping: set[tuple[str, ...]] = {(field,) for field in fields or []}
    for path in paths or []:
        if (not path.startswith('/') or re.search(r'~(?![01])', path)
                or any(ord(character) < 32 or ord(character) == 127 for character in path)
                or any(not component for component in path[1:].split('/'))):
            raise ConfigurationError('--path requires a JSON Pointer with nonempty object members; '
                                     'escape ~ as ~0 and / as ~1.')
        mapping.add(tuple(component.replace('~1', '/').replace('~0', '~')
                          for component in path[1:].split('/')))
    return tuple(sorted(mapping))

def _mapped_value(body: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = body
    for component in path:
        if not isinstance(value, dict):
            return None
        value = value.get(component)
    return value

def matched_trace(
    result: QueryResult, trace_id: str, mapping: tuple[tuple[str, ...], ...] = DEFAULT_TRACE_MAPPING,
) -> QueryResult:
    matches: list[LogEntry] = []
    for record in result.records:
        if not isinstance(record, LogEntry):
            continue
        try:
            body = json.loads(record.line, object_pairs_hook=_json_object, parse_constant=_invalid_constant)
        except (ValueError, RecursionError):
            continue
        if isinstance(body, dict) and any(
            isinstance(value := _mapped_value(body, path), str) and value == trace_id
            for path in mapping
        ):
            matches.append(record)
    matches.sort(key=lambda entry: (entry.timestamp_ns, sorted(entry.labels.items()), entry.line))
    return QueryResult('log', list(matches), result.skipped, result.raw_entries, result.raw_count_exact)


def write_trace_result(result: QueryResult, output_format: str, stream: TextIO, *, service_field: str) -> None:
    if not result.records:
        return
    if output_format != 'human':
        write_result(result, output_format, stream)
        return
    for record in result.records:
        if isinstance(record, LogEntry):
            service = json.dumps(record.labels.get(service_field) or 'unknown', ensure_ascii=False)
            namespace = json.dumps(record.labels.get('namespace') or 'unknown', ensure_ascii=False)
            labels = json.dumps(record.labels, ensure_ascii=False, separators=(',', ':'))
            print(f'{format_timestamp_utc(record.timestamp_ns)} service={service} namespace={namespace} '
                  f'{labels} {record.line}', file=stream)

def run_trace(
    args: argparse.Namespace, *, config_path: Path, environment: Mapping[str, str],
    stdout: TextIO, stderr: TextIO, opener: Opener, sleeper: Sleeper,
    now: datetime, monotonic: Callable[[], float],
) -> int:
    if args.end and not args.start:
        raise ConfigurationError('--end requires --start and cannot be combined with --since.')
    validate_selector(args.selector)
    mapping = resolve_json_mapping(args.fields, args.paths)
    fields = sorted(set(args.fields or [])) if args.fields is not None or args.paths is not None else ['trace_id', 'traceId']
    paths = sorted(set(args.paths or []))
    if not args.trace_id or not args.service_field.strip():
        raise ConfigurationError("Trace ID and service field must not be empty.")
    validate_report_destination(args.report)
    config = load_config(config_path)
    profile = config.profiles.get(args.profile)
    if profile is None:
        raise ConfigurationError(f'Unknown profile: {args.profile!r}.')
    token = environment.get(profile.token_env)
    if not token:
        raise AuthenticationError(f'token environment variable {profile.token_env!r} is not set.')
    start_ns, end_ns = resolve_time_range(since=args.since, start=args.start, end=args.end, now=now)
    started, attempts = monotonic(), 0

    def counted_opener(request: Request, timeout: float) -> ReadableResponse:
        nonlocal attempts
        attempts += 1
        return opener(request, timeout)

    def report_for(result: QueryResult | None, matches: QueryResult | None, reason: str | None = None) -> dict[str, Any]:
        report = log_report(profile=profile, query=args.selector, start_ns=start_ns, end_ns=end_ns,
                            direction=args.direction, limit=args.limit, result=result, attempts=attempts,
                            elapsed_seconds=max(0.0, monotonic() - started), timeout=args.timeout,
                            failure_reason=reason)
        report['trace'] = {'id': args.trace_id, 'source': 'json', 'fields': fields,
                           'paths': paths,
                           'mapping': [list(path) for path in sorted(mapping)],
                           'matching': 'local exact JSON string matching on selector candidates'}
        report['counts']['matched_records'] = len(matches.records) if matches else 0
        report['counts']['filtered_candidates'] = len(result.records) - len(matches.records) if result and matches else 0
        return report

    try:
        result = query_range(profile=profile, token=token, query=args.selector, start_ns=start_ns,
                             end_ns=end_ns, limit=args.limit, direction=args.direction, timeout=args.timeout,
                             opener=counted_opener, sleeper=sleeper)
    except QueryError as error:
        report = report_for(None, None, 'authentication_failed' if isinstance(error, AuthenticationError) else 'request_failed')
        write_log_report(report, args.report, stderr, token=token)
        raise
    matches = matched_trace(result, args.trace_id, mapping)
    write_trace_result(matches, args.output, stdout, service_field=args.service_field)
    print("Trace timeline: equal timestamps do not imply causality.", file=stderr)
    report = report_for(result, matches)
    print(f"Trace candidates: {len(result.records)} examined, {len(matches.records)} matched.", file=stderr)
    if not matches.records:
        print(f"No trace matches under the selected conditions; candidate retrieval is {report['status']}.", file=stderr)
    write_log_report(report, args.report, stderr, token=token)
    return 4 if result.skipped.incomplete else 0
