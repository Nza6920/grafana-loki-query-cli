from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from http.client import HTTPException
import json
import time
import socket
import threading
from typing import Any, Literal, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .config import Profile


class ReadableResponse(Protocol):
    def read(self, size: int = -1) -> bytes: ...
    def close(self) -> None: ...


Opener = Callable[[Request, float], ReadableResponse]
Sleeper = Callable[[float], None]


class QueryError(RuntimeError):
    """Raised when Grafana or Loki cannot return a usable response."""


class HttpAttemptBudgetError(QueryError):
    """Raised before a request or retry wait would exceed its HTTP cap."""


class InvalidQueryError(QueryError):
    """Raised for invalid request parameters or LogQL."""


class QueryTimeoutError(QueryError):
    """Raised when a range request times out."""


class QueryResourceError(QueryError):
    """A response or retained data budget exhausted without retry."""


DEFAULT_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
DEFAULT_RETAINED_BYTES = 64 * 1024 * 1024
MAX_RETAINED_BYTES = 256 * 1024 * 1024


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


def canonical_record_size(record: Record) -> int:
    """Complete deterministic UTF-8 representation, independent of output format."""
    value: dict[str, object] = {"timestamp_ns": record.timestamp_ns, "labels": record.labels}
    if isinstance(record, LogEntry):
        value.update(type="log", line=record.line)
    else:
        value.update(type="metric", value=record.value)
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8"))


class RetainedBudget:
    """Current held record bytes; replacements are atomic on failure."""

    def __init__(self, maximum: int = DEFAULT_RETAINED_BYTES) -> None:
        self.maximum = maximum
        self.used = 0

    def replace(self, previous: int, next_charge: int) -> None:
        updated = self.used - previous + next_charge
        if previous < 0 or previous > self.used or next_charge < 0:
            raise ValueError("invalid retained byte replacement")
        if updated > self.maximum:
            raise QueryResourceError("retained_bytes: incomplete response; retained byte budget exhausted.")
        self.used = updated


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
    raw_entries: int = 0
    raw_count_exact: bool = True


class _DeadlineResponse:
    """Real urllib body reception, including chunk framing, has an absolute deadline."""

    def __init__(self, response: Any, remaining: float) -> None:
        self.response = response
        self.expired = threading.Event()
        # Capture the owned socket before HTTPResponse may clear fp at EOF.
        self.socket = cast(socket.socket, response.fp.raw._sock)
        self.timer = threading.Timer(max(0.0, remaining), self._expire)
        self.timer.daemon = True
        self.timer.start()

    def _expire(self) -> None:
        self.expired.set()
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def read(self, size: int = -1) -> bytes:
        try:
            chunk = cast(bytes, self.response.read1(size))
        except (OSError, ValueError, HTTPException) as error:
            if self.expired.is_set():
                raise QueryTimeoutError("response_timeout: incomplete response reception.") from error
            raise QueryError("Grafana response reception failed.") from error
        if self.expired.is_set():
            raise QueryTimeoutError("response_timeout: incomplete response reception.")
        return chunk

    def close(self) -> None:
        self.timer.cancel()
        self.timer.join()
        self.response.close()


def default_opener(request: Request, timeout: float) -> ReadableResponse:
    started = time.monotonic()
    response = urlopen(request, timeout=timeout)  # noqa: S310 - configured endpoint
    try:
        return _DeadlineResponse(response, timeout - (time.monotonic() - started))
    except BaseException:
        response.close()
        raise


def query_endpoint(profile: Profile) -> str:
    return (
        f"{profile.grafana_url}/api/datasources/proxy/uid/"
        f"{quote(profile.datasource_uid, safe='')}/loki/api/v1/query_range"
    )


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
    monotonic: Callable[[], float] = time.monotonic,
    max_response_bytes: int = DEFAULT_RESPONSE_BYTES,
    max_retained_bytes: int = DEFAULT_RETAINED_BYTES,
) -> QueryResult:
    endpoint = query_endpoint(profile)
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
            monotonic=monotonic,
            max_response_bytes=max_response_bytes,
        )
        return _result_from_payload(payload, query_type, max_retained_bytes)
    except QueryError as error:
        error_type = type(error)
        raise error_type(str(error).replace(token, "[REDACTED]")) from error


