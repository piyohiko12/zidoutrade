import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from zidoutrade.storage import (
    ExclusiveFileLock,
    HashChainJournal,
    IntegrityError,
    IntentStore,
    LockAlreadyHeld,
    SensitiveDataError,
    account_fingerprint,
    atomic_write_json,
    canonical_json_bytes,
    read_json,
)


class StorageTests(unittest.TestCase):
    def test_canonical_json_is_sorted_compact_utf8_and_newline_terminated(self):
        self.assertEqual(
            canonical_json_bytes({"z": 1, "a": "日本"}),
            '{"a":"日本","z":1}\n'.encode("utf-8"),
        )

    def test_raw_account_identifier_key_is_rejected(self):
        with self.assertRaises(SensitiveDataError):
            canonical_json_bytes({"account_id": "111111"})

    def test_keyed_account_fingerprint_requires_strong_secret(self):
        key_a = bytes(range(32))
        key_b = bytes(reversed(range(32)))
        first = account_fingerprint("111111", key_a)
        self.assertEqual(first, account_fingerprint("111111", key_a))
        self.assertNotEqual(first, account_fingerprint("111111", key_b))
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        with self.assertRaises(ValueError):
            account_fingerprint("111111", b"short")
        with self.assertRaises(ValueError):
            account_fingerprint("", key_a)

    def test_atomic_json_round_trip_and_noncanonical_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state.json"
            atomic_write_json(target, {"b": 2, "a": 1})
            self.assertEqual(read_json(target), {"a": 1, "b": 2})
            target.write_text('{"b":2, "a":1}\n', encoding="utf-8")
            with self.assertRaises(IntegrityError):
                read_json(target)

    def test_symlink_and_hardlink_targets_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            actual = root / "actual.json"
            atomic_write_json(actual, {"safe": True})
            link = root / "link.json"
            link.symlink_to(actual)
            with self.assertRaises(IntegrityError):
                read_json(link)
            hard = root / "hard.json"
            os.link(actual, hard)
            with self.assertRaises(IntegrityError):
                read_json(actual)

    def test_exclusive_writer_lock_does_not_steal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "writer.lock"
            first = ExclusiveFileLock(path).acquire()
            try:
                with self.assertRaises(LockAlreadyHeld):
                    ExclusiveFileLock(path).acquire()
            finally:
                first.release()
            with ExclusiveFileLock(path):
                self.assertTrue(path.exists())
            self.assertFalse(path.exists())

    def test_intent_reservation_is_immutable_and_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            store = IntentStore(Path(directory) / "intents")
            document = {
                "account_fingerprint": "a" * 64,
                "quantity": 1,
                "side": "BUY",
                "symbol": "US.TEST",
            }
            reservation = store.reserve("intent-1", document)
            self.assertEqual(store.get("intent-1"), reservation)
            with self.assertRaises(LockAlreadyHeld):
                store.reserve("intent-1", document)

    def test_hash_chain_detects_middle_record_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            journal = HashChainJournal(path)
            journal.append("ONE", {"value": 1}, occurred_at="2026-01-01T00:00:00Z")
            journal.append("TWO", {"value": 2}, occurred_at="2026-01-01T00:00:01Z")
            self.assertEqual(len(journal.read_all()), 2)
            lines = path.read_text(encoding="utf-8").splitlines()
            first = json.loads(lines[0])
            first["payload"]["value"] = 9
            lines[0] = json.dumps(
                first, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(IntegrityError):
                journal.read_all()


if __name__ == "__main__":
    unittest.main()
