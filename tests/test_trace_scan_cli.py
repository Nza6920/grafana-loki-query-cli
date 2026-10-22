from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO, StringIO
import json
import hashlib
from pathlib import Path
import tempfile
from typing import Any
import unittest
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

from loki_query.cli import main


class TraceScanCliTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / 'config.toml'
        self.config.write_text('[profiles.test]\ngrafana_url="https://grafana.example.com"\n'
                               'datasource_uid="logs"\ntoken_env="TOKEN"\n')
        self.work = self.root / 'work'
        self.requests: list[Request] = []

    def invoke(self, command: str, responses: list[Any], *flags: str,
               token: bool = True) -> tuple[int, str, str, Any]:
        out, err = StringIO(), StringIO()
        report = self.root / 'report.json'
        report.unlink(missing_ok=True)
        args = ['--config', str(self.config), command, '--output', 'jsonl', '--report', str(report)]
        if command != 'export':
            args.extend(['--profile', 'test'])
        if command == 'trace':
            args.extend(['--selector', '{app="api"}', '--start', '1970-01-01T00:00:00.000000001Z',
                         '--end', '1970-01-01T00:00:04.000000002Z', 'abc'])
        def opener(request: Request, timeout: float) -> BytesIO:
            self.requests.append(request)
            response = responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return BytesIO(json.dumps({'status': 'success', 'data': {
                'resultType': 'streams', 'result': response}}).encode())
        code = main([*args, *flags], environ={'TOKEN': 'secret'} if token else {},
                    opener=opener, stdout=out, stderr=err, now=datetime(2026, 10, 22, tzinfo=UTC))
        return code, out.getvalue(), err.getvalue(), json.loads(report.read_text()) if report.exists() else {}

    def test_trace_scan_saves_candidates_and_resumes_only_unresolved_child(self) -> None:
        line = '{"trace_id":"abc"}'
        parent = [{'stream': {'app': 'api'}, 'values': [['1', line], ['1', line],
                  ['2000000001', '{"trace_id":"other"}']]}]
        code, output, error, report = self.invoke('trace', [parent, []], '--scan', '--limit', '3',
            '--max-http-attempts', '2', '--work-dir', str(self.work))
        self.assertEqual(code, 4, error)
        self.assertEqual(len(output.splitlines()), 2)
        self.assertEqual(report['counts']['valid_records'], 2)
        self.assertEqual(report['counts']['matched_records'], 2)
        saved = json.loads((self.work / 'state.json').read_text())['payload']
        self.assertEqual(saved['identity']['trace']['id'], 'abc')
        self.assertNotIn('secret', (self.work / 'state.json').read_text())
        replacement = [{'stream': {'app': 'api', 'service': 'custom'}, 'values': [['2', line], ['2', line]]}]
        code, output, error, report = self.invoke('resume', [replacement], '--work-dir', str(self.work),
            '--service-field', 'service', '--output', 'human')
        self.assertEqual(code, 0, error)
        self.assertEqual(len(output.splitlines()), 2)
        self.assertIn('service="custom"', output)
        params = parse_qs(urlsplit(self.requests[-1].full_url).query)
        self.assertEqual((params['start'], params['end']), (['1'], ['2000000001']))
        self.assertEqual(report['usage']['current']['http_attempts'], 1)
        self.assertEqual(report['usage']['cumulative']['http_attempts'], 3)
        code, exported, error, report = self.invoke('export', [], '--work-dir', str(self.work), token=False)
        self.assertEqual(code, 0, error)
        self.assertEqual([json.loads(row)['line'] for row in exported.splitlines()], [line, line])
        self.assertEqual(report['counts']['matched_records'], 2)

    def test_every_source_reuses_scanner_for_subdivision_resume_and_offline_timeline(self) -> None:
        sources = [([], '{"trace_id":"abc"}'),
                   (['--field', 'correlation'], '{"correlation":"abc"}'),
                   (['--path', '/context/id'], '{"context":{"id":"abc"}}'),
                   (['--source', 'label', '--field', 'correlation'], 'original plain text')]
        for index, (source, line) in enumerate(sources):
            with self.subTest(source=source):
                work = str(self.root / f'matrix-{index}')
                labels = {'app': 'api', 'namespace': 'apps', 'correlation': 'abc'}
                parent = [{'stream': labels, 'values': [['1', line], ['1', line], ['2000000001', line]]}]
                child = [{'stream': labels, 'values': [['2000000001', line]]}]
                code, output, error, report = self.invoke('trace', [parent, child], *source,
                    '--scan', '--work-dir', work, '--limit', '3', '--max-http-attempts', '2')
                self.assertEqual(code, 4, error)
                self.assertEqual(len(output.splitlines()), 3)
                self.assertEqual(report['counts']['matched_records'], 3)
                self.assertEqual(report['coverage']['resolved'][0]['start_ns'], '2000000001')
                code, raw, error, saved = self.invoke('export', [], '--work-dir', work, '--output', 'raw', token=False)
                self.assertEqual(code, 4, error)
                self.assertEqual(raw, (line + '\n') * 3)
                self.assertEqual(saved['trace'], report['trace'])
                earlier = [{'stream': labels, 'values': [['2', line], ['2', line]]}]
                code, timeline, error, report = self.invoke('resume', [earlier], '--work-dir', work,
                    '--trace-id', 'abc', '--max-http-attempts', '1', '--max-records', '3')
                self.assertEqual(code, 0, error)
                rows = [json.loads(row) for row in timeline.splitlines()]
                self.assertEqual([row['timestamp'] for row in rows], [
                    '1970-01-01T00:00:00.000000002Z', '1970-01-01T00:00:00.000000002Z',
                    '1970-01-01T00:00:02.000000001Z'])
                self.assertTrue(all(row['labels'] == labels and row['line'] == line for row in rows))
                self.assertEqual(report['usage']['current']['retained_records'], 2)
                self.assertEqual(report['usage']['cumulative']['retained_records'], 3)
                self.assertEqual([json.loads(row) for row in self.invoke('export', [], '--work-dir', work, token=False)[1].splitlines()], rows)
                self.assertIn('HTTP attempt accounting: used=1 cap=1', error)

    def test_scan_defaults_and_candidate_record_budget_apply_even_with_zero_matches(self) -> None:
        code, _, error, _ = self.invoke('trace', [[]], '--scan')
        self.assertEqual(code, 0, error)
        self.assertEqual(parse_qs(urlsplit(self.requests[-1].full_url).query)['limit'], ['1000'])
        for index, source in enumerate([[], ['--field', 'id'], ['--path', '/context/id'],
                                        ['--source', 'label', '--field', 'id']]):
            with self.subTest(source=source):
                work = str(self.root / f'zero-{index}')
                candidates = [{'stream': {'id': 'other'}, 'values': [['1', '{"id":"other"}']]}]
                code, output, error, report = self.invoke('trace', [candidates], *source, '--scan',
                    '--work-dir', work, '--max-records', '1', '--max-http-attempts', '3')
                self.assertEqual(code, 4, error)
                self.assertEqual(output, '')
                self.assertEqual(report['counts']['matched_records'], 0)
                self.assertEqual(report['counts']['valid_records'], 1)
                self.assertEqual(report['usage']['current']['retained_records'], 1)
                self.assertEqual(report['stop_reasons'], ['record_budget'])
                self.assertEqual(report['status'], 'incomplete')
                code, output, error, exported = self.invoke('export', [], '--work-dir', work, token=False)
                self.assertEqual(code, 4, error)
                self.assertEqual(output, '')
                self.assertEqual(exported['status'], 'incomplete')

    def test_resume_identity_assertions_reject_changes_before_credentials_or_http(self) -> None:
        work = str(self.work)
        self.assertEqual(self.invoke('trace', [[]], '--scan', '--work-dir', work, '--max-http-attempts', '1')[0], 0)
        before = (self.work / 'state.json').read_bytes()
        requests = len(self.requests)
        for flags in [('--trace-id', 'other'), ('--source', 'label'), ('--selector', '{app="changed"}'),
                      ('--field', 'other'), ('--path', '/context/id')]:
            with self.subTest(flags=flags):
                code, output, error, _ = self.invoke('resume', [], '--work-dir', work, *flags, token=False)
                self.assertEqual(code, 2, error)
                self.assertEqual(output, '')
                self.assertEqual((self.work / 'state.json').read_bytes(), before)
        code, _, error, _ = self.invoke('resume', [], '--work-dir', work,
            '--field', 'trace_id', '--field', 'traceId', '--field', 'traceId', token=False)
        self.assertEqual(code, 0, error)
        self.assertEqual(len(self.requests), requests)

    def test_semantically_inconsistent_archive_is_rejected_by_resume_and_export(self) -> None:
        for source in ([], ['--source', 'label', '--field', 'trace_id']):
            work = self.root / ('label' if source else 'body')
            self.assertEqual(self.invoke('trace', [[]], *source, '--scan', '--work-dir', str(work))[0], 0)
            original = (work / 'state.json').read_text()
            mutations = ['missing_id', 'changed_report', 'missing_mapping', 'changed_mapping',
                         'changed_query', 'missing_identity', 'changed_counts', 'empty_mapping']
            for mutation in mutations:
                with self.subTest(source=source, mutation=mutation):
                    document = json.loads(original)
                    payload = document['payload']
                    metadata = payload['identity']['trace']
                    if mutation == 'missing_id':
                        del metadata['id']
                    elif mutation == 'changed_report':
                        payload['report']['trace']['id'] = 'other'
                    elif mutation == 'missing_mapping':
                        del metadata['mapping']
                    elif mutation == 'changed_mapping':
                        metadata['mapping'] = [['other']]
                        payload['report']['trace']['mapping'] = [['other']]
                    elif mutation == 'changed_query':
                        payload['identity']['query'] = payload['report']['query'] = '{app="different"}'
                    elif mutation == 'missing_identity':
                        del payload['identity']['trace']
                    elif mutation == 'changed_counts':
                        payload['report']['counts']['matched_records'] = 1
                    elif mutation == 'empty_mapping':
                        for selected in (metadata, payload['report']['trace']):
                            selected.update({'fields': [], 'paths': [], 'mapping': []})
                    document['sha256'] = hashlib.sha256(json.dumps(payload, ensure_ascii=False,
                        sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
                    (work / 'state.json').write_text(json.dumps(document))
                    for command in ('resume', 'export'):
                        code, output, error, _ = self.invoke(command, [], '--work-dir', str(work), token=False)
                        self.assertEqual(code, 2, error)
                        self.assertEqual(output, '')
                        self.assertIn('Unusable scan state', error)
            (work / 'state.json').write_text(original)

    def test_initial_inflight_checkpoint_preserves_trace_semantics_after_interrupt(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            self.invoke('trace', [KeyboardInterrupt()], '--scan', '--work-dir', str(self.work))
        code, output, error, report = self.invoke('export', [], '--work-dir', str(self.work), token=False)
        self.assertEqual(code, 4, error)
        self.assertEqual(output, '')
        self.assertEqual(report['trace']['id'], 'abc')
        self.assertEqual(report['usage']['current']['http_attempts'], 1)
        self.assertEqual(report['status'], 'incomplete')

    def test_trace_scan_only_options_require_explicit_opt_in(self) -> None:
        for flags in [('--work-dir', str(self.work)), ('--max-records', '1'), ('--max-elapsed-seconds', '5')]:
            self.assertEqual(self.invoke('trace', [], *flags)[0], 2)
        self.assertEqual(self.requests, [])