def _request_payload(
    *,
    request: Request,
    timeout: float,
    opener: Opener,
    sleeper: Sleeper,
    monotonic: Callable[[], float],
    max_response_bytes: int,
) -> dict[str, Any]:
    retryable_statuses = {429, 502, 503, 504}
    for attempt in range(3):
        deadline = monotonic() + timeout
        try:
            with closing(opener(request, timeout)) as response:
                body = bytearray()
                while True:
                    if monotonic() >= deadline:
                        raise QueryTimeoutError("response_timeout: incomplete response reception.")
                    chunk = response.read(min(65536, max_response_bytes - len(body) + 1))
                    if monotonic() >= deadline:
                        raise QueryTimeoutError("response_timeout: incomplete response reception.")
                    if len(body) + len(chunk) > max_response_bytes:
                        raise QueryResourceError("response_bytes: incomplete response; byte budget exhausted.")
                    if not chunk:
                        return _decode_payload(body)
                    body.extend(chunk)
        except HTTPError as error:
            error.close()
            if error.code in {401, 403}:
                raise AuthenticationError(
                    f"Grafana request failed with HTTP {error.code}."
                ) from error
            if error.code in {400, 422}:
                raise InvalidQueryError(f"Grafana request failed with HTTP {error.code}.") from error
            if error.code == 504 and attempt == 2:
                raise QueryTimeoutError("Grafana request failed with HTTP 504.") from error
            if error.code not in retryable_statuses or attempt == 2:
                raise QueryError(f"Grafana request failed with HTTP {error.code}.") from error
            retry_after = error.headers.get("Retry-After")
            sleeper(_retry_delay(retry_after, attempt))
        except (URLError, TimeoutError) as error:
            reason = error.reason if isinstance(error, URLError) else "timed out"
            error_type = QueryTimeoutError if isinstance(error, TimeoutError) or isinstance(reason, TimeoutError) else QueryError
            message = "response_timeout: incomplete request or response reception." if error_type is QueryTimeoutError else f"Grafana request failed: {reason}."
            raise error_type(message) from error
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


def _decode_payload(body: bytes | bytearray) -> dict[str, Any]:
    try:
        payload = json.loads(body, parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QueryError("Grafana returned an invalid JSON response.") from error
    if not isinstance(payload, dict):
        raise QueryError("Grafana returned an unexpected JSON response.")
    return payload


def _result_from_payload(
    payload: dict[str, Any], query_type: QueryType, max_retained_bytes: int
) -> QueryResult:
    if payload.get("status") != "success":
        message = payload.get("message") or payload.get("error") or "unknown API error"
        error_type = InvalidQueryError if payload.get("errorType") in {"bad_data", "parse"} else QueryError
        raise error_type(f"Loki query failed: {message}")
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
            raw_count_exact=False,
        )

    retained = RetainedBudget(max_retained_bytes)

    def retain(records: list[Record], record: Record) -> None:
        retained.replace(0, canonical_record_size(record))
        records.append(record)

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
                retain(samples,
                    MetricSample(
                        timestamp_ns=timestamp_ns,
                        labels=labels,
                        value=value,
                    )
                )
        samples.sort(key=lambda sample: sample.timestamp_ns, reverse=True)
        return QueryResult(
            query_type="metric",
            records=samples,
            skipped=ParseSummary(
                skipped_series=skipped_series,
                skipped_samples=skipped_samples,
            ),
        )

    entries: list[Record] = []
    skipped_series = 0
    skipped_entries = 0
    raw_entries = 0
    raw_count_exact = True
    for stream in result:
        if not isinstance(stream, dict):
            skipped_series += 1
            raw_count_exact = False
            continue
        labels = _labels(stream.get("stream"))
        values = stream.get("values")
        if not isinstance(values, list):
            skipped_series += 1
            raw_count_exact = False
            continue
        raw_entries += len(values)
        if labels is None:
            skipped_series += 1
            skipped_entries += len(values)
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
            retain(entries,
                LogEntry(
                    timestamp_ns=timestamp_ns,
                    labels=labels,
                    line=line,
                )
            )
    entries.sort(key=lambda entry: entry.timestamp_ns, reverse=True)
    return QueryResult(
        query_type="log",
        records=entries,
        raw_entries=raw_entries,
        raw_count_exact=raw_count_exact,
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
