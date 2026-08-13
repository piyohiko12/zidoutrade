from pathlib import Path
import tempfile
import unittest

from zidoutrade.journal import JournalEntry, JournalKind, StockJournal
from zidoutrade.storage import IntegrityError


class StockJournalTests(unittest.TestCase):
    def test_append_and_read_redacted_diary(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = StockJournal(Path(directory) / "stock-journal.jsonl")
            entry = JournalEntry(
                session_date="2026-01-02",
                symbol="aapl",
                kind=JournalKind.SIGNAL,
                decision="WAIT",
                reason_codes=("RSI_RECOVERY_NOT_CONFIRMED",),
                note="回復を待った",
                mood="CALM",
                tags=("RSI", "wait"),
            )
            journal.append(entry, occurred_at="2026-01-02T15:00:00Z")
            values = journal.entries()
            self.assertEqual(len(values), 1)
            self.assertEqual(values[0]["symbol"], "US.AAPL")
            self.assertNotIn("hash", values[0])
            self.assertNotIn("order_id", values[0])

    def test_sensitive_notes_are_rejected(self):
        for note in ("account id 999999", "token=abc", "注文 7777777"):
            with self.subTest(note=note):
                with self.assertRaises(ValueError):
                    JournalEntry(
                        "2026-01-02",
                        "US.TEST",
                        JournalKind.MANUAL_NOTE,
                        "NOTE",
                        (),
                        note=note,
                    )

    def test_tamper_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stock-journal.jsonl"
            journal = StockJournal(path)
            journal.append(
                JournalEntry(
                    "2026-01-02",
                    "US.TEST",
                    JournalKind.SAFETY,
                    "PAUSED",
                    ("DATA_STALE",),
                ),
                occurred_at="2026-01-02T15:00:00Z",
            )
            raw = path.read_bytes().replace(b"PAUSED", b"ARMED")
            path.write_bytes(raw)
            with self.assertRaises(IntegrityError):
                journal.entries()


if __name__ == "__main__":
    unittest.main()
