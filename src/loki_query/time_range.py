from __future__ import annotations

from datetime import UTC, datetime, timedelta
import re


class TimeRangeError(ValueError):
    """Raised when a query time range is invalid."""


_DURATION = re.compile(r"^(?P<amount>[1-9][0-9]*)(?P<unit>[smhdw])$")
_SECONDS_PER_UNIT = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
    "w": 7 * 24 * 60 * 60,
}


def parse_duration(value: str) -> timedelta:
    match = _DURATION.fullmatch(value)
    if match is None:
        raise TimeRangeError(
            f"Invalid duration {value!r}; use a positive integer followed by s, m, h, d, or w."
        )
    seconds = int(match.group("amount")) * _SECONDS_PER_UNIT[match.group("unit")]
    return timedelta(seconds=seconds)


def parse_rfc3339(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise TimeRangeError(f"Invalid RFC 3339 timestamp: {value!r}.") from error
    if parsed.tzinfo is None:
        raise TimeRangeError(f"Timestamp must include a UTC offset: {value!r}.")
    return parsed.astimezone(UTC)


def to_nanoseconds(value: datetime) -> int:
    return int(value.timestamp()) * 1_000_000_000 + value.microsecond * 1_000


def resolve_time_range(
    *,
    since: str | None,
    start: str | None,
    end: str | None,
    now: datetime,
) -> tuple[int, int]:
    end_time = parse_rfc3339(end) if end else now.astimezone(UTC)
    if start:
        start_time = parse_rfc3339(start)
    else:
        start_time = end_time - parse_duration(since or "15m")
    if start_time >= end_time:
        raise TimeRangeError("Query start must be earlier than query end.")
    return to_nanoseconds(start_time), to_nanoseconds(end_time)
