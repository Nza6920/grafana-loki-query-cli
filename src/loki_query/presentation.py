"""Present retrieved log evidence without changing the retrieval report.

The command owns the streams and their shared invocation budget. Presentation
owns loss registration, rendering, diagnostics and the report evidence snapshot;
callers decide whether to save the returned report in an investigation.
"""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any, TextIO

from .client import QueryResult
from .output import BudgetedStream, set_retrieval_status, write_result
from .reporting import write_log_diagnostics, write_log_report


def _prepare_report(
    result: QueryResult | None, report: dict[str, Any], stderr: TextIO, *, offline: bool,
) -> dict[str, Any]:
    presented = deepcopy(report)
    if offline:
        presented['current_losses'] = {}
        presented['usage']['current'] = {**presented['usage']['current'],
                                          'skipped_entries': 0, 'skipped_series': 0}
        presented.pop('evidence', None)
    current = presented['usage']['current']
    parse_loss = (bool(current.get('skipped_entries') or current.get('skipped_series'))
                  if 'skipped_entries' in current or 'skipped_series' in current
                  else result is not None and result.skipped.incomplete)
    set_retrieval_status(stderr, presented['status'], parse_loss=parse_loss)
    if isinstance(stderr, BudgetedStream):
        for reason, count in presented.get('current_losses', {}).items():
            stderr.budget.mark_loss(reason, count)
    return presented


def _finish_report(report: dict[str, Any], destination: str | None, stderr: TextIO, *, token: str) -> None:
    write_log_diagnostics(report, stderr)
    if isinstance(stderr, BudgetedStream):
        report['evidence'] = stderr.budget.evidence_status()
    write_log_report(report, destination, token=token)


def present_logs(
    result: QueryResult | None, report: dict[str, Any], output_format: str,
    stdout: TextIO, stderr: TextIO, *, destination: str | None = None, token: str = '',
    offline: bool = False, elapsed_seconds: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Render logs and return a detached report carrying this output's evidence.

    ``None`` is a failed retrieval: emit its diagnostics and report without an
    empty-result message. Streams reuse the command's existing output budget.
    Offline output resets historical presentation losses while keeping retrieval
    coverage and counts. Exceptions propagate for command-level finalization.
    Ordinary queries can sample elapsed time after rendering, before diagnostics;
    this updates only the detached report, leaving the input and its usage intact.
    """
    presented = _prepare_report(result, report, stderr, offline=offline)
    if result is not None:
        write_result(result, output_format, stdout)
    if elapsed_seconds is not None:
        elapsed = elapsed_seconds()
        presented['usage']['current']['elapsed_seconds'] = elapsed
        presented['usage']['cumulative']['elapsed_seconds'] = elapsed
    _finish_report(presented, destination, stderr, token=token)
    return presented


def present_trace(
    result: QueryResult, matches: QueryResult, report: dict[str, Any], output_format: str,
    stdout: TextIO, stderr: TextIO, *, service_field: str,
    destination: str | None = None, token: str = '', offline: bool = False,
) -> dict[str, Any]:
    """Present trace matches selected by the trace module, preserving candidates.

    The trace module owns exact matching and its renderer. Import the renderer
    when presenting so its command orchestration can use this interface too.
    """
    from .trace import write_trace_result

    presented = _prepare_report(result, report, stderr, offline=offline)
    write_trace_result(matches, output_format, stdout, service_field=service_field)
    print('Trace timeline: equal timestamps do not imply causality.', file=stderr)
    print(f'Trace candidates: {len(result.records)} examined, {len(matches.records)} matched.', file=stderr)
    if not matches.records:
        print(f"No trace matches under the selected conditions; candidate retrieval is {presented['status']}.", file=stderr)
    _finish_report(presented, destination, stderr, token=token)
    return presented
