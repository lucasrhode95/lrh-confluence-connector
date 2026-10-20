"""Offline checks for CQL date conversion and minute-inclusive boundaries."""

from __future__ import annotations

import unittest

from confluence_connector.domain.errors import InvalidRequest
from confluence_connector.service.cql_dates import cql_date_literal, normalize_scope_time


class CqlDateChecks(unittest.TestCase):
    def test_offset_timestamps_floor_lower_and_ceil_upper_to_local_minute(self) -> None:
        value = normalize_scope_time(
            "created_after", "2026-10-19T12:00:59.999-03:00"
        )
        assert value is not None
        self.assertEqual(value, "2026-10-19T15:00:59.999000+00:00")
        self.assertEqual(
            cql_date_literal("created_after", value, "after", "UTC+05:30"),
            "2026/10/19 20:30",
        )
        self.assertEqual(
            cql_date_literal("created_before", value, "before", "UTC+05:30"),
            "2026/10/19 20:31",
        )

    def test_naive_timestamp_is_interpreted_as_utc(self) -> None:
        value = normalize_scope_time("modified_after", "2026-10-19T12:00:30")
        assert value is not None
        self.assertEqual(value, "2026-10-19T12:00:30+00:00")
        self.assertEqual(
            cql_date_literal("modified_after", value, "after", "UTC+00:00"),
            "2026/10/19 12:00",
        )

    def test_date_only_is_a_calendar_day_without_timezone_configuration(self) -> None:
        self.assertEqual(
            cql_date_literal("created_before", "2026-10-19", "before", None),
            "2026/10/19",
        )

    def test_timestamp_without_timezone_configuration_is_rejected(self) -> None:
        with self.assertRaisesRegex(InvalidRequest, "status --check"):
            cql_date_literal(
                "created_after", "2026-10-19T12:00:00+00:00", "after", None
            )

    def test_ambiguous_fall_back_hour_is_widened_for_conservative_bounds(self) -> None:
        value = normalize_scope_time("modified_after", "2026-11-01T05:30:45Z")
        assert value is not None
        self.assertEqual(
            cql_date_literal("modified_after", value, "after", "America/New_York"),
            "2026/11/01 00:30",
        )
        self.assertEqual(
            cql_date_literal("modified_before", value, "before", "America/New_York"),
            "2026/11/01 02:31",
        )

    def test_nonexistent_spring_forward_minute_moves_to_next_valid_minute(self) -> None:
        value = normalize_scope_time("modified_before", "2026-03-08T06:59:30Z")
        assert value is not None
        self.assertEqual(
            cql_date_literal("modified_before", value, "before", "America/New_York"),
            "2026/03/08 03:00",
        )


if __name__ == "__main__":
    unittest.main()
