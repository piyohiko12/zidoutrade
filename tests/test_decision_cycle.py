from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from zidoutrade.decision_cycle import (
    CycleWaitReason,
    DecisionCycleRequest,
    run_decision_cycle,
)
from zidoutrade.exchange_calendar import (
    FrozenExchangeCalendar,
    FrozenSession,
    calendar_payload_sha256,
)
from zidoutrade.fees import FeeSchedule, OrderSide, calculate_order_fees
from zidoutrade.market_data import (
    FrozenAdjustmentScale,
    IndicatorBarSeries,
    MarketState,
    PriceScale,
    QuoteSnapshot,
)
from zidoutrade.models import (
    CompletedBar15m,
    DecisionAction,
    PositionSnapshot,
    ReasonCode,
    TrendEligibility,
)
from zidoutrade.risk import RiskBlockReason, RiskPolicy, RiskState
from zidoutrade.selection import (
    PresentedCandidate,
    SelectionState,
    SelectionWorkflow,
)


NY = ZoneInfo("America/New_York")
SYMBOL = "US.TEST"
PRIOR_DAY = date(2026, 8, 12)
DAY = date(2026, 8, 13)


def frozen_calendar(close_hour=16):
    sessions = (
        FrozenSession(
            PRIOR_DAY,
            datetime(2026, 8, 12, 9, 30, tzinfo=NY),
            datetime(2026, 8, 12, 16, 0, tzinfo=NY),
        ),
        FrozenSession(
            DAY,
            datetime(2026, 8, 13, 9, 30, tzinfo=NY),
            datetime(2026, 8, 13, close_hour, 0, tzinfo=NY),
        ),
    )
    digest = calendar_payload_sha256(
        source_revision="synthetic-decision-cycle-v1",
        covered_from=PRIOR_DAY,
        covered_through=DAY,
        sessions=sessions,
    )
    return FrozenExchangeCalendar(
        "synthetic-decision-cycle-v1",
        PRIOR_DAY,
        DAY,
        sessions,
        digest,
    )


def completed_bars(calendar, *, qfq_multiplier=1.0):
    # 16 prior-session declines, then a small uptick and strong recovery.  The
    # last three Wilder RSI values are approximately 0, 7.14, 36.47, and the
    # final close breaks the previous high.
    starts = calendar.sessions[0].bar_starts() + calendar.sessions[1].bar_starts()[:3]
    declining_count = len(starts) - 3
    closes = tuple(
        (120.0 - index) * qfq_multiplier for index in range(declining_count)
    )
    prior = closes[-1]
    closes += (
        (prior - 1.0 * qfq_multiplier),
        prior,
        (prior + 6.0 * qfq_multiplier),
    )
    bars = []
    for index, (start, close) in enumerate(zip(starts, closes)):
        opening = closes[index - 1] if index else close
        high = max(opening, close) + 0.1 * qfq_multiplier
        if index == len(closes) - 2:
            high = closes[-1] / 1.004
        bars.append(
            CompletedBar15m(
                symbol=SYMBOL,
                start=start,
                end=start + timedelta(minutes=15),
                open=opening,
                high=high,
                low=min(opening, close) - 0.1 * qfq_multiplier,
                close=close,
                volume=1_500_000 if index == len(closes) - 1 else 1_000_000,
            )
        )
    return tuple(bars)


def indicator_series(calendar, *, factor=1.0):
    return IndicatorBarSeries(
        bars=completed_bars(calendar, qfq_multiplier=1.0 / factor),
        scale=PriceScale.QFQ,
        adjustment_scale=FrozenAdjustmentScale(
            symbol=SYMBOL,
            target_session=DAY,
            source_daily_session=PRIOR_DAY,
            qfq_to_raw=factor,
            frozen_at=calendar.sessions[-1].open_at,
            calendar_sha256=calendar.sha256,
        ),
    )


def locked_selection(symbol=SYMBOL):
    draft = SelectionWorkflow.new_draft(
        target_session=DAY.isoformat(),
        presented_candidates=(PresentedCandidate(symbol, 1, True),),
        selected_symbol=symbol,
        now=datetime(2026, 8, 12, 20, 0, tzinfo=timezone.utc),
    )
    validated = SelectionWorkflow.validate(draft, now=datetime(2026, 8, 12, 20, 1, tzinfo=timezone.utc))
    armed = SelectionWorkflow.arm(validated, now=datetime(2026, 8, 12, 20, 2, tzinfo=timezone.utc))
    return SelectionWorkflow.lock_session(
        armed,
        session_date=DAY.isoformat(),
        now=datetime(2026, 8, 13, 13, 30, tzinfo=timezone.utc),
    )


def risk_state(**changes):
    values = dict(
        day_start_equity=100_000.0,
        week_start_equity=100_000.0,
        daily_pnl=0.0,
        weekly_pnl=0.0,
        completed_roundtrips_today=0,
    )
    values.update(changes)
    return RiskState(**values)


