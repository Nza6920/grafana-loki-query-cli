from __future__ import annotations

from copy import deepcopy
from io import StringIO
import unittest
from typing import Any, TextIO, cast

from loki_query.client import LogEntry, QueryResult
from loki_query.output import BudgetedStream, OutputBudget
from loki_query.presentation import present_logs


class PresentationTests(unittest.TestCase):
    def test_report_and_shared_nested_evidence_are_immutable_during_output_loss(self) -> None:
        report: dict[str, Any] = {
            'status': 'complete',
            'coverage': {'resolved': [{'start_ns': '1', 'end_ns': '2'}], 'unresolved': []},
            'counts': {'raw_entries': 1, 'valid_records': 1},
            'usage': {'current': {'http_attempts': 1, 'elapsed_seconds': 0.0}},
            'evidence': {'losses': {'historical': 1}},
        }
        original = deepcopy(report)
        budget = OutputBudget(2, 4096)
        stdout, stderr = StringIO(), StringIO()
        result = QueryResult('log', [LogEntry(1, {}, 'large')], raw_entries=1)
        presented = present_logs(result, report, 'raw', cast(TextIO, BudgetedStream(stdout, budget)),
                                 cast(TextIO, BudgetedStream(stderr, budget)))
        self.assertEqual(report, original)
        self.assertEqual(presented['status'], 'complete')
        self.assertEqual(presented['evidence']['losses'], {'record_too_large': 1})
        self.assertEqual(presented['evidence']['local_evidence'], 'incomplete')
        presented['coverage']['resolved'][0]['start_ns'] = 'changed'
        self.assertEqual(report, original)
        self.assertEqual(stdout.getvalue(), '')
