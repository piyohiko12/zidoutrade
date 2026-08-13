from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from zidoutrade.models import (
    CompletedBar15m,
    DecisionAction,
    MarketGates,
    PositionSnapshot,
    ReasonCode,
    StrategyContext,
    TrendEligibility,
)
from zidoutrade.strategy import evaluate_strategy


ET = ZoneInfo("America/New_York")
DAY = (2026, 8, 10)


def make_bar(hour, minute, close, *, high=None, symbol="US.TEST"):
    start = datetime(*DAY, hour, minute, tzinfo=ET)
    chosen_high = close + 0.25 if high is None else high
    return CompletedBar15m(
        symbol,
        start,
        start + timedelta(minutes=15),
        close,
        chosen_high,
        close - 0.5,
        close,
        10_000,
    )


def entry_bars(start_hour=9, start_minute=30):
    initial = datetime(*DAY, start_hour, start_minute, tzinfo=ET)
    specs = ((30.0, 30.25), (31.0, 31.5), (32.0, 32.25))
    result = []
    for index, (close, high) in enumerate(specs):
        at = initial + timedelta(minutes=15 * index)
        result.append(make_bar(at.hour, at.minute, close, high=high))
    return tuple(result)


def context(**overrides):
    bars = overrides.pop("bars", entry_bars())
    defaults = dict(
        active_symbol="US.TEST",
        selected_symbol="US.TEST",
        bars=bars,
        rsi_values=(29.0, 35.0, 35.0001),
        trend=TrendEligibility(True, True, True),
        gates=MarketGates(True, True, True),
        now=bars[-1].end + timedelta(seconds=2),
        position=None,
        traded_roundtrips_today=0,
    )
    defaults.update(overrides)
    return StrategyContext(**defaults)


def position(**overrides):
    defaults = dict(
        entry_raw=100.0,
        atr_raw=2.0,
        current_raw_price=101.0,
        bars_held=2,
        entry_time=datetime(*DAY, 11, 0, tzinfo=ET),
        exchange_close=datetime(*DAY, 16, 0, tzinfo=ET),
        emergency_exit=False,
    )
    defaults.update(overrides)
    return PositionSnapshot(**defaults)


class EntryDecisionTests(unittest.TestCase):
    def test_exact_mandatory_entry_signal(self):
        result = evaluate_strategy(context())
        self.assertEqual(result.action, DecisionAction.ENTER)
        self.assertEqual(result.reasons, (ReasonCode.ENTRY_SIGNAL_CONFIRMED,))
        self.assertEqual(result.signal_bar_end, entry_bars()[-1].end)

    def test_active_symbol_is_never_substituted(self):
        result = evaluate_strategy(context(selected_symbol="US.OTHER"))
        self.assertEqual(result.action, DecisionAction.WAIT)
        self.assertEqual(result.reasons, (ReasonCode.ACTIVE_SYMBOL_MISMATCH,))

    def test_all_failed_external_gates_are_explained(self):
        result = evaluate_strategy(
            context(
                trend=TrendEligibility(False, False, False),
                gates=MarketGates(False, False, False),
                traded_roundtrips_today=1,
            )
        )
        self.assertEqual(result.action, DecisionAction.WAIT)
        self.assertEqual(
            result.reasons[:6],
            (
                ReasonCode.DAILY_DATA_INVALID,
                ReasonCode.SYMBOL_TREND_BLOCKED,
                ReasonCode.SPY_TREND_BLOCKED,
                ReasonCode.MARKET_CLOSED,
                ReasonCode.INTRADAY_DATA_INVALID,
                ReasonCode.SPREAD_TOO_WIDE,
            ),
        )
        self.assertIn(ReasonCode.ROUNDTRIP_LIMIT_REACHED, result.reasons)

    def test_recent_three_must_include_rsi_30_or_lower(self):
        result = evaluate_strategy(context(rsi_values=(30.0001, 35.0, 36.0)))
        self.assertIn(ReasonCode.RSI_NOT_OVERSOLD_RECENTLY, result.reasons)
        result_at_boundary = evaluate_strategy(context(rsi_values=(30.0, 35.0, 36.0)))
        self.assertEqual(result_at_boundary.action, DecisionAction.ENTER)

    def test_recovery_is_strictly_above_35_after_prior_at_or_below_35(self):
        equal = evaluate_strategy(context(rsi_values=(29.0, 35.0, 35.0)))
        self.assertIn(ReasonCode.RSI_RECOVERY_NOT_CONFIRMED, equal.reasons)
        prior_above = evaluate_strategy(context(rsi_values=(29.0, 35.0001, 36.0)))
        self.assertIn(ReasonCode.RSI_RECOVERY_NOT_CONFIRMED, prior_above.reasons)

    def test_indicator_warmup_fails_closed(self):
        result = evaluate_strategy(context(rsi_values=(None, 35.0, 36.0)))
        self.assertIn(ReasonCode.INDICATOR_NOT_READY, result.reasons)

    def test_close_must_be_strictly_above_previous_high(self):
        bars = list(entry_bars())
        bars[-1] = make_bar(10, 0, 31.5, high=31.75)
        result = evaluate_strategy(context(bars=tuple(bars)))
        self.assertIn(ReasonCode.PRICE_CONFIRMATION_MISSING, result.reasons)

    def test_entry_window_boundaries_are_inclusive(self):
        # Recent RSI can span sessions.  This is the earliest valid RTH signal:
        # the prior session's last bar followed by today's first two bars.
        prior_start = datetime(2026, 8, 7, 15, 45, tzinfo=ET)
        prior = CompletedBar15m(
            "US.TEST", prior_start, prior_start + timedelta(minutes=15),
            30.0, 30.25, 29.5, 30.0, 10_000,
        )
        at_ten = (
            prior,
            make_bar(9, 30, 31.0, high=31.5),
            make_bar(9, 45, 32.0, high=32.25),
        )
        at_close = entry_bars(14, 30)  # latest completed bar ends at 15:15
        self.assertEqual(evaluate_strategy(context(bars=at_ten)).action, DecisionAction.ENTER)
        self.assertEqual(evaluate_strategy(context(bars=at_close)).action, DecisionAction.ENTER)

    def test_outside_entry_window_is_blocked(self):
        previous_day = (2026, 8, 7)
        prior_one_start = datetime(*previous_day, 15, 30, tzinfo=ET)
        prior_two_start = datetime(*previous_day, 15, 45, tzinfo=ET)
        too_early = (
            CompletedBar15m(
                "US.TEST", prior_one_start, prior_one_start + timedelta(minutes=15),
                30.0, 30.25, 29.5, 30.0, 10_000,
            ),
            CompletedBar15m(
                "US.TEST", prior_two_start, prior_two_start + timedelta(minutes=15),
                31.0, 31.5, 30.5, 31.0, 10_000,
            ),
            make_bar(9, 30, 32.0, high=32.25),
        )  # latest completed bar ends at 09:45
        result = evaluate_strategy(context(bars=too_early))
        self.assertIn(ReasonCode.OUTSIDE_ENTRY_WINDOW, result.reasons)

    def test_out_of_order_bars_are_rejected_not_sorted(self):
        bars = entry_bars()
        with self.assertRaises(ValueError):
            evaluate_strategy(context(bars=(bars[1], bars[0], bars[2])))


