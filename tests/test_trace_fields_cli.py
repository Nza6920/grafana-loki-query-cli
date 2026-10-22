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


class TraceFieldsCliTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = Path(directory.name) / 'config.toml'
        self.config.write_text('[profiles.test]\ngrafana_url="https://grafana.example.com"\n'
                               'datasource_uid="logs"\ntoken_env="TEST_TOKEN"\n')
        self.report = Path(directory.name) / 'report.json'
        self.stdout, self.stderr = StringIO(), StringIO()
        self.requests: list[Request] = []

    def invoke(self, lines: list[str], *flags: str, trace_id: str = 'abc',
               labels: dict[str, str] | None = None) -> int:
        def opener(request: Request, timeout: float) -> BytesIO:
            self.requests.append(request)
            return BytesIO(json.dumps({'status': 'success', 'data': {
                'resultType': 'streams', 'result': [{'stream': labels or {}, 'values': [
                    [str(index + 1), line] for index, line in enumerate(lines)]}]}}).encode())
        return main(['--config', str(self.config), 'trace', '--profile', 'test',
                     '--selector', '{namespace="prod",app="api"}', '--since', '15m',
                     '--report', str(self.report), '--output', 'jsonl', *flags, trace_id],
                    environ={'TEST_TOKEN': 'secret-token'}, opener=opener,
                    stdout=self.stdout, stderr=self.stderr,
                    now=datetime(2026, 8, 11, tzinfo=UTC))

    def rows(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.stdout.getvalue().splitlines()]

    def test_custom_literal_fields_replace_defaults_and_match_any_selected_field(self) -> None:
        lines = ['{"trace_id":"abc"}', '{"correlation":"abc"}', '{"request.id":"abc"}',
                 '{"request":{"id":"abc"}}', '{"correlation":"ABC"}',
                 '{"correlation":"abcd"}', '{"correlation":123}', '{"other":"abc"}']
        self.assertEqual(self.invoke(lines, '--field', 'correlation', '--field', 'request.id'), 0,
                         self.stderr.getvalue())
        self.assertEqual([row['line'] for row in self.rows()], lines[1:3])
        report = json.loads(self.report.read_text())
        self.assertEqual(report['trace']['fields'], ['correlation', 'request.id'])
        self.assertEqual(report['trace']['mapping'], [['correlation'], ['request.id']])
        self.assertEqual(report['counts']['matched_records'], 2)
        self.assertEqual(report['counts']['filtered_candidates'], 6)

    def test_nested_json_pointers_and_literal_punctuation_are_distinct(self) -> None:
        lines = ['{"trace_id":"abc"}', '{"trace":{"id":"abc"}}',
                 '{"trace.id":"abc"}', '{"trace":{"id":"wrong"},"trace.id":"abc"}',
                 '{"a/b":{"~key":"abc"}}', '{"[literal]":"abc"}',
                 '{"trace":null}', '{"trace":[{"id":"abc"}]}',
                 '{"trace":{"id":123}}', '{"trace":{"id":true}}']
        self.assertEqual(self.invoke(lines, '--path', '/trace/id', '--path', '/a~1b/~0key',
                                     '--field', '[literal]'), 0, self.stderr.getvalue())
        self.assertEqual([row['line'] for row in self.rows()], [lines[1], lines[4], lines[5]])
        report = json.loads(self.report.read_text())
        self.assertEqual(report['trace']['fields'], ['[literal]'])
        self.assertEqual(report['trace']['paths'], ['/a~1b/~0key', '/trace/id'])
        self.assertEqual(report['trace']['mapping'], [['[literal]'], ['a/b', '~key'], ['trace', 'id']])

    def test_unsupported_references_are_rejected_before_network(self) -> None:
        for flags in (('--field', ''), ('--field', 'bad\nkey'), ('--field', 'bad\x00key'),
                      ('--path', ''), ('--path', 'trace.id'), ('--path', '$.trace.id'),
                      ('--path', '/trace/~'), ('--path', '/trace/~2id'),
                      ('--path', '/trace//id'), ('--path', '/'),
                      ('--path', '/trace/\x7fid')):
            with self.subTest(flags=flags):
                self.assertEqual(self.invoke([], *flags), 2)
        self.assertEqual(self.requests, [])

    def test_punctuation_and_escaped_ids_remain_data_and_preserve_all_evidence(self) -> None:
        field = 'request.id[0]"} | json | trace_id="injected'
        trace_id = 'id"\\line\n} |= "injected"'
        matching = json.dumps({field: trace_id})
        labels = {'namespace': 'prod', 'app': 'api', field: 'wrong',
                  'trace_id': trace_id, 'trace_id_extracted': trace_id}
        lines = [matching, json.dumps({'trace_id': trace_id}),
                 json.dumps({field: 123}), matching]
        self.assertEqual(self.invoke(lines, '--field', field, trace_id=trace_id, labels=labels), 0)
        self.assertEqual([row['line'] for row in self.rows()], [matching, matching])
        self.assertTrue(all(row['labels'] == labels for row in self.rows()))
        self.assertEqual([row['timestamp'] for row in self.rows()], [
            '1970-01-01T00:00:00.000000001Z', '1970-01-01T00:00:00.000000004Z'])
        self.assertEqual(parse_qs(urlparse(self.requests[0].full_url).query)['query'],
                         ['{namespace="prod",app="api"}'])

    def test_path_only_never_falls_back_and_pointer_escapes_decode_once(self) -> None:
        lines = ['{"trace_id":"abc"}', '{"trace":{"id":null}}',
                 '{"trace":{"id":false}}', '{"trace":{"id":["abc"]}}',
                 '{"trace":{"id":{"value":"abc"}}}', 'abc',
                 '{"trace":{"id":"wrong","id":"abc"}}',
                 '{"~1":{"id":"abc"}}', '{"/":{"id":"abc"}}']
        self.assertEqual(self.invoke(lines, '--path', '/~01/id', labels={'trace_id': 'abc'}), 0)
        self.assertEqual([row['line'] for row in self.rows()], [lines[7]])
        report = json.loads(self.report.read_text())
        self.assertEqual(report['trace']['fields'], [])
        self.assertEqual(report['trace']['mapping'], [['~1', 'id']])
        self.assertEqual(report['counts']['skipped_entries'], 0)

    def test_mappings_are_canonical_and_human_output_retains_original_line(self) -> None:
        line = '{"trace":{"id":"abc"},"other":"abc"}'
        self.assertEqual(self.invoke([line], '--path', '/trace/id', '--field', 'other',
                                     '--path', '/trace/id', '--path', '/other', '--output', 'human',
                                     labels={'app': 'worker', 'namespace': 'prod'}), 0)
        self.assertTrue(self.stdout.getvalue().endswith(line + '\n'))
        self.assertIn('service="worker" namespace="prod"', self.stdout.getvalue())
        report = json.loads(self.report.read_text())
        self.assertEqual(report['trace']['mapping'], [['other'], ['trace', 'id']])
        self.assertEqual(report['counts']['matched_records'], 1)


if __name__ == '__main__':
    unittest.main()
