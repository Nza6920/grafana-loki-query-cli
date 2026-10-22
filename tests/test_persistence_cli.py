from __future__ import annotations

from io import BytesIO, StringIO
from email.message import Message
import json
from pathlib import Path
import hashlib
import os
import subprocess
import sys
from unittest.mock import patch
import tempfile
from typing import Any
import unittest
from urllib.request import Request
from urllib.error import HTTPError

from loki_query.cli import main


class PersistenceCliTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.work = self.root / 'work'
        self.config = self.root / 'config.toml'
        self.config.write_text('[profiles.test]\ngrafana_url="https://grafana.example.com"\ndatasource_uid="logs"\ntoken_env="TOKEN"\n')
        self.stdout, self.stderr = StringIO(), StringIO()
        self.requests = 0
        self.clock = 0.0
        self.timeouts: list[float] = []

    def scan(self, responses: list[Any], *flags: str) -> int:
        def opener(request: Request, timeout: float) -> BytesIO:
            self.requests += 1
            self.timeouts.append(timeout)
            response = responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return BytesIO(json.dumps({'status': 'success', 'data': {'resultType': 'streams', 'result': response}}).encode())
        return main(['--config', str(self.config), 'query', '--profile', 'test', '--scan',
                     '--start', '1970-01-01T00:00:00Z', '--end', '1970-01-01T00:30:00Z',
                     '--output', 'jsonl', *flags, '{app="api"}'],
                    environ={'TOKEN': 'private-bearer-value'}, opener=opener,
                    stdout=self.stdout, stderr=self.stderr, monotonic=lambda: self.clock)

    def export(self, *flags: str) -> tuple[int, str, str]:
        stdout, stderr = StringIO(), StringIO()
        code = main(['--config', str(self.root / 'missing.toml'), 'export',
                     '--work-dir', str(self.work), '--output', 'jsonl', *flags],
                    environ={}, stdout=stdout, stderr=stderr,
                    opener=lambda request, timeout: self.fail('offline export queried Loki'))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_explicit_work_directory_exports_complete_evidence_without_credentials_or_network(self) -> None:
        first = [{'stream': {'app': 'api'}, 'values': [['900000000001', 'later']]}]
        second = [{'stream': {'app': 'api'}, 'values': [['1', 'same'], ['1', 'same']]}]
        self.assertEqual(self.scan([first, second], '--work-dir', str(self.work)), 0, self.stderr.getvalue())
        code, output, error = self.export()
        self.assertEqual(code, 0, error)
        self.assertEqual([json.loads(line)['line'] for line in output.splitlines()], ['later', 'same', 'same'])
        self.assertIn('coverage: complete', error)
        identity = json.loads((self.work / 'state.json').read_text())['payload']['identity']
        self.assertEqual(identity, {
            'query_type': 'log', 'profile': 'test', 'query': '{app="api"}',
            'target': {'endpoint': 'https://grafana.example.com/api/datasources/proxy/uid/logs/loki/api/v1/query_range', 'datasource_uid': 'logs'},
            'start_ns': '0', 'end_ns': '1800000000000', 'direction': 'backward',
            'result_schema': 'log_entry/v1',
        })
        for path in self.work.iterdir():
            self.assertNotIn('private-bearer-value', path.read_text())

    def test_export_cannot_overwrite_the_checkpoint_with_its_report(self) -> None:
        self.assertEqual(self.scan([[], []], '--work-dir', str(self.work)), 0)
        code, output, error = self.export('--report', str(self.work / 'state.json'))
        self.assertEqual(code, 2, error)
        self.assertEqual(output, '')
        self.assertIn('reserved', error)
        self.assertEqual(self.export()[0], 0)

    def test_interrupt_preserves_completed_interval_and_charges_inflight_attempt(self) -> None:
        first = [{'stream': {'app': 'api'}, 'values': [['900000000001', 'kept']]}]
        with self.assertRaises(KeyboardInterrupt):
            self.scan([first, KeyboardInterrupt()], '--work-dir', str(self.work))
        report = self.root / 'offline-report.json'
        code, output, error = self.export('--report', str(report))
        self.assertEqual(code, 4, error)
        self.assertEqual(json.loads(output)['line'], 'kept')
        saved = json.loads(report.read_text())
        self.assertEqual(saved['status'], 'incomplete')
        self.assertEqual(len(saved['coverage']['resolved']), 1)
        self.assertEqual(len(saved['coverage']['unresolved']), 1)
        self.assertEqual(saved['usage']['current']['http_attempts'], 2)
        self.assertEqual(saved['usage']['cumulative']['http_attempts'], 2)
        # The failed process releases its writer lock; the rejection is now for
        # reusing an existing investigation, never a stale lock marker.
        self.assertEqual(self.scan([], '--work-dir', str(self.work)), 2)
        self.assertIn('already contains a scan', self.stderr.getvalue())

    def test_partial_adaptive_export_selects_child_provenance_without_duplicates(self) -> None:
        parent = [{'stream': {'app': 'api'}, 'values': [['1', 'lower'], ['1000000001', 'upper']]}]
        self.assertEqual(self.scan([parent, []], '--work-dir', str(self.work), '--limit', '2',
                                   '--end', '1970-01-01T00:00:02Z', '--max-http-attempts', '2'), 4)
        report = self.root / 'offline-report.json'
        code, output, error = self.export('--report', str(report))
        self.assertEqual(code, 4, error)
        self.assertEqual([json.loads(line)['line'] for line in output.splitlines()], ['lower'])
        saved = json.loads(report.read_text())
        self.assertEqual(saved['coverage']['unresolved'][0]['sample_origin'], {'start_ns': '0', 'end_ns': '2000000000'})
        self.assertEqual(saved['counts']['raw_entries'], 2)
        self.assertEqual(saved['counts']['valid_records'], 1)
        self.assertEqual(len(saved['requests']), 2)

    def test_no_work_directory_means_no_log_archive_and_work_options_require_scan(self) -> None:
        self.assertEqual(self.scan([[], []]), 0, self.stderr.getvalue())
        self.assertFalse(self.work.exists())
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['config.toml'])
        for flags in ([], ['--query-type', 'metric'], ['--scan', '--query-type', 'metric']):
            code = main(['--config', str(self.config), 'query', '--profile', 'test',
                         '--work-dir', str(self.work), *flags, '{}'],
                        environ={'TOKEN': 'private-bearer-value'}, stdout=self.stdout, stderr=self.stderr,
                        opener=lambda request, timeout: self.fail('invalid flags must not query'))
            self.assertEqual(code, 2, self.stderr.getvalue())
        self.assertFalse(self.work.exists())

    def test_export_rejects_missing_or_corrupt_committed_evidence(self) -> None:
        code, output, _ = self.export()
        self.assertEqual((code, output), (2, ''))
        self.assertEqual(self.scan([[], []], '--work-dir', str(self.work)), 0)
        state = self.work / 'state.json'
        original = state.read_text()
        for content in ('{"payload":', '{}'):
            state.write_text(content)
            code, output, error = self.export()
            self.assertEqual((code, output), (2, ''), error)
            self.assertIn('Unusable scan state', error)
        state.write_text(original)
        state.unlink()
        (self.work / '.state-interrupted.tmp').write_text(original[:80])
        code, output, error = self.export()
        self.assertEqual((code, output), (2, ''), error)
        self.assertIn('Unusable scan state', error)

    def test_interrupted_publication_cannot_commit_completion_without_evidence(self) -> None:
        original_replace = os.replace
        def interrupted_replace(source: Any, destination: Any) -> None:
            if Path(destination).name == 'state.json':
                document = json.loads(Path(source).read_text())
                if document['payload']['report']['status'] == 'complete':
                    raise OSError('simulated interrupted publication')
            original_replace(source, destination)
        first = [{'stream': {}, 'values': [['900000000001', 'kept']]}]
        second = [{'stream': {}, 'values': [['1', 'not-yet-committed']]}]
        with patch('os.replace', side_effect=interrupted_replace):
            self.assertEqual(self.scan([first, second], '--work-dir', str(self.work)), 2)
        report = self.root / 'offline-report.json'
        code, output, error = self.export('--report', str(report))
        self.assertEqual(code, 4, error)
        self.assertEqual([json.loads(line)['line'] for line in output.splitlines()], ['kept'])
        saved = json.loads(report.read_text())
        self.assertEqual(len(saved['coverage']['resolved']), 1)
        self.assertEqual(len(saved['coverage']['unresolved']), 1)
        self.assertEqual(saved['usage']['current']['http_attempts'], 2)

    def test_state_validation_rejects_missing_evidence_even_with_an_intact_envelope(self) -> None:
        self.assertEqual(self.scan([[], []], '--work-dir', str(self.work)), 0)
        state = self.work / 'state.json'
        original = json.loads(state.read_text())
        for mutation in ('evidence', 'coverage', 'version', 'identity'):
            document = json.loads(json.dumps(original))
            payload = document['payload']
            if mutation == 'evidence':
                payload['intervals'][0]['evidence'] = None
            elif mutation == 'coverage':
                payload['intervals'].pop()
            elif mutation == 'version':
                payload['schema_version'] = 99
            else:
                payload['identity']['query'] = 'different'
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
            document['sha256'] = hashlib.sha256(encoded).hexdigest()
            state.write_text(json.dumps(document))
            code, output, error = self.export()
            self.assertEqual((code, output), (2, ''), f'{mutation}: {error}')
            self.assertIn('Unusable scan state', error)

    def test_writer_lock_rejects_competing_scan_and_is_released_after_process_death(self) -> None:
        script = """
from io import BytesIO
import json, sys, time
from loki_query.cli import main
calls = 0
def opener(request, timeout):
    global calls
    calls += 1
    if calls == 2:
        print('INFLIGHT', flush=True)
        time.sleep(60)
    result = [{'stream': {}, 'values': [['900000000001', 'durable']]}] if calls == 1 else []
    return BytesIO(json.dumps({'status': 'success', 'data': {'resultType': 'streams', 'result': result}}).encode())
main(['--config', sys.argv[1], 'query', '--profile', 'test', '--scan', '--work-dir', sys.argv[2],
      '--start', '1970-01-01T00:00:00Z', '--end', '1970-01-01T00:30:00Z', '{}'],
     environ={'TOKEN': 'private-bearer-value'}, opener=opener)
"""
        environment = dict(os.environ)
        environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
        process = subprocess.Popen([sys.executable, '-c', script, str(self.config), str(self.work)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
        try:
            assert process.stdout is not None
            self.assertEqual(process.stdout.readline().strip(), 'INFLIGHT')
            self.assertEqual(self.scan([], '--work-dir', str(self.work)), 2)
            self.assertEqual(self.requests, 0)
            self.assertIn('another writer may be active', self.stderr.getvalue())
            code, output, error = self.export()
            self.assertEqual(code, 4, error)
            self.assertEqual(json.loads(output)['line'], 'durable')
        finally:
            process.kill()
            process.communicate(timeout=5)
        self.stderr = StringIO()
        self.assertEqual(self.scan([], '--work-dir', str(self.work)), 2)
        self.assertIn('already contains a scan', self.stderr.getvalue())
        self.assertNotIn('another writer', self.stderr.getvalue())
        self.assertEqual(self.export()[0], 4)

    def test_checkpoint_io_time_constrains_the_actual_http_timeout(self) -> None:
        original_fsync = os.fsync
        def slow_sync(descriptor: int) -> None:
            self.clock += 0.1
            original_fsync(descriptor)
        with patch('os.fsync', side_effect=slow_sync):
            self.assertEqual(self.scan([[]], '--work-dir', str(self.work), '--max-elapsed-seconds', '1',
                                       '--max-http-attempts', '1'), 4)
        self.assertEqual(self.requests, 1)
        # Two fsync calls publish the initial snapshot and two publish the
        # charged attempt before HTTP; only 0.6s remains at the network seam.
        self.assertAlmostEqual(self.timeouts[0], 0.6 if os.name != "nt" else 0.8)

    def test_saved_authentication_failure_exports_prior_evidence_with_its_failure_status(self) -> None:
        first = [{'stream': {}, 'values': [['900000000001', 'kept']]}]
        rejected = HTTPError('https://grafana.example.com', 401, 'private-bearer-value', Message(), None)
        self.assertEqual(self.scan([first, rejected], '--work-dir', str(self.work)), 3)
        report = self.root / 'offline-report.json'
        code, output, error = self.export('--report', str(report))
        self.assertEqual(code, 3, error)
        self.assertEqual(json.loads(output)['line'], 'kept')
        saved = json.loads(report.read_text())
        self.assertEqual(saved['status'], 'failed')
        self.assertEqual(saved['stop_reasons'], ['authentication_failed'])
        self.assertNotIn('private-bearer-value', report.read_text() + error)
