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


class _SourceAction(argparse.Action):
    def __call__(self, parser: argparse.ArgumentParser, namespace: argparse.Namespace,
                 values: Any, option_string: str | None = None) -> None:
        previous = getattr(namespace, '_explicit_trace_source', None)
        if previous is not None and previous != values:
            raise argparse.ArgumentError(self, 'Choose only one trace source mode.')
        setattr(namespace, '_explicit_trace_source', values)
        setattr(namespace, self.dest, values)


def configure_trace_parser(
    parser: argparse.ArgumentParser, *, limit_type: Callable[[str], int],
    timeout_type: Callable[[str], float],
) -> None:
    parser.add_argument('trace_id', help='Complete case-sensitive trace ID.')
    parser.add_argument('--profile', required=True, help='Named target profile.')
    parser.add_argument('--selector', required=True, help='One Loki stream selector; no pipeline.')
    parser.add_argument('--source', choices=('json', 'label'), default='json', action=_SourceAction,
                        help='Trace field source (default: json); label requires --field.')
    parser.add_argument('--field', dest='fields', action='append', help='Literal trace field name; repeat for alternatives.')
    parser.add_argument('--path', dest='paths', action='append', help='JSON object path; incompatible with label source.')
    ranges = parser.add_mutually_exclusive_group(required=True)
    ranges.add_argument('--since', help='Relative duration such as 15m.')
    ranges.add_argument('--start', help='RFC 3339 range start.')
    parser.add_argument('--end', help='RFC 3339 range end (default: now).')
    parser.add_argument('--limit', type=limit_type, default=100, help='Maximum candidates (1-5000; default: 100).')
    parser.add_argument('--direction', choices=('backward', 'forward'), default='backward')
    parser.add_argument('--output', choices=('human', 'raw', 'jsonl'), default='human')
    parser.add_argument('--service-field', default='app', help='Returned service label (default: app).')
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

def matched_trace(result: QueryResult, trace_id: str) -> QueryResult:
    matches: list[LogEntry] = []
    for record in result.records:
        if not isinstance(record, LogEntry):
            continue
        try:
            body = json.loads(record.line, object_pairs_hook=_json_object, parse_constant=_invalid_constant)
        except (ValueError, RecursionError):
            continue
        if isinstance(body, dict) and any(
            isinstance(body.get(field), str) and body[field] == trace_id
            for field in ('trace_id', 'traceId')
        ):
            matches.append(record)
    matches.sort(key=lambda entry: (entry.timestamp_ns, sorted(entry.labels.items()), entry.line))
    return QueryResult('log', list(matches), result.skipped, result.raw_entries, result.raw_count_exact)


def label_query(selector: str, trace_id: str, fields: list[str] | None, paths: list[str] | None) -> str:
    if paths:
        raise ConfigurationError('--path cannot be combined with --source label.')
    if not fields or any(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', field) is None for field in fields):
        raise ConfigurationError('--source label requires explicit --field names using ASCII label identifiers.')
    # JSON quoted strings are valid Go/LogQL interpreted strings for these values.
    quoted = json.dumps(trace_id, ensure_ascii=False)
    filters = ' or '.join(f'{field}={quoted}' for field in fields)
    return f'{selector} | ({filters})'


def matched_labels(result: QueryResult, trace_id: str, fields: list[str]) -> QueryResult:
    matches = [record for record in result.records if isinstance(record, LogEntry)
               and any(record.labels.get(field) == trace_id for field in fields)]
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
    if not args.trace_id or not args.service_field.strip():
        raise ConfigurationError("Trace ID and service field must not be empty.")
    label_fields = sorted(set(args.fields or []))
    query = (label_query(args.selector, args.trace_id, label_fields, args.paths)
             if args.source == 'label' else args.selector)
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
        report = log_report(profile=profile, query=query, start_ns=start_ns, end_ns=end_ns,
                            direction=args.direction, limit=args.limit, result=result, attempts=attempts,
                            elapsed_seconds=max(0.0, monotonic() - started), timeout=args.timeout,
                            failure_reason=reason)
        fields = label_fields if args.source == 'label' else ['trace_id', 'traceId']
        report['trace'] = {'id': args.trace_id, 'source': args.source, 'fields': fields, 'selector': args.selector,
                           'matching': ('server label filter and local exact returned-label matching'
                                        if args.source == 'label' else
                                        'local exact JSON string matching on selector candidates')}
        if args.source == 'label':
            report['trace']['paths'] = []
            report['trace']['mapping'] = [[field] for field in fields]
        report['counts']['matched_records'] = len(matches.records) if matches else 0
        report['counts']['filtered_candidates'] = len(result.records) - len(matches.records) if result and matches else 0
        return report

    try:
        result = query_range(profile=profile, token=token, query=query, start_ns=start_ns,
                             end_ns=end_ns, limit=args.limit, direction=args.direction, timeout=args.timeout,
                             opener=counted_opener, sleeper=sleeper)
    except QueryError as error:
        report = report_for(None, None, 'authentication_failed' if isinstance(error, AuthenticationError) else 'request_failed')
        write_log_report(report, args.report, stderr, token=token)
        raise
    matches = (matched_labels(result, args.trace_id, label_fields) if args.source == 'label'
               else matched_trace(result, args.trace_id))
    write_trace_result(matches, args.output, stdout, service_field=args.service_field)
    print("Trace timeline: equal timestamps do not imply causality.", file=stderr)
    report = report_for(result, matches)
    print(f"Trace candidates: {len(result.records)} examined, {len(matches.records)} matched.", file=stderr)
    if not matches.records:
        print(f"No trace matches under the selected conditions; candidate retrieval is {report['status']}.", file=stderr)
    write_log_report(report, args.report, stderr, token=token)
    return 4 if result.skipped.incomplete else 0
