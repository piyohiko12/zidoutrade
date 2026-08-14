"""One pure, order-free RSI decision cycle.

This module is the boundary between validated market facts and an execution
*candidate*.  It computes indicators, strategy intent, and frozen V1 risk
sizing, but deliberately has no broker/OpenD import and no persistence or
network side effect.  A returned ``ENTER`` or ``EXIT`` is therefore a typed
decision report, never an order dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
import math
from typing import Optional, Tuple

from .exchange_calendar import CalendarError, FrozenExchangeCalendar, NEW_YORK
from .fees import FeeSchedule
from .indicators import wilder_atr, wilder_rsi
from .market_data import (
    IndicatorBarSeries,
    MarketDataError,
    MarketDataValidationError,
    QuoteSnapshot,
    StaleMarketData,
    build_market_gates,
    validate_completed_15m_bars,
)
from .models import (
    DecisionAction,
    PositionSnapshot,
    ReasonCode,
    StrategyContext,
    StrategyDecision,
    TrendEligibility,
    require_aware_datetime,
    require_number,
    require_symbol,
)
from .risk import (
    PAPER_FEE_SCHEDULE,
    RiskPolicy,
    RiskState,
    SizingRequest,
    SizingResult,
    size_position,
)
from .selection import PresentedCandidate, SelectionRecord, SelectionState
from .strategy import evaluate_strategy


RSI_PERIOD = 14
ATR_PERIOD = 14
ATR_STOP_MULTIPLE = 1.5
MAX_SPREAD_BPS = 10.0
MAX_QUOTE_AGE = timedelta(seconds=2)


class CycleWaitReason(str, Enum):
    """Stable orchestration-level reasons for an order-free WAIT report."""

    SELECTION_NOT_LOCKED = "SELECTION_NOT_LOCKED"
    SELECTION_NOT_PERMITTED = "SELECTION_NOT_PERMITTED"
    SELECTION_INVALID = "SELECTION_INVALID"
    SELECTION_SESSION_MISMATCH = "SELECTION_SESSION_MISMATCH"
    CALENDAR_MISMATCH = "CALENDAR_MISMATCH"
    MARKET_DATA_STALE = "MARKET_DATA_STALE"
    MARKET_DATA_INVALID = "MARKET_DATA_INVALID"
    INDICATOR_NOT_READY = "INDICATOR_NOT_READY"
    POSITION_QUOTE_MISMATCH = "POSITION_QUOTE_MISMATCH"
    POSITION_SESSION_MISMATCH = "POSITION_SESSION_MISMATCH"
    STRATEGY_WAIT = "STRATEGY_WAIT"
    RISK_BLOCKED = "RISK_BLOCKED"
    STOP_PRICE_INVALID = "STOP_PRICE_INVALID"


@dataclass(frozen=True)
class DecisionCycleRequest:
    """Exact, already-collected inputs for one deterministic evaluation."""

    indicator_bars: IndicatorBarSeries
    quote: QuoteSnapshot
    locked_selection: SelectionRecord
    calendar: FrozenExchangeCalendar
    risk_state: RiskState
    trend: TrendEligibility
    now: datetime
    position: Optional[PositionSnapshot] = None
    risk_policy: RiskPolicy = RiskPolicy()

    def __post_init__(self) -> None:
        if type(self.indicator_bars) is not IndicatorBarSeries:
            raise TypeError("indicator_bars must be an exact IndicatorBarSeries")
        if type(self.quote) is not QuoteSnapshot:
            raise TypeError("quote must be an exact raw QuoteSnapshot")
        if type(self.locked_selection) is not SelectionRecord:
            raise TypeError("locked_selection must be an exact SelectionRecord")
        if type(self.calendar) is not FrozenExchangeCalendar:
            raise TypeError("calendar must be an exact FrozenExchangeCalendar")
        if type(self.risk_state) is not RiskState:
            raise TypeError("risk_state must be an exact RiskState")
        if type(self.trend) is not TrendEligibility:
            raise TypeError("trend must be an exact TrendEligibility")
        require_aware_datetime("now", self.now)
        if self.position is not None and type(self.position) is not PositionSnapshot:
            raise TypeError("position must be an exact PositionSnapshot or None")
        if type(self.risk_policy) is not RiskPolicy:
            raise TypeError("risk_policy must be an exact RiskPolicy")


@dataclass(frozen=True)
class DecisionCycleReport:
    """Complete pure result; it cannot be submitted to a broker by itself."""

    action: DecisionAction
    wait_reasons: Tuple[CycleWaitReason, ...]
    strategy: Optional[StrategyDecision] = None
    context: Optional[StrategyContext] = None
    latest_rsi: Optional[float] = None
    latest_atr_qfq: Optional[float] = None
    latest_atr_raw: Optional[float] = None
    sizing: Optional[SizingResult] = None
    fee_schedule: FeeSchedule = PAPER_FEE_SCHEDULE

    def __post_init__(self) -> None:
        if type(self.action) is not DecisionAction:
            raise TypeError("action must be an exact DecisionAction")
        if type(self.wait_reasons) is not tuple:
            raise TypeError("wait_reasons must be a tuple")
        if any(type(reason) is not CycleWaitReason for reason in self.wait_reasons):
            raise TypeError("wait_reasons must contain exact CycleWaitReason values")
        if len(set(self.wait_reasons)) != len(self.wait_reasons):
            raise ValueError("wait_reasons must not contain duplicates")
        if (self.action is DecisionAction.WAIT) != bool(self.wait_reasons):
            raise ValueError("WAIT must have reasons and actionable reports must not")
        if self.strategy is not None and type(self.strategy) is not StrategyDecision:
            raise TypeError("strategy must be an exact StrategyDecision or None")
        if self.context is not None and type(self.context) is not StrategyContext:
            raise TypeError("context must be an exact StrategyContext or None")
        if self.sizing is not None and type(self.sizing) is not SizingResult:
            raise TypeError("sizing must be an exact SizingResult or None")
        if self.fee_schedule is not PAPER_FEE_SCHEDULE:
            raise ValueError("decision cycle fee schedule is frozen for V1")
        if self.latest_rsi is not None:
            rsi = require_number("latest_rsi", self.latest_rsi)
            if not 0.0 <= rsi <= 100.0:
                raise ValueError("latest_rsi must be in [0, 100]")
            object.__setattr__(self, "latest_rsi", rsi)
        for name in ("latest_atr_qfq", "latest_atr_raw"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, require_number(name, value, positive=True))
        if self.action is DecisionAction.ENTER:
            if (
                self.strategy is None
                or self.strategy.action is not DecisionAction.ENTER
                or self.sizing is None
                or not self.sizing.allowed
            ):
                raise ValueError("ENTER requires an allowed sizing and ENTER strategy")
        if self.action is DecisionAction.EXIT:
            if self.strategy is None or self.strategy.action is not DecisionAction.EXIT:
                raise ValueError("EXIT requires an EXIT strategy")
            if self.sizing is not None:
                raise ValueError("EXIT does not use entry sizing")


def _wait(
    reason: CycleWaitReason,
    *,
    strategy: Optional[StrategyDecision] = None,
    context: Optional[StrategyContext] = None,
    latest_rsi: Optional[float] = None,
    latest_atr_qfq: Optional[float] = None,
    latest_atr_raw: Optional[float] = None,
    sizing: Optional[SizingResult] = None,
) -> DecisionCycleReport:
    return DecisionCycleReport(
        action=DecisionAction.WAIT,
        wait_reasons=(reason,),
        strategy=strategy,
        context=context,
        latest_rsi=latest_rsi,
        latest_atr_qfq=latest_atr_qfq,
        latest_atr_raw=latest_atr_raw,
        sizing=sizing,
    )


def _strict_session_date(value: object) -> Optional[date]:
    if type(value) is not str or len(value) != 10:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _locked_symbol(
    record: SelectionRecord, *, session_date: date
) -> Tuple[Optional[str], Optional[CycleWaitReason]]:
    if record.state is not SelectionState.SESSION_LOCKED:
        return None, CycleWaitReason.SELECTION_NOT_LOCKED
    if type(record.trade_permitted) is not bool or not record.trade_permitted:
        return None, CycleWaitReason.SELECTION_NOT_PERMITTED
    if (
        type(record.revision) is not int
        or record.revision < 1
        or record.no_trade_reason is not None
        or type(record.locked_at) is not str
        or not record.locked_at
    ):
        return None, CycleWaitReason.SELECTION_INVALID
    try:
        locked_at = datetime.fromisoformat(record.locked_at.replace("Z", "+00:00"))
    except ValueError:
        return None, CycleWaitReason.SELECTION_INVALID
    if locked_at.tzinfo is None or locked_at.utcoffset() is None:
        return None, CycleWaitReason.SELECTION_INVALID
    target = _strict_session_date(record.target_session)
    if target != session_date:
        return None, CycleWaitReason.SELECTION_SESSION_MISMATCH
    try:
        symbol = require_symbol("selected_symbol", record.selected_symbol)
    except (TypeError, ValueError):
        return None, CycleWaitReason.SELECTION_INVALID
    if type(record.presented_candidates) is not tuple:
        return None, CycleWaitReason.SELECTION_INVALID
    matches = tuple(
        candidate
        for candidate in record.presented_candidates
        if type(candidate) is PresentedCandidate and candidate.symbol == symbol
    )
    if (
        len(matches) != 1
        or type(matches[0].eligible) is not bool
        or not matches[0].eligible
        or type(matches[0].priority) is not int
        or matches[0].priority < 0
        or type(matches[0].reason_codes) is not tuple
    ):
        return None, CycleWaitReason.SELECTION_INVALID
    return symbol, None


def _same_instant(left: datetime, right: datetime) -> bool:
    return left.astimezone(timezone.utc) == right.astimezone(timezone.utc)


def run_decision_cycle(request: DecisionCycleRequest) -> DecisionCycleReport:
    """Evaluate one V1 cycle without importing or calling any broker adapter."""

    if type(request) is not DecisionCycleRequest:
        raise TypeError("request must be an exact DecisionCycleRequest")
    now_ny = request.now.astimezone(NEW_YORK)
    symbol, selection_error = _locked_symbol(
        request.locked_selection, session_date=now_ny.date()
    )
    if selection_error is not None or symbol is None:
        return _wait(selection_error or CycleWaitReason.SELECTION_INVALID)

    try:
        session = request.calendar.session_on(now_ny.date())
    except CalendarError:
        return _wait(CycleWaitReason.CALENDAR_MISMATCH)
    if session is None:
        return _wait(CycleWaitReason.CALENDAR_MISMATCH)

    series = request.indicator_bars
    scale = series.adjustment_scale
    try:
        prior_sessions = tuple(
            item
            for item in request.calendar.sessions
            if item.session_date < session.session_date
        )
        if (
            scale.symbol != symbol
            or scale.target_session != session.session_date
            or scale.calendar_sha256 != request.calendar.sha256
            or not _same_instant(scale.frozen_at, session.open_at)
            or not prior_sessions
            or scale.source_daily_session != prior_sessions[-1].session_date
        ):
            return _wait(CycleWaitReason.CALENDAR_MISMATCH)
        if request.quote.symbol != symbol:
            return _wait(CycleWaitReason.MARKET_DATA_INVALID)
        bars = validate_completed_15m_bars(
            series.bars,
            symbol=symbol,
            as_of=request.now,
            calendar=request.calendar,
        )
        gates = build_market_gates(
            request.quote,
            symbol=symbol,
            as_of=request.now,
            calendar=request.calendar,
            max_spread_bps=MAX_SPREAD_BPS,
            max_age=MAX_QUOTE_AGE,
        )
        rsi_values = wilder_rsi(bars, RSI_PERIOD)
        atr_values = wilder_atr(bars, ATR_PERIOD)
    except StaleMarketData:
        return _wait(CycleWaitReason.MARKET_DATA_STALE)
    except (MarketDataError, CalendarError, TypeError, ValueError):
        return _wait(CycleWaitReason.MARKET_DATA_INVALID)

    latest_rsi = rsi_values[-1]
    latest_atr_qfq = atr_values[-1]
    latest_atr_raw = None  # type: Optional[float]
    if latest_atr_qfq is not None:
        try:
            latest_atr_raw = scale.raw_value(latest_atr_qfq)
            if latest_atr_raw <= 0.0 or not math.isfinite(latest_atr_raw):
                raise MarketDataValidationError("raw ATR must be finite and positive")
        except (MarketDataError, TypeError, ValueError):
            return _wait(CycleWaitReason.MARKET_DATA_INVALID, latest_rsi=latest_rsi)

    position = request.position
    if position is not None:
        if not math.isclose(
            position.current_raw_price,
            request.quote.bid,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            return _wait(
                CycleWaitReason.POSITION_QUOTE_MISMATCH,
                latest_rsi=latest_rsi,
                latest_atr_qfq=latest_atr_qfq,
                latest_atr_raw=latest_atr_raw,
            )
        if (
            position.entry_time.astimezone(NEW_YORK).date() != session.session_date
            or not _same_instant(position.exchange_close, session.close_at)
        ):
            return _wait(
                CycleWaitReason.POSITION_SESSION_MISMATCH,
                latest_rsi=latest_rsi,
                latest_atr_qfq=latest_atr_qfq,
                latest_atr_raw=latest_atr_raw,
            )

    context = StrategyContext(
        active_symbol=symbol,
        selected_symbol=symbol,
        bars=bars,
        rsi_values=rsi_values,
        trend=request.trend,
        gates=gates,
        now=request.now,
        position=position,
        traded_roundtrips_today=request.risk_state.completed_roundtrips_today,
    )
    strategy = evaluate_strategy(context)
    if strategy.action is DecisionAction.WAIT:
        cycle_reason = (
            CycleWaitReason.INDICATOR_NOT_READY
            if ReasonCode.INDICATOR_NOT_READY in strategy.reasons
            else CycleWaitReason.STRATEGY_WAIT
        )
        return _wait(
            cycle_reason,
            strategy=strategy,
            context=context,
            latest_rsi=latest_rsi,
            latest_atr_qfq=latest_atr_qfq,
            latest_atr_raw=latest_atr_raw,
        )
    if strategy.action is DecisionAction.EXIT:
        return DecisionCycleReport(
            action=DecisionAction.EXIT,
            wait_reasons=(),
            strategy=strategy,
            context=context,
            latest_rsi=latest_rsi,
            latest_atr_qfq=latest_atr_qfq,
            latest_atr_raw=latest_atr_raw,
        )
    if latest_atr_qfq is None or latest_atr_raw is None:
        return _wait(
            CycleWaitReason.INDICATOR_NOT_READY,
            strategy=strategy,
            context=context,
            latest_rsi=latest_rsi,
        )

    stop_trigger = request.quote.ask - ATR_STOP_MULTIPLE * latest_atr_raw
    if not math.isfinite(stop_trigger) or stop_trigger <= 0.0:
        return _wait(
            CycleWaitReason.STOP_PRICE_INVALID,
            strategy=strategy,
            context=context,
            latest_rsi=latest_rsi,
            latest_atr_qfq=latest_atr_qfq,
            latest_atr_raw=latest_atr_raw,
        )
    sizing = size_position(
        SizingRequest(
            state=request.risk_state,
            entry_limit=request.quote.ask,
            stop_trigger=stop_trigger,
            entry_fees=PAPER_FEE_SCHEDULE,
            exit_fees=PAPER_FEE_SCHEDULE,
            policy=request.risk_policy,
        )
    )
    if not sizing.allowed:
        return _wait(
            CycleWaitReason.RISK_BLOCKED,
            strategy=strategy,
            context=context,
            latest_rsi=latest_rsi,
            latest_atr_qfq=latest_atr_qfq,
            latest_atr_raw=latest_atr_raw,
            sizing=sizing,
        )
    return DecisionCycleReport(
        action=DecisionAction.ENTER,
        wait_reasons=(),
        strategy=strategy,
        context=context,
        latest_rsi=latest_rsi,
        latest_atr_qfq=latest_atr_qfq,
        latest_atr_raw=latest_atr_raw,
        sizing=sizing,
    )


__all__ = [
    "ATR_PERIOD",
    "ATR_STOP_MULTIPLE",
    "CycleWaitReason",
    "DecisionCycleReport",
    "DecisionCycleRequest",
    "MAX_QUOTE_AGE",
    "MAX_SPREAD_BPS",
    "RSI_PERIOD",
    "run_decision_cycle",
]
