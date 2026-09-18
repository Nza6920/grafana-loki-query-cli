from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import json
import time
from typing import Any, Literal, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .config import Profile


class ReadableResponse(Protocol):
    def read(self) -> bytes: ...
    def close(self) -> None: ...


Opener = Callable[[Request, float], ReadableResponse]
Sleeper = Callable[[float], None]


class QueryError(RuntimeError):
    """Raised when Grafana or Loki cannot return a usable response."""


class AuthenticationError(QueryError):
    """Raised when Grafana rejects the configured bearer token."""


@dataclass(frozen=True)
class LogEntry:
    timestamp_ns: int
    labels: dict[str, str]
    line: str


@dataclass(frozen=True)
class MetricSample:
    timestamp_ns: int
    labels: dict[str, str]
    value: str


QueryType = Literal["log", "metric"]
Record = LogEntry | MetricSample


@dataclass(frozen=True)
class ParseSummary:
    skipped_series: int = 0
    skipped_entries: int = 0
    skipped_samples: int = 0

    @property
    def incomplete(self) -> bool:
        return bool(
            self.skipped_series or self.skipped_entries or self.skipped_samples
        )


@dataclass(frozen=True)
class QueryResult:
    query_type: QueryType
    records: list[Record]
    skipped: ParseSummary = ParseSummary()


def default_opener(request: Request, timeout: float) -> ReadableResponse:
    response = urlopen(request, timeout=timeout)  # noqa: S310 - configured endpoint
    return cast(ReadableResponse, response)


