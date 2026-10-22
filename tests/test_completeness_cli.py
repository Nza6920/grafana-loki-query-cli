from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO, StringIO
from email.message import Message
from urllib.error import HTTPError
import json
from pathlib import Path
import sys
import tempfile
from typing import Any
import unittest
from urllib.parse import parse_qs, urlparse
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from loki_query.cli import main  # noqa: E402


class CompletenessCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Path(self.directory.name) / "config.toml"
        self.config.write_text(
            '[profiles.test]\ngrafana_url = "https://grafana.example.com"\n'
            'datasource_uid = "logs/primary"\ntoken_env = "TEST_TOKEN"\n',
            encoding="utf-8",
        )
        self.report = Path(self.directory.name) / "report.json"
        self.stdout, self.stderr = StringIO(), StringIO()
        self.requests: list[Request] = []

    def invoke(self, result: object, *flags: str) -> int:
        def opener(request: Request, timeout: float) -> BytesIO:
            self.requests.append(request)
            return BytesIO(json.dumps({"status": "success", "data": {
                "resultType": "streams", "result": result,
            }}).encode())

        return main(
            ["--config", str(self.config), "query", "--profile", "test",
             "--report", str(self.report), "--output", "jsonl", *flags, '{app="api"}'],
            environ={"TEST_TOKEN": "secret-token"}, stdout=self.stdout,
            stderr=self.stderr, opener=opener, now=datetime(2026, 8, 11, tzinfo=UTC),
        )

    def read_report(self) -> Any:
        return json.loads(self.report.read_text(encoding="utf-8"))

    def test_empty_success_reports_resolved_absolute_range_and_preserves_defaults(self) -> None:
        self.assertEqual(self.invoke([]), 0, self.stderr.getvalue())
        report = self.read_report()
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["query_type"], "log")
        self.assertEqual(report["profile"], "test")
        self.assertEqual(report["target"]["datasource_uid"], "logs/primary")
        self.assertEqual(report["target"]["endpoint"],
                         "https://grafana.example.com/api/datasources/proxy/uid/logs%2Fprimary/loki/api/v1/query_range")
        self.assertEqual(report["query"], '{app="api"}')
        self.assertEqual(report["direction"], "backward")
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["coverage"]["unresolved"], [])
        self.assertEqual(report["coverage"]["resolved"], [{
            "start_ns": "1786405500000000000", "end_ns": "1786406400000000000",
        }])
        self.assertEqual(report["counts"], {
            "raw_entries": 0, "valid_records": 0, "skipped_entries": 0,
            "skipped_series": 0, "raw_count_exact": True,
        })
        self.assertEqual(report["request_limit"], 100)
        self.assertEqual(report["budgets"]["request_timeout_seconds"], 30.0)
        self.assertEqual(report["usage"]["current"]["http_attempts"], 1)
        self.assertEqual(report["usage"]["current"], report["usage"]["cumulative"])
        self.assertEqual(report["stop_reasons"], [])
        self.assertIn("ingestion", report["completeness_scope"])
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertIn("coverage: complete", self.stderr.getvalue())
        params = parse_qs(urlparse(self.requests[0].full_url).query)
        self.assertEqual(params["limit"], ["100"])
        self.assertEqual(params["direction"], ["backward"])
        self.assertEqual(len(self.requests), 1)

    def test_raw_limit_saturation_survives_invalid_entries_and_invalid_labels(self) -> None:
        code = self.invoke([
            {"stream": {"app": "api"}, "values": [["2", "valid"], ["bad", "lost"]]},
            {"stream": {"app": 42}, "values": [["1", "lost labels"]]},
        ], "--limit", "3")
        self.assertEqual(code, 4)
        report = self.read_report()
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["counts"], {
            "raw_entries": 3, "valid_records": 1, "skipped_entries": 2,
            "skipped_series": 1, "raw_count_exact": True,
        })
        self.assertEqual(report["coverage"]["resolved"], [])
        self.assertEqual(report["coverage"]["unresolved"], [{
            "start_ns": "1786405500000000000", "end_ns": "1786406400000000000",
            "reasons": ["possible_truncation", "parse_loss"],
        }])
        self.assertEqual(len(self.stdout.getvalue().splitlines()), 1)
        self.assertEqual(json.loads(self.stdout.getvalue())["line"], "valid")
        self.assertIn("possible truncation", self.stderr.getvalue())
        self.assertIn("skipped 1 series", self.stderr.getvalue())
        self.assertEqual(len(self.stderr.getvalue().splitlines()), 1)

    def test_retry_failure_reports_attempts_elapsed_waits_and_unresolved_bounds(self) -> None:
        elapsed = 10.0
        attempts = 0

        def opener(request: Request, timeout: float) -> BytesIO:
            nonlocal attempts, elapsed
            attempts += 1
            elapsed += 0.25
            headers = Message()
            headers["Retry-After"] = "2"
            raise HTTPError(request.full_url, 503, "secret-token", headers, None)

        def sleep(seconds: float) -> None:
            nonlocal elapsed
            elapsed += seconds

        code = main(
            ["--config", str(self.config), "query", "--profile", "test",
             "--report", str(self.report), "{}"],
            environ={"TEST_TOKEN": "secret-token"}, stdout=self.stdout,
            stderr=self.stderr, opener=opener, sleeper=sleep, monotonic=lambda: elapsed,
            now=datetime(2026, 8, 11, tzinfo=UTC),
        )
        self.assertEqual(code, 4)
        report = self.read_report()
        self.assertEqual(attempts, 3)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["usage"]["current"], {
            "http_attempts": 3, "elapsed_seconds": 4.75,
        })
        self.assertEqual(report["coverage"]["resolved"], [])
        self.assertEqual(report["coverage"]["unresolved"][0]["reasons"], ["request_failed"])
        self.assertEqual(report["stop_reasons"], ["request_failed"])
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertNotIn("secret-token", self.report.read_text() + self.stderr.getvalue())

    def test_metric_rejects_log_report_option_without_network(self) -> None:
        self.assertEqual(self.invoke([], "--query-type", "metric"), 2)
        self.assertEqual(self.requests, [])
        self.assertIn("--report", self.stderr.getvalue())
        self.assertFalse(self.report.exists())

    def test_report_redacts_credential_even_when_query_contains_its_value(self) -> None:
        code = main(
            ["--config", str(self.config), "query", "--profile", "test",
             "--report", str(self.report), '{app="api"} |= "secret-token"'],
            environ={"TEST_TOKEN": "secret-token"}, stdout=self.stdout,
            stderr=self.stderr, opener=lambda request, timeout: BytesIO(
                b'{"status":"success","data":{"resultType":"streams","result":[]}}'),
        )
        self.assertEqual(code, 0)
        self.assertNotIn("secret-token", self.report.read_text() + self.stderr.getvalue())
        self.assertIn("[REDACTED]", self.read_report()["query"])

    def test_complete_and_limited_samples_keep_descending_jsonl_and_success_exit(self) -> None:
        for limit, status in ((3, "complete"), (2, "incomplete")):
            with self.subTest(limit=limit):
                self.stdout.seek(0)
                self.stdout.truncate()
                self.stderr.seek(0)
                self.stderr.truncate()
                code = self.invoke([
                    {"stream": {"app": "one"}, "values": [["1", "old"]]},
                    {"stream": {"app": "two"}, "values": [["2", "new"]]},
                ], "--limit", str(limit), "--direction", "forward")
                self.assertEqual(code, 0)
                self.assertEqual(self.read_report()["status"], status)
                self.assertEqual([json.loads(line) for line in self.stdout.getvalue().splitlines()], [
                    {"type": "log_entry", "timestamp": "1970-01-01T00:00:00.000000002Z",
                     "labels": {"app": "two"}, "line": "new"},
                    {"type": "log_entry", "timestamp": "1970-01-01T00:00:00.000000001Z",
                     "labels": {"app": "one"}, "line": "old"},
                ])
                self.assertEqual(self.read_report()["direction"], "forward")
                self.assertEqual("possible truncation" in self.stderr.getvalue(), limit == 2)

    def test_malformed_container_is_parse_loss_with_unknown_raw_count(self) -> None:
        self.assertEqual(self.invoke([{"stream": {}, "values": "invalid"}]), 4)
        report = self.read_report()
        self.assertFalse(report["counts"]["raw_count_exact"])
        self.assertEqual(report["counts"]["skipped_series"], 1)
        self.assertEqual(report["coverage"]["unresolved"][0]["reasons"], ["parse_loss"])
        self.assertEqual(self.stdout.getvalue(), "")

    def test_retry_success_and_auth_failure_report_actual_network_work(self) -> None:
        for status, expected_code, expected_attempts in ((429, 0, 2), (403, 3, 1)):
            with self.subTest(status=status):
                attempts = 0
                def opener(request: Request, timeout: float) -> BytesIO:
                    nonlocal attempts
                    attempts += 1
                    if attempts == 1:
                        raise HTTPError(request.full_url, status, "secret-token", Message(), None)
                    return BytesIO(b'{"status":"success","data":{"resultType":"streams","result":[]}}')
                code = main(
                    ["--config", str(self.config), "query", "--profile", "test",
                     "--report", str(self.report), "--output", "raw", "{}"],
                    environ={"TEST_TOKEN": "secret-token"}, stdout=self.stdout,
                    stderr=self.stderr, opener=opener, sleeper=lambda seconds: None,
                )
                self.assertEqual(code, expected_code)
                report = self.read_report()
                self.assertEqual(report["usage"]["current"]["http_attempts"], expected_attempts)
                self.assertEqual(report["status"], "complete" if status == 429 else "failed")
                if status == 403:
                    self.assertEqual(report["coverage"]["unresolved"][0]["reasons"], ["authentication_failed"])
                self.assertEqual(self.stdout.getvalue(), "")
                self.assertNotIn("secret-token", self.report.read_text() + self.stderr.getvalue())

    @unittest.skipUnless(Path("/dev/stdout").exists(), "requires POSIX stdout device")
    def test_report_destination_cannot_send_metadata_to_stdout(self) -> None:
        self.assertEqual(self.invoke([], "--report", "/dev/stdout"), 2)
        self.assertEqual(self.requests, [])
        self.assertIn("regular file", self.stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
