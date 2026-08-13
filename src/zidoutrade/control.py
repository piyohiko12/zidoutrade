"""Local, hash-chained UI control for next-session user selection.

This controller can save a draft, validate it, and arm it in separate calls.
It cannot lock a session early, activate a broker, or place an order.
"""

from __future__ import annotations

from datetime import date, datetime
import hmac
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence

from .selection import (
    PresentedCandidate,
    SelectionError,
    SelectionRecord,
    SelectionState,
    SelectionStore,
    SelectionWorkflow,
)


ARM_CONFIRMATION = "ARM_NEXT_SESSION"


class SelectionController:
    """Persist explicit UI transitions without granting trading authority."""

    def __init__(
        self,
        *,
        runtime_root: Path,
        repository_root: Path,
        candidates_provider: Callable[[], Sequence[PresentedCandidate]],
        clock: Callable[[], datetime],
    ) -> None:
        if not callable(candidates_provider) or not callable(clock):
            raise TypeError("providers must be callable")
        self.store = SelectionStore(runtime_root, repository_root=repository_root)
        self._candidates_provider = candidates_provider
        self._clock = clock

    def _now(self) -> datetime:
        value = self._clock()
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise SelectionError("CONTROL_CLOCK_MUST_BE_TIMEZONE_AWARE")
        return value

    @staticmethod
    def _response(record: SelectionRecord) -> Dict[str, object]:
        return {
            "message": "Selection revision saved.",
            "record_sha256": record.sha256,
            "saved": True,
            "selected_symbol": record.selected_symbol,
            "state": record.state.value,
            "target_session": record.target_session,
        }

    def draft(self, selected_symbol: Optional[str], target_session: str) -> Dict[str, object]:
        now = self._now()
        current = self.store.load_latest(required=False)
        if current is None:
            record = SelectionWorkflow.new_draft(
                target_session=target_session,
                presented_candidates=tuple(self._candidates_provider()),
                selected_symbol=selected_symbol,
                now=now,
            )
        else:
            record = SelectionWorkflow.revise(
                current,
                selected_symbol=selected_symbol,
                current_session_date=now.date().isoformat(),
                target_session=target_session,
                now=now,
            )
        self.store.save(record)
        return self._response(record)

    def _expected_latest(self, expected_sha256: str) -> SelectionRecord:
        if type(expected_sha256) is not str or len(expected_sha256) != 64:
            raise SelectionError("INVALID_EXPECTED_SELECTION_HASH")
        record = self.store.load_latest()
        if not hmac.compare_digest(record.sha256, expected_sha256):
            raise SelectionError("STALE_SELECTION_REVISION")
        return record

    def validate(self, expected_sha256: str) -> Dict[str, object]:
        record = SelectionWorkflow.validate(
            self._expected_latest(expected_sha256), now=self._now()
        )
        self.store.save(record)
        return self._response(record)

    def arm(self, expected_sha256: str, confirmation: str) -> Dict[str, object]:
        if type(confirmation) is not str or not hmac.compare_digest(
            confirmation, ARM_CONFIRMATION
        ):
            raise SelectionError("EXACT_ARM_CONFIRMATION_REQUIRED")
        record = SelectionWorkflow.arm(
            self._expected_latest(expected_sha256), now=self._now()
        )
        self.store.save(record)
        return self._response(record)

    def lock_current_session(self, session_date: date) -> SelectionRecord:
        """Lock only on the exact target date; activation remains separate."""

        if type(session_date) is not date:
            raise SelectionError("SESSION_DATE_MUST_BE_EXACT_DATE")
        current = self.store.load_latest()
        record = SelectionWorkflow.lock_session(
            current, session_date=session_date.isoformat(), now=self._now()
        )
        self.store.save(record)
        return record


__all__ = ["ARM_CONFIRMATION", "SelectionController"]
