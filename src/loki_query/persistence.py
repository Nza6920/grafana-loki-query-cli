"""Opt-in, atomic scan checkpoints and credential-free offline reconstruction."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, BinaryIO

from .client import LogEntry, ParseSummary, QueryResult
from .config import ConfigurationError
from .scanning import ScanInterval, ScanOutcome


STATE_NAME = 'state.json'


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


class WorkDirectory:
    """A single writer, locked by the OS rather than a stale PID marker.

    Every state document contains its evidence; one atomic rename commits both.
    A crash before rename leaves the preceding valid checkpoint intact.
    """

    def __init__(self, destination: str) -> None:
        self.path = Path(destination).expanduser()

    @contextmanager
    def writer(self) -> Iterator[WorkDirectory]:
        handle: BinaryIO | None = None
        acquired = False
        try:
            self.path.mkdir(parents=True, exist_ok=True)
            handle = (self.path / '.writer.lock').open('a+b')
            if os.name == 'nt':
                import msvcrt
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b'\0')
                    handle.flush()
                handle.seek(0)
                getattr(msvcrt, "locking")(handle.fileno(), getattr(msvcrt, "LK_NBLCK"), 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            yield self
        except OSError as error:
            message = ('Cannot access or publish the selected scan work directory.' if acquired
                       else 'Cannot lock the scan work directory; another writer may be active.')
            raise ConfigurationError(message) from error
        finally:
            if handle is not None:
                # Closing releases the kernel lock, including after exceptions;
                # process termination also releases it, without a stale lock.
                handle.close()

    def validate_report(self, destination: str | None) -> None:
        if destination is None:
            return
        selected = Path(destination).expanduser().resolve()
        if selected.parent == self.path.resolve() and (selected.name in (STATE_NAME, '.writer.lock') or selected.name.startswith('.state-')):
            raise ConfigurationError('The --report destination is reserved for scan state; select another file.')

    def require_new(self) -> None:
        if (self.path / STATE_NAME).exists():
            raise ConfigurationError('The work directory already contains a scan; use a new directory or resume it.')
        if any(path.name != '.writer.lock' and not path.name.startswith('.state-') for path in self.path.iterdir()):
            raise ConfigurationError('The work directory is not empty; select a dedicated scan directory.')

    def save(self, outcome: ScanOutcome, *, identity: dict[str, Any] | None = None) -> None:
        report = outcome.report
        if identity is None:
            identity = {key: report[key] for key in ('query_type', 'profile', 'target', 'query', 'start_ns', 'end_ns', 'direction')}
            identity['result_schema'] = 'log_entry/v1'
        intervals: list[dict[str, Any]] = []
        for interval in outcome.intervals:
            evidence = None
            if interval.result is not None:
                records = []
                for record in interval.result.records:
                    if not isinstance(record, LogEntry):
                        raise ConfigurationError('Only log evidence can be persisted as a scan.')
                    records.append({'timestamp_ns': str(record.timestamp_ns), 'labels': record.labels, 'line': record.line})
                evidence = {'records': records, 'raw_entries': interval.result.raw_entries,
                            'raw_count_exact': interval.result.raw_count_exact,
                            'skipped_entries': interval.result.skipped.skipped_entries,
                            'skipped_series': interval.result.skipped.skipped_series}
            intervals.append({'start_ns': str(interval.start_ns), 'end_ns': str(interval.end_ns),
                              'reasons': list(interval.reasons), 'http_attempts': interval.http_attempts,
                              'request_limit': interval.request_limit,
                              'sample_origin': ([str(value) for value in interval.sample_origin]
                                                if interval.sample_origin is not None else None),
                              'evidence': evidence})
        payload = {'schema_version': 1, 'identity': identity, 'intervals': intervals,
                   'report': report, 'exit_code': outcome.exit_code}
        document = {'payload': payload, 'sha256': hashlib.sha256(_canonical(payload)).hexdigest()}
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(mode='wb', prefix='.state-', suffix='.tmp', dir=self.path, delete=False) as output:
                temporary = output.name
                output.write(_canonical(document) + b'\n')
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path / STATE_NAME)
            temporary = None
            if os.name != 'nt':
                directory = os.open(self.path, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except (OSError, ValueError) as error:
            raise ConfigurationError('Cannot publish scan checkpoint; previous committed progress is preserved.') from error
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def load(self) -> tuple[dict[str, Any], ScanOutcome]:
        try:
            document = json.loads((self.path / STATE_NAME).read_text(encoding='utf-8'))
            payload = document['payload']
            if document['sha256'] != hashlib.sha256(_canonical(payload)).hexdigest():
                raise ValueError('checkpoint checksum mismatch')
            return _decode(payload)
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, OverflowError) as error:
            raise ConfigurationError('Unusable scan state: missing, corrupt, incomplete, or incompatible checkpoint/evidence.') from error


def _integer(value: Any, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError('expected integer')
    parsed = int(value)
    if minimum is not None and parsed < minimum:
        raise ValueError('invalid integer range')
    return parsed


def _decode(payload: Any) -> tuple[dict[str, Any], ScanOutcome]:
    if payload['schema_version'] != 1:
        raise ValueError('unsupported state version')
    identity, report = payload['identity'], payload['report']
    if identity['result_schema'] != 'log_entry/v1' or identity['query_type'] != 'log':
        raise ValueError('unsupported result schema')
    for key in ('query_type', 'profile', 'target', 'query', 'start_ns', 'end_ns', 'direction'):
        if report[key] != identity[key]:
            raise ValueError('inconsistent query identity')
    if identity['direction'] not in ('backward', 'forward'):
        raise ValueError('invalid direction')
    start, end = _integer(identity['start_ns']), _integer(identity['end_ns'])
    if start >= end or not isinstance(identity['query'], str) or not identity['query'].strip():
        raise ValueError('invalid query range')
    if not isinstance(identity['profile'], str) or not isinstance(identity['target']['endpoint'], str) or not isinstance(identity['target']['datasource_uid'], str):
        raise ValueError('invalid target')
    intervals: list[ScanInterval] = []
    for item in payload['intervals']:
        lower, upper = _integer(item['start_ns']), _integer(item['end_ns'])
        if not start <= lower < upper <= end:
            raise ValueError('invalid interval bounds')
        reasons = item['reasons']
        if not isinstance(reasons, list) or any(not isinstance(reason, str) or not reason for reason in reasons):
            raise ValueError('invalid interval status')
        evidence = item['evidence']
        result = None
        origin = None
        if evidence is not None:
            records = []
            for record in evidence['records']:
                timestamp = _integer(record['timestamp_ns'])
                if not lower <= timestamp < upper or not isinstance(record['line'], str):
                    raise ValueError('invalid evidence record')
                labels = record['labels']
                if not isinstance(labels, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in labels.items()):
                    raise ValueError('invalid evidence labels')
                records.append(LogEntry(timestamp, labels, record['line']))
            if type(evidence['raw_count_exact']) is not bool:
                raise ValueError('invalid raw count status')
            result = QueryResult('log', list(records), ParseSummary(
                _integer(evidence['skipped_series'], minimum=0), _integer(evidence['skipped_entries'], minimum=0)),
                _integer(evidence['raw_entries'], minimum=len(records)), evidence['raw_count_exact'])
            if not isinstance(item['sample_origin'], list) or len(item['sample_origin']) != 2:
                raise ValueError('missing evidence provenance')
            origin = (_integer(item['sample_origin'][0]), _integer(item['sample_origin'][1]))
            if not start <= origin[0] <= lower < upper <= origin[1] <= end:
                raise ValueError('invalid evidence provenance')
        elif not reasons or item['sample_origin'] is not None:
            raise ValueError('resolved interval is missing evidence')
        limit = item['request_limit']
        intervals.append(ScanInterval(lower, upper, result, _integer(item['http_attempts'], minimum=0),
                                      _integer(limit, minimum=1) if limit is not None else None,
                                      list(reasons), origin))
    cursor = start
    for interval in sorted(intervals, key=lambda interval: interval.start_ns):
        if interval.start_ns != cursor:
            raise ValueError('missing or overlapping coverage')
        cursor = interval.end_ns
    if cursor != end:
        raise ValueError('missing coverage')
    records_combined = [record for interval in intervals if interval.result is not None for record in interval.result.records]
    counts = report['counts']
    if counts['valid_records'] != len(records_combined) or type(counts['raw_count_exact']) is not bool:
        raise ValueError('inconsistent evidence counts')
    combined = QueryResult('log', sorted(records_combined, key=lambda record: record.timestamp_ns, reverse=True),
                           ParseSummary(_integer(counts['skipped_series'], minimum=0), _integer(counts['skipped_entries'], minimum=0)),
                           _integer(counts['raw_entries'], minimum=len(records_combined)), counts['raw_count_exact'])
    coverage = report['coverage']
    for category, expected in (('resolved', [i for i in intervals if not i.reasons]),
                               ('unresolved', [i for i in intervals if i.reasons])):
        observed = coverage[category]
        if len(observed) != len(expected):
            raise ValueError('inconsistent completeness')
        for saved, actual in zip(observed, expected):
            if (_integer(saved['start_ns']), _integer(saved['end_ns']), saved.get('reasons', []), saved['retained_records']) != (actual.start_ns, actual.end_ns, actual.reasons, len(actual.result.records) if actual.result is not None else 0):
                raise ValueError('inconsistent completeness evidence')
    for usage in (report['usage']['current'], report['usage']['cumulative']):
        _integer(usage['http_attempts'], minimum=0)
        if not isinstance(usage['elapsed_seconds'], (int, float)) or not math.isfinite(usage['elapsed_seconds']) or usage['elapsed_seconds'] < 0:
            raise ValueError('invalid usage')
    current, cumulative = report['usage']['current'], report['usage']['cumulative']
    if any(current[key] > cumulative[key] for key in ('http_attempts', 'elapsed_seconds')):
        raise ValueError('inconsistent cumulative usage')
    exit_code = _integer(payload['exit_code'], minimum=0)
    if report['status'] not in ('complete', 'incomplete', 'failed') or exit_code not in (0, 3, 4):
        raise ValueError('invalid completion status')
    if (report['status'] == 'complete') != (not coverage['unresolved']) or (exit_code == 0) != (report['status'] == 'complete'):
        raise ValueError('inconsistent completion status')
    return dict(identity), ScanOutcome(combined, dict(report), exit_code, intervals)
