from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import TextIO

from .client import LogEntry


def format_timestamp_utc(timestamp_ns: int) -> str:
    seconds, nanoseconds = divmod(timestamp_ns, 1_000_000_000)
    base = datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{nanoseconds:09d}Z"


def write_entries(entries: list[LogEntry], output_format: str, stream: TextIO) -> None:
    if not entries:
        if output_format == "human":
            print("No log entries found.", file=stream)
        return
    for entry in entries:
        if output_format == "raw":
            rendered = entry.line
        elif output_format == "jsonl":
            rendered = json.dumps(
                {
                    "timestamp": format_timestamp_utc(entry.timestamp_ns),
                    "labels": entry.labels,
                    "line": entry.line,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        else:
            seconds, nanoseconds = divmod(entry.timestamp_ns, 1_000_000_000)
            local = datetime.fromtimestamp(seconds).astimezone()
            rendered = (
                f"{local.strftime('%Y-%m-%d %H:%M:%S')}.{nanoseconds:09d} "
                f"{local.strftime('%z')} {entry.line}"
            )
        print(rendered, file=stream)
