"""Redacted stock-diary records built on the private hash-chain journal."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
import re
from typing import Any, Dict, Iterable, Optional, Tuple

from .candidates import normalize_us_symbol
from .storage import HashChainJournal


class JournalKind(str, Enum):
    SIGNAL = "SIGNAL"
    TRADE_LIFECYCLE = "TRADE_LIFECYCLE"
    MANUAL_NOTE = "MANUAL_NOTE"
    SAFETY = "SAFETY"


_SECRET_HINT = re.compile(
    r"(?:password|passwd|token|secret|api[_ -]?key|account[_ -]?id|acc[_ -]?id|order[_ -]?id)",
    re.IGNORECASE,
)
_LONG_DIGITS = re.compile(r"\d{6,}")
_TAG = re.compile(r"^[A-Za-z0-9ぁ-んァ-ヶ一-龠_-]{1,24}$")


@dataclass(frozen=True)
class JournalEntry:
    session_date: str
    symbol: str
    kind: JournalKind
    decision: str
    reason_codes: Tuple[str, ...]
    note: str = ""
    mood: Optional[str] = None
    tags: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        try:
            date.fromisoformat(self.session_date)
        except (TypeError, ValueError) as exc:
            raise ValueError("session_date must be YYYY-MM-DD") from exc
        object.__setattr__(self, "symbol", normalize_us_symbol(self.symbol))
        if not isinstance(self.kind, JournalKind):
            raise TypeError("kind must be JournalKind")
        if not isinstance(self.decision, str) or not 1 <= len(self.decision) <= 64:
            raise ValueError("decision must contain 1-64 characters")
        if type(self.reason_codes) is not tuple or any(
            not isinstance(reason, str) or not reason for reason in self.reason_codes
        ):
            raise TypeError("reason_codes must be a tuple of nonempty strings")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("reason_codes cannot contain duplicates")
        if not isinstance(self.note, str) or len(self.note) > 1000:
            raise ValueError("note must be at most 1000 characters")
        if _SECRET_HINT.search(self.note) or _LONG_DIGITS.search(self.note):
            raise ValueError("note may contain sensitive identifiers")
        if self.mood is not None and self.mood not in {
            "CALM",
            "CONFIDENT",
            "UNCERTAIN",
            "STRESSED",
            "REFLECTIVE",
        }:
            raise ValueError("unsupported mood")
        if type(self.tags) is not tuple or len(self.tags) > 8:
            raise ValueError("tags must be a tuple with at most 8 items")
        if any(not _TAG.fullmatch(tag) for tag in self.tags):
            raise ValueError("invalid journal tag")

    def to_payload(self) -> Dict[str, Any]:
        return {
            "decision": self.decision,
            "kind": self.kind.value,
            "mood": self.mood,
            "note": self.note,
            "reason_codes": list(self.reason_codes),
            "schema_version": "RSI_STOCK_JOURNAL_V1",
            "session_date": self.session_date,
            "symbol": self.symbol,
            "tags": list(self.tags),
        }


class StockJournal:
    """A local-only decision diary; the caller chooses an external runtime path."""

    def __init__(self, path: Path) -> None:
        self._journal = HashChainJournal(Path(path))

    def append(self, entry: JournalEntry, *, occurred_at: Optional[str] = None) -> Dict[str, Any]:
        if not isinstance(entry, JournalEntry):
            raise TypeError("entry must be JournalEntry")
        return self._journal.append("STOCK_JOURNAL", entry.to_payload(), occurred_at=occurred_at)

    def entries(self) -> Tuple[Dict[str, Any], ...]:
        records = self._journal.read_all()
        result = []
        for record in records:
            if record.get("event_type") != "STOCK_JOURNAL":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict) or payload.get("schema_version") != "RSI_STOCK_JOURNAL_V1":
                raise ValueError("journal payload schema mismatch")
            # Deliberately expose only diary fields, never chain tokens or any
            # broker/account identifiers from unrelated private events.
            result.append(dict(payload))
        return tuple(result)


__all__ = ["JournalEntry", "JournalKind", "StockJournal"]