def request(**changes):
    calendar = changes.pop("calendar", frozen_calendar())
    now = changes.pop("now", datetime(2026, 8, 13, 10, 16, tzinfo=NY))
    values = dict(
        indicator_bars=indicator_series(calendar),
        quote=QuoteSnapshot(
            symbol=SYMBOL,
            observed_at=now - timedelta(seconds=1),
            bid=100.98,
            ask=101.0,
            last=100.99,
            market_state=MarketState.OPEN,
        ),
        locked_selection=locked_selection(),
        calendar=calendar,
        risk_state=risk_state(),
        risk_policy=RiskPolicy(maximum_investment_cents=1_000_000),
        trend=TrendEligibility(True, True, True),
        now=now,
    )
    values.update(changes)
    return DecisionCycleRequest(**values)


class EntryCycleTests(unittest.TestCase):
    def test_entry_computes_indicators_raw_atr_and_exact_paper_fees(self):
        result = run_decision_cycle(request())
        self.assertEqual(result.action, DecisionAction.ENTER)
        self.assertEqual(result.wait_reasons, ())
        self.assertEqual(result.strategy.reasons, (ReasonCode.ENTRY_SIGNAL_CONFIRMED,))
        self.assertGreater(result.latest_rsi, 35.0)
        self.assertLess(result.latest_rsi, 37.0)
        self.assertAlmostEqual(result.latest_atr_raw, result.latest_atr_qfq)
        self.assertTrue(result.sizing.allowed)
        self.assertIs(
            result.fee_schedule,
            FeeSchedule.JP_US_PAPER_STOCK_2026_08_13,
        )
        expected_buy = calculate_order_fees(
            result.fee_schedule, OrderSide.BUY, result.sizing.qty, 101.0
        ).total
        expected_sell = calculate_order_fees(
            result.fee_schedule,
            OrderSide.SELL,
            result.sizing.qty,
            result.sizing.components.stressed_exit_price,
        ).total
        self.assertEqual(result.sizing.components.entry_fee, float(expected_buy))
        self.assertEqual(result.sizing.components.exit_fee, float(expected_sell))
        self.assertLessEqual(
            result.sizing.components.planned_total_loss,
            result.sizing.risk_budget,
        )

    def test_qfq_atr_is_converted_to_raw_with_frozen_factor(self):
        calendar = frozen_calendar()
        result = run_decision_cycle(
            request(calendar=calendar, indicator_bars=indicator_series(calendar, factor=2.0))
        )
        self.assertEqual(result.action, DecisionAction.ENTER)
        self.assertAlmostEqual(result.latest_atr_raw, result.latest_atr_qfq * 2.0)
        self.assertAlmostEqual(
            result.sizing.components.stop_distance_per_share,
            1.5 * result.latest_atr_raw,
        )

    def test_daily_loss_limit_returns_typed_risk_wait(self):
        result = run_decision_cycle(request(risk_state=risk_state(daily_pnl=-750.0)))
        self.assertEqual(result.action, DecisionAction.WAIT)
        self.assertEqual(result.wait_reasons, (CycleWaitReason.RISK_BLOCKED,))
        self.assertEqual(
            result.sizing.block_reasons,
            (RiskBlockReason.DAILY_LOSS_LIMIT,),
        )

    def test_cycle_has_no_broker_import_or_dispatch(self):
        with patch("builtins.__import__", wraps=__import__) as importer:
            result = run_decision_cycle(request())
        self.assertEqual(result.action, DecisionAction.ENTER)
        imported = tuple(str(call.args[0]) for call in importer.call_args_list if call.args)
        self.assertFalse(any("moomoo" in name.lower() or "broker" in name.lower() for name in imported))


