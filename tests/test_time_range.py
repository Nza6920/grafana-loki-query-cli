from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from loki_query.time_range import (  # noqa: E402
    TimeRangeError,
    resolve_time_range,
)


class TimeRangeTests(unittest.TestCase):
    def test_default_range_is_the_previous_fifteen_minutes(self) -> None:
        start, end = resolve_time_range(
            since=None,
            start=None,
            end=None,
            now=datetime(2026, 8, 11, 8, 0, tzinfo=UTC),
        )

        self.assertEqual(start, 1_786_434_300_000_000_000)
        self.assertEqual(end, 1_786_435_200_000_000_000)

    def test_absolute_range_preserves_fractional_seconds(self) -> None:
        start, end = resolve_time_range(
            since=None,
            start="2026-08-11T08:00:00.123456+08:00",
            end="2026-08-11T00:01:00.654321Z",
            now=datetime(2000, 1, 1, tzinfo=UTC),
        )

        self.assertEqual(start, 1_786_406_400_123_456_000)
        self.assertEqual(end, 1_786_406_460_654_321_000)

    def test_rejects_a_non_positive_range(self) -> None:
        with self.assertRaisesRegex(TimeRangeError, "start must be earlier"):
            resolve_time_range(
                since=None,
                start="2026-08-11T00:01:00Z",
                end="2026-08-11T00:00:00Z",
                now=datetime(2000, 1, 1, tzinfo=UTC),
            )


if __name__ == "__main__":
    unittest.main()
