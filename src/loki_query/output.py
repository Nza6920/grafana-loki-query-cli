from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import TextIO

from .client import LogEntry, MetricSample, QueryResult


def format_timestamp_utc(timestamp_ns: int) -> str:
    seconds, nanoseconds = divmod(timestamp_ns, 1_000_000_000)
    instant = datetime.fromtimestamp(seconds, UTC)
    base = (
        f"{instant.year:04d}-{instant.month:02d}-{instant.day:02d}T"
        f"{instant.hour:02d}:{instant.minute:02d}:{instant.second:02d}"
    )
    return f"{base}.{nanoseconds:09d}Z"


def write_result(result: QueryResult, output_format: str, stream: TextIO) -> None:
    if not result.records:
        if result.skipped.incomplete:
            return
        if output_format == "human":
            noun = "log entries" if result.query_type == "log" else "metric samples"
            print(f"No {noun} found.", file=stream)
        return
    for record in result.records:
        if output_format == "raw":
            rendered = record.line if isinstance(record, LogEntry) else record.value
        elif output_format == "jsonl":
            if isinstance(record, LogEntry):
                json_record: dict[str, object] = {
                    "type": "log_entry",
                    "timestamp": format_timestamp_utc(record.timestamp_ns),
                    "labels": record.labels,
                    "line": record.line,
                }
            else:
                json_record = {
                    "type": "metric_sample",
                    "timestamp": format_timestamp_utc(record.timestamp_ns),
                    "labels": record.labels,
                    "value": record.value,
                }
            rendered = json.dumps(
                json_record, ensure_ascii=False, separators=(",", ":")
            )
        else:
            timestamp = format_timestamp_utc(record.timestamp_ns)
            labels = json.dumps(
                record.labels, ensure_ascii=False, separators=(",", ":")
            )
            rendered_payload = (
                record.line
                if isinstance(record, LogEntry)
                else f"value={record.value}"
            )
            rendered = f"{timestamp} {labels} {rendered_payload}"
        print(rendered, file=stream)
