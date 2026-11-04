from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO, StringIO
import json
from pathlib import Path
import tempfile
import unittest
from urllib.request import Request

from loki_query.cli import main


class OutputBudgetCliTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = Path(directory.name) / 'config.toml'
        self.config.write_text('[profiles.test]\ngrafana_url="https://grafana.example.com"\n'
                               'datasource_uid="logs"\ntoken_env="TEST_TOKEN"\n')
        self.stdout, self.stderr = StringIO(), StringIO()
        self.attempts = 0

    def invoke(self, lines: list[str], *flags: str, metric: bool = False) -> int:
        def opener(request: Request, timeout: float) -> BytesIO:
            self.attempts += 1
            result = ([{'metric': {}, 'values': [[i + 1, line] for i, line in enumerate(lines)]}]
                      if metric else [{'stream': {}, 'values': [[str(i + 1), line] for i, line in enumerate(lines)]}])
            return BytesIO(json.dumps({'status': 'success', 'data': {
                'resultType': 'matrix' if metric else 'streams', 'result': result}}).encode())
        return main(['--config', str(self.config), 'query', '--profile', 'test',
                     *(['--query-type', 'metric'] if metric else ['--direction', 'forward']),
                     *flags, '{}'], environ={'TEST_TOKEN': 'secret'},
                    stdout=self.stdout, stderr=self.stderr, opener=opener,
                    now=datetime(2026, 8, 11, tzinfo=UTC))

    def test_oversized_record_is_dropped_whole_and_later_record_survives(self) -> None:
        code = self.invoke(['中' * 100, 'ok'], '--output', 'jsonl', '--max-record-bytes', '150')
        self.assertEqual(code, 4, self.stderr.getvalue())
        rows = [json.loads(line) for line in self.stdout.getvalue().splitlines()]
        self.assertEqual([row['line'] for row in rows], ['ok'])
        self.assertIn('"record_too_large":1', self.stderr.getvalue())
        self.assertIn('"local_evidence":"incomplete"', self.stderr.getvalue())

    def test_total_budget_stops_before_next_record_without_sampling_smaller_tail(self) -> None:
        code = self.invoke(['last', '中' * 20, 'first'], '--output', 'raw',
                           '--max-output-bytes', '2070', '--max-http-attempts', '1')
        self.assertEqual(code, 4, self.stderr.getvalue())
        self.assertEqual(self.stdout.getvalue(), 'first\n')
        self.assertLessEqual(len((self.stdout.getvalue() + self.stderr.getvalue()).encode()), 2070)
        self.assertIn('"unprocessed_tail":"unknown"', self.stderr.getvalue())
        self.assertEqual(self.stderr.getvalue().splitlines()[-1], 'HTTP attempt accounting: used=1 cap=1')

    def test_record_boundary_counts_utf8_and_delimiter_in_every_format(self) -> None:
        for metric in (False, True):
            for output in ('raw', 'human', 'jsonl'):
                with self.subTest(metric=metric, output=output):
                    self.stdout, self.stderr = StringIO(), StringIO()
                    self.assertEqual(self.invoke(['中"\\尾'], '--output', output, metric=metric), 0)
                    expected = self.stdout.getvalue()
                    size = len(expected.encode())
                    self.stdout, self.stderr = StringIO(), StringIO()
                    self.assertEqual(self.invoke(['中"\\尾'], '--output', output,
                                                 '--max-record-bytes', str(size), metric=metric), 0)
                    self.assertEqual(self.stdout.getvalue(), expected)
                    self.stdout, self.stderr = StringIO(), StringIO()
                    self.assertEqual(self.invoke(['中"\\尾'], '--output', output,
                                                 '--max-record-bytes', str(size - 1), metric=metric), 4)
                    self.assertEqual(self.stdout.getvalue(), '')

    def test_invalid_budget_never_contacts_network(self) -> None:
        for flag, values in (('--max-record-bytes', ['0', '-1', 'inf', '8388609']),
                             ('--max-output-bytes', ['0', '-1', 'nan', '33554433'])):
            for value in values:
                with self.subTest(flag=flag, value=value):
                    self.stdout, self.stderr = StringIO(), StringIO()
                    self.assertEqual(self.invoke([], flag, value), 2)
        self.assertEqual(self.attempts, 0)

    def test_empty_success_reports_both_completeness_states(self) -> None:
        for metric in (False, True):
            self.stdout, self.stderr = StringIO(), StringIO()
            self.assertEqual(self.invoke([], '--output', 'jsonl', metric=metric), 0)
            self.assertEqual(self.stdout.getvalue(), '')
            self.assertIn('"upstream_retrieval":"complete"', self.stderr.getvalue())
            self.assertIn('"local_evidence":"complete"', self.stderr.getvalue())

    def test_report_keeps_retrieved_coverage_after_display_loss(self) -> None:
        report = self.config.parent / 'report.json'
        code = self.invoke(['large' * 100], '--max-record-bytes', '10', '--report', str(report))
        self.assertEqual(code, 4)
        saved = json.loads(report.read_text())
        self.assertEqual(saved['status'], 'complete')
        self.assertEqual(len(saved['coverage']['resolved']), 1)
        self.assertEqual(saved['evidence']['local_evidence'], 'incomplete')

    def test_tiny_budget_never_emits_partial_json_status(self) -> None:
        self.assertEqual(self.invoke([], '--max-output-bytes', '20', '--output', 'jsonl'), 4)
        self.assertLessEqual(len(self.stderr.getvalue().encode()), 20)
        self.assertNotIn('{', self.stderr.getvalue())

    def test_upstream_saturation_is_separate_from_local_complete(self) -> None:
        self.assertEqual(self.invoke(['ok'], '--limit', '1', '--output', 'jsonl'), 0)
        self.assertIn('"upstream_retrieval":"incomplete"', self.stderr.getvalue())
        self.assertIn('"local_evidence":"complete"', self.stderr.getvalue())

    def test_status_remains_standalone_when_diagnostic_newline_cannot_fit(self) -> None:
        missing = self.config.parent / 'missing.toml'
        stderr = StringIO()
        # First discover the public diagnostic length, then exercise its exact byte boundary.
        main(['--config', str(missing), 'query', '--profile', 'test', '{}'], stderr=stderr)
        diagnostic = stderr.getvalue().splitlines()[0]
        stderr = StringIO()
        code = main(['--config', str(missing), 'query', '--profile', 'test',
                     '--max-output-bytes', str(2048 + len(diagnostic.encode())), '{}'], stderr=stderr)
        self.assertEqual(code, 2)
        self.assertTrue(any(line.startswith('evidence: ') for line in stderr.getvalue().splitlines()))

    def test_default_total_and_hard_upper_limits_are_finite(self) -> None:
        self.assertEqual(self.invoke(['x' * 1048576], '--output', 'raw'), 4)
        self.assertEqual(self.stdout.getvalue(), '')
        self.assertIn('"max_record_bytes":4194304', self.stderr.getvalue())
        self.assertIn('"max_output_bytes":1048576', self.stderr.getvalue())
        self.stdout, self.stderr = StringIO(), StringIO()
        self.assertEqual(self.invoke(['ok'], '--max-record-bytes', '8388608',
                                     '--max-output-bytes', '33554432', '--output', 'raw'), 0)
        self.assertEqual(self.stdout.getvalue(), 'ok\n')
