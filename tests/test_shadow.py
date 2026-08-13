import unittest

from zidoutrade.shadow import (
    ExecutionCosts,
    ShadowStrategy,
    ShadowTrade,
    summarize_shadow,
)


class ShadowEvaluationTests(unittest.TestCase):
    def test_net_profit_includes_every_cost_component(self):
        trade = ShadowTrade(
            strategy=ShadowStrategy.USER_SELECTED,
            opportunity_id="2026-01-01:US.TEST",
            symbol="US.TEST",
            filled=True,
            quantity=10,
            entry_price=100.0,
            exit_price=101.0,
            costs=ExecutionCosts(
                commission=1.0,
                regulatory_fees=0.2,
                spread=0.5,
                slippage=0.3,
                other=0.1,
            ),
        )
        self.assertAlmostEqual(trade.gross_pnl, 10.0)
        self.assertAlmostEqual(trade.net_pnl, 7.9)

    def test_unfilled_signal_is_zero_pnl_but_remains_in_fill_rate(self):
        trades = [
            ShadowTrade(
                ShadowStrategy.USER_SELECTED,
                "one",
                "US.TEST",
                False,
            ),
            ShadowTrade(
                ShadowStrategy.USER_SELECTED,
                "two",
                "US.TEST",
                True,
                1,
                10.0,
                11.0,
                ExecutionCosts(commission=0.1),
            ),
        ]
        summary = summarize_shadow(trades, ShadowStrategy.USER_SELECTED)
        self.assertEqual(summary.opportunity_count, 2)
        self.assertEqual(summary.filled_trade_count, 1)
        self.assertEqual(summary.fill_rate, 0.5)
        self.assertAlmostEqual(summary.total_net_pnl, 0.9)

    def test_strategies_are_not_pooled(self):
        trades = [
            ShadowTrade(ShadowStrategy.USER_SELECTED, "u", "US.A", True, 1, 10, 11),
            ShadowTrade(ShadowStrategy.FIXED_BASELINE, "b", "US.B", True, 1, 10, 9),
        ]
        user = summarize_shadow(trades, ShadowStrategy.USER_SELECTED)
        base = summarize_shadow(trades, ShadowStrategy.FIXED_BASELINE)
        self.assertEqual(user.total_net_pnl, 1)
        self.assertEqual(base.total_net_pnl, -1)

    def test_duplicate_opportunity_is_rejected(self):
        trade = ShadowTrade(ShadowStrategy.USER_SELECTED, "dup", "US.A", False)
        with self.assertRaises(ValueError):
            summarize_shadow([trade, trade], ShadowStrategy.USER_SELECTED)

    def test_invalid_and_nonfinite_values_fail_closed(self):
        with self.assertRaises(ValueError):
            ExecutionCosts(slippage=float("nan"))
        with self.assertRaises(ValueError):
            ShadowTrade(ShadowStrategy.USER_SELECTED, "x", "AAPL", False)
        with self.assertRaises(ValueError):
            ShadowTrade(ShadowStrategy.USER_SELECTED, "x", "US.A", False, 1, 10, 11)

    def test_profit_factor_undefined_without_loss(self):
        trade = ShadowTrade(ShadowStrategy.USER_SELECTED, "x", "US.A", True, 1, 10, 11)
        summary = summarize_shadow([trade], ShadowStrategy.USER_SELECTED)
        self.assertIsNone(summary.profit_factor)


if __name__ == "__main__":
    unittest.main()