def query_range(
    *,
    profile: Profile,
    token: str,
    query: str,
    start_ns: int,
    end_ns: int,
    query_type: QueryType = "log",
    limit: int | None = 100,
    direction: str | None = "backward",
    step: str | None = None,
    timeout: float,
    opener: Opener = default_opener,
    sleeper: Sleeper = time.sleep,
) -> QueryResult:
    endpoint = (
        f"{profile.grafana_url}/api/datasources/proxy/uid/"
        f"{quote(profile.datasource_uid, safe='')}/loki/api/v1/query_range"
    )
    request_params = {
        "query": query,
        "start": str(start_ns),
        "end": str(end_ns),
    }
    if query_type == "log":
        if limit is not None:
            request_params["limit"] = str(limit)
        if direction is not None:
            request_params["direction"] = direction
    elif step is not None:
        request_params["step"] = step
    params = urlencode(request_params)
    request = Request(
        f"{endpoint}?{params}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    try:
        payload = _request_payload(
            request=request,
            timeout=timeout,
            opener=opener,
            sleeper=sleeper,
        )
        return _result_from_payload(payload, query_type)
    except QueryError as error:
        error_type = AuthenticationError if isinstance(error, AuthenticationError) else QueryError
        raise error_type(str(error).replace(token, "[REDACTED]")) from error


def _request_payload(
    *,
    request: Request,
    timeout: float,
    opener: Opener,
    sleeper: Sleeper,
) -> dict[str, Any]:
    retryable_statuses = {429, 502, 503, 504}
    for attempt in range(3):
        try:
            with closing(opener(request, timeout)) as response:
                return _decode_payload(response.read())
        except HTTPError as error:
            if error.code in {401, 403}:
                raise AuthenticationError(
                    f"Grafana request failed with HTTP {error.code}."
                ) from error
            if error.code not in retryable_statuses or attempt == 2:
                raise QueryError(f"Grafana request failed with HTTP {error.code}.") from error
            retry_after = error.headers.get("Retry-After")
            sleeper(_retry_delay(retry_after, attempt))
        except (URLError, TimeoutError) as error:
            reason = error.reason if isinstance(error, URLError) else "timed out"
            raise QueryError(f"Grafana request failed: {reason}.") from error
    raise AssertionError("retry loop must return or raise")


def _retry_delay(retry_after: str | None, attempt: int) -> float:
    if retry_after is None:
        return 0.5 * (2.0**attempt)
    try:
        return max(0.0, float(retry_after))
    except ValueError:
        try:
            target = parsedate_to_datetime(retry_after)
            if target.tzinfo is None:
                target = target.replace(tzinfo=UTC)
            return max(0.0, (target - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError):
            return 0.5 * (2.0**attempt)


def _decode_payload(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body, parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QueryError("Grafana returned an invalid JSON response.") from error
    if not isinstance(payload, dict):
        raise QueryError("Grafana returned an unexpected JSON response.")
    return payload


def _result_from_payload(
    payload: dict[str, Any], query_type: QueryType
) -> QueryResult:
    if payload.get("status") != "success":
        message = payload.get("message") or payload.get("error") or "unknown API error"
        raise QueryError(f"Loki query failed: {message}")
    data = payload.get("data")
    expected_result_type = "streams" if query_type == "log" else "matrix"
    if not isinstance(data, dict) or data.get("resultType") != expected_result_type:
        raise QueryError(
            f"Loki returned a result other than {expected_result_type}."
        )
    result = data.get("result")
    if not isinstance(result, list):
        return QueryResult(
            query_type=query_type,
            records=[],
            skipped=ParseSummary(skipped_series=1),
        )

    if query_type == "metric":
        samples: list[Record] = []
        skipped_series = 0
        skipped_samples = 0
        for series in result:
            if not isinstance(series, dict):
                skipped_series += 1
                continue
            labels = _labels(series.get("metric"))
            values = series.get("values")
            if labels is None or not isinstance(values, list):
                skipped_series += 1
                continue
            for sample in values:
                if not isinstance(sample, list) or len(sample) != 2:
                    skipped_samples += 1
                    continue
                timestamp, value = sample
                if not isinstance(value, str):
                    skipped_samples += 1
                    continue
                try:
                    timestamp_ns = _metric_timestamp_ns(timestamp)
                except (ValueError, InvalidOperation):
                    skipped_samples += 1
                    continue
                samples.append(
                    MetricSample(
                        timestamp_ns=timestamp_ns,
                        labels=labels,
                        value=value,
                    )
                )
        return QueryResult(
            query_type="metric",
            records=sorted(
                samples, key=lambda sample: sample.timestamp_ns, reverse=True
            ),
            skipped=ParseSummary(
                skipped_series=skipped_series,
                skipped_samples=skipped_samples,
            ),
        )

    entries: list[Record] = []
    skipped_series = 0
    skipped_entries = 0
    for stream in result:
        if not isinstance(stream, dict):
            skipped_series += 1
            continue
        labels = _labels(stream.get("stream"))
        values = stream.get("values")
        if labels is None or not isinstance(values, list):
            skipped_series += 1
            continue
        for entry in values:
            if not isinstance(entry, list) or len(entry) != 2:
                skipped_entries += 1
                continue
            timestamp, line = entry
            if not isinstance(line, str):
                skipped_entries += 1
                continue
            try:
                timestamp_ns = _log_timestamp_ns(timestamp)
            except (ValueError, InvalidOperation):
                skipped_entries += 1
                continue
            entries.append(
                LogEntry(
                    timestamp_ns=timestamp_ns,
                    labels=labels,
                    line=line,
                )
            )
    return QueryResult(
        query_type="log",
        records=sorted(entries, key=lambda entry: entry.timestamp_ns, reverse=True),
        skipped=ParseSummary(
            skipped_series=skipped_series,
            skipped_entries=skipped_entries,
        ),
    )


def _metric_timestamp_ns(timestamp: object) -> int:
    seconds = Decimal(str(timestamp))
    nanoseconds = seconds * Decimal(1_000_000_000)
    if not nanoseconds.is_finite() or nanoseconds != nanoseconds.to_integral_value():
        raise ValueError("metric timestamp has more than nanosecond precision")
    return _supported_timestamp_ns(int(nanoseconds))


def _log_timestamp_ns(timestamp: object) -> int:
    nanoseconds = Decimal(str(timestamp))
    if not nanoseconds.is_finite() or nanoseconds != nanoseconds.to_integral_value():
        raise ValueError("log timestamp is not an integer")
    return _supported_timestamp_ns(int(nanoseconds))


def _supported_timestamp_ns(timestamp_ns: int) -> int:
    seconds, _ = divmod(timestamp_ns, 1_000_000_000)
    try:
        datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError) as error:
        raise ValueError("timestamp is outside the supported UTC range") from error
    return timestamp_ns


def _labels(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    if not all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in value.items()
    ):
        return None
    return dict(value)
