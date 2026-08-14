from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest

from tests.activation_fixture import (
    RAW_SYNTHETIC_ACCOUNT,
    SECRET,
    SESSION_ID,
    SYMBOL,
    create_fixture,
)
from zidoutrade.activation import (
    ACTIVE_CONFIG_NAME,
    ACTIVATION_MARKER_NAME,
    GLOBAL_STOP_NAME,
    ActivationError,
    ActivationVerifier,
)
from zidoutrade.storage import canonical_json_bytes


class FingerprintBroker:
    def __init__(self, raw_account: str = RAW_SYNTHETIC_ACCOUNT) -> None:
        self.raw_account = raw_account

    def configured_account_fingerprint(self, secret_key: bytes) -> str:
        from zidoutrade.storage import account_fingerprint

        return account_fingerprint(self.raw_account, secret_key)


class ActivationTests(unittest.TestCase):
    def test_full_chain_issues_structured_short_lived_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = create_fixture(Path(directory))
            proof = fixture["proof"]
            self.assertEqual(proof.session_id, SESSION_ID)
            self.assertEqual(proof.selected_symbol, SYMBOL)
            self.assertEqual(proof.runtime_root, str(Path(directory).resolve()))
            self.assertNotIn(RAW_SYNTHETIC_ACCOUNT, json.dumps(proof.unsigned_payload()))
            fixture["verifier"].verify_proof(proof)
            fixture["verifier"].verify_broker_account(proof, FingerprintBroker())

    def test_public_clone_without_local_artifacts_cannot_activate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            fixture_root = Path(tempfile.mkdtemp())
            try:
                fixture = create_fixture(fixture_root)
                verifier = ActivationVerifier(
                    runtime_root=root,
                    repository_root=Path(__file__).resolve().parents[1],
                    calendar=fixture["calendar"],
                    clock=fixture["clock"],
                )
                with self.assertRaises(ActivationError):
                    verifier.verify()
            finally:
                # TemporaryDirectory cannot adopt an existing directory.
                for path in sorted(fixture_root.rglob("*"), reverse=True):
                    if path.is_file():
                        path.unlink()
                    elif path.is_dir():
                        path.rmdir()
                fixture_root.rmdir()

    def test_stop_marker_and_artifact_tamper_invalidate_existing_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = create_fixture(Path(directory))
            root = fixture["root"]
            (root / GLOBAL_STOP_NAME).write_text("STOP\n", encoding="utf-8")
            with self.assertRaises(ActivationError):
                fixture["verifier"].verify_proof(fixture["proof"])
            (root / GLOBAL_STOP_NAME).unlink()

            config_path = root / ACTIVE_CONFIG_NAME
            value = json.loads(config_path.read_text("utf-8"))
            value["planned_risk_fraction"] = 0.25
            config_path.write_bytes(canonical_json_bytes(value))
            config_path.chmod(0o600)
            with self.assertRaises(ActivationError):
                fixture["verifier"].verify_proof(fixture["proof"])

    def test_marker_hmac_and_broker_account_are_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = create_fixture(Path(directory))
            with self.assertRaises(ActivationError):
                fixture["verifier"].verify_broker_account(
                    fixture["proof"], FingerprintBroker("222222")
                )
            marker_path = fixture["root"] / ACTIVATION_MARKER_NAME
            marker = json.loads(marker_path.read_text("utf-8"))
            marker["marker_hmac_sha256"] = "0" * 64
            marker_path.write_bytes(canonical_json_bytes(marker))
            marker_path.chmod(0o600)
            with self.assertRaises(ActivationError):
                fixture["verifier"].verify()

    def test_expired_or_mutated_proof_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = create_fixture(Path(directory))
            proof = fixture["proof"]
            fixture["clock"].value += timedelta(seconds=6)
            with self.assertRaises(ActivationError):
                fixture["verifier"].verify_proof(proof)
            fixture["clock"].value -= timedelta(seconds=6)
            with self.assertRaises(ActivationError):
                fixture["verifier"].verify_proof(
                    replace(proof, selected_symbol="US.OTHER")
                )


if __name__ == "__main__":
    unittest.main()