class ExitDecisionTests(unittest.TestCase):
    def _exit_context(self, pos, rsi=40.0, now=None, gates=None):
        bars = entry_bars(10, 30)
        return context(
            bars=bars,
            rsi_values=(29.0, 35.0, rsi),
            now=now or datetime(*DAY, 11, 45, tzinfo=ET),
            position=pos,
            gates=gates or MarketGates(True, True, True),
        )

    def test_emergency_has_priority_over_every_other_exit(self):
        pos = position(emergency_exit=True, current_raw_price=90, bars_held=8)
        result = evaluate_strategy(self._exit_context(pos, rsi=70))
        self.assertEqual(result.reasons, (ReasonCode.EMERGENCY_EXIT,))

    def test_stop_at_entry_minus_1_5_atr_is_inclusive_and_second_priority(self):
        pos = position(current_raw_price=97.0)  # 100 - 1.5*2
        result = evaluate_strategy(self._exit_context(pos, rsi=70))
        self.assertEqual(result.action, DecisionAction.EXIT)
        self.assertEqual(result.reasons, (ReasonCode.STOP_LOSS,))
        self.assertEqual(result.stop_price, 97.0)

    def test_rsi_60_exit_is_inclusive(self):
        result = evaluate_strategy(self._exit_context(position(), rsi=60.0))
        self.assertEqual(result.reasons, (ReasonCode.RSI_EXIT,))

    def test_max_eight_bars_or_two_hours(self):
        by_bars = evaluate_strategy(self._exit_context(position(bars_held=8)))
        self.assertEqual(by_bars.reasons, (ReasonCode.MAX_HOLD,))
        by_clock = evaluate_strategy(
            self._exit_context(position(), now=datetime(*DAY, 13, 0, tzinfo=ET))
        )
        self.assertEqual(by_clock.reasons, (ReasonCode.MAX_HOLD,))

    def test_exit_fifteen_minutes_before_close(self):
        result = evaluate_strategy(
            self._exit_context(
                position(entry_time=datetime(*DAY, 14, 30, tzinfo=ET)),
                now=datetime(*DAY, 15, 45, tzinfo=ET),
            )
        )
        self.assertEqual(result.reasons, (ReasonCode.CLOSE_APPROACHING,))

    def test_no_fixed_profit_target(self):
        result = evaluate_strategy(
            self._exit_context(position(current_raw_price=130), rsi=59.999)
        )
        self.assertEqual(result.action, DecisionAction.WAIT)
        self.assertEqual(result.reasons, (ReasonCode.HOLD_NO_EXIT,))

    def test_entry_gates_do_not_suppress_known_position_exit(self):
        result = evaluate_strategy(
            self._exit_context(
                position(current_raw_price=97),
                gates=MarketGates(False, False, False),
            )
        )
        self.assertEqual(result.reasons, (ReasonCode.STOP_LOSS,))


if __name__ == "__main__":
    unittest.main()
