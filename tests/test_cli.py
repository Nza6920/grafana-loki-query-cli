from __future__ import annotations

import os
from pathlib import Path
from io import BytesIO, StringIO
import json
from datetime import UTC, datetime
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse
from urllib.error import HTTPError
from urllib.request import Request
from email.message import Message


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from loki_query.cli import main  # noqa: E402


def run_cli(
    *args: str,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    source_path = str(ROOT / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (source_path, environment.get("PYTHONPATH", "")) if part
    )
    environment.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "loki_query", *args],
        cwd=ROOT,
        env=environment,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )


class CliHelpTests(unittest.TestCase):
    def test_help_describes_public_commands(self) -> None:
        result = run_cli("--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("query", result.stdout)
        self.assertIn("profiles", result.stdout)
        self.assertIn("config", result.stdout)

    def test_config_path_honors_xdg_config_home(self) -> None:
        result = run_cli(
            "config",
            "path",
            env={"XDG_CONFIG_HOME": "/tmp/loki-query-test-config"},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "/tmp/loki-query-test-config/loki-query/config.toml",
        )

    def test_profiles_list_reads_named_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text(
                """
[profiles.prod]
grafana_url = "https://grafana-prod.example.com"
datasource_uid = "prod-loki"
token_env = "GRAFANA_PROD_TOKEN"
default_selector = '{namespace="prod"}'

[profiles.staging]
grafana_url = "https://grafana-staging.example.com"
datasource_uid = "staging-loki"
token_env = "GRAFANA_STAGING_TOKEN"
""".strip(),
                encoding="utf-8",
            )

            result = run_cli("--config", str(config_path), "profiles", "list")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["prod", "staging"])

    def test_config_validate_reports_valid_profile_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text(
                """
[profiles.prod]
grafana_url = "https://grafana.example.com"
datasource_uid = "loki"
token_env = "GRAFANA_TOKEN"
""".strip(),
                encoding="utf-8",
            )

            result = run_cli("--config", str(config_path), "config", "validate")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "Configuration is valid (1 profile).")

    def test_config_validate_reports_actionable_error_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text(
                """
[profiles.prod]
grafana_url = "https://grafana.example.com"
token_env = "GRAFANA_TOKEN"
""".strip(),
                encoding="utf-8",
            )

            result = run_cli("--config", str(config_path), "config", "validate")

        self.assertEqual(result.returncode, 2)
        self.assertIn("requires non-empty 'datasource_uid'", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_config_rejects_inline_token_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text(
                """
[profiles.prod]
grafana_url = "https://grafana.example.com"
datasource_uid = "loki"
token_env = "GRAFANA_TOKEN"
token = "must-not-be-stored-here"
""".strip(),
                encoding="utf-8",
            )

            result = run_cli("--config", str(config_path), "config", "validate")

        self.assertEqual(result.returncode, 2)
        self.assertIn("unsupported keys: token", result.stderr)
        self.assertNotIn("must-not-be-stored-here", result.stderr)

    def test_config_reports_malformed_url_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text(
                """
[profiles.prod]
grafana_url = "https://[invalid"
datasource_uid = "loki"
token_env = "GRAFANA_TOKEN"
""".strip(),
                encoding="utf-8",
            )

            result = run_cli("--config", str(config_path), "config", "validate")

        self.assertEqual(result.returncode, 2)
        self.assertIn("absolute HTTP(S) URL", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_config_reports_non_utf8_toml_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_bytes(b"[profiles.prod]\nname = \xff\n")

            result = run_cli("--config", str(config_path), "config", "validate")

        self.assertEqual(result.returncode, 2)
        self.assertIn("must be UTF-8", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class QueryTests(unittest.TestCase):
    def test_end_is_only_valid_with_absolute_start(self) -> None:
        invalid_modes = (
            ("--end", "2026-08-11T00:00:00Z"),
            ("--since", "1h", "--end", "2026-08-11T00:00:00Z"),
        )
        for extra_args in invalid_modes:
            with self.subTest(extra_args=extra_args):
                result = run_cli(
                    "query",
                    "--profile",
                    "prod",
                    *extra_args,
                    '{namespace="prod"}',
                )

                self.assertEqual(result.returncode, 2)
                self.assertIn("--end requires --start", result.stderr)

    def test_query_sends_expected_request_and_sorts_jsonl_across_streams(self) -> None:
        observed: dict[str, object] = {}

        class Response(BytesIO):
            status = 200

        def opener(request: Request, timeout: float) -> Response:
            observed["url"] = request.full_url
            observed["authorization"] = request.get_header("Authorization")
            observed["timeout"] = timeout
            return Response(
                json.dumps(
                    {
                        "status": "success",
                        "data": {
                            "resultType": "streams",
                            "result": [
                                {
                                    "stream": {"app": "api", "pod": "pod-a"},
                                    "values": [["1000000000", "older"]],
                                },
                                {
                                    "stream": {"app": "api", "pod": "pod-b"},
                                    "values": [["2000000000", "newer"]],
                                },
                            ],
                        },
                    }
                ).encode()
            )

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text(
                """
[profiles.prod]
grafana_url = "https://grafana.example.com"
datasource_uid = "loki uid"
token_env = "TEST_GRAFANA_TOKEN"
default_selector = '{{namespace="prod"}}'
""".strip(),
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()
            returncode = main(
                [
                    "--config",
                    str(config_path),
                    "query",
                    "--profile",
                    "prod",
                    "--start",
                    "2026-08-11T00:00:00Z",
                    "--end",
                    "2026-08-11T00:01:00Z",
                    "--limit",
                    "50",
                    "--output",
                    "jsonl",
                    '{app="api"} |= "252143"',
                ],
                environ={"TEST_GRAFANA_TOKEN": "secret-token"},
                stdout=stdout,
                stderr=stderr,
                opener=opener,
            )

        self.assertEqual(returncode, 0, stderr.getvalue())
        self.assertEqual(
            [json.loads(line) for line in stdout.getvalue().splitlines()],
            [
                {
                    "timestamp": "1970-01-01T00:00:02.000000000Z",
                    "labels": {"app": "api", "pod": "pod-b"},
                    "line": "newer",
                },
                {
                    "timestamp": "1970-01-01T00:00:01.000000000Z",
                    "labels": {"app": "api", "pod": "pod-a"},
                    "line": "older",
                },
            ],
        )
        parsed_url = urlparse(str(observed["url"]))
        self.assertEqual(
            parsed_url.path,
            "/api/datasources/proxy/uid/loki%20uid/loki/api/v1/query_range",
        )
        self.assertEqual(observed["authorization"], "Bearer secret-token")
        self.assertEqual(observed["timeout"], 30.0)
        params = parse_qs(parsed_url.query)
        self.assertEqual(params["query"], ['{app="api"} |= "252143"'])
        self.assertEqual(params["start"], ["1786406400000000000"])
        self.assertEqual(params["end"], ["1786406460000000000"])
        self.assertEqual(params["limit"], ["50"])
        self.assertEqual(params["direction"], ["backward"])

    def test_query_reports_auth_failure_with_stable_exit_code(self) -> None:
        def opener(request: Request, timeout: float) -> BytesIO:
            raise HTTPError(
                request.full_url,
                403,
                "secret-token forbidden",
                Message(),
                None,
            )

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text(
                """
[profiles.prod]
grafana_url = "https://grafana.example.com"
datasource_uid = "loki"
token_env = "TEST_GRAFANA_TOKEN"
""".strip(),
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            returncode = main(
                [
                    "--config",
                    str(config_path),
                    "query",
                    "--profile",
                    "prod",
                    '{namespace="prod"}',
                ],
                environ={"TEST_GRAFANA_TOKEN": "secret-token"},
                stdout=stdout,
                stderr=stderr,
                opener=opener,
            )

        self.assertEqual(returncode, 3)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("HTTP 403", stderr.getvalue())
        self.assertNotIn("secret-token", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_query_reads_complete_logql_from_stdin(self) -> None:
        observed_query: list[str] = []

        def opener(request: Request, timeout: float) -> BytesIO:
            observed_query.extend(parse_qs(urlparse(request.full_url).query)["query"])
            return BytesIO(
                b'{"status":"success","data":{"resultType":"streams","result":[]}}'
            )

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text(
                """
[profiles.prod]
grafana_url = "https://grafana.example.com"
datasource_uid = "loki"
token_env = "TEST_GRAFANA_TOKEN"
""".strip(),
                encoding="utf-8",
            )
            stdout = StringIO()
            returncode = main(
                [
                    "--config",
                    str(config_path),
                    "query",
                    "--profile",
                    "prod",
                    "--output",
                    "raw",
                    "-",
                ],
                environ={"TEST_GRAFANA_TOKEN": "token"},
                stdin=StringIO('{namespace="prod"} |= "252143"\n'),
                stdout=stdout,
                stderr=StringIO(),
                opener=opener,
                now=datetime(2026, 8, 11, tzinfo=UTC),
            )

        self.assertEqual(returncode, 0)
        self.assertEqual(observed_query, ['{namespace="prod"} |= "252143"'])
        self.assertEqual(stdout.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
