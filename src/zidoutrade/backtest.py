"""Pure, headless historical-candle proxy for ``RSI_AUTOPILOT_V1``.

The engine deliberately has no file, network, OpenD, SDK, account, or order
side effect.  Signals use completed QFQ RTH 15-minute candles while execution
and PnL use timestamp-matched RAW candles.  Historical top-of-book and order
state are not present in candles, so every result is explicitly labelled an
exploratory proxy rather than a reproduced broker fill history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from statistics import median
import math
from typing import Dict, Optional, Sequence, Tuple

from .decision_cycle import ATR_PERIOD, ATR_STOP_MULTIPLE, RSI_PERIOD
from .fees import OrderSide, calculate_order_fees
from .indicators import validate_bar_series, wilder_atr, wilder_rsi
from .models import (
    NEW_YORK,
    CompletedBar15m,
    ReasonCode,
    TrendEligibility,
    require_aware_datetime,
    require_number,
    require_symbol,
)
from .risk import (
    ExecutionStress,
    PAPER_FEE_SCHEDULE,
    RiskPolicy,
    RiskState,
    SizingRequest,
    SizingResult,
    size_position,
)
from .strategy import (
    CLOSE_EXIT_LEAD,
    ENTRY_END,
    ENTRY_START,
    EXIT_THRESHOLD,
    MAX_BREAKOUT_EXTENSION,
    MAX_HOLD_BARS,
    MAX_HOLD_DURATION,
    OVERSOLD_THRESHOLD,
    RECOVERY_THRESHOLD,
    VOLUME_CONFIRMATION_MULTIPLE,
    VOLUME_LOOKBACK_BARS,
)


MODEL_ID = "HISTORICAL_CANDLE_PROXY_V1"
RESULT_STATUS = "EXPLORATORY_ONLY"
Q013_ATR_CLOSE_CAP = 0.0050
Q015_VARIANT_ID = "Q015_PRIOR_CLOSE_NET_REWARD_RISK_GATE_V1"


class BacktestVariant(str, Enum):
    """Closed set of immutable strategy variants supported by the proxy."""

    BASELINE = "RSI_AUTOPILOT_V1"
    Q013_ATR_CAP_0050_SHADOW = "RSI_AUTOPILOT_V1_Q013_ATR_CAP_0050_SHADOW"
    Q015_PRIOR_CLOSE_NET_REWARD_RISK_GATE_V1 = Q015_VARIANT_ID


@dataclass(frozen=True)
class DatedTrend:
    """Prior-session daily trend facts made available for one target session."""

    session_date: date
    eligibility: TrendEligibility

    def __post_init__(self) -> None:
        if type(self.session_date) is not date:
            raise TypeError("session_date must be an exact date")
        if type(self.eligibility) is not TrendEligibility:
            raise TypeError("eligibility must be an exact TrendEligibility")


@dataclass(frozen=True)
class SessionBoundary:
    """Exact official close used for normal and shortened RTH sessions."""

    session_date: date
    close_at: datetime

    def __post_init__(self) -> None:
        if type(self.session_date) is not date:
            raise TypeError("session_date must be an exact date")
        close_at = require_aware_datetime("close_at", self.close_at)
        if close_at.astimezone(NEW_YORK).date() != self.session_date:
            raise ValueError("close_at must fall on session_date in New York")


@dataclass(frozen=True)
class BacktestConfig:
    """Frozen proxy assumptions and the current risk policy."""

    symbol: str
    initial_equity: float = 100_000.0
    risk_policy: RiskPolicy = RiskPolicy(maximum_investment_cents=1_000_000)
    assumed_spread_bps: float = 10.0
    entry_cushion_bps: float = 10.0
    normal_exit_cushion_bps: float = 15.0
    stressed_exit_cushion_bps: float = 50.0
    model_id: str = MODEL_ID
    strategy_variant: BacktestVariant = BacktestVariant.BASELINE

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", require_symbol("symbol", self.symbol))
        object.__setattr__(
            self,
            "initial_equity",
            require_number("initial_equity", self.initial_equity, positive=True),
        )
        if type(self.risk_policy) is not RiskPolicy:
            raise TypeError("risk_policy must be an exact RiskPolicy")
        if self.risk_policy.maximum_investment_cents is None:
            raise ValueError("backtest risk_policy requires maximum_investment_cents")
        if self.model_id != MODEL_ID:
            raise ValueError("unsupported backtest model_id")
        if type(self.strategy_variant) is not BacktestVariant:
            raise TypeError("strategy_variant must be an exact BacktestVariant")
        frozen = {
            "assumed_spread_bps": 10.0,
            "entry_cushion_bps": 10.0,
            "normal_exit_cushion_bps": 15.0,
            "stressed_exit_cushion_bps": 50.0,
        }
        for name, expected in frozen.items():
            value = require_number(name, getattr(self, name), nonnegative=True)
            if value != expected:
                raise ValueError("%s is frozen at %s for %s" % (name, expected, MODEL_ID))
            object.__setattr__(self, name, value)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "initial_equity": self.initial_equity,
            "risk_policy": self.risk_policy.evidence_payload(),
            "assumed_spread_bps": self.assumed_spread_bps,
            "entry_cushion_bps": self.entry_cushion_bps,
            "normal_exit_cushion_bps": self.normal_exit_cushion_bps,
            "stressed_exit_cushion_bps": self.stressed_exit_cushion_bps,
            "model_id": self.model_id,
            "strategy_variant_id": self.strategy_variant.value,
        }


@dataclass(frozen=True)
class BacktestTrade:
    opportunity_id: str
    session_date: date
    signal_bar_end: datetime
    entry_time: datetime
    exit_time: datetime
    quantity: int
    raw_entry_reference: float
    entry_price: float
    raw_exit_reference: float
    exit_price: float
    atr_raw: float
    stop_price: float
    exit_reason: ReasonCode
    bars_held: int
    gross_pnl: float
    fees: float
    net_pnl: float
    equity_after: float

    def __post_init__(self) -> None:
        if type(self.opportunity_id) is not str or not self.opportunity_id:
            raise ValueError("opportunity_id is required")
        if type(self.session_date) is not date:
            raise TypeError("session_date must be an exact date")
        signal = require_aware_datetime("signal_bar_end", self.signal_bar_end)
        entered = require_aware_datetime("entry_time", self.entry_time)
        exited = require_aware_datetime("exit_time", self.exit_time)
        if entered < signal or exited <= entered:
            raise ValueError("trade timestamps must be signal <= entry < exit")
        if type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("quantity must be a positive exact int")
        for name in (
            "raw_entry_reference",
            "entry_price",
            "raw_exit_reference",
            "exit_price",
            "atr_raw",
            "stop_price",
            "equity_after",
        ):
            object.__setattr__(self, name, require_number(name, getattr(self, name), positive=True))
        for name in ("gross_pnl", "net_pnl"):
            object.__setattr__(self, name, require_number(name, getattr(self, name)))
        object.__setattr__(self, "fees", require_number("fees", self.fees, nonnegative=True))
        if type(self.exit_reason) is not ReasonCode or self.exit_reason not in {
            ReasonCode.STOP_LOSS,
            ReasonCode.RSI_EXIT,
            ReasonCode.MAX_HOLD,
            ReasonCode.CLOSE_APPROACHING,
        }:
            raise ValueError("exit_reason is not a supported historical exit")
        if type(self.bars_held) is not int or self.bars_held < 1:
            raise ValueError("bars_held must be a positive exact int")
        expected_gross = self.quantity * (self.exit_price - self.entry_price)
        if not math.isclose(self.gross_pnl, expected_gross, rel_tol=1e-12, abs_tol=1e-9):
            raise ValueError("gross_pnl is inconsistent with price and quantity")
        if not math.isclose(
            self.net_pnl, self.gross_pnl - self.fees, rel_tol=1e-12, abs_tol=1e-9
        ):
            raise ValueError("net_pnl must equal gross_pnl minus fees")

    def to_dict(self) -> dict:
        return {
            "opportunity_id": self.opportunity_id,
            "session_date": self.session_date.isoformat(),
            "signal_bar_end": self.signal_bar_end.isoformat(),
            "entry_time": self.entry_time.isoformat(),
            "exit_time": self.exit_time.isoformat(),
            "quantity": self.quantity,
            "raw_entry_reference": self.raw_entry_reference,
            "entry_price": self.entry_price,
            "raw_exit_reference": self.raw_exit_reference,
            "exit_price": self.exit_price,
            "atr_raw": self.atr_raw,
            "stop_price": self.stop_price,
            "exit_reason": self.exit_reason.value,
            "bars_held": self.bars_held,
            "gross_pnl": self.gross_pnl,
            "fees": self.fees,
            "net_pnl": self.net_pnl,
            "equity_after": self.equity_after,
        }


@dataclass(frozen=True)
class BacktestReport:
    model_id: str
    status: str
    strategy_variant_id: str
    symbol: str
    first_session: date
    last_session: date
    completed_bar_count: int
    entry_signal_count: int
    risk_blocked_signal_count: int
    no_next_bar_signal_count: int
    trade_count: int
    winning_trades: int
    losing_trades: int
    flat_trades: int
    win_rate_excluding_flat: Optional[float]
    initial_equity: float
    final_equity: float
    total_gross_pnl: float
    total_fees: float
    total_net_pnl: float
    terminal_return: float
    profit_factor: Optional[float]
    closed_trade_max_drawdown: float
    closed_trade_max_drawdown_fraction: float
    assumptions: Tuple[str, ...]
    limitations: Tuple[str, ...]
    config: BacktestConfig
    trades: Tuple[BacktestTrade, ...]

    def __post_init__(self) -> None:
        if self.model_id != MODEL_ID or self.status != RESULT_STATUS:
            raise ValueError("backtest report classification is not exact")
        if (
            type(self.strategy_variant_id) is not str
            or self.strategy_variant_id != self.config.strategy_variant.value
        ):
            raise ValueError("report strategy_variant_id does not match config")
        object.__setattr__(self, "symbol", require_symbol("symbol", self.symbol))
        if type(self.first_session) is not date or type(self.last_session) is not date:
            raise TypeError("report sessions must be exact dates")
        if self.last_session < self.first_session:
            raise ValueError("report session range is reversed")
        integer_fields = (
            "completed_bar_count",
            "entry_signal_count",
            "risk_blocked_signal_count",
            "no_next_bar_signal_count",
            "trade_count",
            "winning_trades",
            "losing_trades",
            "flat_trades",
        )
        for name in integer_fields:
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError("%s must be a nonnegative exact int" % name)
        if self.completed_bar_count == 0:
            raise ValueError("completed_bar_count must be positive")
        if self.trade_count != self.winning_trades + self.losing_trades + self.flat_trades:
            raise ValueError("trade outcome counts do not sum to trade_count")
        for name in (
            "initial_equity",
            "final_equity",
        ):
            object.__setattr__(self, name, require_number(name, getattr(self, name), positive=True))
        for name in ("total_gross_pnl", "total_net_pnl", "terminal_return"):
            object.__setattr__(self, name, require_number(name, getattr(self, name)))
        for name in (
            "total_fees",
            "closed_trade_max_drawdown",
            "closed_trade_max_drawdown_fraction",
        ):
            object.__setattr__(self, name, require_number(name, getattr(self, name), nonnegative=True))
        for name in ("win_rate_excluding_flat", "profit_factor"):
            value = getattr(self, name)
            if value is not None:
                normalized = require_number(name, value, nonnegative=True)
                if name == "win_rate_excluding_flat" and normalized > 1.0:
                    raise ValueError("win_rate_excluding_flat must not exceed 1")
                object.__setattr__(self, name, normalized)
        if type(self.assumptions) is not tuple or not self.assumptions:
            raise ValueError("assumptions must be a nonempty tuple")
        if type(self.limitations) is not tuple or not self.limitations:
            raise ValueError("limitations must be a nonempty tuple")
        if any(type(item) is not str or not item for item in self.assumptions + self.limitations):
            raise ValueError("assumptions and limitations require nonempty strings")
        if type(self.config) is not BacktestConfig or self.config.symbol != self.symbol:
            raise ValueError("report config identity does not match")
        if type(self.trades) is not tuple or any(
            type(trade) is not BacktestTrade for trade in self.trades
        ):
            raise TypeError("trades must contain exact BacktestTrade values")
        if len(self.trades) != self.trade_count:
            raise ValueError("trade_count differs from trades")
        if len({trade.opportunity_id for trade in self.trades}) != len(self.trades):
            raise ValueError("duplicate opportunity_id")
        if not math.isclose(
            self.final_equity,
            self.initial_equity + self.total_net_pnl,
            rel_tol=1e-12,
            abs_tol=1e-8,
        ):
            raise ValueError("final equity does not reconcile to net PnL")
        if not math.isclose(
            self.total_net_pnl,
            self.total_gross_pnl - self.total_fees,
            rel_tol=1e-12,
            abs_tol=1e-8,
        ):
            raise ValueError("report net PnL does not reconcile")

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "status": self.status,
            "strategy_variant_id": self.strategy_variant_id,
            "symbol": self.symbol,
            "first_session": self.first_session.isoformat(),
            "last_session": self.last_session.isoformat(),
            "completed_bar_count": self.completed_bar_count,
            "entry_signal_count": self.entry_signal_count,
            "risk_blocked_signal_count": self.risk_blocked_signal_count,
            "no_next_bar_signal_count": self.no_next_bar_signal_count,
            "trade_count": self.trade_count,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "flat_trades": self.flat_trades,
            "win_rate_excluding_flat": self.win_rate_excluding_flat,
            "initial_equity": self.initial_equity,
            "final_equity": self.final_equity,
            "total_gross_pnl": self.total_gross_pnl,
            "total_fees": self.total_fees,
            "total_net_pnl": self.total_net_pnl,
            "terminal_return": self.terminal_return,
            "profit_factor": self.profit_factor,
            "closed_trade_max_drawdown": self.closed_trade_max_drawdown,
            "closed_trade_max_drawdown_fraction": self.closed_trade_max_drawdown_fraction,
            "assumptions": list(self.assumptions),
            "limitations": list(self.limitations),
            "config": self.config.to_dict(),
            "trades": [trade.to_dict() for trade in self.trades],
        }


def _unique_by_date(name: str, values: Sequence[object]) -> Dict[date, object]:
    result = {}  # type: Dict[date, object]
    previous = None  # type: Optional[date]
    for value in values:
        session_date = getattr(value, "session_date", None)
        if type(session_date) is not date:
            raise TypeError("%s entries must expose an exact session_date" % name)
        if previous is not None and session_date <= previous:
            raise ValueError("%s must be strictly chronological and unique" % name)
        result[session_date] = value
        previous = session_date
    return result


def _validate_paired_bars(
    qfq_bars: Sequence[CompletedBar15m], raw_bars: Sequence[CompletedBar15m], symbol: str
) -> Tuple[Tuple[CompletedBar15m, ...], Tuple[CompletedBar15m, ...]]:
    qfq = validate_bar_series(qfq_bars)
    raw = validate_bar_series(raw_bars)
    if len(qfq) != len(raw):
        raise ValueError("QFQ and RAW bars must have equal length")
    for index, (signal, execution) in enumerate(zip(qfq, raw)):
        if signal.symbol != symbol or execution.symbol != symbol:
            raise ValueError("bar symbol differs from config symbol")
        if signal.start != execution.start or signal.end != execution.end:
            raise ValueError("QFQ and RAW timestamps differ at index %d" % index)
    return qfq, raw


def _entry_signal(
    index: int,
    bars: Tuple[CompletedBar15m, ...],
    rsi: Tuple[Optional[float], ...],
    trend: Optional[DatedTrend],
) -> bool:
    latest = bars[index]
    if trend is None:
        return False
    eligibility = trend.eligibility
    if not (
        eligibility.daily_data_ok
        and eligibility.symbol_daily_ok
        and eligibility.spy_daily_ok
    ):
        return False
    signal_time = latest.end.astimezone(NEW_YORK).timetz().replace(tzinfo=None)
    if not ENTRY_START <= signal_time <= ENTRY_END:
        return False
    if index < max(2, VOLUME_LOOKBACK_BARS):
        return False
    recent = rsi[index - 2 : index + 1]
    if len(recent) != 3 or any(value is None for value in recent):
        return False
    known = tuple(float(value) for value in recent if value is not None)
    if min(known) > OVERSOLD_THRESHOLD:
        return False
    if not (known[-2] <= RECOVERY_THRESHOLD < known[-1]):
        return False
    reference = bars[index - VOLUME_LOOKBACK_BARS : index]
    volumes = tuple(bar.volume for bar in reference)
    if latest.volume <= 0.0 or any(volume <= 0.0 for volume in volumes):
        return False
    if latest.volume < VOLUME_CONFIRMATION_MULTIPLE * median(volumes):
        return False
    prior_high = bars[index - 1].high
    if latest.close <= prior_high:
        return False
    return (latest.close - prior_high) / prior_high <= MAX_BREAKOUT_EXTENSION


def _variant_allows_entry(
    index: int,
    bars: Tuple[CompletedBar15m, ...],
    atr: Tuple[Optional[float], ...],
    variant: BacktestVariant,
) -> bool:
    """Apply a fixed post-signal gate using only the latest completed bar."""

    if type(variant) is not BacktestVariant:
        raise TypeError("variant must be an exact BacktestVariant")
    if variant is BacktestVariant.BASELINE:
        return True
    if variant is BacktestVariant.Q015_PRIOR_CLOSE_NET_REWARD_RISK_GATE_V1:
        # Q015 is evaluated immediately before sizing because its fixed formula
        # requires the next RAW open, prior-session close and the arm-specific
        # entry-time RiskState.  It never composes with Q013.
        return True
    if variant is not BacktestVariant.Q013_ATR_CAP_0050_SHADOW:
        raise ValueError("unsupported backtest strategy variant")
    if index < 0 or index >= len(bars) or index >= len(atr):
        return False
    latest_close = bars[index].close
    latest_atr = atr[index]
    if (
        latest_atr is None
        or not math.isfinite(latest_atr)
        or latest_atr <= 0.0
        or not math.isfinite(latest_close)
        or latest_close <= 0.0
    ):
        return False
    return latest_atr / latest_close <= Q013_ATR_CLOSE_CAP


def _prior_completed_rth_close(
    index: int,
    bars: Tuple[CompletedBar15m, ...],
    boundaries: Dict[date, SessionBoundary],
) -> Optional[float]:
    """Return the final close of the prior official, complete RTH session."""

    if (
        type(index) is not int
        or type(boundaries) is not dict
        or index < 0
        or index >= len(bars)
    ):
        return None
    target_session = bars[index].session_date
    cursor = index - 1
    while cursor >= 0 and bars[cursor].session_date == target_session:
        cursor -= 1
    if cursor < 0:
        return None
    prior_session = bars[cursor].session_date
    prior_close = bars[cursor].close
    official_prior_sessions = tuple(day for day in boundaries if day < target_session)
    if not official_prior_sessions or prior_session != max(official_prior_sessions):
        return None
    boundary = boundaries.get(prior_session)
    if type(boundary) is not SessionBoundary:
        return None
    if (
        prior_session >= target_session
        or bars[cursor].end != boundary.close_at
        or not math.isfinite(prior_close)
        or prior_close <= 0.0
    ):
        return None
    return prior_close


def _q015_allows_entry(
    index: int,
    qfq_bars: Tuple[CompletedBar15m, ...],
    raw_bars: Tuple[CompletedBar15m, ...],
    atr: Tuple[Optional[float], ...],
    *,
    next_index: int,
    state: RiskState,
    config: BacktestConfig,
    boundaries: Dict[date, SessionBoundary],
) -> Optional[SizingResult]:
    """Evaluate the fixed Q015 reward/risk research gate, fail closed.

    Only the completed signal bar, the preceding RTH session's final close and
    the next same-session RAW *open* are read.  No later field of the next bar
    participates in the decision.  The function is pure and order-free.
    """

    if type(index) is not int or type(next_index) is not int:
        return None
    if type(state) is not RiskState or type(config) is not BacktestConfig:
        return None
    if config.strategy_variant is not BacktestVariant.Q015_PRIOR_CLOSE_NET_REWARD_RISK_GATE_V1:
        return None
    if (
        index < 0
        or index >= len(qfq_bars)
        or index >= len(raw_bars)
        or index >= len(atr)
        or next_index != index + 1
        or next_index >= len(raw_bars)
    ):
        return None
    signal_qfq = qfq_bars[index]
    signal_raw = raw_bars[index]
    next_raw = raw_bars[next_index]
    if (
        signal_qfq.start != signal_raw.start
        or signal_qfq.end != signal_raw.end
        or next_raw.session_date != signal_qfq.session_date
        or next_raw.start != signal_qfq.end
    ):
        return None
    latest_atr = atr[index]
    prior_qfq_close = _prior_completed_rth_close(index, qfq_bars, boundaries)
    if latest_atr is None or prior_qfq_close is None:
        return None
    values = (
        signal_qfq.close,
        signal_raw.close,
        next_raw.open,
        latest_atr,
        prior_qfq_close,
    )
    if any(not math.isfinite(value) or value <= 0.0 for value in values):
        return None

    scale = signal_raw.close / signal_qfq.close
    atr_raw = float(latest_atr) * scale
    entry_price = _adverse_buy(next_raw.open, config)
    stop_price = entry_price - ATR_STOP_MULTIPLE * atr_raw
    target_raw = prior_qfq_close * scale
    normal_exit_price = _adverse_sell(target_raw, stressed=False, config=config)
    stressed_exit_price = _adverse_sell(stop_price, stressed=True, config=config)
    derived = (
        scale,
        atr_raw,
        entry_price,
        stop_price,
        target_raw,
        normal_exit_price,
        stressed_exit_price,
    )
    if any(not math.isfinite(value) or value <= 0.0 for value in derived):
        return None
    if normal_exit_price <= entry_price:
        return None

    sizing = size_position(
        SizingRequest(
            state=state,
            entry_limit=entry_price,
            stop_trigger=stop_price,
            entry_fees=PAPER_FEE_SCHEDULE,
            exit_fees=PAPER_FEE_SCHEDULE,
            stress=ExecutionStress(),
            policy=config.risk_policy,
        )
    )
    quantity = sizing.qty
    if type(quantity) is not int or quantity < 1:
        return None
    buy_fee = float(
        calculate_order_fees(
            PAPER_FEE_SCHEDULE, OrderSide.BUY, quantity, entry_price
        ).total
    )
    reward_sell_fee = float(
        calculate_order_fees(
            PAPER_FEE_SCHEDULE, OrderSide.SELL, quantity, normal_exit_price
        ).total
    )
    stress_sell_fee = float(
        calculate_order_fees(
            PAPER_FEE_SCHEDULE, OrderSide.SELL, quantity, stressed_exit_price
        ).total
    )
    if not _q015_reward_covers_loss(
        quantity,
        entry_price,
        normal_exit_price,
        stressed_exit_price,
        buy_fee,
        reward_sell_fee,
        stress_sell_fee,
    ):
        return None
    return sizing


def _q015_reward_covers_loss(
    quantity: int,
    entry_price: float,
    normal_exit_price: float,
    stressed_exit_price: float,
    buy_fee: float,
    reward_sell_fee: float,
    stress_sell_fee: float,
) -> bool:
    """Return the frozen Q015 inclusive fee-aware reward/loss comparison."""

    if type(quantity) is not int or quantity < 1:
        return False
    prices = (entry_price, normal_exit_price, stressed_exit_price)
    fees = (buy_fee, reward_sell_fee, stress_sell_fee)
    if any(
        type(value) not in (int, float)
        or not math.isfinite(float(value))
        or float(value) <= 0.0
        for value in prices
    ):
        return False
    if any(
        type(value) not in (int, float)
        or not math.isfinite(float(value))
        or float(value) < 0.0
        for value in fees
    ):
        return False
    net_reward = (
        quantity * (normal_exit_price - entry_price)
        - buy_fee
        - reward_sell_fee
    )
    stressed_loss = (
        quantity * (entry_price - stressed_exit_price)
        + buy_fee
        + stress_sell_fee
    )
    return (
        math.isfinite(net_reward)
        and math.isfinite(stressed_loss)
        and net_reward >= stressed_loss
    )


def _exit_reason(
    raw_bar: CompletedBar15m,
    current_rsi: Optional[float],
    *,
    stop_price: float,
    bars_held: int,
    entry_time: datetime,
    close_at: datetime,
) -> Optional[ReasonCode]:
    # Pessimistic same-candle ordering: any stop touch wins every conflict.
    if raw_bar.low <= stop_price:
        return ReasonCode.STOP_LOSS
    if current_rsi is not None and current_rsi >= EXIT_THRESHOLD:
        return ReasonCode.RSI_EXIT
    if bars_held >= MAX_HOLD_BARS or raw_bar.end - entry_time >= MAX_HOLD_DURATION:
        return ReasonCode.MAX_HOLD
    if raw_bar.end >= close_at - CLOSE_EXIT_LEAD:
        return ReasonCode.CLOSE_APPROACHING
    return None


def _adverse_buy(reference: float, config: BacktestConfig) -> float:
    bps = config.assumed_spread_bps / 2.0 + config.entry_cushion_bps
    return reference * (1.0 + bps / 10_000.0)


def _adverse_sell(reference: float, *, stressed: bool, config: BacktestConfig) -> float:
    cushion = (
        config.stressed_exit_cushion_bps if stressed else config.normal_exit_cushion_bps
    )
    bps = config.assumed_spread_bps / 2.0 + cushion
    return reference * (1.0 - bps / 10_000.0)


def run_candle_backtest(
    qfq_bars: Sequence[CompletedBar15m],
    raw_bars: Sequence[CompletedBar15m],
    daily_trends: Sequence[DatedTrend],
    sessions: Sequence[SessionBoundary],
    config: BacktestConfig,
) -> BacktestReport:
    """Run a deterministic, order-free candle proxy and return JSON-ready facts."""

    if type(config) is not BacktestConfig:
        raise TypeError("config must be an exact BacktestConfig")
    qfq, raw = _validate_paired_bars(qfq_bars, raw_bars, config.symbol)
    trends_untyped = _unique_by_date("daily_trends", daily_trends)
    sessions_untyped = _unique_by_date("sessions", sessions)
    if any(type(value) is not DatedTrend for value in trends_untyped.values()):
        raise TypeError("daily_trends must contain exact DatedTrend values")
    if any(type(value) is not SessionBoundary for value in sessions_untyped.values()):
        raise TypeError("sessions must contain exact SessionBoundary values")
    trends = {key: value for key, value in trends_untyped.items()}  # type: Dict[date, DatedTrend]
    boundaries = {key: value for key, value in sessions_untyped.items()}  # type: Dict[date, SessionBoundary]
    missing_sessions = set(bar.session_date for bar in qfq) - set(boundaries)
    if missing_sessions:
        raise ValueError("sessions do not cover every intraday bar date")

    rsi_values = wilder_rsi(qfq, RSI_PERIOD)
    atr_values = wilder_atr(qfq, ATR_PERIOD)
    equity = config.initial_equity
    week_start_equity = equity
    week_key = None
    day_start_equity = equity
    active_date = None  # type: Optional[date]
    traded_dates = set()  # type: set[date]
    trades = []  # type: list[BacktestTrade]
    entry_signal_count = 0
    risk_blocked_count = 0
    no_next_count = 0
    index = 0

    while index < len(qfq):
        signal = qfq[index]
        session_date = signal.session_date
        if session_date != active_date:
            active_date = session_date
            day_start_equity = equity
            current_week = (session_date.isocalendar().year, session_date.isocalendar().week)
            if current_week != week_key:
                week_key = current_week
                week_start_equity = equity

        if session_date in traded_dates or not _entry_signal(
            index, qfq, rsi_values, trends.get(session_date)
        ):
            index += 1
            continue
        if not _variant_allows_entry(index, qfq, atr_values, config.strategy_variant):
            index += 1
            continue
        is_q015 = (
            config.strategy_variant
            is BacktestVariant.Q015_PRIOR_CLOSE_NET_REWARD_RISK_GATE_V1
        )
        # Preserve the established baseline/Q013 report semantics: a supported
        # strategy signal is counted even when no safe next bar exists.  Q015's
        # candidate signal is counted only after its entry-time gate passes.
        if not is_q015:
            entry_signal_count += 1
        next_index = index + 1
        boundary = boundaries[session_date]
        if (
            next_index >= len(raw)
            or raw[next_index].session_date != session_date
            or raw[next_index].start != signal.end
            or raw[next_index].start >= boundary.close_at - CLOSE_EXIT_LEAD
        ):
            no_next_count += 1
            index += 1
            continue
        atr_qfq = atr_values[index]
        if atr_qfq is None:
            index += 1
            continue
        state = RiskState(
            day_start_equity=day_start_equity,
            week_start_equity=week_start_equity,
            daily_pnl=equity - day_start_equity,
            weekly_pnl=equity - week_start_equity,
            completed_roundtrips_today=0,
        )
        q015_sizing = None  # type: Optional[SizingResult]
        if is_q015:
            q015_sizing = _q015_allows_entry(
                index,
                qfq,
                raw,
                atr_values,
                next_index=next_index,
                state=state,
                config=config,
                boundaries=boundaries,
            )
            if q015_sizing is None:
                index += 1
                continue
            entry_signal_count += 1
        scale = raw[index].close / signal.close
        atr_raw = float(atr_qfq) * scale
        entry_reference = raw[next_index].open
        entry_price = _adverse_buy(entry_reference, config)
        stop_price = entry_price - ATR_STOP_MULTIPLE * atr_raw
        if not math.isfinite(stop_price) or stop_price <= 0.0:
            risk_blocked_count += 1
            index += 1
            continue
        sizing = q015_sizing
        if sizing is None:
            sizing = size_position(
                SizingRequest(
                    state=state,
                    entry_limit=entry_price,
                    stop_trigger=stop_price,
                    policy=config.risk_policy,
                )
            )
        if not sizing.allowed:
            risk_blocked_count += 1
            index += 1
            continue

        exit_index = next_index
        exit_reason = None  # type: Optional[ReasonCode]
        bars_held = 0
        while exit_index < len(raw) and raw[exit_index].session_date == session_date:
            bars_held += 1
            exit_reason = _exit_reason(
                raw[exit_index],
                rsi_values[exit_index],
                stop_price=stop_price,
                bars_held=bars_held,
                entry_time=raw[next_index].start,
                close_at=boundary.close_at,
            )
            if exit_reason is not None:
                break
            exit_index += 1
        if exit_reason is None:
            # A supplied calendar/bar mismatch must never create an overnight proxy.
            raise ValueError("session ended without a deterministic flat exit")

        exit_bar = raw[exit_index]
        if exit_reason is ReasonCode.STOP_LOSS:
            exit_reference = min(stop_price, exit_bar.open)
            exit_price = _adverse_sell(exit_reference, stressed=True, config=config)
        else:
            exit_reference = exit_bar.close
            exit_price = _adverse_sell(
                exit_reference,
                stressed=exit_reason is ReasonCode.CLOSE_APPROACHING,
                config=config,
            )
        quantity = sizing.qty
        buy_fee = float(
            calculate_order_fees(PAPER_FEE_SCHEDULE, OrderSide.BUY, quantity, entry_price).total
        )
        sell_fee = float(
            calculate_order_fees(PAPER_FEE_SCHEDULE, OrderSide.SELL, quantity, exit_price).total
        )
        gross = quantity * (exit_price - entry_price)
        fees = buy_fee + sell_fee
        net = gross - fees
        equity += net
        trade = BacktestTrade(
            opportunity_id="%s:%s" % (config.symbol, signal.end.isoformat()),
            session_date=session_date,
            signal_bar_end=signal.end,
            entry_time=raw[next_index].start,
            exit_time=exit_bar.end,
            quantity=quantity,
            raw_entry_reference=entry_reference,
            entry_price=entry_price,
            raw_exit_reference=exit_reference,
            exit_price=exit_price,
            atr_raw=atr_raw,
            stop_price=stop_price,
            exit_reason=exit_reason,
            bars_held=bars_held,
            gross_pnl=gross,
            fees=fees,
            net_pnl=net,
            equity_after=equity,
        )
        trades.append(trade)
        traded_dates.add(session_date)
        index = exit_index + 1

    trade_tuple = tuple(trades)
    net_values = tuple(trade.net_pnl for trade in trade_tuple)
    wins = tuple(value for value in net_values if value > 0.0)
    losses = tuple(value for value in net_values if value < 0.0)
    flats = tuple(value for value in net_values if value == 0.0)
    decided = len(wins) + len(losses)
    peak = config.initial_equity
    maximum_drawdown = 0.0
    maximum_drawdown_fraction = 0.0
    for trade in trade_tuple:
        peak = max(peak, trade.equity_after)
        drawdown = peak - trade.equity_after
        maximum_drawdown = max(maximum_drawdown, drawdown)
        maximum_drawdown_fraction = max(maximum_drawdown_fraction, drawdown / peak)
    total_gross = math.fsum(trade.gross_pnl for trade in trade_tuple)
    total_fees = math.fsum(trade.fees for trade in trade_tuple)
    total_net = math.fsum(net_values)
    gross_profit = math.fsum(wins)
    gross_loss = abs(math.fsum(losses))

    q015_assumptions = ()
    q015_limitations = ()
    if config.strategy_variant is BacktestVariant.Q015_PRIOR_CLOSE_NET_REWARD_RISK_GATE_V1:
        q015_assumptions = (
            "Q015 uses the prior official completed RTH close only as a modelled reward proxy.",
        )
        q015_limitations = (
            "The Q015 prior-close proxy is not an exit target, is not guaranteed to be reached, and remains IN_SAMPLE_POST_HOC until separately sealed evidence exists.",
        )

    return BacktestReport(
        model_id=MODEL_ID,
        status=RESULT_STATUS,
        strategy_variant_id=config.strategy_variant.value,
        symbol=config.symbol,
        first_session=qfq[0].session_date,
        last_session=qfq[-1].session_date,
        completed_bar_count=len(qfq),
        entry_signal_count=entry_signal_count,
        risk_blocked_signal_count=risk_blocked_count,
        no_next_bar_signal_count=no_next_count,
        trade_count=len(trade_tuple),
        winning_trades=len(wins),
        losing_trades=len(losses),
        flat_trades=len(flats),
        win_rate_excluding_flat=(len(wins) / decided) if decided else None,
        initial_equity=config.initial_equity,
        final_equity=equity,
        total_gross_pnl=total_gross,
        total_fees=total_fees,
        total_net_pnl=total_net,
        terminal_return=(equity / config.initial_equity) - 1.0,
        profit_factor=(gross_profit / gross_loss) if losses else None,
        closed_trade_max_drawdown=maximum_drawdown,
        closed_trade_max_drawdown_fraction=maximum_drawdown_fraction,
        assumptions=(
            "QFQ completed RTH 15-minute candles determine RSI/ATR and signals.",
            "Timestamp-matched RAW candles determine execution references and PnL.",
            "Historical spread is unobservable; a frozen 10 bp spread proxy is assumed.",
            "Historical fills are unobservable; every eligible next-bar proxy entry is assumed filled in full.",
            "Entry uses the next RAW bar open plus half-spread and a 10 bp adverse cushion.",
            "Normal exits use completed RAW bar close minus half-spread and 15 bp; stop/close exits use 50 bp.",
            "If a stop and another exit occur in one candle, the stop is applied first.",
            "A proxy entry requires a same-session next bar beginning before close minus 15 minutes.",
        ) + q015_assumptions,
        limitations=(
            "This is exploratory candle evidence, not a broker fill replay or final out-of-sample proof.",
            "Acquisition-date QFQ history is not proven point-in-time and may contain later corporate-action adjustments; timestamp no-lookahead does not remove that limitation.",
            "Bid/ask path, queue position, partial fills, rejects, halts, and latency cannot be reconstructed.",
            "Daily trend eligibility is trusted as a precomputed point-in-time input.",
            "User symbol-selection effects and survivorship bias are not estimated by this single-symbol engine.",
            "Drawdown is measured only on closed-trade equity and can understate intratrade drawdown.",
        ) + q015_limitations,
        config=config,
        trades=trade_tuple,
    )


__all__ = [
    "BacktestConfig",
    "BacktestReport",
    "BacktestTrade",
    "BacktestVariant",
    "DatedTrend",
    "MODEL_ID",
    "Q013_ATR_CLOSE_CAP",
    "Q015_VARIANT_ID",
    "RESULT_STATUS",
    "SessionBoundary",
    "_prior_completed_rth_close",
    "_q015_allows_entry",
    "_q015_reward_covers_loss",
    "run_candle_backtest",
]
