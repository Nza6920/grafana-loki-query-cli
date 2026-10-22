from __future__ import annotations

from datetime import UTC, datetime
from email.message import Message
from io import BytesIO, StringIO
import json
from pathlib import Path
import tempfile
from typing import Any
import unittest
from urllib.error import HTTPError
from urllib.request import Request

from loki_query.cli import main


class HttpAllowanceCliTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = Path(directory.name) / 'config.toml'
        self.config.write_text('[profiles.test]\ngrafana_url="https://grafana.example.com"\n'
                               'datasource_uid="logs"\ntoken_env="TEST_TOKEN"\n')
        self.report = Path(directory.name) / 'report.json'
        self.stdout, self.stderr = StringIO(), StringIO()
        self.attempts = 0
        self.waits: list[float] = []

    def invoke(self, *flags: str, opener: Any = None) -> int:
        def unavailable(request: Request, timeout: float) -> BytesIO:
            self.attempts += 1
            raise HTTPError(request.full_url, 503, 'secret-token', Message(), None)
        return main(['--config', str(self.config), *flags],
                    environ={'TEST_TOKEN': 'secret-token'}, stdout=self.stdout,
                    stderr=self.stderr, opener=opener or unavailable,
                    sleeper=self.waits.append, now=datetime(2026, 8, 11, tzinfo=UTC))

    def test_ordinary_log_cap_stops_retry_before_wait_and_reports_budget_gap(self) -> None:
        code = self.invoke('query', '--profile', 'test', '--max-http-attempts', '1',
                           '--report', str(self.report), '--output', 'jsonl', '{}')
        self.assertEqual(code, 4, self.stderr.getvalue())
        self.assertEqual(self.attempts, 1)
        self.assertEqual(self.waits, [])
        self.assertEqual(self.stdout.getvalue(), '')
        self.assertEqual(self.stderr.getvalue().splitlines()[-1], 'HTTP attempt accounting: used=1 cap=1')
        report = json.loads(self.report.read_text())
        self.assertEqual(report['budgets']['max_http_attempts'], 1)
        self.assertEqual(report['usage']['current']['http_attempts'], 1)
        self.assertEqual(report['stop_reasons'], ['http_attempt_budget'])
        self.assertEqual(report['coverage']['unresolved'][0]['reasons'], ['http_attempt_budget'])

    def command(self, kind: str, cap: int | None = None) -> list[str]:
        flags = (['trace', '--profile', 'test', '--selector', '{}', '--since', '15m']
                 if kind == 'trace' else ['query', '--profile', 'test'])
        if kind == 'metric':
            flags += ['--query-type', 'metric']
        if cap is not None:
            flags += ['--max-http-attempts', str(cap)]
        return [*flags, '--output', 'jsonl', 'abc' if kind == 'trace' else '{}']

    def response(self, kind: str) -> BytesIO:
        result = ([{'metric': {'app': 'api'}, 'values': [[1, '2']]}] if kind == 'metric'
                  else [{'stream': {'app': 'api'}, 'values': [['1', '{"trace_id":"abc"}']]}])
        return BytesIO(json.dumps({'status': 'success', 'data': {
            'resultType': 'matrix' if kind == 'metric' else 'streams', 'result': result}}).encode())

    def test_all_network_commands_accept_remaining_allowance_and_account_retry_success(self) -> None:
        for kind in ('log', 'metric', 'trace'):
            with self.subTest(kind=kind):
                self.stdout = StringIO()
                self.stderr = StringIO()
                self.attempts = 0
                self.waits = []
                def opener(request: Request, timeout: float) -> BytesIO:
                    self.attempts += 1
                    if self.attempts == 1:
                        headers = Message()
                        headers['Retry-After'] = '2'
                        raise HTTPError(request.full_url, 429, 'secret-token', headers, None)
                    return self.response(kind)
                self.assertEqual(self.invoke(*self.command(kind, 2), opener=opener), 0, self.stderr.getvalue())
                self.assertEqual(self.attempts, 2)
                self.assertEqual(self.waits, [2.0])
                self.assertEqual(self.stderr.getvalue().count('HTTP attempt accounting:'), 1)
                self.assertEqual(self.stderr.getvalue().splitlines()[-1], 'HTTP attempt accounting: used=2 cap=2')
                rows = [json.loads(line) for line in self.stdout.getvalue().splitlines()]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['type'], 'metric_sample' if kind == 'metric' else 'log_entry')
                self.assertNotIn('secret-token', self.stdout.getvalue() + self.stderr.getvalue())

    def test_large_caps_retain_three_attempt_retry_ceiling_and_defaults_add_no_marker(self) -> None:
        for kind in ('log', 'metric', 'trace'):
            for cap in (None, 60):
                with self.subTest(kind=kind, cap=cap):
                    self.attempts = 0
                    self.waits = []
                    self.stderr = StringIO()
                    self.assertEqual(self.invoke(*self.command(kind, cap)), 4)
                    self.assertEqual(self.attempts, 3)
                    self.assertEqual(self.waits, [0.5, 1.0])
                    self.assertEqual('HTTP attempt accounting:' in self.stderr.getvalue(), cap is not None)

    def test_cap_exhaustion_auth_invalid_query_and_parse_failures_count_failed_attempts(self) -> None:
        for kind in ('log', 'metric', 'trace'):
            for status, code in ((503, 4), (403, 3), (400, 4), (200, 4)):
                with self.subTest(kind=kind, status=status):
                    self.attempts = 0
                    self.waits = []
                    self.stdout, self.stderr = StringIO(), StringIO()
                    def opener(request: Request, timeout: float) -> BytesIO:
                        self.attempts += 1
                        if status != 200:
                            raise HTTPError(request.full_url, status, 'secret-token', Message(), None)
                        return BytesIO(b'not json')
                    self.assertEqual(self.invoke(*self.command(kind, 1), opener=opener), code, self.stderr.getvalue())
                    self.assertEqual(self.attempts, 1)
                    self.assertEqual(self.waits, [])
                    self.assertEqual(self.stdout.getvalue(), '')
                    self.assertEqual(self.stderr.getvalue().splitlines()[-1], 'HTTP attempt accounting: used=1 cap=1')

    def test_trace_budget_report_carries_identity_and_effective_cap(self) -> None:
        self.assertEqual(self.invoke('trace', '--profile', 'test', '--selector', '{}', '--since', '15m',
                                     '--max-http-attempts', '1', '--report', str(self.report), 'abc'), 4)
        report = json.loads(self.report.read_text())
        self.assertEqual(report['trace']['id'], 'abc')
        self.assertEqual(report['budgets']['max_http_attempts'], 1)
        self.assertEqual(report['usage']['current']['http_attempts'], 1)
        self.assertEqual(report['stop_reasons'], ['http_attempt_budget'])

    def test_pre_network_errors_are_zero_and_metric_completeness_report_stays_rejected(self) -> None:
        for flags in (['--profile', 'missing'], ['--since', 'invalid'], ['--report', str(self.report)]):
            with self.subTest(flags=flags):
                self.stderr = StringIO()
                self.assertEqual(self.invoke('query', '--profile', 'test', '--query-type', 'metric',
                                             '--max-http-attempts', '9', *flags, '{}'), 2)
                self.assertEqual(self.attempts, 0)
                self.assertEqual(self.stderr.getvalue().splitlines()[-1], 'HTTP attempt accounting: used=0 cap=9')
        self.assertFalse(self.report.exists())
        for cap in ('0', '-1'):
            self.assertEqual(self.invoke('query', '--profile', 'test', '--max-http-attempts', cap, '{}'), 2)
            self.assertEqual(self.attempts, 0)

    def test_interruptions_unexpected_io_and_report_errors_keep_final_accounting(self) -> None:
        class InterruptedRead(BytesIO):
            def read(self, size: int | None = -1) -> bytes:
                raise KeyboardInterrupt
        class BrokenOutput(StringIO):
            def write(self, text: str) -> int:
                raise OSError('output failed')
        for kind in ('log', 'metric', 'trace'):
            for stage in ('opener', 'read', 'sleeper', 'output', 'unexpected'):
                with self.subTest(kind=kind, stage=stage):
                    self.stderr = StringIO()
                    self.stdout = BrokenOutput() if stage == 'output' else StringIO()
                    def opener(request: Request, timeout: float) -> BytesIO:
                        if stage == 'opener':
                            raise KeyboardInterrupt
                        if stage == 'unexpected':
                            raise OSError('unexpected network I/O')
                        if stage == 'read':
                            return InterruptedRead()
                        if stage == 'sleeper':
                            raise HTTPError(request.full_url, 503, 'unavailable', Message(), None)
                        return self.response(kind)
                    def sleep(seconds: float) -> None:
                        raise KeyboardInterrupt
                    with self.assertRaises(OSError if stage in ('output', 'unexpected') else KeyboardInterrupt):
                        main(['--config', str(self.config), *self.command(kind, 2)],
                             environ={'TEST_TOKEN': 'secret-token'}, stdout=self.stdout, stderr=self.stderr,
                             opener=opener, sleeper=sleep)
                    self.assertEqual(self.stderr.getvalue().splitlines()[-1], 'HTTP attempt accounting: used=1 cap=2')
        self.stdout, self.stderr = StringIO(), StringIO()
        def remove_destination(request: Request, timeout: float) -> BytesIO:
            self.report.mkdir()
            return self.response('log')
        self.assertEqual(self.invoke('query', '--profile', 'test', '--max-http-attempts', '7',
                                     '--report', str(self.report), '{}', opener=remove_destination), 2)
        self.assertEqual(self.stderr.getvalue().splitlines()[-1], 'HTTP attempt accounting: used=1 cap=7')

    def test_scan_resume_usage_is_current_process_and_offline_export_has_no_marker(self) -> None:
        work = self.config.parent / 'work'
        def empty(request: Request, timeout: float) -> BytesIO:
            self.attempts += 1
            return BytesIO(b'{"status":"success","data":{"resultType":"streams","result":[]}}')
        for command, cap, expected_code, cumulative in (('query', '1', 4, 1), ('resume', '59', 0, 2)):
            flags = [command, '--profile', 'test', '--work-dir', str(work),
                     '--max-http-attempts', cap, '--report', str(self.report)]
            if command == 'query':
                flags += ['--scan', '--since', '30m', '{}']
            self.stderr = StringIO()
            self.assertEqual(self.invoke(*flags, opener=empty), expected_code, self.stderr.getvalue())
            self.assertEqual(self.stderr.getvalue().splitlines()[-1], f'HTTP attempt accounting: used=1 cap={cap}')
            report = json.loads(self.report.read_text())
            self.assertEqual(report['usage']['current']['http_attempts'], 1)
            self.assertEqual(report['usage']['cumulative']['http_attempts'], cumulative)
        self.stderr = StringIO()
        self.assertEqual(self.invoke('resume', '--profile', 'test', '--work-dir', str(work),
                                     '--max-http-attempts', '58', opener=empty), 0)
        self.assertEqual(self.stderr.getvalue().splitlines()[-1], 'HTTP attempt accounting: used=0 cap=58')
        self.stderr = StringIO()
        self.assertEqual(self.invoke('export', '--work-dir', str(work), opener=empty), 0)
        self.assertNotIn('HTTP attempt accounting:', self.stderr.getvalue())
        self.assertEqual(self.attempts, 2)


if __name__ == '__main__':
    unittest.main()
