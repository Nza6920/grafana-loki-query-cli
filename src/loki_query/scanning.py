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
    sample_origin: tuple[int, int] | None = None


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
    monotonic: Callable[[], float], progress: Callable[[ScanOutcome], None] | None = None,
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
    requested: list[ScanInterval] = []
    raw_entries = skipped_entries = skipped_series = 0
    raw_count_exact = True

    def snapshot() -> ScanOutcome:
        records: list[Record] = [record for i in intervals if i.result is not None for record in i.result.records]
        combined = QueryResult('log', sorted(records, key=lambda record: record.timestamp_ns, reverse=True),
                               ParseSummary(skipped_series, skipped_entries), raw_entries, raw_count_exact)
        report = log_report(profile=profile, query=query, start_ns=start_ns, end_ns=end_ns,
                            direction=direction, limit=options.limit, result=combined,
                            attempts=attempts, elapsed_seconds=max(0.0, monotonic() - started), timeout=options.timeout)

        def coverage_interval(item: ScanInterval) -> dict[str, Any]:
            coverage: dict[str, Any] = {
                'start_ns': str(item.start_ns), 'end_ns': str(item.end_ns),
                'http_attempts': item.http_attempts, 'request_limit': item.request_limit,
                'retained_records': len(item.result.records) if item.result is not None else 0,
                'sample_origin': ({'start_ns': str(item.sample_origin[0]), 'end_ns': str(item.sample_origin[1])}
                                  if item.sample_origin is not None else None),
            }
            if item.reasons:
                coverage['reasons'] = list(item.reasons)
            return coverage

        report['coverage'] = {
            'resolved': [coverage_interval(i) for i in intervals if not i.reasons],
            'unresolved': [coverage_interval(i) for i in intervals if i.reasons],
        }
        report['requests'] = [{'start_ns': str(i.start_ns), 'end_ns': str(i.end_ns),
                               'http_attempts': i.http_attempts, 'request_limit': i.request_limit,
                               'reasons': list(i.reasons)} for i in requested if i.http_attempts]
        report['status'] = 'failed' if fatal_code else 'incomplete' if report['coverage']['unresolved'] else 'complete'
        report['stop_reasons'] = stop_reasons
        report['budgets'] = {'max_http_attempts': options.max_http_attempts,
                             'max_elapsed_seconds': options.max_elapsed_seconds,
                             'max_records': options.max_records, 'request_timeout_seconds': options.timeout}
        return ScanOutcome(combined, report, fatal_code or (4 if report['coverage']['unresolved'] else 0), intervals)

    def checkpoint() -> None:
        if progress is not None:
            progress(snapshot())

    checkpoint()

    def check_budget() -> float:
        if attempts >= options.max_http_attempts:
            raise BudgetStopped("http_attempt_budget")
        remaining = options.max_elapsed_seconds - (monotonic() - started)
        if remaining <= 0:
            raise BudgetStopped("elapsed_time_budget")
        return remaining

    def bounded_opener(request: Request, timeout: float) -> ReadableResponse:
        nonlocal attempts
        check_budget()
        attempts += 1
        interval.http_attempts += 1
        checkpoint()
        # Durable checkpoint I/O consumes elapsed time too. Recheck after
        # publication so a slow disk cannot permit an over-budget request.
        remaining = options.max_elapsed_seconds - (monotonic() - started)
        if remaining <= 0:
            attempts -= 1
            interval.http_attempts -= 1
            raise BudgetStopped("elapsed_time_budget")
        return opener(request, min(timeout, remaining))

    def bounded_sleep(seconds: float) -> None:
        remaining = check_budget()
        if seconds >= remaining:
            raise BudgetStopped("elapsed_time_budget")
        sleeper(seconds)

    position = 0
    while position < len(intervals):
        interval = intervals[position]
        try:
            retained = sum(len(i.result.records) for i in intervals if i.result is not None)
            replaced = len(interval.result.records) if interval.result is not None else 0
            capacity = options.max_records - retained + replaced
            if capacity <= 0:
                raise BudgetStopped("record_budget")
            request_limit = min(options.limit, capacity)
            check_budget()
            interval.request_limit = request_limit
            requested.append(interval)
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
        else:
            previous_result, previous_origin = interval.result, interval.sample_origin
            interval.result = replace(result, records=[
                record for record in result.records
                if interval.start_ns <= record.timestamp_ns < interval.end_ns
            ][:request_limit])
            interval.sample_origin = (interval.start_ns, interval.end_ns)
            interval.reasons = []
            if result.raw_entries >= request_limit:
                interval.reasons.append("possible_truncation")
            if result.skipped.incomplete or not result.raw_count_exact:
                interval.reasons.append("parse_loss")
            # An incomplete replacement may contain less usable evidence than its
            # inherited sample. Keep one sample by provenance, never concatenate.
            if (interval.reasons and previous_result is not None
                    and len(previous_result.records) > len(interval.result.records)):
                interval.result, interval.sample_origin = previous_result, previous_origin
            raw_count_exact = raw_count_exact and result.raw_count_exact
            raw_entries += result.raw_entries
            skipped_entries += result.skipped.skipped_entries
            skipped_series += result.skipped.skipped_series
        if "possible_truncation" in interval.reasons or "timeout" in interval.reasons:
            if interval.end_ns - interval.start_ns > 1_000_000_000:
                midpoint = (interval.start_ns + interval.end_ns) // 2
                children = [ScanInterval(interval.start_ns, midpoint),
                            ScanInterval(midpoint, interval.end_ns)]
                for child in children:
                    if interval.result is not None:
                        child.result = replace(interval.result, records=[
                            record for record in interval.result.records
                            if child.start_ns <= record.timestamp_ns < child.end_ns
                        ])
                    child.sample_origin = interval.sample_origin
                    child.reasons = ["unqueried", *interval.reasons]
                if direction == 'backward':
                    children.reverse()
                intervals[position:position + 1] = children
                checkpoint()
                continue
            interval.reasons.append("minimum_interval")
        position += 1
        checkpoint()
    if stop_reasons:
        for interval in intervals:
            if "unqueried" in interval.reasons:
                interval.reasons.extend(reason for reason in stop_reasons if reason not in interval.reasons)
    checkpoint()
    return snapshot()