class FailClosedCycleTests(unittest.TestCase):
    def test_spread_gate_is_10_bps_inclusive_and_rejects_above(self):
        base = request()

        def bid_for_spread_bps(ask, spread_bps):
            fraction = spread_bps / 10_000.0
            return ask * (2.0 - fraction) / (2.0 + fraction)

        at_limit = replace(
            base.quote,
            bid=bid_for_spread_bps(base.quote.ask, 10.0),
        )
        self.assertAlmostEqual(at_limit.spread_bps, 10.0)
        self.assertEqual(
            run_decision_cycle(replace(base, quote=at_limit)).action,
            DecisionAction.ENTER,
        )

        above_limit = replace(
            base.quote,
            bid=bid_for_spread_bps(base.quote.ask, 10.001),
        )
        result = run_decision_cycle(replace(base, quote=above_limit))
        self.assertEqual(result.wait_reasons, (CycleWaitReason.STRATEGY_WAIT,))
        self.assertIn(ReasonCode.SPREAD_TOO_WIDE, result.strategy.reasons)

    def test_unlocked_or_unpermitted_selection_waits(self):
        locked = locked_selection()
        unlocked = replace(locked, state=SelectionState.ARMED_NEXT_SESSION, locked_at=None)
        result = run_decision_cycle(request(locked_selection=unlocked))
        self.assertEqual(result.wait_reasons, (CycleWaitReason.SELECTION_NOT_LOCKED,))
        denied = replace(locked, trade_permitted=False)
        result = run_decision_cycle(request(locked_selection=denied))
        self.assertEqual(result.wait_reasons, (CycleWaitReason.SELECTION_NOT_PERMITTED,))

    def test_forged_locked_selection_and_session_mismatch_wait(self):
        locked = locked_selection()
        forged = replace(locked, locked_at="not-a-time")
        self.assertEqual(
            run_decision_cycle(request(locked_selection=forged)).wait_reasons,
            (CycleWaitReason.SELECTION_INVALID,),
        )
        wrong_day = replace(locked, target_session=PRIOR_DAY.isoformat())
        self.assertEqual(
            run_decision_cycle(request(locked_selection=wrong_day)).wait_reasons,
            (CycleWaitReason.SELECTION_SESSION_MISMATCH,),
        )

    def test_symbol_quote_or_scale_identity_mismatch_waits(self):
        base = request()
        wrong_quote = replace(base.quote, symbol="US.OTHER")
        self.assertEqual(
            run_decision_cycle(replace(base, quote=wrong_quote)).wait_reasons,
            (CycleWaitReason.MARKET_DATA_INVALID,),
        )
        wrong_scale = replace(
            base.indicator_bars.adjustment_scale,
            calendar_sha256="0" * 64,
        )
        wrong_series = replace(base.indicator_bars, adjustment_scale=wrong_scale)
        self.assertEqual(
            run_decision_cycle(replace(base, indicator_bars=wrong_series)).wait_reasons,
            (CycleWaitReason.CALENDAR_MISMATCH,),
        )

    def test_stale_quote_and_missing_latest_bar_wait(self):
        base = request()
        stale = replace(base.quote, observed_at=base.now - timedelta(seconds=3))
        self.assertEqual(
            run_decision_cycle(replace(base, quote=stale)).wait_reasons,
            (CycleWaitReason.MARKET_DATA_STALE,),
        )
        missing = replace(base.indicator_bars, bars=base.indicator_bars.bars[:-1])
        self.assertEqual(
            run_decision_cycle(replace(base, indicator_bars=missing)).wait_reasons,
            (CycleWaitReason.MARKET_DATA_STALE,),
        )

    def test_wide_spread_and_closed_market_remain_strategy_waits(self):
        base = request()
        wide = replace(base.quote, bid=90.0)
        result = run_decision_cycle(replace(base, quote=wide))
        self.assertEqual(result.wait_reasons, (CycleWaitReason.STRATEGY_WAIT,))
        self.assertIn(ReasonCode.SPREAD_TOO_WIDE, result.strategy.reasons)
        closed = replace(base.quote, market_state=MarketState.CLOSED)
        result = run_decision_cycle(replace(base, quote=closed))
        self.assertEqual(result.wait_reasons, (CycleWaitReason.STRATEGY_WAIT,))
        self.assertIn(ReasonCode.MARKET_CLOSED, result.strategy.reasons)


class ExitCycleTests(unittest.TestCase):
    def test_known_position_exit_uses_raw_bid_and_does_not_run_entry_sizing(self):
        base = request()
        position = PositionSnapshot(
            entry_raw=90.0,
            atr_raw=1.0,
            current_raw_price=base.quote.bid,
            bars_held=8,
            entry_time=datetime(2026, 8, 13, 9, 45, tzinfo=NY),
            exchange_close=base.calendar.sessions[-1].close_at,
        )
        result = run_decision_cycle(replace(base, position=position))
        self.assertEqual(result.action, DecisionAction.EXIT)
        self.assertEqual(result.strategy.reasons, (ReasonCode.MAX_HOLD,))
        self.assertIsNone(result.sizing)

    def test_position_quote_and_session_identity_are_exact(self):
        base = request()
        valid = PositionSnapshot(
            entry_raw=90.0,
            atr_raw=1.0,
            current_raw_price=base.quote.bid,
            bars_held=1,
            entry_time=datetime(2026, 8, 13, 9, 45, tzinfo=NY),
            exchange_close=base.calendar.sessions[-1].close_at,
        )
        price_mismatch = replace(valid, current_raw_price=base.quote.last)
        self.assertEqual(
            run_decision_cycle(replace(base, position=price_mismatch)).wait_reasons,
            (CycleWaitReason.POSITION_QUOTE_MISMATCH,),
        )
        wrong_session = replace(
            valid,
            entry_time=datetime(2026, 8, 12, 15, 0, tzinfo=NY),
        )
        self.assertEqual(
            run_decision_cycle(replace(base, position=wrong_session)).wait_reasons,
            (CycleWaitReason.POSITION_SESSION_MISMATCH,),
        )


if __name__ == "__main__":
    unittest.main()
