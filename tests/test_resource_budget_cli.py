from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from io import BytesIO, StringIO
from pathlib import Path
import json
import tempfile
import time
import unittest
from urllib.request import Request

from loki_query.cli import main


class ResourceBudgetCliTests(unittest.TestCase):
    def test_response_actual_bytes_are_limited_without_retry_or_partial_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.toml'
            config.write_text('[profiles.test]\ngrafana_url="https://example.com"\n'
                              'datasource_uid="logs"\ntoken_env="TEST_TOKEN"\n')
            response = BytesIO(b'{"status":"success","data":{"resultType":"streams","result":[]}}' + b' ' * 100)
            attempts = 0
            def opener(request: Request, timeout: float) -> BytesIO:
                nonlocal attempts
                attempts += 1
                return response
            stdout, stderr = StringIO(), StringIO()
            code = main(['--config', str(config), 'query', '--profile', 'test',
                         '--max-response-bytes', '80', '--max-http-attempts', '3', '{}'],
                        environ={'TEST_TOKEN': 'token'}, stdout=stdout, stderr=stderr,
                        opener=opener, now=datetime(2026, 8, 11, tzinfo=UTC))
            self.assertEqual(code, 4, stderr.getvalue())
            self.assertIn('response_bytes', stderr.getvalue())
            self.assertIn('incomplete', stderr.getvalue())
            self.assertEqual(stdout.getvalue(), '')
            self.assertEqual(attempts, 1)
            self.assertTrue(response.closed)
            self.assertEqual(stderr.getvalue().splitlines()[-1], 'HTTP attempt accounting: used=1 cap=3')

    def run_query(self, body: bytes, *flags: str, metric: bool = False,
                  clock: Callable[[], float] = time.monotonic, trace: bool = False,
                  length: str | None = None) -> tuple[int, str, str, bool, int]:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.toml'
            config.write_text('[profiles.test]\ngrafana_url="https://example.com"\n'
                              'datasource_uid="logs"\ntoken_env="TEST_TOKEN"\n')
            class Response(BytesIO):
                def __init__(self) -> None:
                    super().__init__(body)
                    self.headers = {} if length is None else {"Content-Length": length}
            response = Response()
            attempts = 0
            def opener(request: Request, timeout: float) -> BytesIO:
                nonlocal attempts
                attempts += 1
                return response
            stdout, stderr = StringIO(), StringIO()
            code = main(['--config', str(config), 'trace' if trace else 'query', '--profile', 'test',
                         '--output', 'jsonl', *(['--query-type', 'metric'] if metric else []),
                         *(['--selector', '{}', '--since', '15m'] if trace else []),
                         *flags, 'abc' if trace else '{}'],
                        environ={'TEST_TOKEN': 'token'}, stdout=stdout, stderr=stderr,
                        opener=opener, monotonic=clock, now=datetime(2026, 8, 11, tzinfo=UTC))
            return code, stdout.getvalue(), stderr.getvalue(), response.closed, attempts

    def test_complete_canonical_records_charge_labels_payload_and_utf8(self) -> None:
        # Canonical log record is exactly 63 UTF-8 bytes (Chinese is three bytes).
        body = ('{"status":"success","data":{"resultType":"streams","result":'
                '[{"stream":{"a":"中"},"values":[["1","x"]]}]}}').encode()
        for cap, expected in ((63, 0), (62, 4)):
            with self.subTest(cap=cap):
                code, stdout, stderr, closed, attempts = self.run_query(body, '--max-retained-bytes', str(cap))
                self.assertEqual(code, expected, stderr)
                self.assertTrue(closed)
                self.assertEqual(attempts, 1)
                if expected:
                    self.assertEqual(stdout, '')
                    self.assertIn('retained_bytes', stderr)
                else:
                    self.assertIn('中', stdout)

    def test_request_deadline_includes_opening_and_every_body_read(self) -> None:
        current = 0.0
        def clock() -> float:
            nonlocal current
            current += 1.0
            return current
        body = b'{"status":"success","data":{"resultType":"streams","result":[]}}'
        code, stdout, stderr, closed, attempts = self.run_query(body, '--timeout', '1', clock=clock)
        self.assertEqual(code, 4, stderr)
        self.assertIn('response_timeout', stderr)
        self.assertEqual(stdout, '')
        self.assertTrue(closed)
        self.assertEqual(attempts, 1)

    def test_default_transport_deadline_interrupts_continuous_body(self) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass
            def do_GET(self) -> None:
                self.send_response(200)
                self.end_headers()
                try:
                    for _ in range(80):
                        self.wfile.write(b' ')
                        self.wfile.flush()
                        time.sleep(0.04)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / 'config.toml'
                config.write_text(f'[profiles.test]\ngrafana_url="http://127.0.0.1:{server.server_port}"\n'
                                  'datasource_uid="logs"\ntoken_env="TEST_TOKEN"\n')
                stderr = StringIO()
                started = time.monotonic()
                code = main(['--config', str(config), 'query', '--profile', 'test', '--timeout', '0.4', '{}'],
                            environ={'TEST_TOKEN': 'token'}, stdout=StringIO(), stderr=stderr)
                elapsed = time.monotonic() - started
                self.assertEqual(code, 4, stderr.getvalue())
                self.assertLess(elapsed, 2.0)
                self.assertIn('response_timeout', stderr.getvalue())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_actual_response_boundary_ignores_missing_false_or_invalid_length(self) -> None:
        body = b'{"status":"success","data":{"resultType":"streams","result":[]}}'
        for length in (None, '1', '999999', 'invalid'):
            for cap, expected in ((len(body), 0), (len(body) - 1, 4)):
                with self.subTest(length=length, cap=cap):
                    code, _, stderr, closed, attempts = self.run_query(body, '--max-response-bytes', str(cap), length=length)
                    self.assertEqual(code, expected, stderr)
                    self.assertTrue(closed)
                    self.assertEqual(attempts, 1)

    def test_metric_retention_and_response_protection_include_payload_and_labels(self) -> None:
        for label, value in (('中' * 100, '2'), ('a', '2' * 100)):
            body = json.dumps({'status': 'success', 'data': {'resultType': 'matrix', 'result': [
                {'metric': {'app': label}, 'values': [[1, value]]}]}}).encode()
            for flag in ('--max-retained-bytes', '--max-response-bytes'):
                code, stdout, stderr, closed, attempts = self.run_query(body, flag, '90', metric=True)
                self.assertEqual(code, 4, stderr)
                self.assertEqual(stdout, '')
                self.assertTrue(closed)
                self.assertEqual(attempts, 1)

    def test_invalid_resource_configuration_fails_before_http(self) -> None:
        for flag, hard in (('--max-response-bytes', 67108864), ('--max-retained-bytes', 268435456)):
            for value in ('0', '-1', 'inf', 'nan', str(hard + 1)):
                code, _, _, _, attempts = self.run_query(b'', flag, value)
                self.assertEqual(code, 2)
                self.assertEqual(attempts, 0)

    def test_bad_json_is_never_salvaged_and_response_is_closed(self) -> None:
        for body in (b'{"status":"success"', b'\xff'):
            code, stdout, stderr, closed, attempts = self.run_query(body)
            self.assertEqual(code, 4, stderr)
            self.assertEqual(stdout, '')
            self.assertTrue(closed)
            self.assertEqual(attempts, 1)

    def test_trace_and_scan_share_response_protection_without_resource_recovery(self) -> None:
        for trace, flags in ((True, []), (False, ['--scan', '--since', '1m'])):
            code, stdout, stderr, closed, attempts = self.run_query(b'x' * 200, '--max-response-bytes', '100',
                                                                  *flags, trace=trace)
            self.assertEqual(code, 4, stderr)
            self.assertIn('response_bytes', stderr)
            self.assertEqual(stdout, '')
            self.assertTrue(closed)
            self.assertEqual(attempts, 1)
