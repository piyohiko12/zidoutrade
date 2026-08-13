from pathlib import Path
import tempfile
import unittest

from tests.activation_fixture import create_fixture
from zidoutrade.broker import (
    LimitOrderRequest,
    OrderRecord,
    PositionRecord,
    ReconciliationSnapshot,
    Side,
)
from zidoutrade.runner import ExecutionMode, RunnerSafetyError, TradingRunner
from zidoutrade.state_machine import ControlState, ExposureState
from zidoutrade.storage import IntentStore


class FakeBroker:
    def __init__(self):
        self.symbol = "US.TEST"
        self.orders = []
        self.position = 0
        self.reconcile_calls = 0
        self.place_calls = 0

    def reconcile(self, symbol):
        self.reconcile_calls += 1
        return ReconciliationSnapshot(
            symbol=self.symbol,
            orders=tuple(self.orders),
            position=PositionRecord(
                symbol=self.symbol,
                quantity=self.position,
                sellable_quantity=self.position,
            ),
        )

    def configured_account_fingerprint(self, secret_key):
        from tests.activation_fixture import RAW_SYNTHETIC_ACCOUNT
        from zidoutrade.storage import account_fingerprint

        return account_fingerprint(RAW_SYNTHETIC_ACCOUNT, secret_key)

    def place_limit(self, request: LimitOrderRequest, *, selected_symbol: str):
        self.place_calls += 1
        raise AssertionError("reviewed runner must never reach broker place_limit")


class RunnerTests(unittest.TestCase):
    def test_shadow_startup_reconciles_but_never_dispatches(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = FakeBroker()
            runner = TradingRunner(runtime_root=Path(directory), broker=broker)
            try:
                state = runner.start(
                    session_id="2026-08-13",
                    selected_symbol="US.TEST",
                    account_fingerprint="f" * 64,
                )
                self.assertEqual(state.exposure, ExposureState.FLAT)
                runner.set_control(ControlState.ARMED)
                with self.assertRaises(RunnerSafetyError):
                    runner.dispatch_entry(activation_proof=None, decision=None)
                self.assertEqual(broker.reconcile_calls, 1)
                self.assertEqual(broker.place_calls, 0)
                self.assertEqual(list((Path(directory) / "intents").glob("*.json")), [])
            finally:
                runner.close()

    def test_active_start_requires_exact_local_proof_and_account_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = create_fixture(Path(directory))
            broker = FakeBroker()
            runner = TradingRunner(
                runtime_root=Path(directory),
                broker=broker,
                mode=ExecutionMode.PAPER_SIMULATE,
                activation_verifier=fixture["verifier"],
            )
            with self.assertRaises(RunnerSafetyError):
                runner.start(
                    session_id="2026-08-13",
                    selected_symbol="US.TEST",
                    account_fingerprint=fixture["fingerprint"],
                )
            self.assertEqual(broker.place_calls, 0)

    def test_reviewed_release_blocks_active_dispatch_before_intent_or_broker(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = create_fixture(Path(directory))
            broker = FakeBroker()
            runner = TradingRunner(
                runtime_root=Path(directory),
                broker=broker,
                mode=ExecutionMode.PAPER_SIMULATE,
                activation_verifier=fixture["verifier"],
            )
            try:
                runner.start(
                    session_id="2026-08-13",
                    selected_symbol="US.TEST",
                    account_fingerprint=fixture["fingerprint"],
                    activation_proof=fixture["proof"],
                )
                runner.set_control(ControlState.ARMED)
                with self.assertRaisesRegex(
                    RunnerSafetyError, "disabled in this reviewed release"
                ):
                    runner.dispatch_entry(
                        activation_proof=fixture["proof"], decision=None
                    )
                with self.assertRaisesRegex(
                    RunnerSafetyError, "disabled in this reviewed release"
                ):
                    runner.dispatch_exit(
                        activation_proof=fixture["proof"], decision=None
                    )
                self.assertEqual(broker.place_calls, 0)
                self.assertEqual(list((Path(directory) / "intents").glob("*.json")), [])
            finally:
                runner.close()

    def test_session_identity_is_canonical_and_same_day_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = FakeBroker()
            first = TradingRunner(runtime_root=Path(directory), broker=broker)
            with self.assertRaises(RunnerSafetyError):
                first.start(
                    session_id="2026-08-13-second",
                    selected_symbol="US.TEST",
                    account_fingerprint="f" * 64,
                )
            first.close()
        with tempfile.TemporaryDirectory() as directory:
            broker = FakeBroker()
            first = TradingRunner(runtime_root=Path(directory), broker=broker)
            first.start(
                session_id="2026-08-13",
                selected_symbol="US.TEST",
                account_fingerprint="f" * 64,
            )
            first.close()
            second = TradingRunner(runtime_root=Path(directory), broker=broker)
            try:
                with self.assertRaises(RunnerSafetyError):
                    second.start(
                        session_id="2026-08-13",
                        selected_symbol="US.OTHER",
                        account_fingerprint="f" * 64,
                    )
            finally:
                second.close()

    def test_orphan_intent_is_query_only_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = FakeBroker()
            first = TradingRunner(runtime_root=Path(directory), broker=broker)
            first.start(
                session_id="2026-08-13",
                selected_symbol="US.TEST",
                account_fingerprint="f" * 64,
            )
            first.close()
            IntentStore(Path(directory) / "intents").reserve(
                "session-BUY-orphan01",
                {
                    "account_fingerprint": "f" * 64,
                    "flatten": False,
                    "limit_price": "10.00",
                    "quantity": 1,
                    "remark": "RSI1-session-BUY-orphan01",
                    "session_id": "2026-08-13",
                    "side": "BUY",
                    "symbol": "US.TEST",
                },
            )
            recovered = TradingRunner(runtime_root=Path(directory), broker=broker)
            try:
                state = recovered.start(
                    session_id="2026-08-13",
                    selected_symbol="US.TEST",
                    account_fingerprint="f" * 64,
                )
                self.assertEqual(state.exposure, ExposureState.RECOVERY_REQUIRED)
                self.assertEqual(broker.place_calls, 0)
            finally:
                recovered.close()

    def test_runtime_root_inside_git_checkout_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory) / "checkout"
            (checkout / ".git").mkdir(parents=True)
            with self.assertRaises(RunnerSafetyError):
                TradingRunner(runtime_root=checkout / "runtime", broker=FakeBroker())

    def test_unknown_filled_order_for_selected_symbol_forces_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = FakeBroker()
            broker.orders = [
                OrderRecord(
                    order_id="synthetic-external-filled",
                    symbol="US.TEST",
                    side=Side.BUY,
                    quantity=1,
                    filled_quantity=1,
                    status="FILLED_ALL",
                    remark="external-order",
                )
            ]
            runner = TradingRunner(runtime_root=Path(directory), broker=broker)
            try:
                state = runner.start(
                    session_id="2026-08-13",
                    selected_symbol="US.TEST",
                    account_fingerprint="f" * 64,
                )
                self.assertEqual(state.exposure, ExposureState.RECOVERY_REQUIRED)
            finally:
                runner.close()


if __name__ == "__main__":
    unittest.main()
