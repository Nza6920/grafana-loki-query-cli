from __future__ import annotations

from datetime import UTC, datetime
from io import TextIOBase
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
        write_record(rendered + "\n", stream)


DEFAULT_RECORD_BYTES = 4 * 1024 * 1024
MAX_RECORD_BYTES = 8 * 1024 * 1024
DEFAULT_OUTPUT_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 32 * 1024 * 1024


class OutputBudget:
    """One invocation's UTF-8 output allowance, shared by both streams."""

    def __init__(self, record_bytes: int, output_bytes: int) -> None:
        self.record_bytes = record_bytes
        self.output_bytes = output_bytes
        self.reserve = min(2048, output_bytes)
        self.used = 0
        self.stopped = False
        self.upstream_status = "unknown"
        self.losses: dict[str, int] = {}

    def mark_loss(self, reason: str, count: int = 1) -> None:
        self.losses[reason] = self.losses.get(reason, 0) + count

    def write(self, text: str, stream: TextIO, *, record: bool = False) -> int:
        size = len(text.encode("utf-8"))
        if self.stopped:
            return len(text)
        if record and size > self.record_bytes:
            self.mark_loss("record_too_large")
            return len(text)
        if self.used + size > self.output_bytes - self.reserve:
            self.mark_loss("output_budget")
            self.stopped = True
            return len(text)
        stream.write(text)
        self.used += size
        return len(text)

    def evidence_status(self) -> dict[str, object]:
        return {
            "upstream_retrieval": self.upstream_status,
            "local_evidence": "incomplete" if self.losses else "complete",
            "max_record_bytes": self.record_bytes,
            "max_output_bytes": self.output_bytes,
            "losses": dict(self.losses),
            "unprocessed_tail": "unknown" if self.stopped else "none",
        }

    def ensure_status_capacity(self) -> None:
        if self.reserve < 512:
            self.mark_loss("status_budget")

    def finish(self, stderr: TextIO, accounting: str = "", *, leading_newline: bool = False) -> None:
        status = "evidence: " + json.dumps(self.evidence_status(), separators=(",", ":")) + "\n"
        available = self.output_bytes - self.used
        if leading_newline and available:
            stderr.write("\n")
            available -= 1
        accounting_bytes = len(accounting.encode("utf-8"))
        remaining = max(0, available - accounting_bytes)
        if len(status.encode("utf-8")) > remaining:
            status = "incomplete\n" if remaining >= 11 else ""
        stderr.write(status)
        if accounting_bytes <= available:
            stderr.write(accounting)


class BudgetedStream(TextIOBase):
    def __init__(self, stream: TextIO, budget: OutputBudget) -> None:
        self.stream = stream
        self.budget = budget
        self.needs_newline = False

    def write(self, text: str, /) -> int:
        previous = self.budget.used
        result = self.budget.write(text, self.stream)
        if self.budget.used > previous:
            self.needs_newline = not text.endswith("\n")
        return result

    def write_record(self, text: str) -> None:
        self.budget.write(text, self.stream, record=True)


def write_record(text: str, stream: TextIO) -> None:
    """Emit one complete, already rendered record including its delimiter."""
    if isinstance(stream, BudgetedStream):
        stream.write_record(text)
    else:
        stream.write(text)


def set_retrieval_status(stream: TextIO, status: str, *, parse_loss: bool = False) -> None:
    if isinstance(stream, BudgetedStream):
        stream.budget.upstream_status = status
        if parse_loss:
            stream.budget.mark_loss("parse_loss")
