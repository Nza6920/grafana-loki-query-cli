from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.request import Request

from .client import (
    AuthenticationError, InvalidQueryError, Opener, ParseSummary, QueryError,
    QueryResult, QueryTimeoutError, ReadableResponse, Record, Sleeper, query_range,
)
from .config import Profile
from .reporting import log_report


@dataclass(frozen=True)
class ScanOptions:
    limit: int = 1000
    max_http_attempts: int = 60
    max_elapsed_seconds: float = 300.0
    max_records: int = 10000
    timeout: float = 30.0


@dataclass
class ScanInterval:
    start_ns: int
    end_ns: int
    result: QueryResult | None = None
    http_attempts: int = 0
    request_limit: int | None = None
    reasons: list[str] = field(default_factory=lambda: ["unqueried"])


class BudgetStopped(Exception):
    pass


@dataclass
class ScanOutcome:
    result: QueryResult
    report: dict[str, Any]
    exit_code: int
    intervals: list[ScanInterval]


def scan_range(
    *, profile: Profile, token: str, query: str, start_ns: int, end_ns: int,
    direction: str, options: ScanOptions, opener: Opener, sleeper: Sleeper,
    monotonic: Callable[[], float],
) -> ScanOutcome:
    started = monotonic()
    attempts = 0
    stop_reasons: list[str] = []
    fatal_code = 0
    width = 900_000_000_000
    intervals = [ScanInterval(start, min(start + width, end_ns))
                 for start in range(start_ns, end_ns, width)]
    if direction == 'backward':
        intervals.reverse()
    records: list[Record] = []
    raw_entries = skipped_entries = skipped_series = 0
    raw_count_exact = True

    def check_budget() -> float:
        if attempts >= options.max_http_attempts:
            raise BudgetStopped("http_attempt_budget")
        remaining = options.max_elapsed_seconds - (monotonic() - started)
        if remaining <= 0:
            raise BudgetStopped("elapsed_time_budget")
        return remaining

    def bounded_opener(request: Request, timeout: float) -> ReadableResponse:
        nonlocal attempts
        remaining = check_budget()
        attempts += 1
        interval.http_attempts += 1
        return opener(request, min(timeout, remaining))

    def bounded_sleep(seconds: float) -> None:
        remaining = check_budget()
        if seconds >= remaining:
            raise BudgetStopped("elapsed_time_budget")
        sleeper(seconds)

    for interval in intervals:
        try:
            if len(records) >= options.max_records:
                raise BudgetStopped("record_budget")
            request_limit = min(options.limit, options.max_records - len(records))
            check_budget()
            interval.request_limit = request_limit
            result = query_range(profile=profile, token=token, query=query,
                                 start_ns=interval.start_ns, end_ns=interval.end_ns,
                                 limit=request_limit, direction=direction,
                                 timeout=options.timeout, opener=bounded_opener, sleeper=bounded_sleep)
        except BudgetStopped as error:
            stop_reasons = [str(error)]
            if interval.http_attempts:
                interval.reasons = list(stop_reasons)
            break
        except (AuthenticationError, InvalidQueryError) as error:
            reason = "authentication_failed" if isinstance(error, AuthenticationError) else "invalid_query"
            interval.reasons = [reason]
            stop_reasons = [reason]
            fatal_code = 3 if isinstance(error, AuthenticationError) else 4
            break
        except QueryError as error:
            interval.reasons = ["timeout" if isinstance(error, QueryTimeoutError) else "request_failed"]
            continue
        interval.result = replace(result, records=result.records[:request_limit])
        interval.reasons = []
        if result.raw_entries >= request_limit:
            interval.reasons.append("possible_truncation")
        if result.skipped.incomplete or not result.raw_count_exact:
            interval.reasons.append("parse_loss")
        raw_count_exact = raw_count_exact and result.raw_count_exact
        records.extend(interval.result.records)
        raw_entries += result.raw_entries
        skipped_entries += result.skipped.skipped_entries
        skipped_series += result.skipped.skipped_series
    if stop_reasons:
        for interval in intervals:
            if interval.reasons == ["unqueried"]:
                interval.reasons = ["unqueried", *stop_reasons]
    combined = QueryResult('log', sorted(records, key=lambda record: record.timestamp_ns, reverse=True),
                           ParseSummary(skipped_series, skipped_entries), raw_entries, raw_count_exact)
    report = log_report(profile=profile, query=query, start_ns=start_ns, end_ns=end_ns,
                        direction=direction, limit=options.limit, result=combined,
                        attempts=attempts, elapsed_seconds=max(0.0, monotonic() - started), timeout=options.timeout)
    report['coverage'] = {
        'resolved': [{'start_ns': str(i.start_ns), 'end_ns': str(i.end_ns),
                      'http_attempts': i.http_attempts, 'request_limit': i.request_limit} for i in intervals if not i.reasons],
        'unresolved': [{'start_ns': str(i.start_ns), 'end_ns': str(i.end_ns), 'reasons': i.reasons,
                        'http_attempts': i.http_attempts, 'request_limit': i.request_limit}
                       for i in intervals if i.reasons],
    }
    report['status'] = 'failed' if fatal_code else 'incomplete' if report['coverage']['unresolved'] else 'complete'
    report['stop_reasons'] = stop_reasons
    report['budgets'] = {'max_http_attempts': options.max_http_attempts,
                         'max_elapsed_seconds': options.max_elapsed_seconds,
                         'max_records': options.max_records, 'request_timeout_seconds': options.timeout}
    return ScanOutcome(combined, report, fatal_code or (4 if report['coverage']['unresolved'] else 0), intervals)
