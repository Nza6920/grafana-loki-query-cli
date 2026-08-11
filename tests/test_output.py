from __future__ import annotations

from io import StringIO
from pathlib import Path
import json
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from loki_query.client import LogEntry  # noqa: E402
from loki_query.output import write_entries  # noqa: E402


class OutputTests(unittest.TestCase):
    def test_raw_outputs_only_log_lines(self) -> None:
        output = StringIO()
        write_entries(
            [LogEntry(1_000_000_000, {"pod": "a"}, "first")], "raw", output
        )
        self.assertEqual(output.getvalue(), "first\n")

    def test_jsonl_preserves_unicode_and_structured_fields(self) -> None:
        output = StringIO()
        write_entries(
            [LogEntry(1_000_000_001, {"namespace": "生产"}, "请求成功")],
            "jsonl",
            output,
        )
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "timestamp": "1970-01-01T00:00:01.000000001Z",
                "labels": {"namespace": "生产"},
                "line": "请求成功",
            },
        )

    def test_empty_result_is_human_message_but_machine_output_is_empty(self) -> None:
        human = StringIO()
        raw = StringIO()
        jsonl = StringIO()

        write_entries([], "human", human)
        write_entries([], "raw", raw)
        write_entries([], "jsonl", jsonl)

        self.assertEqual(human.getvalue(), "No log entries found.\n")
        self.assertEqual(raw.getvalue(), "")
        self.assertEqual(jsonl.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
