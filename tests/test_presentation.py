from __future__ import annotations

from copy import deepcopy
from io import StringIO
import unittest
from typing import Any, TextIO, cast

from loki_query.client import LogEntry, QueryResult
from loki_query.output import BudgetedStream, OutputBudget
from loki_query.presentation import present_logs, present_trace


class PresentationTests(unittest.TestCase):
    def test_trace_presentation_returns_detached_evidence_without_changing_candidates(self) -> None:
        report: dict[str, Any] = {
            'status': 'complete',
            'coverage': {'resolved': [{'start_ns': '1', 'end_ns': '2'}], 'unresolved': []},
            'counts': {'raw_entries': 2, 'valid_records': 2, 'matched_records': 1},
            'usage': {'current': {'http_attempts': 1, 'elapsed_seconds': 0.0}},
            'trace': {'id': 'abc', 'mapping': [['trace_id']]},
            'evidence': {'losses': {'historical': 1}},
        }
        original = deepcopy(report)
        match = LogEntry(1, {}, '{"trace_id":"abc"}')
        candidates = QueryResult('log', [match, LogEntry(2, {}, '{"trace_id":"other"}')], raw_entries=2)
        matches = QueryResult('log', [match], raw_entries=2)
        budget = OutputBudget(2, 4096)
        stdout, stderr = StringIO(), StringIO()
        presented = present_trace(candidates, matches, report, 'raw',
            cast(TextIO, BudgetedStream(stdout, budget)), cast(TextIO, BudgetedStream(stderr, budget)),
            service_field='app')
        self.assertEqual(report, original)
        self.assertEqual(len(candidates.records), 2)
        self.assertEqual(presented['status'], 'complete')
        self.assertEqual(presented['evidence']['losses'], {'record_too_large': 1})
        self.assertEqual(stdout.getvalue(), '')
        self.assertIn('2 examined, 1 matched.', stderr.getvalue())
        presented['trace']['mapping'][0][0] = 'changed'
        presented['coverage']['resolved'][0]['start_ns'] = 'changed'
        self.assertEqual(report, original)

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

    def test_scan_current_losses_are_snapshotted_without_mutating_retrieval_evidence(self) -> None:
        report: dict[str, Any] = {
            'status': 'complete',
            'coverage': {'resolved': [{'start_ns': '0', 'end_ns': '2'}], 'unresolved': []},
            'counts': {'raw_entries': 2, 'valid_records': 1, 'skipped_entries': 1, 'skipped_series': 0},
            'usage': {'current': {'http_attempts': 2, 'elapsed_seconds': 0.0,
                                  'skipped_entries': 1, 'skipped_series': 0}},
            'current_losses': {'response_timeout': 1},
            'requests': [{'reasons': ['response_timeout']}],
        }
        original = deepcopy(report)
        budget = OutputBudget(4096, 4096)
        stdout, stderr = StringIO(), StringIO()
        result = QueryResult('log', [LogEntry(1, {}, 'kept')], raw_entries=2)
        presented = present_logs(result, report, 'raw', cast(TextIO, BudgetedStream(stdout, budget)),
                                 cast(TextIO, BudgetedStream(stderr, budget)))
        self.assertEqual(stdout.getvalue(), 'kept\n')
        self.assertEqual(presented['status'], 'complete')
        self.assertEqual(presented['evidence']['local_evidence'], 'incomplete')
        self.assertEqual(presented['evidence']['losses'], {'parse_loss': 1, 'response_timeout': 1})
        self.assertEqual(report, original)
        presented['current_losses']['response_timeout'] = 99
        presented['requests'][0]['reasons'].append('changed')
        self.assertEqual(report, original)
