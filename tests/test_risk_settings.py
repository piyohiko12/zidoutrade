from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from zidoutrade.risk_settings import (
    MAX_SAFE_CENTS,
    SAVE_CONFIRMATION,
    RiskSettingsConflictError,
    RiskSettingsError,
    RiskSettingsStore,
    RiskSettingsRecord,
    default_public_risk_settings,
    parse_risk_settings_update,
)
from zidoutrade.storage import canonical_json_bytes, canonical_sha256


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def request_payload(**changes):
    value = {
        "confirmation": SAVE_CONFIRMATION,
        "daily_loss_limit_basis_points": 75,
        "expected_sha256": None,
        "maximum_investment_cents": 50_000,
        "planned_risk_basis_points": 25,
        "risk_policy_version": "RSI_RISK_POLICY_V2",
        "target_session": "2026-08-14",
        "weekly_loss_limit_basis_points": 200,
    }
    value.update(changes)
    return value


class RiskSettingsModelTests(unittest.TestCase):
    def test_exact_public_contract_builds_frozen_policy(self):
        update = parse_risk_settings_update(request_payload())
        self.assertEqual(update.target_session, "2026-08-14")
        self.assertEqual(update.policy.planned_risk_basis_points, 25)
        self.assertEqual(update.policy.daily_loss_limit_basis_points, 75)
        self.assertEqual(update.policy.weekly_loss_limit_basis_points, 200)
        self.assertEqual(update.policy.maximum_investment_cents, 50_000)
        self.assertIsNone(update.expected_sha256)

    def test_null_investment_is_valid_but_explicitly_blocks_entry(self):
        update = parse_risk_settings_update(
            request_payload(maximum_investment_cents=None)
        )
        self.assertIsNone(update.policy.maximum_investment_cents)
        public = default_public_risk_settings(editable=False)
        self.assertTrue(public["entry_blocked"])
        self.assertFalse(public["editable"])
        self.assertIsNone(public["maximum_investment_cents"])

    def test_schema_types_version_hard_limits_and_order_are_fail_closed(self):
        attacks = [
            {**request_payload(), "extra": True},
            request_payload(confirmation="yes"),
            request_payload(risk_policy_version="RSI_RISK_POLICY_V3"),
            request_payload(planned_risk_basis_points=True),
            request_payload(daily_loss_limit_basis_points=75.0),
            request_payload(weekly_loss_limit_basis_points="200"),
            request_payload(planned_risk_basis_points=101),
            request_payload(daily_loss_limit_basis_points=201),
            request_payload(weekly_loss_limit_basis_points=501),
            request_payload(
                planned_risk_basis_points=76,
                daily_loss_limit_basis_points=75,
            ),
            request_payload(
                daily_loss_limit_basis_points=201,
                weekly_loss_limit_basis_points=200,
            ),
            request_payload(maximum_investment_cents=0),
            request_payload(maximum_investment_cents=True),
            request_payload(maximum_investment_cents=MAX_SAFE_CENTS + 1),
            request_payload(expected_sha256="A" * 64),
            request_payload(target_session="2026-8-14"),
        ]
        for attack in attacks:
            with self.subTest(attack=attack), self.assertRaises(RiskSettingsError):
                parse_risk_settings_update(attack)

    def test_revision_chain_shape_is_exact(self):
        from zidoutrade.risk import RiskPolicy

        fields = {
            "target_session": "2026-08-14",
            "policy": RiskPolicy(),
            "created_at": "2026-08-13T00:00:00.000000Z",
            "updated_at": "2026-08-13T00:00:00.000000Z",
        }
        with self.assertRaisesRegex(RiskSettingsError, "chain"):
            RiskSettingsRecord(revision=1, parent_sha256="a" * 64, **fields)
        with self.assertRaisesRegex(RiskSettingsError, "chain"):
            RiskSettingsRecord(revision=2, parent_sha256=None, **fields)


