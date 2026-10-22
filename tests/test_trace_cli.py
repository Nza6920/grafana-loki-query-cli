from __future__ import annotations

from datetime import UTC, datetime
from email.message import Message
from urllib.error import HTTPError
from io import BytesIO, StringIO
import json
from pathlib import Path
import tempfile
from typing import Any
import unittest
from urllib.parse import parse_qs, urlparse
from urllib.request import Request

from loki_query.cli import main


class TraceCliTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = Path(directory.name) / 'config.toml'
        self.config.write_text('[profiles.test]\ngrafana_url="https://grafana.example.com"\n'
                               'datasource_uid="logs"\ntoken_env="TEST_TOKEN"\n')
        self.report = Path(directory.name) / 'report.json'
        self.stdout, self.stderr = StringIO(), StringIO()
        self.requests: list[Request] = []

    def invoke(self, result: object, *flags: str, trace_id: str = 'abc',
               selector: str = '{namespace="prod",app=~"api|worker"}') -> int:
        def opener(request: Request, timeout: float) -> BytesIO:
            self.requests.append(request)
            return BytesIO(json.dumps({'status': 'success', 'data': {
                'resultType': 'streams', 'result': result}}).encode())
        return main(['--config', str(self.config), 'trace', '--profile', 'test',
                     '--selector', selector, '--since', '15m', '--report', str(self.report),
                     '--output', 'jsonl', *flags, trace_id],
                    environ={'TEST_TOKEN': 'secret-token'}, opener=opener,
                    stdout=self.stdout, stderr=self.stderr,
                    now=datetime(2026, 8, 11, tzinfo=UTC))

    def read_report(self) -> Any:
        return json.loads(self.report.read_text())

    def test_default_fields_match_only_exact_json_strings_and_preserve_evidence(self) -> None:
        lines = ['{"trace_id":"abc","message":"ok"}', '{"traceId":"abc"}',
                 '{"trace_id":"abcd"}', '{"trace_id":"ABC"}', '{"other":"abc"}',
                 '{"trace_id":123}', 'abc', '{"nested":{"trace_id":"abc"}}']
        labels = {'app': 'api', 'namespace': 'prod', 'trace_id': 'abc',
                  'trace_id_extracted': 'abc'}
        self.assertEqual(self.invoke([{'stream': labels, 'values': [
            [str(i + 1), line] for i, line in enumerate(lines)]}]), 0, self.stderr.getvalue())
        rows = [json.loads(line) for line in self.stdout.getvalue().splitlines()]
        self.assertEqual([row['line'] for row in rows], lines[:2])
        self.assertEqual(rows[0], {'type': 'log_entry', 'timestamp': '1970-01-01T00:00:00.000000001Z',
                                  'labels': labels, 'line': lines[0]})
        params = parse_qs(urlparse(self.requests[0].full_url).query)
        self.assertEqual(params['query'], ['{namespace="prod",app=~"api|worker"}'])
        self.assertEqual(params['limit'], ['100'])
        self.assertEqual(self.read_report()['counts']['raw_entries'], 8)
        self.assertEqual(self.read_report()['counts']['matched_records'], 2)
        self.assertEqual(self.read_report()['counts']['filtered_candidates'], 6)
        self.assertEqual(self.read_report()['status'], 'complete')

    def test_human_timeline_uses_app_namespace_and_stable_nanosecond_order(self) -> None:
        line = '{"trace_id":"abc"}'
        self.assertEqual(self.invoke([
            {'stream': {'namespace': 'z', 'app': 'worker', 'service': 'wrong'}, 'values': [['2', line]]},
            {'stream': {'namespace': 'a', 'app': 'api'}, 'values': [['1', line], ['1', line]]},
            {'stream': {'namespace': 'b'}, 'values': [['1', line]]},
        ], '--output', 'human'), 0)
        rows = self.stdout.getvalue().splitlines()
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0], rows[1])
        self.assertIn('1970-01-01T00:00:00.000000001Z service="api" namespace="a"', rows[0])
        self.assertIn('service="unknown" namespace="b"', rows[2])
        self.assertIn('1970-01-01T00:00:00.000000002Z service="worker" namespace="z"', rows[3])
        self.assertTrue(all(row.endswith(line) for row in rows))
        self.assertIn('equal timestamps do not imply causality', self.stderr.getvalue())

    def test_custom_service_field_does_not_change_trace_predicate(self) -> None:
        self.assertEqual(self.invoke([{'stream': {'service_name': 'custom'}, 'values': [
            ['1', '{"trace_id":"abc"}']]}], '--output', 'human', '--service-field', 'service_name'), 0)
        self.assertIn('service="custom" namespace="unknown"', self.stdout.getvalue())

    def test_zero_matches_remain_incomplete_when_candidates_saturate(self) -> None:
        self.assertEqual(self.invoke([{'stream': {}, 'values': [['1', '{"trace_id":"other"}']]}],
                                     '--limit', '1'), 0)
        self.assertEqual(self.stdout.getvalue(), '')
        self.assertEqual(self.read_report()['status'], 'incomplete')
        self.assertEqual(self.read_report()['counts']['matched_records'], 0)
        self.assertIn('possible truncation', self.stderr.getvalue())
        self.assertIn('No trace matches under the selected conditions; candidate retrieval is incomplete', self.stderr.getvalue())

    def test_empty_complete_and_parse_loss_are_distinct(self) -> None:
        self.assertEqual(self.invoke([]), 0)
        self.assertIn('No trace matches under the selected conditions; candidate retrieval is complete', self.stderr.getvalue())
        self.stderr.seek(0)
        self.stderr.truncate()
        self.assertEqual(self.invoke([{'stream': {}, 'values': [
            ['bad', '{"trace_id":"abc"}'], ['1', '{"trace_id":"abc"}'], ['2', 'not JSON']]}]), 4)
        report = self.read_report()
        self.assertEqual(report['counts']['matched_records'], 1)
        self.assertEqual(report['counts']['filtered_candidates'], 1)
        self.assertEqual(report['counts']['skipped_entries'], 1)
        self.assertEqual(report['coverage']['unresolved'][0]['reasons'], ['parse_loss'])

    def test_unsafe_scope_and_empty_identity_are_rejected_before_network(self) -> None:
        for selector in ('', '{} | json', '{} or {}', '{app="api"', '{app="api"}}'):
            with self.subTest(selector=selector):
                self.assertEqual(self.invoke([], selector=selector), 2)
        self.assertEqual(self.invoke([], trace_id=''), 2)
        self.assertEqual(self.invoke([], '--service-field', ''), 2)
        self.assertEqual(self.requests, [])

    def test_numeric_values_and_ambiguous_or_invalid_json_never_confirm_matches(self) -> None:
        lines = ['{"trace_id":123}', '{"traceId":true}', '{"trace_id":null}',
                 '{"trace_id":"123","extra":NaN}', '{"trace_id":"wrong","trace_id":"123"}',
                 '{"trace_id":"123"}']
        self.assertEqual(self.invoke([{'stream': {}, 'values': [
            [str(i), line] for i, line in enumerate(lines)]}], trace_id='123'), 0)
        self.assertEqual([json.loads(row)['line'] for row in self.stdout.getvalue().splitlines()], [lines[-1]])
        self.assertEqual(self.read_report()['counts']['skipped_entries'], 0)

    def test_escaped_id_never_enters_logql_and_raw_output_keeps_original_spelling(self) -> None:
        trace_id = 'a"\\\n} |= "injected"'
        line = json.dumps({'traceId': trace_id}, ensure_ascii=True, indent=None)
        selector = '{namespace="prod",app=~"a}b|api",note=`quoted"value`}'
        self.assertEqual(self.invoke([{'stream': {'app': 'api'}, 'values': [['1', line]]}],
                                     '--output', 'raw', trace_id=trace_id, selector=selector), 0)
        self.assertEqual(self.stdout.getvalue(), line + '\n')
        self.assertEqual(parse_qs(urlparse(self.requests[0].full_url).query)['query'], [selector])
        self.assertEqual(self.read_report()['trace']['id'], trace_id)

    def test_escaped_json_keys_and_values_match_without_rewriting_evidence(self) -> None:
        line = r'{"trace\u005fid":"\u0061bc"}'
        self.assertEqual(self.invoke([{'stream': {'app': 'api'}, 'values': [['1', line]]}],
                                     '--output', 'raw'), 0)
        self.assertEqual(self.stdout.getvalue(), line + '\n')

    def test_profile_selector_and_explicit_range_are_required(self) -> None:
        for flags in ([], ['--profile', 'test'], ['--profile', 'test', '--selector', '{}'],
                      ['--selector', '{}', '--since', '15m']):
            with self.subTest(flags=flags):
                self.assertEqual(main(['--config', str(self.config), 'trace', *flags, 'abc'],
                                      environ={}, stdout=self.stdout, stderr=self.stderr), 2)

    def test_auth_failure_has_trace_identity_and_actual_attempt_report(self) -> None:
        def opener(request: Request, timeout: float) -> BytesIO:
            self.requests.append(request)
            raise HTTPError(request.full_url, 403, 'secret-token', Message(), None)
        code = main(['--config', str(self.config), 'trace', '--profile', 'test',
                     '--selector', '{app="api"}', '--since', '15m', '--report', str(self.report), 'abc'],
                    environ={'TEST_TOKEN': 'secret-token'}, opener=opener,
                    stdout=self.stdout, stderr=self.stderr)
        self.assertEqual(code, 3)
        self.assertEqual(self.stdout.getvalue(), '')
        report = self.read_report()
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['trace']['id'], 'abc')
        self.assertEqual(report['usage']['current']['http_attempts'], 1)
        self.assertEqual(report['coverage']['unresolved'][0]['reasons'], ['authentication_failed'])
        self.assertNotIn('secret-token', self.report.read_text() + self.stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
