"""Hash-pinned US-session calendar used by market-data validation.

The module intentionally contains no rule that attempts to *calculate* an
exchange calendar.  Weekday arithmetic is not authoritative: holidays,
unscheduled closures and early closes must arrive in a reviewed calendar
snapshot whose content hash is pinned by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import json
import re
from typing import Any, Dict, Iterable, Optional, Tuple
from zoneinfo import ZoneInfo


NEW_YORK = ZoneInfo("America/New_York")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class CalendarError(ValueError):
    """The frozen calendar is missing, ambiguous, stale, or malformed."""


def _require_ny_datetime(name: str, value: object) -> datetime:
    if type(value) is not datetime:
        raise CalendarError("%s must be an exact datetime" % name)
    if value.tzinfo is None or value.utcoffset() is None:
        raise CalendarError("%s must be timezone-aware" % name)
    if getattr(value.tzinfo, "key", None) != "America/New_York":
        raise CalendarError("%s must use America/New_York ZoneInfo" % name)
    return value


@dataclass(frozen=True)
class FrozenSession:
    """One reviewed exchange session, including an optional early close."""

    session_date: date
    open_at: datetime
    close_at: datetime

    def __post_init__(self) -> None:
        if type(self.session_date) is not date:
            raise CalendarError("session_date must be an exact date")
        opened = _require_ny_datetime("open_at", self.open_at)
        closed = _require_ny_datetime("close_at", self.close_at)
        if opened.date() != self.session_date or closed.date() != self.session_date:
            raise CalendarError("session timestamps must match session_date")
        if (opened.hour, opened.minute, opened.second, opened.microsecond) != (9, 30, 0, 0):
            raise CalendarError("US regular sessions must open at 09:30 America/New_York")
        if closed <= opened:
            raise CalendarError("session close must be after open")
        if closed.hour > 16 or (closed.hour == 16 and closed.minute > 0):
            raise CalendarError("session close cannot extend beyond 16:00")
        if closed.second or closed.microsecond:
            raise CalendarError("session close must be minute-aligned")
        if (closed - opened) % timedelta(minutes=15):
            raise CalendarError("session length must be divisible into 15-minute bars")

    def payload(self) -> Dict[str, str]:
        return {
            "close_at": self.close_at.isoformat(),
            "open_at": self.open_at.isoformat(),
            "session_date": self.session_date.isoformat(),
        }

    def bar_starts(self) -> Tuple[datetime, ...]:
        count = int((self.close_at - self.open_at) / timedelta(minutes=15))
        return tuple(self.open_at + timedelta(minutes=15 * index) for index in range(count))


def calendar_payload_sha256(
    *,
    source_revision: str,
    covered_from: date,
    covered_through: date,
    sessions: Iterable[FrozenSession],
) -> str:
    """Hash the exact immutable payload expected by ``FrozenExchangeCalendar``."""

    payload = {
        "covered_from": covered_from.isoformat(),
        "covered_through": covered_through.isoformat(),
        "sessions": [session.payload() for session in sessions],
        "source_revision": source_revision,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded + b"\n").hexdigest()


@dataclass(frozen=True)
class FrozenExchangeCalendar:
    """An immutable, externally pinned calendar snapshot.

    ``covered_from``/``covered_through`` describe reviewed calendar coverage,
    including dates on which the exchange is closed.  Absence of a session is
    therefore meaningful only inside that interval.  The caller must pin the
    expected SHA-256 from an independently reviewed artifact.
    """

    source_revision: str
    covered_from: date
    covered_through: date
    sessions: Tuple[FrozenSession, ...]
    expected_sha256: str

    def __post_init__(self) -> None:
        if type(self.source_revision) is not str or not self.source_revision.strip():
            raise CalendarError("source_revision is required")
        if type(self.covered_from) is not date or type(self.covered_through) is not date:
            raise CalendarError("calendar coverage must use exact date values")
        if self.covered_through < self.covered_from:
            raise CalendarError("calendar coverage is inverted")
        if type(self.sessions) is not tuple or not self.sessions:
            raise CalendarError("frozen calendar must contain at least one session")
        if not _SHA256_RE.fullmatch(self.expected_sha256):
            raise CalendarError("expected_sha256 must be a lowercase SHA-256")

        previous = None  # type: Optional[date]
        for session in self.sessions:
            if type(session) is not FrozenSession:
                raise CalendarError("sessions must contain exact FrozenSession values")
            if not self.covered_from <= session.session_date <= self.covered_through:
                raise CalendarError("session lies outside frozen coverage")
            if previous is not None and session.session_date <= previous:
                raise CalendarError("sessions must be unique and strictly chronological")
            previous = session.session_date

        actual = calendar_payload_sha256(
            source_revision=self.source_revision,
            covered_from=self.covered_from,
            covered_through=self.covered_through,
            sessions=self.sessions,
        )
        if actual != self.expected_sha256:
            raise CalendarError("frozen calendar SHA-256 mismatch")

    @property
    def sha256(self) -> str:
        return self.expected_sha256

    def assert_covered(self, value: date) -> None:
        if type(value) is not date:
            raise CalendarError("coverage lookup requires an exact date")
        if not self.covered_from <= value <= self.covered_through:
            raise CalendarError("date is outside the frozen calendar coverage")

    def session_on(self, value: date) -> Optional[FrozenSession]:
        self.assert_covered(value)
        matches = tuple(session for session in self.sessions if session.session_date == value)
        if len(matches) > 1:
            raise CalendarError("ambiguous duplicate session")
        return matches[0] if matches else None

    def sessions_between(self, first: date, last: date) -> Tuple[FrozenSession, ...]:
        self.assert_covered(first)
        self.assert_covered(last)
        if last < first:
            raise CalendarError("calendar range is inverted")
        return tuple(
            session for session in self.sessions if first <= session.session_date <= last
        )

    def active_session(self, at: datetime) -> Optional[FrozenSession]:
        if type(at) is not datetime or at.tzinfo is None or at.utcoffset() is None:
            raise CalendarError("calendar clock must be timezone-aware")
        eastern = at.astimezone(NEW_YORK)
        session = self.session_on(eastern.date())
        if session is not None and session.open_at <= eastern < session.close_at:
            return session
        return None

    def latest_completed_session(self, at: datetime) -> FrozenSession:
        if type(at) is not datetime or at.tzinfo is None or at.utcoffset() is None:
            raise CalendarError("calendar clock must be timezone-aware")
        eastern = at.astimezone(NEW_YORK)
        self.assert_covered(eastern.date())
        completed = tuple(session for session in self.sessions if session.close_at <= eastern)
        if not completed:
            raise CalendarError("no completed session exists in frozen coverage")
        return completed[-1]

    def latest_completed_bar_end(self, at: datetime) -> datetime:
        """Return the last 15-minute RTH boundary completed at ``at``."""

        if type(at) is not datetime or at.tzinfo is None or at.utcoffset() is None:
            raise CalendarError("calendar clock must be timezone-aware")
        eastern = at.astimezone(NEW_YORK)
        self.assert_covered(eastern.date())
        active = self.active_session(eastern)
        if active is not None:
            elapsed = eastern - active.open_at
            complete_count = int(elapsed // timedelta(minutes=15))
            if complete_count:
                return active.open_at + timedelta(minutes=15 * complete_count)
        return self.latest_completed_session(eastern).close_at

    def expected_bar_starts(self, first: datetime, last_end: datetime) -> Tuple[datetime, ...]:
        """Return every frozen 15-minute slot from ``first`` through ``last_end``."""

        if (
            type(first) is not datetime
            or first.tzinfo is None
            or first.utcoffset() is None
            or type(last_end) is not datetime
            or last_end.tzinfo is None
            or last_end.utcoffset() is None
        ):
            raise CalendarError("bar range timestamps must be timezone-aware")
        first_ny = first.astimezone(NEW_YORK)
        last_ny = last_end.astimezone(NEW_YORK)
        if last_ny <= first_ny:
            raise CalendarError("bar range is empty or inverted")
        sessions = self.sessions_between(first_ny.date(), last_ny.date())
        all_starts = tuple(start for session in sessions for start in session.bar_starts())
        selected = tuple(
            start
            for start in all_starts
            if start >= first_ny and start + timedelta(minutes=15) <= last_ny
        )
        if not selected or selected[0] != first_ny:
            raise CalendarError("first bar is not a frozen RTH slot")
        if selected[-1] + timedelta(minutes=15) != last_ny:
            raise CalendarError("last bar is not a frozen RTH slot")
        return selected


__all__ = [
    "CalendarError",
    "FrozenExchangeCalendar",
    "FrozenSession",
    "NEW_YORK",
    "calendar_payload_sha256",
]
