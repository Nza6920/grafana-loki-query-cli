from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO, StringIO
import json
from pathlib import Path
import tempfile
from typing import Any
import unittest
from urllib.parse import parse_qs, urlparse
from urllib.request import Request

from loki_query.cli import main


class TraceLabelsCliTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = Path(directory.name) / 'config.toml'
        self.config.write_text('[profiles.test]\ngrafana_url="https://grafana.example.com"\n'
                               'datasource_uid="logs"\ntoken_env="TEST_TOKEN"\n')
        self.report = Path(directory.name) / 'report.json'
        self.stdout, self.stderr = StringIO(), StringIO()
        self.requests: list[Request] = []

    def invoke(self, result: object, *flags: str, trace_id: str = 'abc') -> int:
        def opener(request: Request, timeout: float) -> BytesIO:
            self.requests.append(request)
            return BytesIO(json.dumps({'status': 'success', 'data': {
                'resultType': 'streams', 'result': result}}).encode())
        return main(['--config', str(self.config), 'trace', '--profile', 'test',
                     '--selector', '{namespace="prod",app=~"api|worker"}',
                     '--since', '15m', '--report', str(self.report),
                     '--output', 'jsonl', *flags, trace_id],
                    environ={'TEST_TOKEN': 'secret-token'}, opener=opener,
                    stdout=self.stdout, stderr=self.stderr,
                    now=datetime(2026, 8, 11, tzinfo=UTC))

    def read_report(self) -> Any:
        return json.loads(self.report.read_text())

    def test_label_only_ids_match_without_body_and_preserve_original_evidence(self) -> None:
        labels = {'app': 'api', 'namespace': 'prod', 'correlation_id': 'abc',
                  'trace_id': 'different', 'trace_id_extracted': 'different'}
        self.assertEqual(self.invoke([
            {'stream': labels, 'values': [['2', 'plain text'], ['1', '{"trace_id":"other"}']]},
            {'stream': {'correlation_id': 'ABC'}, 'values': [['3', '{"trace_id":"abc"}']]},
            {'stream': {'correlation_id': 'abcd'}, 'values': [['4', 'abc']]},
            {'stream': {'trace_id': 'abc'}, 'values': [['5', '{"trace_id":"abc"}']]},
        ], '--source', 'label', '--field', 'correlation_id'), 0, self.stderr.getvalue())
        rows = [json.loads(line) for line in self.stdout.getvalue().splitlines()]
        self.assertEqual([row['line'] for row in rows], ['{"trace_id":"other"}', 'plain text'])
        self.assertEqual(rows[0]['labels'], labels)
        self.assertEqual(rows[0]['timestamp'], '1970-01-01T00:00:00.000000001Z')
        self.assertEqual(set(rows[0]), {'type', 'timestamp', 'labels', 'line'})
        params = parse_qs(urlparse(self.requests[0].full_url).query)
        self.assertEqual(params['query'], ['{namespace="prod",app=~"api|worker"} | (correlation_id="abc")'])
        self.assertEqual(self.read_report()['trace']['source'], 'label')
        self.assertEqual(self.read_report()['trace']['fields'], ['correlation_id'])
        self.assertEqual(self.read_report()['counts']['matched_records'], 2)
        self.assertEqual(self.read_report()['counts']['raw_entries'], 5)

    def test_label_options_are_explicit_and_reject_incompatible_or_unsafe_fields(self) -> None:
        invalid = [(), ('--field', ''), ('--field', 'trace.id'),
                   ('--field', 'trace-id'), ('--field', '123trace'),
                   ('--field', 'trace_id) or app!=""'), ('--field', 'třace'),
                   ('--field', 'trace_id', '--path', '/trace/id')]
        for flags in invalid:
            with self.subTest(flags=flags):
                self.assertEqual(self.invoke([], '--source', 'label', *flags), 2)
        self.assertEqual(self.invoke([], '--source', 'json', '--source', 'label', '--field', 'trace_id'), 2)
        self.assertEqual(self.requests, [])

    def test_any_selected_label_matches_and_mapping_is_canonical(self) -> None:
        self.assertEqual(self.invoke([
            {'stream': {'traceId': 'abc'}, 'values': [['1', 'first']]},
            {'stream': {'trace_id': 'abc'}, 'values': [['2', 'second']]},
            {'stream': {'other': 'abc'}, 'values': [['3', 'third']]},
        ], '--source', 'label', '--field', 'trace_id', '--field', 'traceId',
            '--field', 'trace_id'), 0, self.stderr.getvalue())
        self.assertEqual([json.loads(line)['line'] for line in self.stdout.getvalue().splitlines()],
                         ['first', 'second'])
        self.assertEqual(self.read_report()['trace']['mapping'], [['traceId'], ['trace_id']])
        query = parse_qs(urlparse(self.requests[0].full_url).query)['query'][0]
        self.assertEqual(query, '{namespace="prod",app=~"api|worker"} | (traceId="abc" or trace_id="abc")')

    def test_label_id_escaping_cannot_alter_selector_and_report_binds_selected_conditions(self) -> None:
        trace_id = 'a"\\\n} | app!="" ☃ 😀'
        labels = {'app': 'api', 'namespace': 'prod', 'trace_id': trace_id}
        self.assertEqual(self.invoke([{'stream': labels, 'values': [['1', 'unchanged body']]}],
                                     '--source', 'label', '--field', 'trace_id', '--output', 'raw',
                                     trace_id=trace_id), 0, self.stderr.getvalue())
        query = parse_qs(urlparse(self.requests[0].full_url).query)['query'][0]
        self.assertEqual(query,
                         '{namespace="prod",app=~"api|worker"} | (trace_id="a\\"\\\\\\n} | app!=\\"\\" ☃ 😀")')
        self.assertEqual(self.stdout.getvalue(), 'unchanged body\n')
        report = self.read_report()
        self.assertEqual(report['trace']['id'], trace_id)
        self.assertEqual(report['trace']['selector'], '{namespace="prod",app=~"api|worker"}')

    def test_label_no_match_is_scoped_to_candidate_completeness(self) -> None:
        self.assertEqual(self.invoke([], '--source', 'label', '--field', 'trace_id'), 0)
        self.assertIn('candidate retrieval is complete', self.stderr.getvalue())
        self.stderr.seek(0)
        self.stderr.truncate()
        self.assertEqual(self.invoke([{'stream': {}, 'values': [['1', '{"trace_id":"abc"}']]}],
                                     '--source', 'label', '--field', 'trace_id', '--limit', '1'), 0)
        report = self.read_report()
        self.assertEqual(report['status'], 'incomplete')
        self.assertEqual(report['counts']['matched_records'], 0)
        self.assertEqual(report['counts']['raw_entries'], 1)
        self.assertEqual(report['coverage']['unresolved'][0]['reasons'], ['possible_truncation'])
        self.assertEqual(self.stdout.getvalue(), '')
        self.assertIn('candidate retrieval is incomplete', self.stderr.getvalue())

    def test_label_timeline_preserves_duplicates_service_settings_and_parse_loss(self) -> None:
        self.assertEqual(self.invoke([
            {'stream': {'trace_id': 'abc', 'service_name': 'worker', 'namespace': 'prod'},
             'values': [['2', 'later'], ['bad', 'malformed']]},
            {'stream': {'trace_id': 'abc', 'namespace': 'prod'},
             'values': [['1', 'earlier'], ['1', 'earlier']]},
        ], '--source', 'label', '--field', 'trace_id', '--service-field', 'service_name',
            '--output', 'human'), 4)
        rows = self.stdout.getvalue().splitlines()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0], rows[1])
        self.assertIn('1970-01-01T00:00:00.000000001Z service="unknown" namespace="prod"', rows[0])
        self.assertIn('1970-01-01T00:00:00.000000002Z service="worker" namespace="prod"', rows[2])
        self.assertTrue(rows[0].endswith('earlier'))
        self.assertTrue(rows[2].endswith('later'))
        report = self.read_report()
        self.assertEqual(report['counts']['matched_records'], 3)
        self.assertEqual(report['counts']['raw_entries'], 4)
        self.assertEqual(report['counts']['skipped_entries'], 1)
        self.assertEqual(report['coverage']['unresolved'][0]['reasons'], ['parse_loss'])


if __name__ == '__main__':
    unittest.main()
