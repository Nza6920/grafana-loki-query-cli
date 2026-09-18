from __future__ import annotations

import os
from contextlib import redirect_stderr, redirect_stdout
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from io import BytesIO, StringIO
import json
from datetime import UTC, datetime
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from urllib.error import HTTPError
from urllib.request import Request
from email.message import Message


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from loki_query.cli import main  # noqa: E402
from loki_query.config import default_config_path  # noqa: E402


def run_cli(
    *args: str,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    distribution_version: str | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    source_path = str(ROOT / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (source_path, environment.get("PYTHONPATH", "")) if part
    )
    environment.update(env or {})
    with tempfile.TemporaryDirectory() as directory:
        if distribution_version is not None:
            metadata_dir = Path(directory) / "loki_query.dist-info"
            metadata_dir.mkdir()
            (metadata_dir / "METADATA").write_text(
                f"Metadata-Version: 2.1\nName: loki-query\nVersion: {distribution_version}\n",
                encoding="utf-8",
            )
            environment["PYTHONPATH"] = os.pathsep.join(
                (directory, environment["PYTHONPATH"])
            )
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
    def test_help_returns_success_and_uses_only_injected_stdout(self) -> None:
        stdout, stderr = StringIO(), StringIO()
        ambient_stdout, ambient_stderr = StringIO(), StringIO()
        with redirect_stdout(ambient_stdout), redirect_stderr(ambient_stderr):
            result = main(["--help"], stdout=stdout, stderr=stderr, environ={})

        self.assertEqual(result, 0)
        self.assertIn("usage: loki-query", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(ambient_stdout.getvalue(), "")
        self.assertEqual(ambient_stderr.getvalue(), "")

    def test_version_returns_success_and_uses_injected_stdout(self) -> None:
        stdout, stderr, ambient_stdout = StringIO(), StringIO(), StringIO()
        with patch("loki_query.cli.version", return_value="0.2.0"), redirect_stdout(ambient_stdout):
            result = main(["--version"], stdout=stdout, stderr=stderr, environ={})

        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue(), "loki-query 0.2.0\n")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(ambient_stdout.getvalue(), "")

    def test_nested_help_and_parser_errors_use_injected_streams(self) -> None:
        cases: tuple[tuple[list[str], int, str], ...] = (
            (["query", "--help"], 0, "usage: loki-query query"),
            (["profiles", "list", "--help"], 0, "usage: loki-query profiles list"),
            (["config", "validate", "--help"], 0, "usage: loki-query config validate"),
            ([], 2, "the following arguments are required: command"),
            (["unknown"], 2, "invalid choice: 'unknown'"),
            (["query"], 2, "the following arguments are required: --profile, logql"),
            (["query", "--profile", "test", "--limit", "0", "{}"], 2, "limit must be between 1 and 5000"),
            (["config", "validate", "--unknown"], 2, "unrecognized arguments: --unknown"),
        )
        for argv, expected_code, message in cases:
            with self.subTest(argv=argv):
                stdout, stderr = StringIO(), StringIO()
                ambient_stdout, ambient_stderr = StringIO(), StringIO()
                with redirect_stdout(ambient_stdout), redirect_stderr(ambient_stderr):
                    result = main(argv, stdout=stdout, stderr=stderr, environ={})
                self.assertEqual(result, expected_code)
                destination, other = (stdout, stderr) if expected_code == 0 else (stderr, stdout)
                self.assertIn(message, destination.getvalue())
                self.assertEqual(other.getvalue(), "")
                self.assertEqual(ambient_stdout.getvalue(), "")
                self.assertEqual(ambient_stderr.getvalue(), "")

    def test_version_reports_installed_distribution_without_loading_config(self) -> None:
        result = run_cli("--version", distribution_version="0.2.0")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "loki-query 0.2.0")
        self.assertEqual(result.stderr, "")

    def test_version_reports_actionable_error_without_distribution_metadata(self) -> None:
        stderr = StringIO()
        with patch(
            "loki_query.cli.version",
            side_effect=PackageNotFoundError("loki-query"),
        ):
            result = main(["--version"], stderr=stderr)

        self.assertEqual(result, 2)
        self.assertIn("install loki-query", stderr.getvalue())

    def test_help_describes_public_commands(self) -> None:
        result = run_cli("--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("query", result.stdout)
        self.assertIn("profiles", result.stdout)
        self.assertIn("config", result.stdout)
        self.assertIn("--version", result.stdout)

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

    def test_config_path_honors_loki_query_config_before_xdg(self) -> None:
        result = run_cli(
            "config",
            "path",
            env={
                "LOKI_QUERY_CONFIG": "/tmp/loki-query-explicit.toml",
                "XDG_CONFIG_HOME": "/tmp/loki-query-test-config",
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "/tmp/loki-query-explicit.toml")

    def test_default_config_path_uses_appdata_on_windows(self) -> None:
        self.assertEqual(
            default_config_path(
                environ={"APPDATA": "C:/Users/example/AppData/Roaming"},
                platform="win32",
                home=Path("C:/Users/example"),
            ),
            Path("C:/Users/example/AppData/Roaming/loki-query/config.toml"),
        )

    def test_windows_falls_back_to_roaming_directory_under_home(self) -> None:
        self.assertEqual(
            default_config_path(
                environ={},
                platform="win32",
                home=Path("C:/Users/example"),
            ),
            Path("C:/Users/example/AppData/Roaming/loki-query/config.toml"),
        )

    def test_xdg_config_home_overrides_appdata_on_windows(self) -> None:
        self.assertEqual(
            default_config_path(
                environ={
                    "XDG_CONFIG_HOME": "/config/from-xdg",
                    "APPDATA": "C:/Users/example/AppData/Roaming",
                },
                platform="win32",
                home=Path("C:/Users/example"),
            ),
            Path("/config/from-xdg/loki-query/config.toml"),
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
    def test_expected_query_failures_return_codes_through_injected_stderr(self) -> None:
        def opener(request: Request, timeout: float) -> BytesIO:
            return BytesIO(b'{"status":"error","message":"query rejected"}')

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text(
                '[profiles.test]\ngrafana_url = "https://example.invalid"\n'
                'datasource_uid = "loki"\ntoken_env = "TEST_TOKEN"\n',
                encoding="utf-8",
            )
            cases: tuple[tuple[str, dict[str, str], int, str], ...] = (
                ("missing", {"TEST_TOKEN": "token"}, 2, "Unknown profile"),
                ("test", {}, 3, "token environment variable 'TEST_TOKEN' is not set"),
                ("test", {"TEST_TOKEN": "token"}, 4, "query rejected"),
            )
            for profile, environment, code, message in cases:
                with self.subTest(code=code):
                    stdout, stderr = StringIO(), StringIO()
                    result = main(
                        ["--config", str(config_path), "query", "--profile", profile, "{}"],
                        environ=environment, stdout=stdout, stderr=stderr, opener=opener,
                    )
                    self.assertEqual(result, code)
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertIn(message, stderr.getvalue())
                    self.assertNotIn("Traceback", stderr.getvalue())

    def test_unexpected_exceptions_propagate_from_query_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text(
                '[profiles.test]\ngrafana_url = "https://example.invalid"\n'
                'datasource_uid = "loki"\ntoken_env = "TEST_TOKEN"\n',
                encoding="utf-8",
            )
            for failure in (RuntimeError("unexpected defect"), SystemExit(7)):
                with self.subTest(failure=type(failure).__name__):
                    def opener(request: Request, timeout: float) -> BytesIO:
                        raise failure

                    stdout, stderr = StringIO(), StringIO()
                    with self.assertRaises(type(failure)) as raised:
                        main(
                            ["--config", str(config_path), "query", "--profile", "test", "{}"],
                            environ={"TEST_TOKEN": "token"}, stdout=stdout,
                            stderr=stderr, opener=opener,
                        )
                    self.assertIs(raised.exception, failure)
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertEqual(stderr.getvalue(), "")

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
                    "type": "log_entry",
                    "timestamp": "1970-01-01T00:00:02.000000000Z",
                    "labels": {"app": "api", "pod": "pod-b"},
                    "line": "newer",
                },
                {
                    "type": "log_entry",
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

    def test_metric_query_flattens_and_sorts_jsonl_samples(self) -> None:
        observed_url: list[str] = []

        def opener(request: Request, timeout: float) -> BytesIO:
            observed_url.append(request.full_url)
            return BytesIO(
                json.dumps(
                    {
                        "status": "success",
                        "data": {
                            "resultType": "matrix",
                            "result": [
                                {
                                    "metric": {"app": "api", "pod": "pod-a"},
                                    "values": [[1.000000001, "0.10000000000000001"]],
                                },
                                {
                                    "metric": {"app": "api", "pod": "pod-b"},
                                    "values": [[2, "3.5"]],
                                },
                            ],
                        },
                    }
                ).encode()
            )

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text(
                '[profiles.prod]\ngrafana_url = "https://grafana.example.com"\n'
                'datasource_uid = "loki"\ntoken_env = "TEST_GRAFANA_TOKEN"\n',
                encoding="utf-8",
            )
            stdout, stderr = StringIO(), StringIO()
            returncode = main(
                [
                    "--config",
                    str(config_path),
                    "query",
                    "--profile",
                    "prod",
                    "--query-type",
                    "metric",
                    "--step",
                    "30s",
                    "--output",
                    "jsonl",
                    'sum(rate({app="api"}[5m]))',
                ],
                environ={"TEST_GRAFANA_TOKEN": "token"},
                stdout=stdout,
                stderr=stderr,
                opener=opener,
                now=datetime(2026, 8, 11, tzinfo=UTC),
            )

        self.assertEqual(returncode, 0, stderr.getvalue())
        self.assertEqual(
            [json.loads(line) for line in stdout.getvalue().splitlines()],
            [
                {
                    "type": "metric_sample",
                    "timestamp": "1970-01-01T00:00:02.000000000Z",
                    "labels": {"app": "api", "pod": "pod-b"},
                    "value": "3.5",
                },
                {
                    "type": "metric_sample",
                    "timestamp": "1970-01-01T00:00:01.000000001Z",
                    "labels": {"app": "api", "pod": "pod-a"},
                    "value": "0.10000000000000001",
                },
            ],
        )
        params = parse_qs(urlparse(observed_url[0]).query)
        self.assertEqual(params["step"], ["30s"])
        self.assertNotIn("limit", params)
        self.assertNotIn("direction", params)

    def test_metric_query_supports_human_and_raw_output(self) -> None:
        def opener(request: Request, timeout: float) -> BytesIO:
            return BytesIO(
                b'{"status":"success","data":{"resultType":"matrix",'
                b'"result":[{"metric":{"namespace":"production"},'
                b'"values":[[1.000000001,"0.10000000000000001"]]}]}}'
            )

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text(
                '[profiles.prod]\ngrafana_url = "https://grafana.example.com"\n'
                'datasource_uid = "loki"\ntoken_env = "TEST_GRAFANA_TOKEN"\n',
                encoding="utf-8",
            )
            outputs: dict[str, str] = {}
            for output_format in ("human", "raw"):
                stdout, stderr = StringIO(), StringIO()
                returncode = main(
                    [
                        "--config",
                        str(config_path),
                        "query",
                        "--profile",
                        "prod",
                        "--query-type",
                        "metric",
                        "--output",
                        output_format,
                        "{}",
                    ],
                    environ={"TEST_GRAFANA_TOKEN": "token"},
                    stdout=stdout,
                    stderr=stderr,
                    opener=opener,
                )
                self.assertEqual(returncode, 0, stderr.getvalue())
                outputs[output_format] = stdout.getvalue()

        self.assertEqual(
            outputs["human"],
            '1970-01-01T00:00:01.000000001Z {"namespace":"production"} '
            "value=0.10000000000000001\n",
        )
        self.assertEqual(outputs["raw"], "0.10000000000000001\n")

    def test_query_type_rejects_incompatible_flags_before_network_access(self) -> None:
        calls = 0

        def opener(request: Request, timeout: float) -> BytesIO:
            nonlocal calls
            calls += 1
            return BytesIO(
                b'{"status":"success","data":{"resultType":"streams","result":[]}}'
            )

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text(
                '[profiles.prod]\ngrafana_url = "https://grafana.example.com"\n'
                'datasource_uid = "loki"\ntoken_env = "TEST_GRAFANA_TOKEN"\n',
                encoding="utf-8",
            )
            cases = (
                (["--query-type", "metric", "--limit", "1"], "--limit"),
                (
                    ["--query-type", "metric", "--direction", "forward"],
                    "--direction",
                ),
                (["--query-type", "log", "--step", "15"], "--step"),
            )
            for flags, invalid_flag in cases:
                with self.subTest(flags=flags):
                    stdout, stderr = StringIO(), StringIO()
                    returncode = main(
                        [
                            "--config",
                            str(config_path),
                            "query",
                            "--profile",
                            "prod",
                            *flags,
                            "{}",
                        ],
                        environ={"TEST_GRAFANA_TOKEN": "token"},
                        stdout=stdout,
                        stderr=stderr,
                        opener=opener,
                    )
                    self.assertEqual(returncode, 2)
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertIn(invalid_flag, stderr.getvalue())

        self.assertEqual(calls, 0)

    def test_declared_query_type_rejects_mismatched_or_unknown_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text(
                '[profiles.prod]\ngrafana_url = "https://grafana.example.com"\n'
                'datasource_uid = "loki"\ntoken_env = "TEST_GRAFANA_TOKEN"\n',
                encoding="utf-8",
            )
            cases = (
                ("log", "matrix", "streams"),
                ("metric", "streams", "matrix"),
                ("metric", "vector", "matrix"),
            )
            for query_type, result_type, expected in cases:
                with self.subTest(query_type=query_type, result_type=result_type):
                    def opener(request: Request, timeout: float) -> BytesIO:
                        return BytesIO(
                            json.dumps(
                                {
                                    "status": "success",
                                    "data": {"resultType": result_type, "result": []},
                                }
                            ).encode()
                        )

                    stdout, stderr = StringIO(), StringIO()
                    returncode = main(
                        [
                            "--config",
                            str(config_path),
                            "query",
                            "--profile",
                            "prod",
                            "--query-type",
                            query_type,
                            "{}",
                        ],
                        environ={"TEST_GRAFANA_TOKEN": "token"},
                        stdout=stdout,
                        stderr=stderr,
                        opener=opener,
                    )
                    self.assertEqual(returncode, 4)
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertIn(expected, stderr.getvalue())

    def test_empty_metric_result_has_type_specific_machine_and_human_output(self) -> None:
        def opener(request: Request, timeout: float) -> BytesIO:
            return BytesIO(
                b'{"status":"success","data":{"resultType":"matrix","result":[]}}'
            )

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text(
                '[profiles.prod]\ngrafana_url = "https://grafana.example.com"\n'
                'datasource_uid = "loki"\ntoken_env = "TEST_GRAFANA_TOKEN"\n',
                encoding="utf-8",
            )
            outputs: dict[str, str] = {}
            for output_format in ("human", "raw", "jsonl"):
                stdout, stderr = StringIO(), StringIO()
                returncode = main(
                    [
                        "--config",
                        str(config_path),
                        "query",
                        "--profile",
                        "prod",
                        "--query-type",
                        "metric",
                        "--output",
                        output_format,
                        "{}",
                    ],
                    environ={"TEST_GRAFANA_TOKEN": "token"},
                    stdout=stdout,
                    stderr=stderr,
                    opener=opener,
                )
                self.assertEqual(returncode, 0, stderr.getvalue())
                outputs[output_format] = stdout.getvalue()

        self.assertEqual(outputs["human"], "No metric samples found.\n")
        self.assertEqual(outputs["raw"], "")
        self.assertEqual(outputs["jsonl"], "")

    def test_partial_stream_and_matrix_results_emit_valid_records_and_one_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text(
                '[profiles.prod]\ngrafana_url = "https://grafana.example.com"\n'
                'datasource_uid = "loki"\ntoken_env = "TEST_GRAFANA_TOKEN"\n',
                encoding="utf-8",
            )
            cases = (
                (
                    "log",
                    "streams",
                    [
                        {
                            "stream": {"app": "safe"},
                            "values": [["2000000000", "valid line"], ["bad", "SECRET_LINE"]],
                        },
                        {"stream": {"SECRET_LABEL": "hidden"}, "values": "bad"},
                    ],
                    "valid line",
                ),
                (
                    "metric",
                    "matrix",
                    [
                        {
                            "metric": {"app": "safe"},
                            "values": [[2, "valid-value"], ["bad", "SECRET_VALUE"]],
                        },
                        {"metric": {"SECRET_LABEL": "hidden"}, "values": "bad"},
                    ],
                    "valid-value",
                ),
            )
            for query_type, result_type, result, valid_payload in cases:
                with self.subTest(query_type=query_type):
                    def opener(request: Request, timeout: float) -> BytesIO:
                        return BytesIO(
                            json.dumps(
                                {
                                    "status": "success",
                                    "data": {
                                        "resultType": result_type,
                                        "result": result,
                                    },
                                }
                            ).encode()
                        )

                    stdout, stderr = StringIO(), StringIO()
                    returncode = main(
                        [
                            "--config",
                            str(config_path),
                            "query",
                            "--profile",
                            "prod",
                            "--query-type",
                            query_type,
                            "--output",
                            "jsonl",
                            "{}",
                        ],
                        environ={"TEST_GRAFANA_TOKEN": "token"},
                        stdout=stdout,
                        stderr=stderr,
                        opener=opener,
                    )

                    self.assertEqual(returncode, 4)
                    self.assertIn(valid_payload, stdout.getvalue())
                    warning_lines = stderr.getvalue().splitlines()
                    self.assertEqual(len(warning_lines), 1)
                    self.assertIn("incomplete results", warning_lines[0])
                    self.assertIn("skipped 1 series", warning_lines[0])
                    self.assertNotIn("SECRET", stderr.getvalue())
                    self.assertNotIn("hidden", stderr.getvalue())

    def test_wholly_malformed_success_has_empty_stdout_and_query_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text(
                '[profiles.prod]\ngrafana_url = "https://grafana.example.com"\n'
                'datasource_uid = "loki"\ntoken_env = "TEST_GRAFANA_TOKEN"\n',
                encoding="utf-8",
            )
            cases = (
                (
                    "metric",
                    "matrix",
                    [{"metric": {"SECRET_LABEL": "hidden"}, "values": "bad"}],
                ),
                (
                    "log",
                    "streams",
                    [{"stream": {"SECRET_LABEL": "hidden"}, "values": "bad"}],
                ),
                ("metric", "matrix", {"SECRET_VALUE": "hidden"}),
                ("log", "streams", {"SECRET_VALUE": "hidden"}),
                (
                    "metric",
                    "matrix",
                    [{"metric": {}, "values": [[1e100, "SECRET_VALUE"]]}],
                ),
            )
            for query_type, result_type, result in cases:
                with self.subTest(query_type=query_type, result=result):
                    def opener(request: Request, timeout: float) -> BytesIO:
                        return BytesIO(
                            json.dumps(
                                {
                                    "status": "success",
                                    "data": {
                                        "resultType": result_type,
                                        "result": result,
                                    },
                                }
                            ).encode()
                        )

                    stdout, stderr = StringIO(), StringIO()
                    returncode = main(
                        [
                            "--config",
                            str(config_path),
                            "query",
                            "--profile",
                            "prod",
                            "--query-type",
                            query_type,
                            "{}",
                        ],
                        environ={"TEST_GRAFANA_TOKEN": "token"},
                        stdout=stdout,
                        stderr=stderr,
                        opener=opener,
                    )

                    self.assertEqual(returncode, 4)
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertEqual(len(stderr.getvalue().splitlines()), 1)
                    self.assertIn("incomplete results", stderr.getvalue())
                    self.assertNotIn("SECRET", stderr.getvalue())
                    self.assertNotIn("hidden", stderr.getvalue())

    def test_query_uses_injected_sleeper_for_retry_policy(self) -> None:
        attempts = 0
        sleeps: list[float] = []
        observed_url: list[str] = []

        def opener(request: Request, timeout: float) -> BytesIO:
            nonlocal attempts
            attempts += 1
            observed_url.append(request.full_url)
            if attempts < 3:
                headers = Message()
                headers["Retry-After"] = "0"
                raise HTTPError(request.full_url, 503, "unavailable", headers, None)
            return BytesIO(
                b'{"status":"success","data":{"resultType":"matrix","result":[]}}'
            )

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text(
                '[profiles.prod]\ngrafana_url = "https://grafana.example.com"\n'
                'datasource_uid = "loki"\ntoken_env = "TEST_GRAFANA_TOKEN"\n',
                encoding="utf-8",
            )
            returncode = main(
                [
                    "--config",
                    str(config_path),
                    "query",
                    "--profile",
                    "prod",
                    "--query-type",
                    "metric",
                    "--step",
                    "15",
                    "--output",
                    "raw",
                    "{}",
                ],
                environ={"TEST_GRAFANA_TOKEN": "token"},
                stdout=StringIO(),
                stderr=StringIO(),
                opener=opener,
                sleeper=sleeps.append,
            )

        self.assertEqual(returncode, 0)
        self.assertEqual(attempts, 3)
        self.assertEqual(sleeps, [0.0, 0.0])
        self.assertEqual(
            parse_qs(urlparse(observed_url[-1]).query)["step"], ["15"]
        )

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
