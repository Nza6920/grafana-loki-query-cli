from __future__ import annotations

from io import StringIO
from pathlib import Path
import json
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from loki_query.client import LogEntry, MetricSample, QueryResult  # noqa: E402
from loki_query.output import write_result  # noqa: E402


class OutputTests(unittest.TestCase):
    def test_raw_outputs_only_log_lines(self) -> None:
        output = StringIO()
        write_result(
            QueryResult(
                "log", [LogEntry(1_000_000_000, {"pod": "a"}, "first")]
            ),
            "raw",
            output,
        )
        self.assertEqual(output.getvalue(), "first\n")

    def test_jsonl_preserves_unicode_and_structured_fields(self) -> None:
        output = StringIO()
        write_result(
            QueryResult(
                "log",
                [LogEntry(1_000_000_001, {"namespace": "生产"}, "请求成功")],
            ),
            "jsonl",
            output,
        )
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "type": "log_entry",
                "timestamp": "1970-01-01T00:00:01.000000001Z",
                "labels": {"namespace": "生产"},
                "line": "请求成功",
            },
        )

    def test_empty_result_is_human_message_but_machine_output_is_empty(self) -> None:
        human = StringIO()
        raw = StringIO()
        jsonl = StringIO()

        write_result(QueryResult("log", []), "human", human)
        write_result(QueryResult("log", []), "raw", raw)
        write_result(QueryResult("log", []), "jsonl", jsonl)

        self.assertEqual(human.getvalue(), "No log entries found.\n")
        self.assertEqual(raw.getvalue(), "")
        self.assertEqual(jsonl.getvalue(), "")

    def test_metric_human_and_raw_output_preserve_value_and_utc_timestamp(self) -> None:
        result = QueryResult(
            "metric",
            [MetricSample(1_000_000_001, {"namespace": "生产"}, "0.10000000000000001")],
        )
        human, raw = StringIO(), StringIO()

        write_result(result, "human", human)
        write_result(result, "raw", raw)

        self.assertEqual(
            human.getvalue(),
            '1970-01-01T00:00:01.000000001Z {"namespace":"生产"} '
            "value=0.10000000000000001\n",
        )
        self.assertEqual(raw.getvalue(), "0.10000000000000001\n")


if __name__ == "__main__":
    unittest.main()
