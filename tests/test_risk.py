from dataclasses import FrozenInstanceError
from decimal import Decimal
import math
import unittest

from zidoutrade.fees import FeeSchedule, OrderSide, calculate_order_fees
from zidoutrade.risk import (
    ExecutionStress,
    PAPER_FEE_SCHEDULE,
    RiskBlockReason,
    RiskPolicy,
    RiskState,
    SizingRequest,
    entry_allowed,
    size_position,
)


def state(**overrides):
    values = dict(
        day_start_equity=100_000.0,
        week_start_equity=100_000.0,
        daily_pnl=0.0,
        weekly_pnl=0.0,
        completed_roundtrips_today=0,
    )
    values.update(overrides)
    return RiskState(**values)


class FeeAndStressTests(unittest.TestCase):
    def test_frozen_paper_schedule_is_versioned_and_side_specific(self):
        self.assertIs(
            PAPER_FEE_SCHEDULE,
            FeeSchedule.JP_US_PAPER_STOCK_2026_08_13,
        )
        buy = calculate_order_fees(PAPER_FEE_SCHEDULE, OrderSide.BUY, 3, 10)
        sell = calculate_order_fees(PAPER_FEE_SCHEDULE, OrderSide.SELL, 3, 10)
        self.assertEqual(buy.total, Decimal("0.16"))
        self.assertEqual(sell.total, Decimal("0.18"))
        self.assertGreater(sell.total, buy.total)

    def test_stress_defaults_are_explicit(self):
        stress = ExecutionStress()
        self.assertEqual(stress.stop_slippage_fraction, 0.005)
        self.assertEqual(stress.stop_slippage_floor, 0.05)
        self.assertEqual(stress.minimum_price_increment, 0.01)

    def test_stress_values_cannot_be_substituted(self):
        with self.assertRaisesRegex(ValueError, "V1 freezes"):
            ExecutionStress(stop_slippage_fraction=0.001)
        with self.assertRaisesRegex(ValueError, "V1 freezes"):
            ExecutionStress(stop_slippage_floor=0.01)


class RiskGateTests(unittest.TestCase):
    def test_default_policy_values(self):
        policy = RiskPolicy()
        self.assertEqual(policy.planned_risk_fraction, 0.0025)
        self.assertEqual(policy.daily_loss_fraction, 0.0075)
        self.assertEqual(policy.weekly_loss_fraction, 0.02)
        self.assertEqual(policy.max_notional_fraction, 0.10)
        self.assertEqual(policy.max_roundtrips_per_day, 1)

    def test_frozen_policy_cannot_be_widened_or_narrowed_by_a_caller(self):
        for field, value in (
            ("planned_risk_fraction", 0.01),
            ("daily_loss_fraction", 0.50),
            ("weekly_loss_fraction", 0.50),
            ("max_notional_fraction", 1.0),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "V1 freezes"):
                    RiskPolicy(**{field: value})

    def test_daily_limit_is_inclusive(self):
        just_before = entry_allowed(state(daily_pnl=-749.99))
        at_limit = entry_allowed(state(daily_pnl=-750.0))
        self.assertTrue(just_before.allowed)
        self.assertFalse(at_limit.allowed)
        self.assertEqual(at_limit.reasons, (RiskBlockReason.DAILY_LOSS_LIMIT,))

    def test_weekly_limit_uses_separate_week_anchor(self):
        result = entry_allowed(
            state(week_start_equity=80_000, weekly_pnl=-1_600)
        )
        self.assertEqual(result.reasons, (RiskBlockReason.WEEKLY_LOSS_LIMIT,))

    def test_roundtrip_limit_and_combined_reasons(self):
        result = entry_allowed(
            state(daily_pnl=-800, weekly_pnl=-2_100, completed_roundtrips_today=1)
        )
        self.assertEqual(
            result.reasons,
            (
                RiskBlockReason.DAILY_LOSS_LIMIT,
                RiskBlockReason.WEEKLY_LOSS_LIMIT,
                RiskBlockReason.ROUNDTRIP_LIMIT,
            ),
        )


