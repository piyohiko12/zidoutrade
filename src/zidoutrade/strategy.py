"""Deterministic RSI entry/exit decisions.

This module produces intent only; it never imports a broker SDK or submits an
order.  Entry failures are returned as stable reason codes so the UI can
explain every blocked condition without inventing a score.
"""

from __future__ import annotations

from datetime import time, timedelta
from typing import List, Optional

from .indicators import validate_bar_series
from .models import (
    NEW_YORK,
    DecisionAction,
    ReasonCode,
    StrategyContext,
    StrategyDecision,
)


ENTRY_START = time(10, 0)
ENTRY_END = time(15, 15)
OVERSOLD_THRESHOLD = 30.0
RECOVERY_THRESHOLD = 35.0
EXIT_THRESHOLD = 60.0
MAX_HOLD_BARS = 8
MAX_HOLD_DURATION = timedelta(hours=2)
CLOSE_EXIT_LEAD = timedelta(minutes=15)


def _decision(
    action: DecisionAction,
    reason: ReasonCode,
    signal_bar_end=None,  # type: ignore[no-untyped-def]
    stop_price: Optional[float] = None,
) -> StrategyDecision:
    return StrategyDecision(action, (reason,), signal_bar_end, stop_price)


def _evaluate_position(context: StrategyContext) -> StrategyDecision:
    position = context.position
    assert position is not None  # narrowed by caller
    if context.now < position.entry_time:
        raise ValueError("now cannot precede position entry_time")
    signal_end = context.bars[-1].end if context.bars else None
    stop_price = position.stop_price

    # Exit priority is frozen: emergency, hard stop, RSI, max hold, close.
    if position.emergency_exit:
        return _decision(
            DecisionAction.EXIT, ReasonCode.EMERGENCY_EXIT, signal_end, stop_price
        )
    if position.current_raw_price <= stop_price:
        return _decision(DecisionAction.EXIT, ReasonCode.STOP_LOSS, signal_end, stop_price)

    latest_rsi = context.rsi_values[-1] if context.rsi_values else None
    if latest_rsi is not None and latest_rsi >= EXIT_THRESHOLD:
        return _decision(DecisionAction.EXIT, ReasonCode.RSI_EXIT, signal_end, stop_price)
    if (
        position.bars_held >= MAX_HOLD_BARS
        or context.now - position.entry_time >= MAX_HOLD_DURATION
    ):
        return _decision(DecisionAction.EXIT, ReasonCode.MAX_HOLD, signal_end, stop_price)
    if context.now >= position.exchange_close - CLOSE_EXIT_LEAD:
        return _decision(
            DecisionAction.EXIT, ReasonCode.CLOSE_APPROACHING, signal_end, stop_price
        )
    return _decision(DecisionAction.WAIT, ReasonCode.HOLD_NO_EXIT, signal_end, stop_price)


def evaluate_strategy(context: StrategyContext) -> StrategyDecision:
    """Evaluate one frozen strategy cycle.

    A selected symbol is never substituted.  Entry requires all mandatory
    gates; exits are evaluated independently so an entry pause cannot suppress
    management of an already-known long position.
    """

    if type(context) is not StrategyContext:
        raise TypeError("context must be an exact StrategyContext")

    if context.bars:
        checked = validate_bar_series(context.bars)
        if any(bar.symbol != context.active_symbol for bar in checked):
            raise ValueError("bar symbol must equal active_symbol")

    if context.position is not None:
        return _evaluate_position(context)

    if context.active_symbol != context.selected_symbol:
        return _decision(DecisionAction.WAIT, ReasonCode.ACTIVE_SYMBOL_MISMATCH)
    if not context.bars:
        return _decision(DecisionAction.WAIT, ReasonCode.NO_COMPLETED_BARS)

    latest = context.bars[-1]
    reasons = []  # type: List[ReasonCode]
    if not context.trend.daily_data_ok:
        reasons.append(ReasonCode.DAILY_DATA_INVALID)
    if not context.trend.symbol_daily_ok:
        reasons.append(ReasonCode.SYMBOL_TREND_BLOCKED)
    if not context.trend.spy_daily_ok:
        reasons.append(ReasonCode.SPY_TREND_BLOCKED)
    if not context.gates.market_open:
        reasons.append(ReasonCode.MARKET_CLOSED)
    if not context.gates.data_ok:
        reasons.append(ReasonCode.INTRADAY_DATA_INVALID)
    if not context.gates.spread_ok:
        reasons.append(ReasonCode.SPREAD_TOO_WIDE)

    signal_time = latest.end.astimezone(NEW_YORK).timetz().replace(tzinfo=None)
    if not ENTRY_START <= signal_time <= ENTRY_END:
        reasons.append(ReasonCode.OUTSIDE_ENTRY_WINDOW)
    if context.traded_roundtrips_today >= 1:
        reasons.append(ReasonCode.ROUNDTRIP_LIMIT_REACHED)

    recent_rsi = context.rsi_values[-3:]
    if len(recent_rsi) < 3 or any(value is None for value in recent_rsi):
        reasons.append(ReasonCode.INDICATOR_NOT_READY)
    else:
        known_recent = tuple(float(value) for value in recent_rsi if value is not None)
        if min(known_recent) > OVERSOLD_THRESHOLD:
            reasons.append(ReasonCode.RSI_NOT_OVERSOLD_RECENTLY)
        previous_rsi = known_recent[-2]
        current_rsi = known_recent[-1]
        if not (previous_rsi <= RECOVERY_THRESHOLD < current_rsi):
            reasons.append(ReasonCode.RSI_RECOVERY_NOT_CONFIRMED)

    if len(context.bars) < 2 or latest.close <= context.bars[-2].high:
        reasons.append(ReasonCode.PRICE_CONFIRMATION_MISSING)

    if reasons:
        return StrategyDecision(DecisionAction.WAIT, tuple(reasons), latest.end)
    return _decision(
        DecisionAction.ENTER, ReasonCode.ENTRY_SIGNAL_CONFIRMED, latest.end
    )
