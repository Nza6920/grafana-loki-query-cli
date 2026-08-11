from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import json
import time
from typing import Any, Protocol, cast
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
    limit: int,
    timeout: float,
    opener: Opener = default_opener,
    sleeper: Sleeper = time.sleep,
) -> list[LogEntry]:
    endpoint = (
        f"{profile.grafana_url}/api/datasources/proxy/uid/"
        f"{quote(profile.datasource_uid, safe='')}/loki/api/v1/query_range"
    )
    params = urlencode(
        {
            "query": query,
            "start": str(start_ns),
            "end": str(end_ns),
            "limit": str(limit),
            "direction": "backward",
        }
    )
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
        return _entries_from_payload(payload)
    except AuthenticationError as error:
        raise AuthenticationError(str(error).replace(token, "[REDACTED]")) from error
    except QueryError as error:
        raise QueryError(str(error).replace(token, "[REDACTED]")) from error


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
            raise QueryError(f"Grafana request failed: {error.reason if isinstance(error, URLError) else 'timed out'}.") from error
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
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QueryError("Grafana returned an invalid JSON response.") from error
    if not isinstance(payload, dict):
        raise QueryError("Grafana returned an unexpected JSON response.")
    return payload


def _entries_from_payload(payload: dict[str, Any]) -> list[LogEntry]:
    if payload.get("status") != "success":
        message = payload.get("message") or payload.get("error") or "unknown API error"
        raise QueryError(f"Loki query failed: {message}")
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("resultType") != "streams":
        raise QueryError("Loki returned a result other than log streams.")
    result = data.get("result")
    if not isinstance(result, list):
        raise QueryError("Loki response is missing stream results.")

    entries: list[LogEntry] = []
    try:
        for stream in result:
            labels = stream["stream"]
            for timestamp, line in stream["values"]:
                entries.append(
                    LogEntry(
                        timestamp_ns=int(timestamp),
                        labels={str(key): str(value) for key, value in labels.items()},
                        line=str(line),
                    )
                )
    except (KeyError, TypeError, ValueError) as error:
        raise QueryError("Loki returned malformed stream data.") from error
    return sorted(entries, key=lambda entry: entry.timestamp_ns, reverse=True)