class RiskSettingsStoreTests(unittest.TestCase):
    def make_store(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        os.chmod(root, 0o700)
        return root, RiskSettingsStore(root, repository_root=REPOSITORY_ROOT)

    def test_store_requires_explicit_absolute_owner_only_external_directory(self):
        with self.assertRaisesRegex(RiskSettingsError, "absolute"):
            RiskSettingsStore(Path("relative/runtime"), repository_root=REPOSITORY_ROOT)

        with tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT) as raw_inside:
            inside = Path(raw_inside)
            os.chmod(inside, 0o700)
            with self.assertRaisesRegex(RiskSettingsError, "outside repository"):
                RiskSettingsStore(inside, repository_root=REPOSITORY_ROOT)

        with tempfile.TemporaryDirectory() as raw_open:
            open_root = Path(raw_open)
            os.chmod(open_root, 0o755)
            with self.assertRaisesRegex(RiskSettingsError, "owner-only"):
                RiskSettingsStore(open_root, repository_root=REPOSITORY_ROOT)

    def test_append_only_hash_chain_and_optimistic_concurrency(self):
        root, store = self.make_store()
        now = datetime(2026, 8, 13, 1, 2, 3, tzinfo=timezone.utc)
        first = store.save_next_session(
            parse_risk_settings_update(request_payload()),
            current_session_date="2026-08-13",
            now=now,
        )
        self.assertEqual(first.revision, 1)
        self.assertIsNone(first.parent_sha256)
        self.assertEqual(store.load_latest(), first)
        self.assertEqual(stat.S_IMODE(store.latest_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(store.revisions.stat().st_mode), 0o700)
        immutable = list(store.revisions.iterdir())
        self.assertEqual(len(immutable), 1)
        self.assertEqual(stat.S_IMODE(immutable[0].stat().st_mode), 0o600)
        for path in (store.latest_path, immutable[0]):
            raw = path.read_bytes()
            self.assertTrue(raw.endswith(b"\n"))
            self.assertFalse(raw.endswith(b"\n\n"))
            self.assertEqual(set(json.loads(raw)), {"record", "sha256"})

        second = store.save_next_session(
            parse_risk_settings_update(
                request_payload(
                    expected_sha256=first.sha256,
                    maximum_investment_cents=75_000,
                    target_session="2026-08-17",
                )
            ),
            current_session_date="2026-08-13",
            now=datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(second.revision, 2)
        self.assertEqual(second.parent_sha256, first.sha256)
        self.assertEqual(len(list(store.revisions.iterdir())), 2)
        self.assertEqual(store.load_latest(), second)
        self.assertIsNone(store.policy_for_session("2026-08-14"))
        self.assertEqual(
            store.policy_for_session("2026-08-17").maximum_investment_cents,
            75_000,
        )

        with self.assertRaises(RiskSettingsConflictError):
            store.save_next_session(
                parse_risk_settings_update(
                    request_payload(
                        expected_sha256=first.sha256,
                        target_session="2026-08-18",
                    )
                ),
                current_session_date="2026-08-13",
                now=now,
            )

    def test_missing_or_tampered_parent_revision_fails_closed(self):
        _, store = self.make_store()
        first = store.save_next_session(
            parse_risk_settings_update(request_payload()),
            current_session_date="2026-08-13",
            now=datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc),
        )
        store.save_next_session(
            parse_risk_settings_update(
                request_payload(
                    expected_sha256=first.sha256,
                    target_session="2026-08-17",
                )
            ),
            current_session_date="2026-08-13",
            now=datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc),
        )
        parent_path = next(
            path for path in store.revisions.iterdir() if first.sha256 in path.name
        )
        parent_path.write_text(
            parent_path.read_text(encoding="utf-8").replace('"revision":1', '"revision":9'),
            encoding="utf-8",
        )
        with self.assertRaises(RiskSettingsError):
            store.load_latest(required=True)

    def test_recomputed_grandparent_tamper_fails_closed_for_three_revisions(self):
        _, store = self.make_store()
        first = store.save_next_session(
            parse_risk_settings_update(request_payload()),
            current_session_date="2026-08-13",
            now=datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc),
        )
        second = store.save_next_session(
            parse_risk_settings_update(
                request_payload(
                    expected_sha256=first.sha256,
                    target_session="2026-08-17",
                )
            ),
            current_session_date="2026-08-13",
            now=datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc),
        )
        third = store.save_next_session(
            parse_risk_settings_update(
                request_payload(
                    expected_sha256=second.sha256,
                    target_session="2026-08-18",
                )
            ),
            current_session_date="2026-08-13",
            now=datetime(2026, 8, 13, 3, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(store.load_latest(required=True), third)

        grandparent_path = next(
            path for path in store.revisions.iterdir() if first.sha256 in path.name
        )
        envelope = json.loads(grandparent_path.read_text(encoding="utf-8"))
        envelope["record"]["maximum_investment_cents"] = 60_000
        envelope["sha256"] = canonical_sha256(envelope["record"])
        grandparent_path.write_bytes(canonical_json_bytes(envelope))

        with self.assertRaisesRegex(RiskSettingsError, "parent revision is invalid"):
            store.load_latest(required=True)

    def test_current_or_past_session_can_never_be_changed_mid_session(self):
        _, store = self.make_store()
        for target in ("2026-08-12", "2026-08-13"):
            with self.subTest(target=target), self.assertRaisesRegex(
                RiskSettingsError, "future session"
            ):
                store.save_next_session(
                    parse_risk_settings_update(
                        request_payload(target_session=target)
                    ),
                    current_session_date="2026-08-13",
                )
        self.assertIsNone(store.load_latest())

    def test_tamper_and_noncanonical_latest_fail_closed(self):
        _, store = self.make_store()
        record = store.save_next_session(
            parse_risk_settings_update(request_payload()),
            current_session_date="2026-08-13",
            now=datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc),
        )
        value = json.loads(store.latest_path.read_text(encoding="utf-8"))
        value["record"]["planned_risk_basis_points"] = 100
        store.latest_path.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RiskSettingsError, "hash mismatch"):
            store.load_latest(required=True)
        self.assertTrue(record.sha256)

    def test_public_view_has_no_secret_account_or_order_fields(self):
        _, store = self.make_store()
        record = store.save_next_session(
            parse_risk_settings_update(
                request_payload(maximum_investment_cents=None)
            ),
            current_session_date="2026-08-13",
            now=datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc),
        )
        public = store.public_view(editable=True)
        self.assertEqual(public["sha256"], record.sha256)
        self.assertTrue(public["entry_blocked"])
        self.assertTrue(public["editable"])
        normalized_keys = " ".join(public).lower()
        for forbidden in ("account", "password", "secret", "order"):
            self.assertNotIn(forbidden, normalized_keys)


if __name__ == "__main__":
    unittest.main()
