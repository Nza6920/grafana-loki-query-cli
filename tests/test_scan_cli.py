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
from urllib.parse import parse_qs, urlparse
from urllib.request import Request

from loki_query.cli import main


class ScanCliTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = Path(directory.name) / 'config.toml'
        self.config.write_text('[profiles.test]\ngrafana_url="https://grafana.example.com"\ndatasource_uid="logs"\ntoken_env="TOKEN"\n')
        self.report = Path(directory.name) / 'report.json'
        self.stdout, self.stderr = StringIO(), StringIO()
        self.requests: list[dict[str, list[str]]] = []
        self.timeouts: list[float] = []
        self.sleeps: list[float] = []
        self.clock = 0.0
        self.cost = 0.0

    def invoke(self, responses: list[Any], *flags: str) -> int:
        def opener(request: Request, timeout: float) -> BytesIO:
            self.requests.append(parse_qs(urlparse(request.full_url).query))
            self.timeouts.append(timeout)
            self.clock += self.cost
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return BytesIO(json.dumps({'status': 'success', 'data': {'resultType': 'streams', 'result': response}}).encode())
        def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)
            self.clock += seconds
        return main(['--config', str(self.config), 'query', '--profile', 'test',
                     '--scan', '--report', str(self.report), '--output', 'jsonl',
                     '--start', '1970-01-01T00:00:00Z', '--end', '1970-01-01T00:31:00Z',
                     *flags, '{app="api"} |= "retain this"'],
                    environ={'TOKEN': 'secret'}, stdout=self.stdout, stderr=self.stderr,
                    opener=opener, sleeper=sleep, monotonic=lambda: self.clock,
                    now=datetime(2026, 1, 1, tzinfo=UTC))

    def read_report(self) -> Any:
        return json.loads(self.report.read_text())

    def test_scan_partitions_exact_half_open_intervals_and_freezes_query(self) -> None:
        self.assertEqual(self.invoke([[], [], []]), 0, self.stderr.getvalue())
        self.assertEqual([(r['start'][0], r['end'][0]) for r in self.requests],
                         [('1800000000000', '1860000000000'), ('900000000000', '1800000000000'), ('0', '900000000000')])
        self.assertTrue(all(r['query'] == ['{app="api"} |= "retain this"'] for r in self.requests))
        self.assertTrue(all(r['limit'] == ['1000'] for r in self.requests))
        report = self.read_report()
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['budgets'], {'max_http_attempts': 60, 'max_elapsed_seconds': 300.0,
                                           'max_records': 10000, 'request_timeout_seconds': 30.0})
        self.assertEqual(len(report['coverage']['resolved']), 3)

    def test_forward_scan_preserves_duplicate_records_and_global_descending_output(self) -> None:
        responses = [[{'stream': {'app': 'api'}, 'values': [['1', 'same'], ['1', 'same']]}],
                     [{'stream': {}, 'values': [['900000000001', 'later']]}], []]
        self.assertEqual(self.invoke(responses, '--direction', 'forward'), 0)
        self.assertEqual([r['start'][0] for r in self.requests], ['0', '900000000000', '1800000000000'])
        self.assertEqual([json.loads(line)['line'] for line in self.stdout.getvalue().splitlines()], ['later', 'same', 'same'])

    def test_sub_microsecond_and_pre_epoch_ranges_remain_exact(self) -> None:
        self.assertEqual(self.invoke([[]], '--start', '1969-12-31T23:59:59.999999999Z',
                                     '--end', '1970-01-01T00:00:00.000000002Z'), 0, self.stderr.getvalue())
        self.assertEqual(self.requests[0]['start'], ['-1'])
        self.assertEqual(self.requests[0]['end'], ['2'])

    def test_attempt_budget_charges_retries_and_does_not_wait_without_an_attempt(self) -> None:
        error = HTTPError('https://grafana.example.com', 429, 'limited', Message(), None)
        self.assertEqual(self.invoke([error], '--max-http-attempts', '1'), 4)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.sleeps, [])
        report = self.read_report()
        self.assertEqual(report['usage']['current']['http_attempts'], 1)
        self.assertEqual(report['stop_reasons'], ['http_attempt_budget'])
        self.assertEqual(len(report['coverage']['unresolved']), 3)

    def test_partial_parsing_and_raw_saturation_preserve_evidence_and_independent_progress(self) -> None:
        first = [{'stream': {}, 'values': [['1800000000001', 'valid'], ['bad', 'lost']]}]
        self.assertEqual(self.invoke([first, [], []], '--limit', '2'), 4)
        report = self.read_report()
        self.assertEqual(len(report['coverage']['resolved']), 2)
        self.assertEqual(report['coverage']['unresolved'][0]['reasons'], ['possible_truncation', 'parse_loss'])
        self.assertEqual(report['counts']['raw_entries'], 2)
        self.assertEqual(report['counts']['valid_records'], 1)
        self.assertEqual(json.loads(self.stdout.getvalue())['line'], 'valid')
        self.assertEqual(len(self.requests), 3)

    def test_record_budget_bounds_request_and_retained_evidence(self) -> None:
        first = [{'stream': {}, 'values': [['1800000000001', 'a'], ['1800000000002', 'b']]}]
        second = [{'stream': {}, 'values': [['900000000001', 'c'], ['900000000002', 'excess']]}]
        self.assertEqual(self.invoke([first, second], '--max-records', '3'), 4)
        self.assertEqual([r['limit'] for r in self.requests], [['3'], ['1']])
        self.assertEqual(len(self.stdout.getvalue().splitlines()), 3)
        report = self.read_report()
        self.assertEqual(report['counts']['raw_entries'], 4)
        self.assertEqual(report['counts']['valid_records'], 3)
        self.assertEqual(report['stop_reasons'], ['record_budget'])

    def test_elapsed_budget_constrains_timeout_and_stops_before_retry_after(self) -> None:
        self.cost = 0.75
        headers = Message()
        headers['Retry-After'] = '2'
        error = HTTPError('https://grafana.example.com', 429, 'limited', headers, None)
        self.assertEqual(self.invoke([[], error], '--max-elapsed-seconds', '2'), 4)
        self.assertEqual(self.timeouts, [2.0, 1.25])
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.read_report()['stop_reasons'], ['elapsed_time_budget'])
        self.assertEqual(self.read_report()['usage']['current']['elapsed_seconds'], 1.5)

    def test_retry_waits_count_elapsed_and_respect_two_retry_ceiling(self) -> None:
        headers = Message()
        headers['Retry-After'] = '1'
        error = HTTPError('https://grafana.example.com', 503, 'temporary', headers, None)
        self.assertEqual(self.invoke([error, error, error, [], []]), 4)
        self.assertEqual(self.sleeps, [1.0, 1.0])
        self.assertEqual(self.read_report()['usage']['current'], {'http_attempts': 5, 'elapsed_seconds': 2.0})
        self.assertEqual(len(self.read_report()['coverage']['resolved']), 2)

    def test_fatal_http_failures_stop_immediately_and_keep_prior_evidence(self) -> None:
        for status, code, reason in ((401, 3, 'authentication_failed'), (400, 4, 'invalid_query')):
            with self.subTest(status=status):
                self.requests.clear()
                self.stdout.seek(0)
                self.stdout.truncate()
                error = HTTPError('https://grafana.example.com', status, 'secret', Message(), None)
                first = [{'stream': {}, 'values': [['1800000000001', 'evidence']]}]
                self.assertEqual(self.invoke([first, error]), code)
                self.assertEqual(len(self.requests), 2)
                self.assertEqual(json.loads(self.stdout.getvalue())['line'], 'evidence')
                self.assertEqual(self.read_report()['stop_reasons'], [reason])
                self.assertEqual(self.read_report()['status'], 'failed')
                self.assertNotIn('secret', self.report.read_text() + self.stderr.getvalue())

    def test_timeout_becomes_gap_and_independent_intervals_continue(self) -> None:
        self.assertEqual(self.invoke([TimeoutError(), [], []]), 4)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.read_report()['coverage']['unresolved'][0]['reasons'], ['timeout'])

    def test_scan_flags_require_log_mode_and_finite_positive_budgets_before_network(self) -> None:
        for flags in (('--query-type', 'metric'), ('--max-http-attempts', '0'),
                      ('--max-records', '-1'), ('--max-elapsed-seconds', 'nan'),
                      ('--timeout', 'inf')):
            with self.subTest(flags=flags):
                self.assertEqual(self.invoke([], *flags), 2)
                self.assertEqual(self.requests, [])
        code = main(['--config', str(self.config), 'query', '--profile', 'test',
                     '--max-records', '2', '{}'], environ={'TOKEN': 'secret'},
                    stdout=self.stdout, stderr=self.stderr,
                    opener=lambda request, timeout: self.fail('must reject before HTTP'))
        self.assertEqual(code, 2)

    def test_successful_retry_counts_attempts_without_subdividing_rate_limit(self) -> None:
        headers = Message()
        headers['Retry-After'] = '0.25'
        error = HTTPError('https://grafana.example.com', 429, 'limited', headers, None)
        self.assertEqual(self.invoke([error, [], [], []]), 0)
        self.assertEqual(self.requests[0], self.requests[1])
        self.assertEqual(self.sleeps, [0.25])
        self.assertEqual(self.read_report()['usage']['current'], {'http_attempts': 4, 'elapsed_seconds': 0.25})

    def test_relative_range_is_frozen_at_supplied_reference_time(self) -> None:
        requests: list[dict[str, list[str]]] = []
        def opener(request: Request, timeout: float) -> BytesIO:
            requests.append(parse_qs(urlparse(request.full_url).query))
            return BytesIO(b'{"status":"success","data":{"resultType":"streams","result":[]}}')
        code = main(['--config', str(self.config), 'query', '--profile', 'test', '--scan',
                     '--since', '16m', '--report', str(self.report), '{}'],
                    environ={'TOKEN': 'secret'}, stdout=self.stdout, stderr=self.stderr,
                    opener=opener, now=datetime(1970, 1, 1, 0, 16, tzinfo=UTC))
        self.assertEqual(code, 0)
        self.assertEqual([(r['start'][0], r['end'][0]) for r in requests],
                         [('900000000000', '960000000000'), ('0', '900000000000')])

    def test_elapsed_budget_stops_new_intervals_after_response_consumes_allowance(self) -> None:
        self.cost = 1.0
        self.assertEqual(self.invoke([[]], '--max-elapsed-seconds', '1'), 4)
        self.assertEqual(self.timeouts, [1.0])
        self.assertEqual(self.read_report()['stop_reasons'], ['elapsed_time_budget'])
        self.assertEqual(len(self.read_report()['coverage']['resolved']), 1)

    def test_report_distinguishes_budget_interrupted_attempt_from_unqueried_work(self) -> None:
        error = HTTPError('https://grafana.example.com', 429, 'limited', Message(), None)
        self.assertEqual(self.invoke([error], '--max-http-attempts', '1'), 4)
        gaps = self.read_report()['coverage']['unresolved']
        self.assertEqual(gaps[0]['reasons'], ['http_attempt_budget'])
        self.assertEqual(gaps[0]['http_attempts'], 1)
        self.assertEqual(gaps[0]['request_limit'], 1000)
        self.assertEqual(gaps[1]['reasons'], ['unqueried', 'http_attempt_budget'])
        self.assertEqual(gaps[1]['http_attempts'], 0)
        self.assertIsNone(gaps[1]['request_limit'])

    def test_nonfinite_retry_after_never_causes_nonfinite_sleep(self) -> None:
        cases: list[tuple[str, int, list[Any]]] = [('inf', 4, []), ('nan', 0, [[], [], []])]
        for retry_after, expected_code, responses in cases:
            with self.subTest(retry_after=retry_after):
                self.requests.clear()
                self.sleeps.clear()
                headers = Message()
                headers['Retry-After'] = retry_after
                error = HTTPError('https://grafana.example.com', 429, 'limited', headers, None)
                self.assertEqual(self.invoke([error, *responses]), expected_code)
                self.assertEqual(self.sleeps, [] if retry_after == 'inf' else [0.0])
