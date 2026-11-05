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
