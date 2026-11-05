"""Present retrieved log evidence without changing the retrieval report.

The command owns the streams and their shared invocation budget. Presentation
owns loss registration, rendering, diagnostics and the report evidence snapshot;
callers decide whether to save the returned report in an investigation.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, TextIO

from .client import QueryResult
from .output import BudgetedStream, set_retrieval_status, write_result
from .reporting import write_log_report


def present_logs(
    result: QueryResult | None, report: dict[str, Any], output_format: str,
    stdout: TextIO, stderr: TextIO, *, destination: str | None = None, token: str = '',
) -> dict[str, Any]:
    """Render logs and return a detached report carrying this output's evidence.

    ``None`` is a failed retrieval: emit its diagnostics and report without an
    empty-result message. Streams reuse the command's existing output budget.
    Exceptions propagate so command-level accounting and finalization still run.
    """
    presented = deepcopy(report)
    current = presented['usage']['current']
    parse_loss = (bool(current.get('skipped_entries') or current.get('skipped_series'))
                  if 'skipped_entries' in current or 'skipped_series' in current
                  else result is not None and result.skipped.incomplete)
    set_retrieval_status(stderr, presented['status'], parse_loss=parse_loss)
    if isinstance(stderr, BudgetedStream):
        for reason, count in presented.get('current_losses', {}).items():
            stderr.budget.mark_loss(reason, count)
    if result is not None:
        write_result(result, output_format, stdout)
    write_log_report(presented, destination, stderr, token=token)
    return presented


def present_trace(
    result: QueryResult, matches: QueryResult, report: dict[str, Any], output_format: str,
    stdout: TextIO, stderr: TextIO, *, service_field: str,
    destination: str | None = None, token: str = '',
) -> dict[str, Any]:
    """Present trace matches selected by the trace module, preserving candidates.

    The trace module owns exact matching and its renderer. Import the renderer
    when presenting so its command orchestration can use this interface too.
    """
    from .trace import write_trace_result

    presented = deepcopy(report)
    current = presented['usage']['current']
    parse_loss = (bool(current['skipped_entries'] or current['skipped_series'])
                  if 'skipped_entries' in current else result.skipped.incomplete)
    set_retrieval_status(stderr, presented['status'], parse_loss=parse_loss)
    if isinstance(stderr, BudgetedStream):
        for reason, count in presented.get('current_losses', {}).items():
            stderr.budget.mark_loss(reason, count)
    write_trace_result(matches, output_format, stdout, service_field=service_field)
    print('Trace timeline: equal timestamps do not imply causality.', file=stderr)
    print(f'Trace candidates: {len(result.records)} examined, {len(matches.records)} matched.', file=stderr)
    if not matches.records:
        print(f"No trace matches under the selected conditions; candidate retrieval is {presented['status']}.", file=stderr)
    write_log_report(presented, destination, stderr, token=token)
    return presented
