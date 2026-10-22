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
    delta = value.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


def parse_rfc3339_ns(value: str) -> int:
    fraction = re.search(r"[.,](\d+)(?=Z$|[+-]\d{2}:?\d{2}$)", value)
    if fraction and len(fraction.group(1)) > 9:
        raise TimeRangeError("Timestamp must not exceed nanosecond precision.")
    parsed = parse_rfc3339(value)
    remainder = int(fraction.group(1).ljust(9, "0")) % 1000 if fraction else 0
    return to_nanoseconds(parsed) + remainder


def resolve_time_range(
    *,
    since: str | None,
    start: str | None,
    end: str | None,
    now: datetime,
) -> tuple[int, int]:
    end_ns = parse_rfc3339_ns(end) if end else to_nanoseconds(now)
    if start:
        start_ns = parse_rfc3339_ns(start)
    else:
        duration = parse_duration(since or "15m")
        start_ns = end_ns - (duration.days * 86400 + duration.seconds) * 1_000_000_000
    if start_ns >= end_ns:
        raise TimeRangeError("Query start must be earlier than query end.")
    return start_ns, end_ns
