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


def policy(**overrides):
    values = dict(maximum_investment_cents=2_000_000)
    values.update(overrides)
    return RiskPolicy(**values)


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
        value = RiskPolicy()
        self.assertEqual(value.planned_risk_basis_points, 25)
        self.assertEqual(value.daily_loss_limit_basis_points, 75)
        self.assertEqual(value.weekly_loss_limit_basis_points, 200)
        self.assertIsNone(value.maximum_investment_cents)
        self.assertEqual(value.planned_risk_fraction, 0.0025)
        self.assertEqual(value.daily_loss_fraction, 0.0075)
        self.assertEqual(value.weekly_loss_fraction, 0.02)
        self.assertEqual(value.max_notional_fraction, 0.10)
        self.assertEqual(value.max_roundtrips_per_day, 1)

    def test_hard_basis_point_ceilings_ordering_and_exact_types(self):
        accepted = RiskPolicy(
            planned_risk_basis_points=100,
            daily_loss_limit_basis_points=200,
            weekly_loss_limit_basis_points=500,
            maximum_investment_cents=1,
        )
        self.assertEqual(accepted.weekly_loss_limit_basis_points, 500)
        for changes in (
            {"planned_risk_basis_points": 101},
            {"planned_risk_basis_points": 76},
            {"daily_loss_limit_basis_points": 201},
            {"daily_loss_limit_basis_points": 201, "weekly_loss_limit_basis_points": 500},
            {"weekly_loss_limit_basis_points": 501},
            {"planned_risk_basis_points": 80, "daily_loss_limit_basis_points": 75},
            {"daily_loss_limit_basis_points": 300, "weekly_loss_limit_basis_points": 200},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                RiskPolicy(**changes)
        for changes in (
            {"planned_risk_basis_points": 25.0},
            {"daily_loss_limit_basis_points": True},
            {"weekly_loss_limit_basis_points": "200"},
            {"maximum_investment_cents": 100.0},
        ):
            with self.subTest(changes=changes), self.assertRaises(TypeError):
                RiskPolicy(**changes)

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
    def test_missing_investment_limit_blocks_instead_of_meaning_unlimited(self):
        result = size_position(
            SizingRequest(state(), entry_limit=100, stop_trigger=99)
        )
        self.assertFalse(result.allowed)
        self.assertIsNone(result.investment_cap_cents)
        self.assertEqual(
            result.block_reasons,
            (RiskBlockReason.INVESTMENT_LIMIT_NOT_SET,),
        )

    def test_default_case_is_notional_limited_to_ten_percent(self):
        request = SizingRequest(
            state(), entry_limit=100, stop_trigger=98.5, policy=policy()
        )
        result = size_position(request)
        self.assertTrue(result.allowed)
        self.assertEqual(result.qty, 100)
        self.assertEqual(result.risk_budget, 250.0)
        self.assertEqual(result.notional_cap, 10_000.0)
        self.assertLessEqual(result.components.planned_total_loss, 250.0)
        self.assertLessEqual(result.qty * request.entry_limit, result.notional_cap)

    def test_absolute_investment_cap_includes_cent_rounded_buy_fee(self):
        blocked = size_position(
            SizingRequest(
                state(),
                entry_limit=100,
                stop_trigger=99,
                policy=policy(maximum_investment_cents=10_050),
            )
        )
        self.assertEqual(
            blocked.block_reasons,
            (RiskBlockReason.ONE_SHARE_EXCEEDS_INVESTMENT,),
        )
        allowed = size_position(
            SizingRequest(
                state(),
                entry_limit=100,
                stop_trigger=99,
                policy=policy(maximum_investment_cents=10_051),
            )
        )
        self.assertEqual(allowed.qty, 1)
        self.assertEqual(allowed.components.entry_cash_required, 100.51)

    def test_sizing_uses_remaining_daily_and_weekly_budgets(self):
        value = policy(
            planned_risk_basis_points=100,
            daily_loss_limit_basis_points=200,
            weekly_loss_limit_basis_points=500,
        )
        daily = size_position(
            SizingRequest(
                state=state(daily_pnl=-1_990.0),
                entry_limit=100,
                stop_trigger=99,
                policy=value,
            )
        )
        self.assertEqual(daily.risk_budget, 10.0)
        self.assertLessEqual(daily.components.planned_total_loss, 10.0)
        weekly = size_position(
            SizingRequest(
                state=state(weekly_pnl=-4_990.0),
                entry_limit=100,
                stop_trigger=99,
                policy=value,
            )
        )
        self.assertEqual(weekly.risk_budget, 10.0)
        self.assertLessEqual(weekly.components.planned_total_loss, 10.0)

    def test_risk_limited_integer_search_returns_largest_qty(self):
        request = SizingRequest(
            state=state(day_start_equity=1_000, week_start_equity=1_000),
            entry_limit=10,
            stop_trigger=9,
            policy=policy(),
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
            policy=policy(),
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
                policy=policy(),
            )

    def test_slippage_uses_larger_of_fraction_and_floor(self):
        low_price = size_position(
            SizingRequest(
                state=state(day_start_equity=1_000, week_start_equity=1_000),
                entry_limit=2,
                stop_trigger=1.5,
                policy=policy(),
            )
        )
        self.assertEqual(low_price.components.stop_slippage_per_share, 0.05)
        high_price = size_position(
            SizingRequest(
                state(), entry_limit=200, stop_trigger=190, policy=policy()
            )
        )
        self.assertAlmostEqual(high_price.components.stop_slippage_per_share, 0.95)

    def test_one_share_can_fail_notional_cap(self):
        result = size_position(
            SizingRequest(
                state=state(day_start_equity=1_000, week_start_equity=1_000),
                entry_limit=101,
                stop_trigger=100,
                policy=policy(),
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
                policy=policy(),
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
                policy=policy(),
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
            SizingRequest(
                state(), entry_limit=100, stop_trigger=100, policy=policy()
            )
        with self.assertRaises(TypeError):
            SizingRequest(
                state(),
                entry_limit=100,
                stop_trigger=99,
                entry_fees="JP_US_PAPER_STOCK_2026_08_13",
                policy=policy(),
            )

    def test_models_are_frozen(self):
        value = RiskPolicy()
        with self.assertRaises(FrozenInstanceError):
            value.planned_risk_basis_points = 100


if __name__ == "__main__":
    unittest.main()
