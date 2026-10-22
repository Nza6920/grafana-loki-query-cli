from __future__ import annotations

from datetime import UTC, datetime
from email.message import Message
from io import BytesIO, StringIO
import json
import hashlib
from pathlib import Path
import tempfile
from typing import Any
import unittest
from urllib.parse import parse_qs, urlsplit
from urllib.error import HTTPError
from urllib.request import Request

from loki_query.cli import main


class ResumeCliTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.work = self.root / 'work'
        self.config = self.root / 'config.toml'
        self.config.write_text('[profiles.test]\ngrafana_url="https://grafana.example.com"\ndatasource_uid="logs"\ntoken_env="TOKEN"\n')
        self.requests: list[Request] = []
        self.clock = 0.0
        self.http_cost = 0.0

    def invoke(self, command: str, responses: list[Any], *flags: str, token: str = 'first-token', now: datetime | None = None, token_env: str = 'TOKEN') -> tuple[int, str, str, dict[str, Any]]:
        stdout, stderr = StringIO(), StringIO()
        report = self.root / 'report.json'
        report.unlink(missing_ok=True)
        def opener(request: Request, timeout: float) -> BytesIO:
            self.requests.append(request)
            self.clock += self.http_cost
            response = responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return BytesIO(json.dumps({'status': 'success', 'data': {'resultType': 'streams', 'result': response}}).encode())
        argv = ['--config', str(self.config), command, '--work-dir', str(self.work), '--output', 'jsonl', '--report', str(report)]
        if command != 'export':
            argv.extend(['--profile', 'test'])
        if command == 'query':
            argv.append('--scan')
            if '--start' not in flags:
                argv.extend(['--since', '30m'])
        argv.extend(flags)
        if command == 'query':
            argv.append('{app="api"}')
        def sleep(seconds: float) -> None:
            self.clock += seconds
        code = main(argv, environ={token_env: token} if token else {}, opener=opener,
                    stdout=stdout, stderr=stderr, monotonic=lambda: self.clock,
                    now=now or datetime(1970, 1, 1, 0, 30, tzinfo=UTC), sleeper=sleep)
        return code, stdout.getvalue(), stderr.getvalue(), json.loads(report.read_text()) if report.exists() else {}

    def test_second_process_resume_freezes_bounds_skips_complete_and_rotates_token(self) -> None:
        later = [{'stream': {}, 'values': [['900000000001', 'frozen']]}]
        earlier = [{'stream': {}, 'values': [['1', 'retried']]}]
        self.assertEqual(self.invoke('query', [later], '--max-http-attempts', '1')[0], 4)
        code, output, error, report = self.invoke('resume', [earlier], '--max-http-attempts', '3', token='rotated-token', now=datetime(2026, 10, 22, tzinfo=UTC))
        self.assertEqual(code, 0, error)
        self.assertEqual([json.loads(line)['line'] for line in output.splitlines()], ['frozen', 'retried'])
        self.assertEqual(len(self.requests), 2)
        params = parse_qs(urlsplit(self.requests[-1].full_url).query)
        self.assertEqual((params['start'], params['end']), (['0'], ['900000000000']))
        self.assertEqual(self.requests[-1].get_header('Authorization'), 'Bearer rotated-token')
        self.assertEqual(report['usage']['current']['http_attempts'], 1)
        self.assertEqual(report['usage']['cumulative']['http_attempts'], 2)
        self.assertEqual(report['counts']['raw_entries'], 2)
        code, again, error, report = self.invoke('resume', [], token='')
        self.assertEqual(code, 0, error)
        self.assertEqual(again, output)
        self.assertEqual(report['usage']['current']['http_attempts'], 0)
        self.assertEqual(report['usage']['cumulative']['http_attempts'], 2)
        self.assertEqual(self.invoke('export', [])[0], 0)

    def test_incompatible_cumulative_usage_is_rejected_before_credentials_or_http(self) -> None:
        self.assertEqual(self.invoke('query', [[]], '--max-http-attempts', '1')[0], 4)
        state = self.work / 'state.json'
        document = json.loads(state.read_text())
        document['payload']['report']['usage']['cumulative']['http_attempts'] = 0
        document['sha256'] = hashlib.sha256(json.dumps(document['payload'], ensure_ascii=False,
            sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        state.write_text(json.dumps(document))
        code, output, error, _ = self.invoke('resume', [], token='')
        self.assertEqual(code, 2, error)
        self.assertIn('Unusable scan state', error)
        self.assertEqual(output, '')
        self.assertEqual(len(self.requests), 1)

    def test_fresh_record_allowance_keeps_archive_larger_than_new_budget(self) -> None:
        later = [{'stream': {}, 'values': [['900000000001', 'a'], ['900000000002', 'b'], ['900000000003', 'c']]}]
        self.assertEqual(self.invoke('query', [later], '--max-http-attempts', '1')[0], 4)
        one = [{'stream': {}, 'values': [['1', 'new']]}]
        code, output, error, report = self.invoke('resume', [one], '--max-records', '1', '--max-http-attempts', '1')
        self.assertEqual(code, 4, error)
        self.assertEqual(len(output.splitlines()), 4)
        self.assertEqual(report['usage']['current']['retained_records'], 1)
        self.assertEqual(report['usage']['cumulative']['retained_records'], 4)
        self.assertEqual(parse_qs(urlsplit(self.requests[-1].full_url).query)['limit'], ['1'])
        # All archived samples become baseline in the next execution.
        code, output, error, report = self.invoke('resume', [[]], '--max-records', '1', '--max-http-attempts', '1')
        self.assertEqual(code, 4, error)
        self.assertEqual(len(output.splitlines()), 4)
        self.assertEqual(report['usage']['current']['retained_records'], 0)
        self.assertEqual(report['usage']['cumulative']['http_attempts'], 3)
        self.assertEqual(self.invoke('export', [])[0], 4)

    def test_target_and_scope_changes_are_rejected_without_touching_saved_evidence(self) -> None:
        self.assertEqual(self.invoke('query', [[]], '--max-http-attempts', '1')[0], 4)
        original = self.config.read_text()
        before = (self.work / 'state.json').read_bytes()
        for old, new in [('grafana.example.com', 'other.example.com'), ('"logs"', '"other"'),
                         ('profiles.test', 'profiles.other')]:
            with self.subTest(change=new):
                self.config.write_text(original.replace(old, new))
                code, output, error, _ = self.invoke('resume', [], token='')
                self.assertEqual(code, 2, error)
                self.assertEqual(output, '')
                self.assertEqual((self.work / 'state.json').read_bytes(), before)
        self.config.write_text(original)
        for flags in [('--since', '5m'), ('--start', '1970-01-01T00:00:01Z'),
                      ('--direction', 'forward'), ('{app="changed"}',)]:
            with self.subTest(flags=flags):
                self.assertEqual(self.invoke('resume', [], *flags, token='')[0], 2)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.invoke('export', [])[0], 4)

    def test_interruption_and_retry_checkpoints_count_current_run_only_once(self) -> None:
        later = [{'stream': {}, 'values': [['900000000001', 'frozen']]}]
        self.assertEqual(self.invoke('query', [later], '--max-http-attempts', '1')[0], 4)
        with self.assertRaises(KeyboardInterrupt):
            self.invoke('resume', [KeyboardInterrupt()])
        code, output, error, report = self.invoke('export', [])
        self.assertEqual(code, 4, error)
        self.assertEqual(report['usage']['current']['http_attempts'], 1)
        self.assertEqual(report['usage']['cumulative']['http_attempts'], 2)
        self.assertEqual(json.loads(output)['line'], 'frozen')
        failure = HTTPError('https://grafana.example.com', 503, 'temporary', Message(), None)
        earlier = [{'stream': {}, 'values': [['1', 'recovered']]}]
        code, output, error, report = self.invoke('resume', [failure, earlier])
        self.assertEqual(code, 0, error)
        self.assertEqual(report['usage']['current']['http_attempts'], 2)
        self.assertEqual(report['usage']['cumulative']['http_attempts'], 4)
        self.assertEqual(report['usage']['current']['raw_entries'], 1)
        self.assertEqual(report['counts']['raw_entries'], 2)
        self.assertEqual(len(output.splitlines()), 2)
        self.assertEqual(self.invoke('export', [])[3]['usage']['cumulative']['http_attempts'], 4)

    def test_token_environment_name_can_rotate_and_cumulative_elapsed_is_not_readded(self) -> None:
        self.http_cost = 1.0
        self.assertEqual(self.invoke('query', [[]], '--max-http-attempts', '1')[0], 4)
        self.config.write_text(self.config.read_text().replace('"TOKEN"', '"ROTATED_TOKEN"'))
        headers = Message()
        headers['Retry-After'] = '2'
        failure = HTTPError('https://grafana.example.com', 503, 'temporary', headers, None)
        code, _, error, report = self.invoke('resume', [failure, []], token='rotated-value', token_env='ROTATED_TOKEN')
        self.assertEqual(code, 0, error)
        self.assertEqual(report['usage']['current']['elapsed_seconds'], 4.0)
        self.assertEqual(report['usage']['cumulative']['elapsed_seconds'], 5.0)
        self.assertEqual(report['usage']['cumulative']['http_attempts'], 3)
        self.assertEqual(self.requests[-1].get_header('Authorization'), 'Bearer rotated-value')
        self.assertNotIn('rotated-value', (self.work / 'state.json').read_text())
        self.assertEqual(self.invoke('resume', [], token='')[3]['usage']['cumulative']['elapsed_seconds'], 5.0)

    def test_requery_replaces_saved_samples_including_empty_and_preserves_failed_evidence(self) -> None:
        initial = [{'stream': {'app': 'api'}, 'values': [['1', 'old'], ['1', 'old']]}]
        failure = HTTPError('https://grafana.example.com', 500, 'temporary', Message(), None)
        cases: list[tuple[list[Any], int, list[str]]] = [
            ([[]], 0, []),
            ([[{'stream': {}, 'values': [['2', 'new'], ['2', 'new']]}]], 0, ['new', 'new']),
            ([[{'stream': {}, 'values': [['2', 'new-valid'], ['invalid']]}]], 4, ['new-valid']),
            ([failure, failure, failure], 4, ['old', 'old']),
        ]
        for index, (responses, expected_code, expected_lines) in enumerate(cases):
            with self.subTest(index=index):
                self.work = self.root / f'replacement-{index}'
                code, _, error, _ = self.invoke('query', [initial], '--since', '1s', '--limit', '2',
                    now=datetime(1970, 1, 1, 0, 0, 1, tzinfo=UTC))
                self.assertEqual(code, 4, error)
                code, output, error, report = self.invoke('resume', responses)
                self.assertEqual(code, expected_code, error)
                self.assertEqual([json.loads(line)['line'] for line in output.splitlines()], expected_lines)
                code, exported, error, exported_report = self.invoke('export', [])
                self.assertEqual(code, expected_code, error)
                self.assertEqual(exported, output)
                self.assertEqual(report['usage']['current']['raw_entries'], 0 if index in (0, 3) else 2)
                if index == 2:
                    self.assertEqual(report['status'], 'incomplete')
                    self.assertEqual(report['usage']['current']['retained_records'], 1)
                    self.assertEqual(exported_report['status'], 'incomplete')
                    self.assertEqual(exported_report['counts']['valid_records'], 1)

    def test_resume_queries_only_unresolved_odd_nanosecond_child(self) -> None:
        parent = [{'stream': {}, 'values': [['1', 'old'], ['1', 'old'], ['2000000001', 'boundary']]}]
        code, _, error, _ = self.invoke('query', [parent, []], '--start', '1970-01-01T00:00:00.000000001Z', '--end',
            '1970-01-01T00:00:04.000000002Z', '--limit', '3', '--max-http-attempts', '2')
        self.assertEqual(code, 4, error)
        replacement = [{'stream': {}, 'values': [['1', 'new'], ['1', 'new']]}]
        code, output, error, report = self.invoke('resume', [replacement])
        self.assertEqual(code, 0, error)
        self.assertEqual([json.loads(line)['line'] for line in output.splitlines()], ['new', 'new'])
        params = parse_qs(urlsplit(self.requests[-1].full_url).query)
        self.assertEqual((params['start'], params['end']), (['1'], ['2000000001']))
        self.assertEqual(report['usage']['current']['http_attempts'], 1)
        self.assertEqual(report['usage']['cumulative']['http_attempts'], 3)
        self.assertEqual(self.invoke('export', [])[1], output)

    def test_repeated_partial_resumes_replace_without_appending_and_keep_duplicates(self) -> None:
        initial = [{'stream': {}, 'values': [['1', 'old'], ['1', 'old']]}]
        self.assertEqual(self.invoke('query', [initial], '--since', '1s', '--limit', '2',
            now=datetime(1970, 1, 1, 0, 0, 1, tzinfo=UTC))[0], 4)
        for values, expected in [
            ([['2', 'new-valid'], ['invalid']], ['new-valid']),
            ([['3', 'repeated'], ['3', 'repeated'], ['invalid']], ['repeated', 'repeated']),
            ([['invalid']], []),
        ]:
            with self.subTest(expected=expected):
                code, output, error, report = self.invoke('resume', [[{'stream': {}, 'values': values}]])
                self.assertEqual(code, 4, error)
                self.assertEqual([json.loads(line)['line'] for line in output.splitlines()], expected)
                self.assertEqual(report['usage']['current']['retained_records'], len(expected))
                self.assertEqual(report['usage']['cumulative']['retained_records'], len(expected))
                code, exported, error, exported_report = self.invoke('export', [])
                self.assertEqual(code, 4, error)
                self.assertEqual(exported, output)
                self.assertEqual(exported_report['status'], 'incomplete')
