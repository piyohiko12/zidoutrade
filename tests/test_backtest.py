from dataclasses import replace
from datetime import date, datetime, time, timedelta
import json
import unittest
from zoneinfo import ZoneInfo

from zidoutrade.backtest import (
    BacktestConfig,
    BacktestVariant,
    DatedTrend,
    MODEL_ID,
    Q013_ATR_CLOSE_CAP,
    RESULT_STATUS,
    SessionBoundary,
    _variant_allows_entry,
    run_candle_backtest,
)
from zidoutrade.models import CompletedBar15m, ReasonCode, TrendEligibility
from zidoutrade.risk import RiskPolicy


NY = ZoneInfo("America/New_York")
SYMBOL = "US.TEST"
DAY1 = date(2026, 8, 12)
DAY2 = date(2026, 8, 13)


def starts_for(day):
    opened = datetime.combine(day, time(9, 30), tzinfo=NY)
    return tuple(opened + timedelta(minutes=15 * index) for index in range(26))


def synthetic_bars(*, include_exit=True):
    starts = starts_for(DAY1) + starts_for(DAY2)[: (12 if include_exit else 3)]
    closes = [120.0 - index for index in range(26)]
    closes.extend((94.0, 95.0, 101.0))
    closes.extend(101.0 + 0.1 * index for index in range(len(starts) - len(closes)))
    bars = []
    for index, (start, close) in enumerate(zip(starts, closes)):
        opening = close if index == 0 else closes[index - 1]
        high = max(opening, close) + 0.1
        low = min(opening, close) - 0.1
        volume = 1_000_000.0
        if start.date() == DAY2 and start.time() == time(9, 45):
            high = 100.6
        if start.date() == DAY2 and start.time() == time(10, 0):
            high = 101.1
            volume = 1_500_000.0
        bars.append(
            CompletedBar15m(
                SYMBOL,
                start,
                start + timedelta(minutes=15),
                opening,
                high,
                low,
                close,
                volume,
            )
        )
    return tuple(bars)


def trends():
    return (
        DatedTrend(DAY1, TrendEligibility(False, False, True)),
        DatedTrend(DAY2, TrendEligibility(True, True, True)),
    )


def sessions(*, day2_close=time(16, 0)):
    return (
        SessionBoundary(DAY1, datetime.combine(DAY1, time(16, 0), tzinfo=NY)),
        SessionBoundary(DAY2, datetime.combine(DAY2, day2_close, tzinfo=NY)),
    )


def config(**changes):
    values = {
        "symbol": SYMBOL,
        "risk_policy": RiskPolicy(maximum_investment_cents=1_000_000),
    }
    values.update(changes)
    return BacktestConfig(**values)