class PositionSizingTests(unittest.TestCase):
    def test_default_case_is_notional_limited_to_ten_percent(self):
        request = SizingRequest(state(), entry_limit=100, stop_trigger=98.5)
        result = size_position(request)
        self.assertTrue(result.allowed)
        self.assertEqual(result.qty, 100)
        self.assertEqual(result.risk_budget, 250.0)
        self.assertEqual(result.notional_cap, 10_000.0)
        self.assertLessEqual(result.components.planned_total_loss, 250.0)
        self.assertLessEqual(result.qty * request.entry_limit, result.notional_cap)

    def test_risk_limited_integer_search_returns_largest_qty(self):
        request = SizingRequest(
            state=state(day_start_equity=1_000, week_start_equity=1_000),
            entry_limit=10,
            stop_trigger=9,
        )
        result = size_position(request)
        # Risk budget $2.50. Per-share stressed loss is $1.05 plus fee floors.
        self.assertEqual(result.qty, 2)
        self.assertLessEqual(result.components.planned_total_loss, 2.50)

    def test_sizing_uses_versioned_buy_and_sell_fee_components(self):
        request = SizingRequest(
            state=state(day_start_equity=10_000, week_start_equity=10_000),
            entry_limit=20,
            stop_trigger=19,
        )
        result = size_position(request)
        expected_buy = calculate_order_fees(
            PAPER_FEE_SCHEDULE, OrderSide.BUY, result.qty, request.entry_limit
        ).total
        expected_sell = calculate_order_fees(
            PAPER_FEE_SCHEDULE,
            OrderSide.SELL,
            result.qty,
            result.components.stressed_exit_price,
        ).total
        self.assertEqual(result.components.entry_fee, float(expected_buy))
        self.assertEqual(result.components.exit_fee, float(expected_sell))

    def test_caller_cannot_substitute_report_only_real_fee_schedule(self):
        with self.assertRaisesRegex(ValueError, "frozen paper fee"):
            SizingRequest(
                state=state(),
                entry_limit=20,
                stop_trigger=19,
                entry_fees=FeeSchedule.JP_US_BASIC_CASH_2026_08_13,
            )

    def test_slippage_uses_larger_of_fraction_and_floor(self):
        low_price = size_position(
            SizingRequest(
                state=state(day_start_equity=1_000, week_start_equity=1_000),
                entry_limit=2,
                stop_trigger=1.5,
            )
        )
        self.assertEqual(low_price.components.stop_slippage_per_share, 0.05)
        high_price = size_position(
            SizingRequest(state(), entry_limit=200, stop_trigger=190)
        )
        self.assertAlmostEqual(high_price.components.stop_slippage_per_share, 0.95)

    def test_one_share_can_fail_notional_cap(self):
        result = size_position(
            SizingRequest(
                state=state(day_start_equity=1_000, week_start_equity=1_000),
                entry_limit=101,
                stop_trigger=100,
            )
        )
        self.assertEqual(result.qty, 0)
        self.assertEqual(
            result.block_reasons, (RiskBlockReason.ONE_SHARE_EXCEEDS_NOTIONAL,)
        )

    def test_one_share_can_fail_risk_budget(self):
        result = size_position(
            SizingRequest(
                state=state(day_start_equity=100, week_start_equity=100),
                entry_limit=10,
                stop_trigger=9,
            )
        )
        self.assertEqual(result.qty, 0)
        self.assertEqual(
            result.block_reasons, (RiskBlockReason.ONE_SHARE_EXCEEDS_RISK,)
        )

    def test_gate_failure_returns_zero_without_sizing(self):
        result = size_position(
            SizingRequest(
                state=state(completed_roundtrips_today=1),
                entry_limit=100,
                stop_trigger=99,
            )
        )
        self.assertEqual(result.qty, 0)
        self.assertEqual(result.block_reasons, (RiskBlockReason.ROUNDTRIP_LIMIT,))

    def test_invalid_or_ambiguous_numbers_fail_closed(self):
        with self.assertRaises(TypeError):
            RiskState(True, 1000, 0, 0, 0)
        with self.assertRaises(ValueError):
            RiskState(math.inf, 1000, 0, 0, 0)
        with self.assertRaises(ValueError):
            SizingRequest(state(), entry_limit=100, stop_trigger=100)
        with self.assertRaises(TypeError):
            SizingRequest(
                state(),
                entry_limit=100,
                stop_trigger=99,
                entry_fees="JP_US_PAPER_STOCK_2026_08_13",
            )

    def test_models_are_frozen(self):
        policy = RiskPolicy()
        with self.assertRaises(FrozenInstanceError):
            policy.planned_risk_fraction = 1.0


if __name__ == "__main__":
    unittest.main()
