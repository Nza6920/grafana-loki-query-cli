from __future__ import annotations

from email.message import Message
from io import BytesIO
from pathlib import Path
import json
import sys
import unittest
from urllib.error import HTTPError
from urllib.request import Request


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from loki_query.client import QueryError, query_range  # noqa: E402
from loki_query.config import Profile  # noqa: E402


class Response(BytesIO):
    status = 200


PROFILE = Profile(
    name="prod",
    grafana_url="https://grafana.example.com",
    datasource_uid="loki",
    token_env="GRAFANA_TOKEN",
)


def success_response() -> Response:
    return Response(
        json.dumps(
            {
                "status": "success",
                "data": {"resultType": "streams", "result": []},
            }
        ).encode()
    )


class RetryTests(unittest.TestCase):
    def test_retries_transient_http_failures_twice(self) -> None:
        attempts = 0
        sleeps: list[float] = []

        def opener(request: Request, timeout: float) -> Response:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                headers = Message()
                headers["Retry-After"] = "0"
                raise HTTPError(request.full_url, 503, "unavailable", headers, None)
            return success_response()

        entries = query_range(
            profile=PROFILE,
            token="secret-token",
            query='{namespace="prod"}',
            start_ns=1,
            end_ns=2,
            limit=1,
            timeout=30,
            opener=opener,
            sleeper=sleeps.append,
        )

        self.assertEqual(entries, [])
        self.assertEqual(attempts, 3)
        self.assertEqual(sleeps, [0.0, 0.0])

    def test_auth_failure_is_not_retried_and_token_is_redacted(self) -> None:
        attempts = 0

        def opener(request: Request, timeout: float) -> Response:
            nonlocal attempts
            attempts += 1
            raise HTTPError(
                request.full_url,
                401,
                "secret-token is invalid",
                Message(),
                None,
            )

        with self.assertRaises(QueryError) as raised:
            query_range(
                profile=PROFILE,
                token="secret-token",
                query='{namespace="prod"}',
                start_ns=1,
                end_ns=2,
                limit=1,
                timeout=30,
                opener=opener,
                sleeper=lambda _: None,
            )

        self.assertEqual(attempts, 1)
        self.assertIn("HTTP 401", str(raised.exception))
        self.assertNotIn("secret-token", str(raised.exception))

    def test_loki_api_error_redacts_token_from_message(self) -> None:
        def opener(request: Request, timeout: float) -> Response:
            return Response(
                b'{"status":"error","message":"secret-token is not allowed"}'
            )

        with self.assertRaises(QueryError) as raised:
            query_range(
                profile=PROFILE,
                token="secret-token",
                query='{namespace="prod"}',
                start_ns=1,
                end_ns=2,
                limit=1,
                timeout=30,
                opener=opener,
                sleeper=lambda _: None,
            )

        self.assertIn("[REDACTED]", str(raised.exception))
        self.assertNotIn("secret-token", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