class CandleBacktestTests(unittest.TestCase):
    def test_default_variant_equals_explicit_baseline_and_is_reported(self):
        bars = synthetic_bars()
        implicit = run_candle_backtest(bars, bars, trends(), sessions(), config())
        explicit = run_candle_backtest(
            bars,
            bars,
            trends(),
            sessions(),
            config(strategy_variant=BacktestVariant.BASELINE),
        )

        self.assertEqual(implicit.to_dict(), explicit.to_dict())
        self.assertEqual(
            implicit.strategy_variant_id,
            BacktestVariant.BASELINE.value,
        )
        self.assertEqual(
            implicit.to_dict()["config"]["strategy_variant_id"],
            BacktestVariant.BASELINE.value,
        )

    def test_variant_rejects_strings_instead_of_coercing_them(self):
        with self.assertRaisesRegex(TypeError, "exact BacktestVariant"):
            config(
                strategy_variant="RSI_AUTOPILOT_V1_Q013_ATR_CAP_0050_SHADOW"
            )

    def test_q013_atr_cap_includes_boundary_and_fails_closed(self):
        bars = synthetic_bars()
        index = len(bars) - 1
        close = bars[index].close
        prefix = (None,) * index

        self.assertTrue(
            _variant_allows_entry(
                index,
                bars,
                prefix + (close * Q013_ATR_CLOSE_CAP,),
                BacktestVariant.Q013_ATR_CAP_0050_SHADOW,
            )
        )
        self.assertFalse(
            _variant_allows_entry(
                index,
                bars,
                prefix + (close * Q013_ATR_CLOSE_CAP + 1e-12,),
                BacktestVariant.Q013_ATR_CAP_0050_SHADOW,
            )
        )
        for blocked in (None, 0.0, float("nan"), float("inf")):
            self.assertFalse(
                _variant_allows_entry(
                    index,
                    bars,
                    prefix + (blocked,),
                    BacktestVariant.Q013_ATR_CAP_0050_SHADOW,
                )
            )

    def test_q013_variant_only_removes_baseline_candidates(self):
        bars = synthetic_bars()
        baseline = set(range(len(bars)))
        atr = tuple(
            bar.close * (Q013_ATR_CLOSE_CAP if index % 2 else 0.0060)
            for index, bar in enumerate(bars)
        )
        accepted = {
            index
            for index in baseline
            if _variant_allows_entry(
                index,
                bars,
                atr,
                BacktestVariant.Q013_ATR_CAP_0050_SHADOW,
            )
        }

        self.assertTrue(accepted)
        self.assertLess(len(accepted), len(baseline))
        self.assertLessEqual(accepted, baseline)

    def test_q013_gate_is_independent_of_future_bars_and_atr_values(self):
        bars = synthetic_bars()
        index = len(bars) - 3
        allowed_atr = bars[index].close * Q013_ATR_CLOSE_CAP
        first = (None,) * index + (allowed_atr, 999.0, 999.0)
        second = (None,) * index + (allowed_atr, 0.0, 0.0)

        self.assertEqual(
            _variant_allows_entry(
                index,
                bars,
                first,
                BacktestVariant.Q013_ATR_CAP_0050_SHADOW,
            ),
            _variant_allows_entry(
                index,
                bars,
                second,
                BacktestVariant.Q013_ATR_CAP_0050_SHADOW,
            ),
        )

    def test_signal_enters_no_earlier_than_next_raw_bar_and_reports_costs(self):
        bars = synthetic_bars()
        result = run_candle_backtest(bars, bars, trends(), sessions(), config())

        self.assertEqual(result.model_id, MODEL_ID)
        self.assertEqual(result.status, RESULT_STATUS)
        self.assertEqual(result.entry_signal_count, 1)
        self.assertEqual(result.trade_count, 1)
        trade = result.trades[0]
        self.assertEqual(trade.signal_bar_end.time(), time(10, 15))
        self.assertEqual(trade.entry_time, trade.signal_bar_end)
        self.assertEqual(trade.raw_entry_reference, 101.0)
        self.assertGreater(trade.entry_price, trade.raw_entry_reference)
        self.assertGreater(trade.quantity, 0)
        self.assertGreater(trade.fees, 0.0)
        self.assertAlmostEqual(result.total_net_pnl, result.total_gross_pnl - result.total_fees)
        self.assertAlmostEqual(result.final_equity, result.initial_equity + result.total_net_pnl)
        self.assertIn("Historical fills are unobservable", " ".join(result.assumptions))
        json.dumps(result.to_dict(), allow_nan=False)

    def test_stop_touch_wins_same_bar_conflict_and_uses_gap_pessimism(self):
        qfq = synthetic_bars()
        raw = list(qfq)
        entry_index = next(
            index
            for index, bar in enumerate(raw)
            if bar.start.date() == DAY2 and bar.start.time() == time(10, 15)
        )
        gap_index = entry_index + 1
        gap_bar = raw[gap_index]
        raw[gap_index] = replace(
            gap_bar,
            open=90.0,
            high=max(gap_bar.high, gap_bar.close),
            low=89.0,
        )

        result = run_candle_backtest(qfq, tuple(raw), trends(), sessions(), config())
        trade = result.trades[0]
        self.assertEqual(trade.exit_reason, ReasonCode.STOP_LOSS)
        self.assertEqual(trade.bars_held, 2)
        self.assertEqual(trade.raw_exit_reference, 90.0)
        self.assertLess(trade.exit_price, trade.raw_exit_reference)

    def test_one_roundtrip_per_session_and_max_hold_are_enforced(self):
        bars = synthetic_bars()
        result = run_candle_backtest(bars, bars, trends(), sessions(), config())
        self.assertEqual(result.trade_count, 1)
        self.assertEqual(result.trades[0].exit_reason, ReasonCode.MAX_HOLD)
        self.assertEqual(result.trades[0].bars_held, 8)

    def test_close_approach_uses_the_frozen_stressed_exit_cushion(self):
        bars = synthetic_bars()
        result = run_candle_backtest(
            bars, bars, trends(), sessions(day2_close=time(11, 0)), config()
        )
        trade = result.trades[0]
        self.assertEqual(trade.exit_reason, ReasonCode.CLOSE_APPROACHING)
        self.assertAlmostEqual(trade.exit_price, trade.raw_exit_reference * 0.9945)

    def test_risk_and_investment_policy_can_block_the_proxy_entry(self):
        bars = synthetic_bars()
        tiny_cap = RiskPolicy(maximum_investment_cents=100)
        result = run_candle_backtest(
            bars, bars, trends(), sessions(), config(risk_policy=tiny_cap)
        )
        self.assertEqual(result.entry_signal_count, 1)
        self.assertEqual(result.risk_blocked_signal_count, 1)
        self.assertEqual(result.trade_count, 0)
        self.assertEqual(result.final_equity, result.initial_equity)

    def test_signal_without_safe_same_session_next_bar_is_not_filled(self):
        bars = synthetic_bars(include_exit=False)
        result = run_candle_backtest(
            bars, bars, trends(), sessions(day2_close=time(10, 15)), config()
        )
        self.assertEqual(result.entry_signal_count, 1)
        self.assertEqual(result.no_next_bar_signal_count, 1)
        self.assertEqual(result.trade_count, 0)

    def test_raw_and_qfq_timestamps_must_match_exactly(self):
        qfq = synthetic_bars()
        raw = tuple(
            replace(
                bar,
                start=bar.start + timedelta(days=1),
                end=bar.end + timedelta(days=1),
            )
            for bar in qfq
        )
        with self.assertRaisesRegex(ValueError, "timestamps differ"):
            run_candle_backtest(qfq, raw, trends(), sessions(), config())

    def test_proxy_assumptions_are_frozen_against_result_tuning(self):
        with self.assertRaisesRegex(ValueError, "frozen"):
            config(assumed_spread_bps=0.0)
        with self.assertRaisesRegex(ValueError, "maximum_investment"):
            config(risk_policy=RiskPolicy())


if __name__ == "__main__":
    unittest.main()
