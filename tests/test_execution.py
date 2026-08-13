from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from tests.activation_fixture import SYMBOL, create_fixture
from zidoutrade.activation import GLOBAL_STOP_NAME
from zidoutrade.execution import (
    DecisionKind,
    ExecutionEvidenceError,
    MAX_RELATIVE_SPREAD,
    QuoteSnapshot,
    build_entry_decision,
    build_exit_decision,
    verify_execution_decision,
)
from zidoutrade.models import (
    CompletedBar15m,
    MarketGates,
    PositionSnapshot,
    StrategyContext,
    TrendEligibility,
)
from zidoutrade.risk import RiskPolicy, RiskState, SizingRequest


NY = ZoneInfo("America/New_York")


def bars():
    def one(day, hour, minute, opening, high, low, close, volume=100_000):
        start = datetime(2026, 8, day, hour, minute, tzinfo=NY)
        return CompletedBar15m(
            symbol=SYMBOL,
            start=start,
            end=start + timedelta(minutes=15),
            open=opening,
            high=high,
            low=low,
            close=close,
            volume=volume,
        )

    prior = tuple(
        one(12, 13 + index // 4, (index % 4) * 15, 9.9, 10.0, 9.8, 9.9)
        for index in range(11)
    )
    return prior + (
        one(13, 9, 45, 10.0, 10.1, 9.9, 10.0),
        one(13, 10, 0, 10.0, 10.35, 9.9, 10.1),
        one(13, 10, 15, 10.1, 10.5, 10.0, 10.4, 150_000),
    )


def risk_state(**changes):
    values = {
        "day_start_equity": 100_000,
        "week_start_equity": 100_000,
        "daily_pnl": 0.0,
        "weekly_pnl": 0.0,
        "completed_roundtrips_today": 0,
    }
    values.update(changes)
    return RiskState(**values)


class ExecutionTests(unittest.TestCase):
    def entry_inputs(self, fixture):
        now = fixture["clock"].value
        context = StrategyContext(
            active_symbol=SYMBOL,
            selected_symbol=SYMBOL,
            bars=bars(),
            rsi_values=(None,) * 11 + (29.0, 34.0, 36.0),
            trend=TrendEligibility(True, True, True),
            gates=MarketGates(True, True, True),
            now=now,
            traded_roundtrips_today=0,
        )
        quote = QuoteSnapshot(
            symbol=SYMBOL,
            bid=Decimal("10.39"),
            ask=Decimal("10.40"),
            observed_at=now - timedelta(seconds=1),
        )
        sizing = SizingRequest(
            state=risk_state(),
            entry_limit=10.40,
            stop_trigger=8.90,
            policy=RiskPolicy(maximum_investment_cents=1_000_000),
        )
        return context, quote, sizing

    def build_entry(self, fixture, **changes):
        context, quote, sizing = self.entry_inputs(fixture)
        values = {
            "verifier": fixture["verifier"],
            "proof": fixture["proof"],
            "calendar": fixture["calendar"],
            "context": context,
            "sizing_request": sizing,
            "atr_raw": 1.0,
            "quote": quote,
            "entry_dispatches_today": 0,
            "exit_dispatches_today": 0,
        }
        values.update(changes)
        return build_entry_decision(**values)

    def test_entry_binds_exact_symbol_quantity_limit_and_all_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = create_fixture(Path(directory))
            decision = self.build_entry(fixture)
            self.assertEqual(decision.kind, DecisionKind.ENTRY)
            self.assertEqual(decision.symbol, SYMBOL)
            self.assertGreater(decision.quantity, 0)
            self.assertEqual(decision.limit_price, Decimal("10.40"))
            verify_execution_decision(
                verifier=fixture["verifier"],
                proof=fixture["proof"],
                decision=decision,
                expected_kind=DecisionKind.ENTRY,
                expected_session_id="2026-08-13",
                expected_symbol=SYMBOL,
            )

    def test_tamper_stale_quote_wide_spread_and_loss_limit_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = create_fixture(Path(directory))
            decision = self.build_entry(fixture)
            with self.assertRaises(ExecutionEvidenceError):
                verify_execution_decision(
                    verifier=fixture["verifier"],
                    proof=fixture["proof"],
                    decision=replace(decision, quantity=decision.quantity + 1),
                    expected_kind=DecisionKind.ENTRY,
                    expected_session_id="2026-08-13",
                    expected_symbol=SYMBOL,
                )

            context, quote, sizing = self.entry_inputs(fixture)
            with self.assertRaises(ExecutionEvidenceError):
                self.build_entry(
                    fixture,
                    quote=replace(
                        quote,
                        observed_at=fixture["clock"].value - timedelta(seconds=3),
                    ),
                )
            wide = replace(quote, bid=Decimal("10.00"))
            with self.assertRaises(ExecutionEvidenceError):
                self.build_entry(fixture, quote=wide)
            blocked = replace(sizing, state=risk_state(daily_pnl=-750.0))
            with self.assertRaises(ExecutionEvidenceError):
                self.build_entry(fixture, sizing_request=blocked)

    def test_entry_spread_threshold_is_exactly_10_bps_and_inclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = create_fixture(Path(directory))
            context, quote, sizing = self.entry_inputs(fixture)
            self.assertEqual(MAX_RELATIVE_SPREAD, Decimal("0.0010"))

            at_limit = replace(
                quote,
                bid=Decimal("9.995"),
                ask=Decimal("10.005"),
            )
            self.assertEqual(at_limit.relative_spread, MAX_RELATIVE_SPREAD)
            at_limit_sizing = replace(
                sizing,
                entry_limit=10.005,
                stop_trigger=8.505,
            )
            at_limit_decision = self.build_entry(
                fixture,
                context=context,
                quote=at_limit,
                sizing_request=at_limit_sizing,
            )

            narrower = replace(at_limit, bid=Decimal("9.996"))
            narrower_decision = self.build_entry(
                fixture,
                context=context,
                quote=narrower,
                sizing_request=at_limit_sizing,
            )
            self.assertNotEqual(
                at_limit_decision.risk_sha256,
                narrower_decision.risk_sha256,
            )

            over = replace(at_limit, bid=Decimal("9.994999"))
            self.assertGreater(over.relative_spread, MAX_RELATIVE_SPREAD)
            with self.assertRaisesRegex(ExecutionEvidenceError, "spread gate"):
                self.build_entry(fixture, quote=over)

    def test_latest_completed_bar_and_one_roundtrip_counters_are_mandatory(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = create_fixture(Path(directory))
            context, _, _ = self.entry_inputs(fixture)
            stale_context = replace(
                context,
                bars=context.bars[:-1],
                rsi_values=context.rsi_values[:-1],
            )
            with self.assertRaises(ExecutionEvidenceError):
                self.build_entry(fixture, context=stale_context)
            with self.assertRaises(ExecutionEvidenceError):
                self.build_entry(fixture, entry_dispatches_today=1)

    def test_nondefault_valid_policy_is_propagated_into_risk_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = create_fixture(Path(directory))
            _, _, sizing = self.entry_inputs(fixture)
            conservative = self.build_entry(fixture, sizing_request=sizing)
            wider_within_hard_limits = replace(
                sizing,
                policy=RiskPolicy(
                    planned_risk_basis_points=100,
                    daily_loss_limit_basis_points=200,
                    weekly_loss_limit_basis_points=500,
                    maximum_investment_cents=1_000_000,
                ),
            )
            configured = self.build_entry(
                fixture, sizing_request=wider_within_hard_limits
            )
            self.assertNotEqual(conservative.risk_sha256, configured.risk_sha256)
            self.assertGreaterEqual(configured.quantity, conservative.quantity)

    def test_exit_is_full_reconciled_quantity_and_not_blocked_by_loss_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = create_fixture(Path(directory))
            now = fixture["clock"].value
            quote = QuoteSnapshot(
                symbol=SYMBOL,
                bid=Decimal("10.39"),
                ask=Decimal("10.40"),
                observed_at=now - timedelta(seconds=1),
            )
            context = StrategyContext(
                active_symbol=SYMBOL,
                selected_symbol=SYMBOL,
                bars=bars(),
                rsi_values=(None,) * 11 + (29.0, 34.0, 60.0),
                trend=TrendEligibility(True, True, True),
                gates=MarketGates(True, True, True),
                now=now,
                position=PositionSnapshot(
                    entry_raw=10.0,
                    atr_raw=1.0,
                    current_raw_price=10.39,
                    bars_held=1,
                    entry_time=datetime(2026, 8, 13, 9, 45, tzinfo=NY),
                    exchange_close=datetime(2026, 8, 13, 16, 0, tzinfo=NY),
                ),
            )
            decision = build_exit_decision(
                verifier=fixture["verifier"],
                proof=fixture["proof"],
                calendar=fixture["calendar"],
                context=context,
                risk_state=risk_state(daily_pnl=-900.0, weekly_pnl=-3000.0),
                quote=quote,
                known_position_quantity=3,
                entry_dispatches_today=1,
                exit_dispatches_today=0,
                risk_policy=RiskPolicy(maximum_investment_cents=1_000_000),
            )
            self.assertEqual(decision.quantity, 3)
            self.assertEqual(decision.limit_price, Decimal("10.39"))
            verify_execution_decision(
                verifier=fixture["verifier"],
                proof=fixture["proof"],
                decision=decision,
                expected_kind=DecisionKind.EXIT,
                expected_session_id="2026-08-13",
                expected_symbol=SYMBOL,
                expected_position_quantity=3,
                expected_exit_dispatches=0,
            )

    def test_stop_marker_after_decision_or_expiry_prevents_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = create_fixture(Path(directory))
            decision = self.build_entry(fixture)
            (fixture["root"] / GLOBAL_STOP_NAME).write_text("STOP\n", encoding="utf-8")
            with self.assertRaises(ExecutionEvidenceError):
                verify_execution_decision(
                    verifier=fixture["verifier"],
                    proof=fixture["proof"],
                    decision=decision,
                    expected_kind=DecisionKind.ENTRY,
                    expected_session_id="2026-08-13",
                    expected_symbol=SYMBOL,
                )
            (fixture["root"] / GLOBAL_STOP_NAME).unlink()
            fixture["clock"].value += timedelta(seconds=3)
            with self.assertRaises(ExecutionEvidenceError):
                verify_execution_decision(
                    verifier=fixture["verifier"],
                    proof=fixture["proof"],
                    decision=decision,
                    expected_kind=DecisionKind.ENTRY,
                    expected_session_id="2026-08-13",
                    expected_symbol=SYMBOL,
                )


if __name__ == "__main__":
    unittest.main()
