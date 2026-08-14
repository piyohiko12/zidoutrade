from datetime import date, datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from zidoutrade.exchange_calendar import (
    CalendarError,
    FrozenExchangeCalendar,
    FrozenSession,
    calendar_payload_sha256,
)


NY = ZoneInfo("America/New_York")


def make_session(day, close_hour=16):
    return FrozenSession(
        day,
        datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY),
        datetime(day.year, day.month, day.day, close_hour, 0, tzinfo=NY),
    )


def make_calendar(days, covered_from=None, covered_through=None):
    sessions = tuple(make_session(day) for day in days)
    first = covered_from or days[0]
    last = covered_through or days[-1]
    digest = calendar_payload_sha256(
        source_revision="synthetic-test-v1",
        covered_from=first,
        covered_through=last,
        sessions=sessions,
    )
    return FrozenExchangeCalendar(
        "synthetic-test-v1", first, last, sessions, digest
    )


class FrozenCalendarTests(unittest.TestCase):
    def test_hash_mismatch_fails_closed(self):
        day = date(2026, 8, 10)
        with self.assertRaisesRegex(CalendarError, "SHA-256 mismatch"):
            FrozenExchangeCalendar(
                "synthetic-test-v1",
                day,
                day,
                (make_session(day),),
                "0" * 64,
            )

    def test_timezone_must_be_named_new_york_zone(self):
        day = date(2026, 8, 10)
        with self.assertRaisesRegex(CalendarError, "America/New_York"):
            FrozenSession(
                day,
                datetime(2026, 8, 10, 13, 30, tzinfo=ZoneInfo("UTC")),
                datetime(2026, 8, 10, 20, 0, tzinfo=ZoneInfo("UTC")),
            )

    def test_holiday_absence_is_authoritative_inside_coverage(self):
        friday = date(2026, 7, 3)
        monday = date(2026, 7, 6)
        calendar = make_calendar(
            [friday, monday],
            covered_from=friday,
            covered_through=monday,
        )
        self.assertIsNone(calendar.session_on(date(2026, 7, 4)))
        self.assertEqual(
            calendar.latest_completed_session(
                datetime(2026, 7, 5, 12, 0, tzinfo=NY)
            ).session_date,
            friday,
        )

    def test_early_close_changes_last_bar(self):
        day = date(2026, 11, 27)
        session = make_session(day, close_hour=13)
        digest = calendar_payload_sha256(
            source_revision="synthetic-early-close",
            covered_from=day,
            covered_through=day,
            sessions=(session,),
        )
        calendar = FrozenExchangeCalendar(
            "synthetic-early-close", day, day, (session,), digest
        )
        self.assertEqual(
            calendar.latest_completed_bar_end(
                datetime(2026, 11, 27, 14, 0, tzinfo=NY)
            ),
            datetime(2026, 11, 27, 13, 0, tzinfo=NY),
        )

    def test_completed_bar_boundary_uses_only_elapsed_intervals(self):
        previous = date(2026, 8, 10)
        current = date(2026, 8, 11)
        calendar = make_calendar([previous, current])
        self.assertEqual(
            calendar.latest_completed_bar_end(
                datetime(2026, 8, 11, 9, 44, 59, tzinfo=NY)
            ),
            datetime(2026, 8, 10, 16, 0, tzinfo=NY),
        )
        self.assertEqual(
            calendar.latest_completed_bar_end(
                datetime(2026, 8, 11, 9, 45, 0, tzinfo=NY)
            ),
            datetime(2026, 8, 11, 9, 45, tzinfo=NY),
        )

    def test_outside_coverage_is_not_inferred(self):
        day = date(2026, 8, 10)
        calendar = make_calendar([day])
        with self.assertRaisesRegex(CalendarError, "outside"):
            calendar.session_on(day + timedelta(days=1))


if __name__ == "__main__":
    unittest.main()
