from __future__ import annotations

from io import BytesIO, StringIO
import json
from pathlib import Path
import tempfile
from typing import Any
import unittest
from urllib.request import Request

from loki_query.cli import main


class TraceBudgetCliTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / 'config.toml'
        self.config.write_text('[profiles.test]\ngrafana_url="https://grafana.example.com"\n'
                               'datasource_uid="logs"\ntoken_env="TOKEN"\n')
        self.out, self.err = StringIO(), StringIO()
        self.attempts = 0

    def invoke(self, candidates: list[dict[str, Any]], *flags: str) -> int:
        self.out, self.err = StringIO(), StringIO()
        def opener(request: Request, timeout: float) -> BytesIO:
            self.attempts += 1
            return BytesIO(json.dumps({'status': 'success', 'data': {
                'resultType': 'streams', 'result': candidates}}).encode())
        return main(['--config', str(self.config), 'trace', 'abc', '--profile', 'test',
                     '--selector', '{}', '--start', '1970-01-01T00:00:00Z',
                     '--end', '1970-01-01T00:00:10Z', *flags],
                    environ={'TOKEN': 'secret'}, opener=opener, stdout=self.out, stderr=self.err)

    def test_every_trace_format_uses_complete_record_budget(self) -> None:
        candidates = [{'stream': {'app': '中'}, 'values': [['1', '{"trace_id":"abc","message":"中\\\\尾"}']]}]
        for scan in ([], ['--scan']):
            for output in ('human', 'raw', 'jsonl'):
                with self.subTest(scan=scan, output=output):
                    self.assertEqual(self.invoke(candidates, *scan, '--output', output), 0, self.err.getvalue())
                    expected = self.out.getvalue()
                    size = len(expected.encode())
                    self.assertEqual(self.invoke(candidates, *scan, '--output', output,
                                                 '--max-record-bytes', str(size)), 0)
                    self.assertEqual(self.out.getvalue(), expected)
                    self.assertEqual(self.invoke(candidates, *scan, '--output', output,
                                                 '--max-record-bytes', str(size - 1)), 4)
                    self.assertEqual(self.out.getvalue(), '')
                    self.assertIn('"record_too_large":1', self.err.getvalue())
                    self.assertIn('"upstream_retrieval":"complete"', self.err.getvalue())

    def test_scan_checkpoint_retains_candidates_and_final_display_evidence(self) -> None:
        work = self.root / 'work'
        report = self.root / 'report.json'
        candidates = [{'stream': {}, 'values': [['1', '{"trace_id":"abc"}'],
                                               ['2', '{"trace_id":"other"}']]}]
        self.assertEqual(self.invoke(candidates, '--scan', '--work-dir', str(work),
                                     '--report', str(report), '--max-record-bytes', '1'), 4)
        saved_report = json.loads(report.read_text())
        state = json.loads((work / 'state.json').read_text())['payload']
        self.assertEqual(saved_report['status'], 'complete')
        self.assertEqual(saved_report['counts']['matched_records'], 1)
        self.assertEqual(state['report']['counts']['valid_records'], 2)
        self.assertEqual(state['report']['evidence'], saved_report['evidence'])
        self.assertEqual(state['report']['evidence']['losses'], {'record_too_large': 1})

    def test_unmatched_candidates_across_streams_share_retained_budget(self) -> None:
        candidates = [{'stream': {'app': str(index)}, 'values': [
            [str(index + 1), json.dumps({'trace_id': 'other', 'message': 'x' * 300})]]}
            for index in range(12)]
        for scan in ([], ['--scan']):
            with self.subTest(scan=scan):
                self.assertEqual(self.invoke(candidates, *scan, '--max-retained-bytes', '1000',
                                             '--max-http-attempts', '1'), 4, self.err.getvalue())
                self.assertEqual(self.out.getvalue(), '')
                self.assertIn('retained_bytes', self.err.getvalue())
                self.assertIn('"upstream_retrieval":"incomplete"', self.err.getvalue())
                self.assertIn('"local_evidence":"incomplete"', self.err.getvalue())
                self.assertEqual(self.err.getvalue().splitlines()[-1], 'HTTP attempt accounting: used=1 cap=1')

    def test_trace_diagnostics_and_output_share_total_budget_without_sampling(self) -> None:
        candidates = [{'stream': {}, 'values': [['1', '{"trace_id":"abc","m":"first"}'],
                      ['2', '{"trace_id":"abc","m":"' + '中' * 40 + '"}'],
                      ['3', '{"trace_id":"abc","m":"last"}']]}]
        for scan in ([], ['--scan']):
            with self.subTest(scan=scan):
                self.assertEqual(self.invoke(candidates, *scan, '--output', 'raw',
                    '--max-output-bytes', '2100', '--max-http-attempts', '1'), 4)
                self.assertEqual(self.out.getvalue(), '{"trace_id":"abc","m":"first"}\n')
                self.assertLessEqual(len((self.out.getvalue() + self.err.getvalue()).encode()), 2100)
                self.assertIn('"upstream_retrieval":"complete"', self.err.getvalue())
                self.assertIn('"output_budget":1', self.err.getvalue())
                self.assertEqual(self.err.getvalue().count('HTTP attempt accounting:'), 1)

    def test_selected_json_and_label_fields_keep_exact_matching_and_order(self) -> None:
        cases: list[tuple[list[str], list[dict[str, Any]]]] = [(['--field', 'correlation', '--path', '/context/id'], [
            {'stream': {}, 'values': [['3', '{"trace_id":"abc"}'],
                ['2', '{"context":{"id":"abc"}}'], ['1', '{"correlation":"abc"}'],
                ['4', '{"correlation":"abc-tail"}']]}]),
            (['--source', 'label', '--field', 'correlation', '--field', 'request_id'], [
                {'stream': {'correlation': 'abc-tail'}, 'values': [['3', 'unmatched']]},
                {'stream': {'request_id': 'abc'}, 'values': [['2', 'second']]},
                {'stream': {'correlation': 'abc'}, 'values': [['1', 'first']]}])]
        for flags, candidates in cases:
            for scan in ([], ['--scan']):
                with self.subTest(flags=flags, scan=scan):
                    self.assertEqual(self.invoke(candidates, *flags, *scan, '--output', 'jsonl'), 0)
                    rows = [json.loads(line) for line in self.out.getvalue().splitlines()]
                    self.assertEqual([row['timestamp'] for row in rows],
                                     ['1970-01-01T00:00:00.000000001Z', '1970-01-01T00:00:00.000000002Z'])
                    self.assertIn('2 matched.', self.err.getvalue())

    def test_parse_loss_remains_incomplete_after_trace_report(self) -> None:
        candidates = [{'stream': {}, 'values': [['invalid', 'bad'], ['1', '{"trace_id":"other"}']]}]
        for scan in ([], ['--scan', '--max-http-attempts', '1']):
            with self.subTest(scan=scan):
                self.assertEqual(self.invoke(candidates, *scan), 4)
                self.assertIn('candidate retrieval is incomplete', self.err.getvalue())
                self.assertIn('"parse_loss":1', self.err.getvalue())
                self.assertIn('"local_evidence":"incomplete"', self.err.getvalue())

    def test_trace_response_cap_is_not_retried(self) -> None:
        candidates = [{'stream': {}, 'values': [['1', '{"trace_id":"abc"}']]}]
        for scan in ([], ['--scan']):
            with self.subTest(scan=scan):
                before = self.attempts
                self.assertEqual(self.invoke(candidates, *scan, '--max-response-bytes', '30'), 4)
                self.assertEqual(self.attempts - before, 1)
                self.assertIn('"response_bytes":1', self.err.getvalue())

    def test_giant_trace_fields_are_dropped_whole_and_later_match_survives(self) -> None:
        for scan in ([], ['--scan']):
            for output in ('human', 'raw', 'jsonl'):
                for field in ('message', 'exception', 'context', 'trace'):
                    with self.subTest(scan=scan, output=output, field=field):
                        large = json.dumps({'trace_id': 'abc', field: '中' * 3000})
                        candidates: list[dict[str, Any]] = [{'stream': {'app': '中' * 1000}, 'values': [['1', large]]},
                                      {'stream': {}, 'values': [['2', '{"trace_id":"abc","m":"ok"}']]}]
                        self.assertEqual(self.invoke(candidates, *scan, '--output', output,
                                                     '--max-record-bytes', '500'), 4)
                        self.assertNotIn('中', self.out.getvalue())
                        self.assertIn('ok', self.out.getvalue())
                        self.assertIn('"record_too_large":1', self.err.getvalue())
                        if output == 'jsonl':
                            self.assertEqual(len([json.loads(line) for line in self.out.getvalue().splitlines()]), 1)

    def test_invalid_trace_output_budgets_fail_before_networking(self) -> None:
        for flag, values in (('--max-record-bytes', ['0', '-1', 'inf', '8388609']),
                             ('--max-output-bytes', ['0', '-1', 'nan', '33554433'])):
            for value in values:
                with self.subTest(flag=flag, value=value):
                    self.assertEqual(self.invoke([], flag, value), 2)
        self.assertEqual(self.attempts, 0)
