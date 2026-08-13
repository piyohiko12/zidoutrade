from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from zidoutrade.selection import (
    PresentedCandidate,
    SelectionError,
    SelectionState,
    SelectionStore,
    SelectionWorkflow,
    canonical_json_bytes,
    canonical_sha256,
)


NOW = datetime(2026, 8, 13, 5, 0, tzinfo=timezone.utc)


def presented():
    return (
        PresentedCandidate("US.MSFT", priority=2, eligible=True),
        PresentedCandidate("US.AAPL", priority=1, eligible=True),
        PresentedCandidate("US.BAD", priority=3, eligible=False, reason_codes=("TRADING_HALTED",)),
    )


class SelectionTests(unittest.TestCase):
    def test_canonical_json_is_sorted_minified_and_has_one_lf(self):
        value = {"z": [3, 2], "a": "日本語"}
        expected = '{"a":"日本語","z":[3,2]}\n'.encode("utf-8")
        self.assertEqual(canonical_json_bytes(value), expected)
        self.assertEqual(canonical_sha256(value), hashlib.sha256(expected).hexdigest())
        with self.assertRaisesRegex(SelectionError, "NON_CANONICAL_JSON_VALUE"):
            canonical_json_bytes({"nan": float("nan")})

    def test_state_flow_is_hash_chained_and_trade_permission_requires_choice(self):
        draft = SelectionWorkflow.new_draft(
            target_session="2026-08-14",
            presented_candidates=presented(),
            selected_symbol="aapl",
            now=NOW,
        )
        self.assertEqual(draft.state, SelectionState.DRAFT)
        self.assertEqual(draft.selected_symbol, "US.AAPL")
        self.assertFalse(draft.trade_permitted)
        # Presentation order is explicit user priority then ticker, never a model score.
        self.assertEqual([item.symbol for item in draft.presented_candidates], ["US.AAPL", "US.MSFT", "US.BAD"])

        validated = SelectionWorkflow.validate(draft, now=NOW)
        armed = SelectionWorkflow.arm(validated, now=NOW)
        locked = SelectionWorkflow.lock_session(armed, session_date="2026-08-14", now=NOW)
        self.assertEqual([draft.revision, validated.revision, armed.revision, locked.revision], [1, 2, 3, 4])
        self.assertEqual(validated.parent_sha256, draft.sha256)
        self.assertEqual(armed.parent_sha256, validated.sha256)
        self.assertEqual(locked.parent_sha256, armed.sha256)
        self.assertTrue(locked.trade_permitted)
        self.assertEqual(locked.state, SelectionState.SESSION_LOCKED)

    def test_no_choice_is_a_formal_no_trade_outcome(self):
        draft = SelectionWorkflow.new_draft(
            target_session="2026-08-14", presented_candidates=presented(), now=NOW
        )
        validated = SelectionWorkflow.validate(draft, now=NOW)
        armed = SelectionWorkflow.arm(validated, now=NOW)
        locked = SelectionWorkflow.lock_session(armed, session_date="2026-08-14", now=NOW)
        self.assertIsNone(locked.selected_symbol)
        self.assertFalse(locked.trade_permitted)
        self.assertEqual(locked.no_trade_reason, "NO_USER_SELECTION")

    def test_selection_must_be_exactly_one_eligible_presented_candidate(self):
        with self.assertRaisesRegex(SelectionError, "SELECTION_NOT_PRESENTED"):
            SelectionWorkflow.new_draft(
                target_session="2026-08-14",
                presented_candidates=presented(),
                selected_symbol="US.NVDA",
                now=NOW,
            )
        with self.assertRaisesRegex(SelectionError, "SELECTION_NOT_ELIGIBLE"):
            SelectionWorkflow.new_draft(
                target_session="2026-08-14",
                presented_candidates=presented(),
                selected_symbol="US.BAD",
                now=NOW,
            )

    def test_revision_before_session_is_new_draft_but_mid_session_replacement_is_forbidden(self):
        draft = SelectionWorkflow.new_draft(
            target_session="2026-08-14",
            presented_candidates=presented(),
            selected_symbol="US.AAPL",
            now=NOW,
        )
        validated = SelectionWorkflow.validate(draft, now=NOW)
        armed = SelectionWorkflow.arm(validated, now=NOW)
        revised = SelectionWorkflow.revise(
            armed,
            selected_symbol="US.MSFT",
            current_session_date="2026-08-13",
            now=NOW,
        )
        self.assertEqual(revised.revision, 4)
        self.assertEqual(revised.state, SelectionState.DRAFT)
        self.assertEqual(revised.selected_symbol, "US.MSFT")
        self.assertEqual(revised.parent_sha256, armed.sha256)
        with self.assertRaisesRegex(SelectionError, "MID_SESSION_REPLACEMENT_FORBIDDEN"):
            SelectionWorkflow.revise(
                revised,
                selected_symbol="US.AAPL",
                current_session_date="2026-08-14",
                now=NOW,
            )
        with self.assertRaisesRegex(SelectionError, "TARGET_SESSION_IS_FIXED"):
            SelectionWorkflow.revise(
                revised,
                selected_symbol="US.AAPL",
                current_session_date="2026-08-13",
                target_session="2026-08-17",
                now=NOW,
            )

    def test_locked_record_cannot_be_revised(self):
        draft = SelectionWorkflow.new_draft(
            target_session="2026-08-14",
            presented_candidates=presented(),
            selected_symbol="US.AAPL",
            now=NOW,
        )
        locked = SelectionWorkflow.lock_session(
            SelectionWorkflow.arm(SelectionWorkflow.validate(draft, now=NOW), now=NOW),
            session_date="2026-08-14",
            now=NOW,
        )
        with self.assertRaisesRegex(SelectionError, "SESSION_ALREADY_LOCKED"):
            SelectionWorkflow.revise(
                locked,
                selected_symbol="US.MSFT",
                current_session_date="2026-08-13",
                now=NOW,
            )

    def test_selection_expires_only_after_target_date(self):
        draft = SelectionWorkflow.new_draft(
            target_session="2026-08-14",
            presented_candidates=presented(),
            selected_symbol="US.AAPL",
            now=NOW,
        )
        locked = SelectionWorkflow.lock_session(
            SelectionWorkflow.arm(SelectionWorkflow.validate(draft, now=NOW), now=NOW),
            session_date="2026-08-14",
            now=NOW,
        )
        with self.assertRaisesRegex(SelectionError, "TARGET_SESSION_HAS_NOT_EXPIRED"):
            SelectionWorkflow.expire(
                locked, current_session_date="2026-08-14", now=NOW
            )
        expired = SelectionWorkflow.expire(
            locked, current_session_date="2026-08-17", now=NOW
        )
        self.assertEqual(expired.state, SelectionState.EXPIRED)
        self.assertFalse(expired.trade_permitted)
        self.assertEqual(expired.no_trade_reason, "SELECTION_EXPIRED")

    def test_store_is_append_only_verifies_hash_and_requires_external_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            repo = base / "repo"
            repo.mkdir()
            with self.assertRaisesRegex(SelectionError, "RUNTIME_PATH_INSIDE_REPOSITORY"):
                SelectionStore(repo / "runtime", repository_root=repo)

            store = SelectionStore(base / "runtime", repository_root=repo)
            draft = SelectionWorkflow.new_draft(
                target_session="2026-08-14",
                presented_candidates=presented(),
                selected_symbol="US.AAPL",
                now=NOW,
            )
            revision_path = store.save(draft)
            self.assertTrue(revision_path.exists())
            self.assertEqual(store.load_latest(), draft)

            validated = SelectionWorkflow.validate(draft, now=NOW)
            store.save(validated)
            self.assertEqual(store.load_latest(), validated)
            self.assertEqual(len(tuple((base / "runtime" / "selection-revisions").iterdir())), 2)

            latest_path = base / "runtime" / "selection-latest.json"
            envelope = json.loads(latest_path.read_text(encoding="utf-8"))
            envelope["record"]["selected_symbol"] = "US.MSFT"
            latest_path.write_text(
                json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SelectionError, "SELECTION_HASH_MISMATCH"):
                store.load_latest()

    def test_store_rejects_symlink_and_hardlink_latest_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            store = SelectionStore(base / "runtime")
            draft = SelectionWorkflow.new_draft(
                target_session="2026-08-14",
                presented_candidates=presented(),
                selected_symbol="US.AAPL",
                now=NOW,
            )
            revision_path = store.save(draft)
            store.latest_path.unlink()
            store.latest_path.symlink_to(revision_path)
            with self.assertRaisesRegex(SelectionError, "UNSAFE_SELECTION_FILE"):
                store.load_latest()

            store.latest_path.unlink()
            os.link(str(revision_path), str(store.latest_path))
            with self.assertRaisesRegex(SelectionError, "UNSAFE_SELECTION_FILE"):
                store.load_latest()


if __name__ == "__main__":
    unittest.main()
