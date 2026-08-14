"""Immutable domain models for the RSI autopilot core.

The models deliberately contain no broker SDK types.  Values crossing this
boundary are validated strictly so malformed or ambiguous market data fails
closed before it reaches strategy code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import Enum
import math
import re
from typing import Optional, Tuple
from zoneinfo import ZoneInfo


NEW_YORK = ZoneInfo("America/New_York")
_SYMBOL_RE = re.compile(r"^US\.[A-Z][A-Z0-9.-]{0,14}$")


def require_bool(name: str, value: object) -> bool:
    if type(value) is not bool:
        raise TypeError("%s must be an exact bool" % name)
    return value


def require_number(
    name: str,
    value: object,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> float:
    """Return a finite float while rejecting bool and numeric lookalikes."""

    if type(value) not in (int, float):
        raise TypeError("%s must be an int or float (bool is not accepted)" % name)
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("%s must be finite" % name)
    if positive and result <= 0.0:
        raise ValueError("%s must be greater than zero" % name)
    if nonnegative and result < 0.0:
        raise ValueError("%s must be nonnegative" % name)
    return result


def require_int(name: str, value: object, *, nonnegative: bool = False) -> int:
    if type(value) is not int:
        raise TypeError("%s must be an exact int" % name)
    if nonnegative and value < 0:
        raise ValueError("%s must be nonnegative" % name)
    return value


def require_aware_datetime(name: str, value: object) -> datetime:
    if type(value) is not datetime:
        raise TypeError("%s must be an exact datetime" % name)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("%s must be timezone-aware" % name)
    return value


def require_symbol(name: str, value: object) -> str:
    if type(value) is not str:
        raise TypeError("%s must be an exact str" % name)
    if not _SYMBOL_RE.fullmatch(value):
        raise ValueError("%s must use canonical US.<TICKER> form" % name)
    return value


@dataclass(frozen=True)
class CompletedBar15m:
    """A single, completed US regular-session 15-minute OHLCV bar."""

    symbol: str
    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    complete: bool = True

    def __post_init__(self) -> None:
        require_symbol("symbol", self.symbol)
        start = require_aware_datetime("start", self.start)
        end = require_aware_datetime("end", self.end)
        require_bool("complete", self.complete)
        if not self.complete:
            raise ValueError("forming/incomplete bars are forbidden")
        if end <= start or end - start != timedelta(minutes=15):
            raise ValueError("bar interval must be exactly 15 minutes")

        start_ny = start.astimezone(NEW_YORK)
        end_ny = end.astimezone(NEW_YORK)
        if start_ny.date() != end_ny.date():
            raise ValueError("a bar cannot cross an RTH session date")
        if start_ny.second or start_ny.microsecond or start_ny.minute % 15:
            raise ValueError("bar start must lie on a 15-minute boundary")
        if end_ny.second or end_ny.microsecond or end_ny.minute % 15:
            raise ValueError("bar end must lie on a 15-minute boundary")
        if start_ny.timetz().replace(tzinfo=None) < time(9, 30):
            raise ValueError("pre-market bars are forbidden")
        if end_ny.timetz().replace(tzinfo=None) > time(16, 0):
            raise ValueError("after-hours bars are forbidden")

        open_price = require_number("open", self.open, positive=True)
        high = require_number("high", self.high, positive=True)
        low = require_number("low", self.low, positive=True)
        close = require_number("close", self.close, positive=True)
        volume = require_number("volume", self.volume, nonnegative=True)
        if low > min(open_price, close) or high < max(open_price, close):
            raise ValueError("high/low do not contain open and close")
        if high < low:
            raise ValueError("high must be greater than or equal to low")

        object.__setattr__(self, "open", open_price)
        object.__setattr__(self, "high", high)
        object.__setattr__(self, "low", low)
        object.__setattr__(self, "close", close)
        object.__setattr__(self, "volume", volume)

    @property
    def session_date(self):  # type: ignore[no-untyped-def]
        return self.start.astimezone(NEW_YORK).date()

    @property
    def bar_id(self) -> str:
        return "%s:%s" % (self.symbol, self.end.astimezone(NEW_YORK).isoformat())


@dataclass(frozen=True)
class TrendEligibility:
    """Daily trend checks computed only from data through the prior session."""

    symbol_daily_ok: bool
    spy_daily_ok: bool
    daily_data_ok: bool = True

    def __post_init__(self) -> None:
        require_bool("symbol_daily_ok", self.symbol_daily_ok)
        require_bool("spy_daily_ok", self.spy_daily_ok)
        require_bool("daily_data_ok", self.daily_data_ok)


@dataclass(frozen=True)
class MarketGates:
    """Execution-quality gates.  All must pass for a new entry."""

    data_ok: bool
    spread_ok: bool
    market_open: bool = True

    def __post_init__(self) -> None:
        require_bool("data_ok", self.data_ok)
        require_bool("spread_ok", self.spread_ok)
        require_bool("market_open", self.market_open)


@dataclass(frozen=True)
class PositionSnapshot:
    """Minimal raw-price position state needed by the pure exit strategy."""

    entry_raw: float
    atr_raw: float
    current_raw_price: float
    bars_held: int
    entry_time: datetime
    exchange_close: datetime
    emergency_exit: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "entry_raw", require_number("entry_raw", self.entry_raw, positive=True)
        )
        object.__setattr__(
            self, "atr_raw", require_number("atr_raw", self.atr_raw, positive=True)
        )
        object.__setattr__(
            self,
            "current_raw_price",
            require_number("current_raw_price", self.current_raw_price, positive=True),
        )
        require_int("bars_held", self.bars_held, nonnegative=True)
        entry_time = require_aware_datetime("entry_time", self.entry_time)
        exchange_close = require_aware_datetime("exchange_close", self.exchange_close)
        if exchange_close <= entry_time:
            raise ValueError("exchange_close must be after entry_time")
        require_bool("emergency_exit", self.emergency_exit)
        if self.stop_price <= 0.0:
            raise ValueError("entry_raw - 1.5 * atr_raw must remain positive")

    @property
    def stop_price(self) -> float:
        return self.entry_raw - 1.5 * self.atr_raw


class DecisionAction(str, Enum):
    WAIT = "WAIT"
    ENTER = "ENTER"
    EXIT = "EXIT"


class ReasonCode(str, Enum):
    ACTIVE_SYMBOL_MISMATCH = "ACTIVE_SYMBOL_MISMATCH"
    NO_COMPLETED_BARS = "NO_COMPLETED_BARS"
    INDICATOR_NOT_READY = "INDICATOR_NOT_READY"
    DAILY_DATA_INVALID = "DAILY_DATA_INVALID"
    SYMBOL_TREND_BLOCKED = "SYMBOL_TREND_BLOCKED"
    SPY_TREND_BLOCKED = "SPY_TREND_BLOCKED"
    MARKET_CLOSED = "MARKET_CLOSED"
    INTRADAY_DATA_INVALID = "INTRADAY_DATA_INVALID"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    OUTSIDE_ENTRY_WINDOW = "OUTSIDE_ENTRY_WINDOW"
    ROUNDTRIP_LIMIT_REACHED = "ROUNDTRIP_LIMIT_REACHED"
    RSI_NOT_OVERSOLD_RECENTLY = "RSI_NOT_OVERSOLD_RECENTLY"
    RSI_RECOVERY_NOT_CONFIRMED = "RSI_RECOVERY_NOT_CONFIRMED"
    VOLUME_HISTORY_INSUFFICIENT = "VOLUME_HISTORY_INSUFFICIENT"
    VOLUME_DATA_INVALID = "VOLUME_DATA_INVALID"
    VOLUME_CONFIRMATION_MISSING = "VOLUME_CONFIRMATION_MISSING"
    PRICE_REFERENCE_INVALID = "PRICE_REFERENCE_INVALID"
    PRICE_CONFIRMATION_MISSING = "PRICE_CONFIRMATION_MISSING"
    BREAKOUT_TOO_EXTENDED = "BREAKOUT_TOO_EXTENDED"
    ENTRY_SIGNAL_CONFIRMED = "ENTRY_SIGNAL_CONFIRMED"
    HOLD_NO_EXIT = "HOLD_NO_EXIT"
    EMERGENCY_EXIT = "EMERGENCY_EXIT"
    STOP_LOSS = "STOP_LOSS"
    RSI_EXIT = "RSI_EXIT"
    MAX_HOLD = "MAX_HOLD"
    CLOSE_APPROACHING = "CLOSE_APPROACHING"


@dataclass(frozen=True)
class StrategyDecision:
    action: DecisionAction
    reasons: Tuple[ReasonCode, ...]
    signal_bar_end: Optional[datetime]
    stop_price: Optional[float] = None

    def __post_init__(self) -> None:
        if type(self.action) is not DecisionAction:
            raise TypeError("action must be an exact DecisionAction")
        if type(self.reasons) is not tuple or not self.reasons:
            raise ValueError("reasons must be a nonempty tuple")
        if any(type(reason) is not ReasonCode for reason in self.reasons):
            raise TypeError("every reason must be an exact ReasonCode")
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("reason codes must not be duplicated")
        if self.signal_bar_end is not None:
            require_aware_datetime("signal_bar_end", self.signal_bar_end)
        if self.stop_price is not None:
            stop = require_number("stop_price", self.stop_price, positive=True)
            object.__setattr__(self, "stop_price", stop)


@dataclass(frozen=True)
class StrategyContext:
    """All pure inputs needed for one strategy evaluation."""

    active_symbol: str
    selected_symbol: str
    bars: Tuple[CompletedBar15m, ...]
    rsi_values: Tuple[Optional[float], ...]
    trend: TrendEligibility
    gates: MarketGates
    now: datetime
    position: Optional[PositionSnapshot] = None
    traded_roundtrips_today: int = 0

    def __post_init__(self) -> None:
        require_symbol("active_symbol", self.active_symbol)
        require_symbol("selected_symbol", self.selected_symbol)
        if type(self.bars) is not tuple:
            raise TypeError("bars must be a tuple")
        if any(type(bar) is not CompletedBar15m for bar in self.bars):
            raise TypeError("bars must contain exact CompletedBar15m values")
        if type(self.rsi_values) is not tuple:
            raise TypeError("rsi_values must be a tuple")
        if len(self.rsi_values) != len(self.bars):
            raise ValueError("rsi_values must align one-to-one with bars")
        normalized = []
        for index, value in enumerate(self.rsi_values):
            if value is None:
                normalized.append(None)
                continue
            number = require_number("rsi_values[%d]" % index, value)
            if not 0.0 <= number <= 100.0:
                raise ValueError("RSI values must be in [0, 100]")
            normalized.append(number)
        object.__setattr__(self, "rsi_values", tuple(normalized))
        if type(self.trend) is not TrendEligibility:
            raise TypeError("trend must be an exact TrendEligibility")
        if type(self.gates) is not MarketGates:
            raise TypeError("gates must be an exact MarketGates")
        require_aware_datetime("now", self.now)
        if self.position is not None and type(self.position) is not PositionSnapshot:
            raise TypeError("position must be an exact PositionSnapshot or None")
        require_int(
            "traded_roundtrips_today", self.traded_roundtrips_today, nonnegative=True
        )
