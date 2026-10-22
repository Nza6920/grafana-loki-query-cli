from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TextIO

from .client import QueryResult, query_endpoint
from .config import ConfigurationError, Profile


def log_report(
    *, profile: Profile, query: str, start_ns: int, end_ns: int,
    direction: str, limit: int, result: QueryResult | None, attempts: int,
    elapsed_seconds: float, timeout: float, failure_reason: str | None = None,
) -> dict[str, Any]:
    interval = {"start_ns": str(start_ns), "end_ns": str(end_ns)}
    usage = {"http_attempts": attempts, "elapsed_seconds": elapsed_seconds}
    reasons = [failure_reason] if failure_reason else []
    result = result if result is not None else QueryResult("log", [], raw_count_exact=False)
    if result.raw_entries >= limit:
        reasons.append("possible_truncation")
    if result.skipped.incomplete:
        reasons.append("parse_loss")
    return {
        "schema_version": 1,
        "query_type": "log",
        "profile": profile.name,
        "target": {"endpoint": query_endpoint(profile), "datasource_uid": profile.datasource_uid},
        "query": query,
        "start_ns": str(start_ns), "end_ns": str(end_ns),
        "direction": direction,
        "status": "failed" if failure_reason else "incomplete" if reasons else "complete",
        "completeness_scope": "Retrieval under the declared query and range only; does not prove ingestion completeness or absence of business events.",
        "coverage": {"resolved": [] if reasons else [interval],
                     "unresolved": [{**interval, "reasons": reasons}] if reasons else []},
        "counts": {"raw_entries": result.raw_entries, "valid_records": len(result.records),
                   "skipped_entries": result.skipped.skipped_entries,
                   "skipped_series": result.skipped.skipped_series, "raw_count_exact": result.raw_count_exact},
        "request_limit": limit,
        "usage": {"current": usage, "cumulative": dict(usage)},
        "budgets": {"max_http_attempts": 3, "max_elapsed_seconds": None, "max_records": None,
                    "request_timeout_seconds": timeout},
        "stop_reasons": [failure_reason] if failure_reason else [],
    }


def validate_report_destination(destination: str | None) -> None:
    if destination is None:
        return
    path = Path(destination).expanduser()
    try:
        if destination == "-" or (path.exists() and not path.is_file()):
            raise ConfigurationError("--report requires a regular file, not stdout or a device.")
        if not path.parent.is_dir():
            raise ConfigurationError("The --report parent directory must already exist.")
    except OSError as error:
        raise ConfigurationError("Cannot inspect the selected JSON report destination.") from error


def _redact(value: Any, token: str) -> Any:
    if isinstance(value, str):
        return value.replace(token, "[REDACTED]")
    if isinstance(value, list):
        return [_redact(item, token) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, token) for key, item in value.items()}
    return value


def write_log_report(
    report: dict[str, Any], destination: str | None, stderr: TextIO, *, token: str,
) -> None:
    counts, usage = report["counts"], report["usage"]["current"]
    reasons = [reason for interval in report["coverage"]["unresolved"] for reason in interval["reasons"]]
    warning = ""
    if reasons:
        warning = "warning: incomplete results; "
        if "possible_truncation" in reasons:
            warning += "possible truncation (returned limit); "
        if "parse_loss" in reasons:
            warning += (f"skipped {counts['skipped_series']} series, "
                        f"{counts['skipped_entries']} log entries; ")
    print(
        warning + f"coverage: {report['status']}; "
        f"resolved {len(report['coverage']['resolved'])}, "
        f"unresolved {len(report['coverage']['unresolved'])}; "
        f"raw {counts['raw_entries']}, valid {counts['valid_records']}; "
        f"HTTP attempts {usage['http_attempts']}, elapsed {usage['elapsed_seconds']:.3f}s "
        "(query/range retrieval only; not ingestion completeness).",
        file=stderr,
    )
    if destination is not None:
        try:
            Path(destination).expanduser().write_text(
                json.dumps(_redact(report, token), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as error:
            raise ConfigurationError("Cannot write JSON report to the selected destination.") from error
