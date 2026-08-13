"""Fail-closed market-data boundary for the RSI autopilot.

The pure validators in this module accept only completed, chronological data
whose timestamps can be reconciled against a hash-pinned exchange calendar.
The optional OpenD quote adapter is inert until called and receives an SDK
loader explicitly; it never imports the SDK, opens a trade context, inspects
an account, or sends an order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
import math
import re
from statistics import median
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, Tuple
from zoneinfo import ZoneInfo

from .candidates import CandidateInput, InstrumentKind, normalize_us_symbol
from .exchange_calendar import FrozenExchangeCalendar, NEW_YORK
from .indicators import simple_moving_average, validate_bar_series
from .models import CompletedBar15m, MarketGates


OPEND_HOST = "127.0.0.1"
OPEND_PORT = 11111
SPY_SYMBOL = "US.SPY"


class MarketDataError(RuntimeError):
    """Base class for unavailable or unsafe market data."""


class MarketDataUnavailable(MarketDataError):
    """The quote-only provider did not return an authoritative response."""


class MarketDataValidationError(MarketDataError):
    """Market data is malformed, mixed-scale, future, forming, or gapped."""


class StaleMarketData(MarketDataValidationError):
    """A required latest completed observation is absent or too old."""


class PriceScale(str, Enum):
    RAW = "RAW"
    QFQ = "QFQ"


class MarketState(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    HALTED = "HALTED"
    UNKNOWN = "UNKNOWN"


def _finite_number(name: str, value: object, *, positive: bool = False) -> float:
    if type(value) not in (int, float):
        raise MarketDataValidationError("%s must be an int or float" % name)
    result = float(value)
    if not math.isfinite(result):
        raise MarketDataValidationError("%s must be finite" % name)
    if positive and result <= 0:
        raise MarketDataValidationError("%s must be positive" % name)
    return result


def _aware(name: str, value: object) -> datetime:
    if type(value) is not datetime:
        raise MarketDataValidationError("%s must be an exact datetime" % name)
    if value.tzinfo is None or value.utcoffset() is None:
        raise MarketDataValidationError("%s must be timezone-aware" % name)
    return value


@dataclass(frozen=True)
class DailyBar:
    """One completed daily RTH bar with an explicit adjustment scale."""

    symbol: str
    session_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    scale: PriceScale

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_us_symbol(self.symbol))
        if type(self.session_date) is not date:
            raise MarketDataValidationError("session_date must be an exact date")
        if type(self.scale) is not PriceScale:
            raise MarketDataValidationError("scale must be an exact PriceScale")
        opened = _finite_number("open", self.open, positive=True)
        high = _finite_number("high", self.high, positive=True)
        low = _finite_number("low", self.low, positive=True)
        closed = _finite_number("close", self.close, positive=True)
        volume = _finite_number("volume", self.volume)
        if volume < 0:
            raise MarketDataValidationError("volume must be nonnegative")
        if high < max(opened, closed) or low > min(opened, closed) or high < low:
            raise MarketDataValidationError("daily high/low are incoherent")
        object.__setattr__(self, "open", opened)
        object.__setattr__(self, "high", high)
        object.__setattr__(self, "low", low)
        object.__setattr__(self, "close", closed)
        object.__setattr__(self, "volume", volume)


@dataclass(frozen=True)
class ScaleAlignedDailyHistory:
    """Date-aligned RAW and QFQ histories validated as one observation set."""

    raw: Tuple[DailyBar, ...]
    qfq: Tuple[DailyBar, ...]
    current_qfq_to_raw: float


@dataclass(frozen=True)
class FrozenAdjustmentScale:
    """Session-bound, immutable conversion from QFQ indicator units to raw."""

    symbol: str
    target_session: date
    source_daily_session: date
    qfq_to_raw: float
    frozen_at: datetime
    calendar_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_us_symbol(self.symbol))
        if type(self.target_session) is not date or type(self.source_daily_session) is not date:
            raise MarketDataValidationError("adjustment scale sessions must be exact dates")
        if self.source_daily_session >= self.target_session:
            raise MarketDataValidationError(
                "adjustment scale must be frozen from a prior completed session"
            )
        factor = _finite_number("qfq_to_raw", self.qfq_to_raw, positive=True)
        _aware("frozen_at", self.frozen_at)
        if not re.fullmatch(r"[0-9a-f]{64}", self.calendar_sha256):
            raise MarketDataValidationError("calendar_sha256 must be a lowercase SHA-256")
        object.__setattr__(self, "qfq_to_raw", factor)

    def raw_value(self, qfq_value: object) -> float:
        """Convert a finite QFQ price or ATR to the frozen raw-price scale."""

        value = _finite_number("qfq_value", qfq_value)
        result = value * self.qfq_to_raw
        if not math.isfinite(result):
            raise MarketDataValidationError("converted raw value is non-finite")
        return result


@dataclass(frozen=True)
class IndicatorBarSeries:
    """QFQ-only bars for RSI/ATR plus their frozen raw conversion factor."""

    bars: Tuple[CompletedBar15m, ...]
    scale: PriceScale
    adjustment_scale: FrozenAdjustmentScale

    def __post_init__(self) -> None:
        if type(self.bars) is not tuple or not self.bars:
            raise MarketDataValidationError("indicator bars must be a nonempty tuple")
        if self.scale is not PriceScale.QFQ:
            raise MarketDataValidationError("RSI/ATR indicator bars must be QFQ")
        if type(self.adjustment_scale) is not FrozenAdjustmentScale:
            raise MarketDataValidationError(
                "indicator bars require an exact FrozenAdjustmentScale"
            )
        if any(
            type(bar) is not CompletedBar15m
            or bar.symbol != self.adjustment_scale.symbol
            for bar in self.bars
        ):
            raise MarketDataValidationError("indicator bar symbol/identity mismatch")


@dataclass(frozen=True)
class QuoteSnapshot:
    """Fresh raw quote, raw top of book, and market state observed together."""

    symbol: str
    observed_at: datetime
    bid: float
    ask: float
    last: float
    market_state: MarketState
    halted: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_us_symbol(self.symbol))
        _aware("observed_at", self.observed_at)
        bid = _finite_number("bid", self.bid, positive=True)
        ask = _finite_number("ask", self.ask, positive=True)
        last = _finite_number("last", self.last, positive=True)
        if bid > ask:
            raise MarketDataValidationError("crossed top of book is ambiguous")
        if type(self.market_state) is not MarketState:
            raise MarketDataValidationError("market_state must be an exact MarketState")
        if type(self.halted) is not bool:
            raise MarketDataValidationError("halted must be an exact bool")
        object.__setattr__(self, "bid", bid)
        object.__setattr__(self, "ask", ask)
        object.__setattr__(self, "last", last)

    @property
    def spread_bps(self) -> float:
        midpoint = (self.bid + self.ask) / 2.0
        return (self.ask - self.bid) / midpoint * 10_000.0

    @property
    def scale(self) -> PriceScale:
        """Execution quotes are always raw; adjusted quotes are never orders."""

        return PriceScale.RAW


@dataclass(frozen=True)
class InstrumentMetadata:
    """Classification from a separately trusted point-in-time metadata source."""

    symbol: str
    instrument_kind: InstrumentKind
    is_halted: bool = False
    has_delisting_risk: bool = False
    status_known: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_us_symbol(self.symbol))
        if type(self.instrument_kind) is not InstrumentKind:
            raise MarketDataValidationError(
                "instrument_kind must be an exact InstrumentKind"
            )
        for name in ("is_halted", "has_delisting_risk", "status_known"):
            if type(getattr(self, name)) is not bool:
                raise MarketDataValidationError("%s must be an exact bool" % name)


class InstrumentClassifier(Protocol):
    """Trusted classifier; unknown or leveraged products must fail closed."""

    def classify(self, symbol: str, *, as_of: datetime) -> InstrumentMetadata:
        ...


def instrument_metadata_payload_sha256(
    *,
    source_revision: str,
    observed_at: datetime,
    valid_through: datetime,
    instruments: Sequence[InstrumentMetadata],
) -> str:
    """Hash a deterministic point-in-time instrument-classification snapshot."""

    if type(source_revision) is not str or not source_revision.strip():
        raise MarketDataValidationError("metadata source_revision is required")
    observed = _aware("observed_at", observed_at).astimezone(timezone.utc)
    expires = _aware("valid_through", valid_through).astimezone(timezone.utc)
    if expires < observed:
        raise MarketDataValidationError("metadata validity interval is inverted")
    values = tuple(instruments)
    if any(type(item) is not InstrumentMetadata for item in values):
        raise MarketDataValidationError(
            "metadata snapshot must contain exact InstrumentMetadata values"
        )
    payload = {
        "instruments": [
            {
                "has_delisting_risk": item.has_delisting_risk,
                "instrument_kind": item.instrument_kind.value,
                "is_halted": item.is_halted,
                "status_known": item.status_known,
                "symbol": item.symbol,
            }
            for item in values
        ],
        "observed_at": observed.isoformat(),
        "source_revision": source_revision,
        "valid_through": expires.isoformat(),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded + b"\n").hexdigest()


@dataclass(frozen=True)
class FrozenInstrumentClassifier:
    """Hash-pinned classifier that returns UNKNOWN for an absent instrument."""

    source_revision: str
    observed_at: datetime
    valid_through: datetime
    instruments: Tuple[InstrumentMetadata, ...]
    expected_sha256: str

    def __post_init__(self) -> None:
        observed = _aware("observed_at", self.observed_at)
        expires = _aware("valid_through", self.valid_through)
        if expires < observed:
            raise MarketDataValidationError("metadata validity interval is inverted")
        if type(self.instruments) is not tuple:
            raise MarketDataValidationError("instruments must be a tuple")
        if any(type(item) is not InstrumentMetadata for item in self.instruments):
            raise MarketDataValidationError(
                "instruments must contain exact InstrumentMetadata values"
            )
        symbols = tuple(item.symbol for item in self.instruments)
        if symbols != tuple(sorted(symbols)) or len(symbols) != len(set(symbols)):
            raise MarketDataValidationError(
                "metadata instruments must be unique and sorted by symbol"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", self.expected_sha256):
            raise MarketDataValidationError(
                "metadata expected_sha256 must be a lowercase SHA-256"
            )
        actual = instrument_metadata_payload_sha256(
            source_revision=self.source_revision,
            observed_at=self.observed_at,
            valid_through=self.valid_through,
            instruments=self.instruments,
        )
        if actual != self.expected_sha256:
            raise MarketDataValidationError("instrument metadata SHA-256 mismatch")

    def classify(self, symbol: str, *, as_of: datetime) -> InstrumentMetadata:
        code = normalize_us_symbol(symbol)
        clock = _aware("as_of", as_of)
        if not self.observed_at <= clock <= self.valid_through:
            raise StaleMarketData("instrument metadata snapshot is outside validity")
        matches = tuple(item for item in self.instruments if item.symbol == code)
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise MarketDataValidationError("instrument classification is ambiguous")
        return InstrumentMetadata(
            symbol=code,
            instrument_kind=InstrumentKind.UNKNOWN,
            status_known=False,
        )


def validate_daily_bars(
    bars: Sequence[DailyBar],
    *,
    symbol: str,
    scale: PriceScale,
    as_of: datetime,
    calendar: FrozenExchangeCalendar,
) -> Tuple[DailyBar, ...]:
    """Validate completed daily history with no missing frozen sessions."""

    normalized_symbol = normalize_us_symbol(symbol)
    cutoff = _aware("as_of", as_of)
    if isinstance(bars, (str, bytes)) or not isinstance(bars, Sequence):
        raise MarketDataValidationError("daily bars must be a sequence")
    checked = tuple(bars)
    if not checked:
        raise MarketDataValidationError("daily bars must not be empty")
    previous = None  # type: Optional[date]
    for index, bar in enumerate(checked):
        if type(bar) is not DailyBar:
            raise MarketDataValidationError(
                "daily bars[%d] must be an exact DailyBar" % index
            )
        if bar.symbol != normalized_symbol or bar.scale is not scale:
            raise MarketDataValidationError("daily symbol/scale mismatch")
        calendar.assert_covered(bar.session_date)
        if calendar.session_on(bar.session_date) is None:
            raise MarketDataValidationError("daily bar falls on a closed calendar date")
        if previous is not None and bar.session_date <= previous:
            raise MarketDataValidationError("daily bars are duplicate or out of order")
        previous = bar.session_date

    expected = tuple(
        session.session_date
        for session in calendar.sessions_between(
            checked[0].session_date, checked[-1].session_date
        )
    )
    actual = tuple(bar.session_date for bar in checked)
    if actual != expected:
        raise MarketDataValidationError("daily history has a missing or extra session")
    latest = calendar.latest_completed_session(cutoff).session_date
    if checked[-1].session_date != latest:
        raise StaleMarketData("latest completed daily session is missing")
    return checked


def validate_scale_alignment(
    raw: Sequence[DailyBar],
    qfq: Sequence[DailyBar],
    *,
    latest_ratio_tolerance: float = 0.001,
    per_bar_ratio_tolerance: float = 0.001,
) -> ScaleAlignedDailyHistory:
    """Reject RAW/QFQ mixing and inconsistent adjustment ratios.

    Each day's OHLC values must share one adjustment ratio and volume must be
    unchanged.  For provider-style QFQ, the newest completed bar must be on the
    current raw-price scale within the configured rounding tolerance.
    """

    raw_bars = tuple(raw)
    qfq_bars = tuple(qfq)
    if not raw_bars or len(raw_bars) != len(qfq_bars):
        raise MarketDataValidationError("RAW and QFQ histories must align one-to-one")
    latest_tolerance = _finite_number(
        "latest_ratio_tolerance", latest_ratio_tolerance
    )
    per_bar_tolerance = _finite_number(
        "per_bar_ratio_tolerance", per_bar_ratio_tolerance
    )
    if latest_tolerance < 0 or per_bar_tolerance < 0:
        raise MarketDataValidationError("scale tolerances must be nonnegative")

    ratios = []
    for index, (raw_bar, qfq_bar) in enumerate(zip(raw_bars, qfq_bars)):
        if type(raw_bar) is not DailyBar or type(qfq_bar) is not DailyBar:
            raise MarketDataValidationError("scale histories contain non-DailyBar values")
        if raw_bar.scale is not PriceScale.RAW or qfq_bar.scale is not PriceScale.QFQ:
            raise MarketDataValidationError("RAW/QFQ tags are missing or reversed")
        if raw_bar.symbol != qfq_bar.symbol or raw_bar.session_date != qfq_bar.session_date:
            raise MarketDataValidationError("RAW/QFQ symbol or date mismatch")
        if not math.isclose(raw_bar.volume, qfq_bar.volume, rel_tol=1e-9, abs_tol=1e-6):
            raise MarketDataValidationError("RAW/QFQ volume mismatch")

        field_ratios = tuple(
            getattr(raw_bar, name) / getattr(qfq_bar, name)
            for name in ("open", "high", "low", "close")
        )
        reference = field_ratios[-1]
        if any(
            not math.isclose(
                value,
                reference,
                rel_tol=per_bar_tolerance,
                abs_tol=per_bar_tolerance,
            )
            for value in field_ratios
        ):
            raise MarketDataValidationError(
                "inconsistent RAW/QFQ OHLC ratio at index %d" % index
            )
        ratios.append(reference)

    latest_ratio = ratios[-1]
    if not math.isclose(
        latest_ratio, 1.0, rel_tol=latest_tolerance, abs_tol=latest_tolerance
    ):
        raise MarketDataValidationError("latest QFQ bar is not on the current raw scale")
    return ScaleAlignedDailyHistory(raw_bars, qfq_bars, latest_ratio)


def freeze_adjustment_scale(
    history: ScaleAlignedDailyHistory,
    *,
    target_session: date,
    frozen_at: datetime,
    calendar: FrozenExchangeCalendar,
) -> FrozenAdjustmentScale:
    """Freeze the prior completed daily QFQ-to-raw factor for one session."""

    if type(history) is not ScaleAlignedDailyHistory:
        raise MarketDataValidationError("history must be a ScaleAlignedDailyHistory")
    if type(target_session) is not date:
        raise MarketDataValidationError("target_session must be an exact date")
    clock = _aware("frozen_at", frozen_at).astimezone(NEW_YORK)
    session = calendar.session_on(target_session)
    if session is None:
        raise MarketDataValidationError("target_session is not an exchange session")
    # The factor is frozen at the official open boundary, before an indicator
    # decision can consume any bar from this session.
    if clock != session.open_at:
        raise MarketDataValidationError("adjustment scale must freeze at official session open")
    if not history.raw or not history.qfq:
        raise MarketDataValidationError("aligned daily history is empty")
    symbol = history.raw[-1].symbol
    source_session = history.raw[-1].session_date
    if source_session >= target_session:
        raise MarketDataValidationError(
            "adjustment source must be the prior completed daily session"
        )
    return FrozenAdjustmentScale(
        symbol=symbol,
        target_session=target_session,
        source_daily_session=source_session,
        qfq_to_raw=history.current_qfq_to_raw,
        frozen_at=clock,
        calendar_sha256=calendar.sha256,
    )


def assert_adjustment_scale_unchanged(
    frozen: FrozenAdjustmentScale,
    refreshed_history: ScaleAlignedDailyHistory,
    *,
    relative_tolerance: float = 0.001,
) -> None:
    """Fail closed if a refreshed provider series changes the frozen factor."""

    if type(frozen) is not FrozenAdjustmentScale:
        raise MarketDataValidationError("frozen must be a FrozenAdjustmentScale")
    if type(refreshed_history) is not ScaleAlignedDailyHistory:
        raise MarketDataValidationError(
            "refreshed_history must be a ScaleAlignedDailyHistory"
        )
    tolerance = _finite_number("relative_tolerance", relative_tolerance)
    if tolerance < 0:
        raise MarketDataValidationError("relative_tolerance must be nonnegative")
    if not refreshed_history.raw or refreshed_history.raw[-1].symbol != frozen.symbol:
        raise MarketDataValidationError("adjustment scale symbol changed")
    source_pairs = tuple(
        (raw_bar, qfq_bar)
        for raw_bar, qfq_bar in zip(refreshed_history.raw, refreshed_history.qfq)
        if raw_bar.session_date == frozen.source_daily_session
        and qfq_bar.session_date == frozen.source_daily_session
    )
    if len(source_pairs) != 1:
        raise MarketDataValidationError(
            "frozen adjustment source session is missing or ambiguous"
        )
    refreshed_factor = source_pairs[0][0].close / source_pairs[0][1].close
    if not math.isclose(
        refreshed_factor,
        frozen.qfq_to_raw,
        rel_tol=tolerance,
        abs_tol=tolerance,
    ):
        raise MarketDataValidationError("QFQ-to-raw adjustment scale changed mid-session")


def validate_completed_15m_bars(
    bars: Sequence[CompletedBar15m],
    *,
    symbol: str,
    as_of: datetime,
    calendar: FrozenExchangeCalendar,
    completion_lag: timedelta = timedelta(seconds=30),
) -> Tuple[CompletedBar15m, ...]:
    """Reject future, forming, duplicate, gapped, and stale intraday bars."""

    normalized_symbol = normalize_us_symbol(symbol)
    cutoff_clock = _aware("as_of", as_of)
    if type(completion_lag) is not timedelta:
        raise MarketDataValidationError("completion_lag must be an exact timedelta")
    if completion_lag < timedelta(0) or completion_lag >= timedelta(minutes=15):
        raise MarketDataValidationError("completion_lag must be in [0, 15 minutes)")
    cutoff = cutoff_clock - completion_lag
    try:
        checked = validate_bar_series(bars)
    except (TypeError, ValueError) as exc:
        raise MarketDataValidationError(str(exc)) from exc
    if any(bar.symbol != normalized_symbol for bar in checked):
        raise MarketDataValidationError("intraday symbol mismatch")
    if any(bar.end > cutoff for bar in checked):
        raise MarketDataValidationError("future or forming intraday bar")

    expected_last = calendar.latest_completed_bar_end(cutoff)
    if checked[-1].end.astimezone(NEW_YORK) != expected_last:
        raise StaleMarketData("latest completed 15-minute bar is missing")
    try:
        expected_starts = calendar.expected_bar_starts(checked[0].start, expected_last)
    except ValueError as exc:
        raise MarketDataValidationError(str(exc)) from exc
    actual_starts = tuple(bar.start.astimezone(NEW_YORK) for bar in checked)
    if actual_starts != expected_starts:
        raise MarketDataValidationError("intraday history has a missing or extra RTH bar")
    return checked


def validate_quote_snapshot(
    snapshot: QuoteSnapshot,
    *,
    symbol: str,
    as_of: datetime,
    calendar: FrozenExchangeCalendar,
    max_age: timedelta = timedelta(seconds=60),
    future_tolerance: timedelta = timedelta(seconds=2),
) -> QuoteSnapshot:
    """Validate symbol, exchange coverage, future skew, and quote freshness."""

    if type(snapshot) is not QuoteSnapshot:
        raise MarketDataValidationError("snapshot must be an exact QuoteSnapshot")
    if snapshot.symbol != normalize_us_symbol(symbol):
        raise MarketDataValidationError("snapshot symbol mismatch")
    clock = _aware("as_of", as_of)
    if type(max_age) is not timedelta or max_age <= timedelta(0):
        raise MarketDataValidationError("max_age must be a positive timedelta")
    if type(future_tolerance) is not timedelta or future_tolerance < timedelta(0):
        raise MarketDataValidationError("future_tolerance must be nonnegative")
    calendar.assert_covered(clock.astimezone(NEW_YORK).date())
    observed = snapshot.observed_at.astimezone(clock.tzinfo)
    if observed > clock + future_tolerance:
        raise MarketDataValidationError("quote timestamp is in the future")
    if clock - observed > max_age:
        raise StaleMarketData("quote snapshot is stale")
    return snapshot


def build_market_gates(
    snapshot: QuoteSnapshot,
    *,
    symbol: str,
    as_of: datetime,
    calendar: FrozenExchangeCalendar,
    max_spread_bps: float,
    max_age: timedelta = timedelta(seconds=60),
) -> MarketGates:
    """Translate a validated quote into explicit strategy execution gates."""

    checked = validate_quote_snapshot(
        snapshot,
        symbol=symbol,
        as_of=as_of,
        calendar=calendar,
        max_age=max_age,
    )
    threshold = _finite_number("max_spread_bps", max_spread_bps)
    if threshold < 0:
        raise MarketDataValidationError("max_spread_bps must be nonnegative")
    session_open = calendar.active_session(as_of) is not None
    return MarketGates(
        data_ok=True,
        spread_ok=checked.spread_bps <= threshold,
        market_open=(
            session_open
            and checked.market_state is MarketState.OPEN
            and not checked.halted
        ),
    )


def build_candidate_facts(
    *,
    priority: int,
    metadata: InstrumentMetadata,
    raw_daily: Sequence[DailyBar],
    qfq_daily: Sequence[DailyBar],
    completed_15m: IndicatorBarSeries,
    spy_raw_daily: Sequence[DailyBar],
    spy_qfq_daily: Sequence[DailyBar],
    as_of: datetime,
    calendar: FrozenExchangeCalendar,
) -> CandidateInput:
    """Build point-in-time candidate facts without predicting or ranking returns."""

    if isinstance(priority, bool) or not isinstance(priority, int) or priority < 0:
        raise MarketDataValidationError("priority must be a nonnegative exact int")
    symbol = metadata.symbol
    raw = validate_daily_bars(
        raw_daily,
        symbol=symbol,
        scale=PriceScale.RAW,
        as_of=as_of,
        calendar=calendar,
    )
    qfq = validate_daily_bars(
        qfq_daily,
        symbol=symbol,
        scale=PriceScale.QFQ,
        as_of=as_of,
        calendar=calendar,
    )
    pair = validate_scale_alignment(raw, qfq)
    if type(completed_15m) is not IndicatorBarSeries:
        raise MarketDataValidationError(
            "completed_15m must be a QFQ IndicatorBarSeries"
        )
    if completed_15m.adjustment_scale.calendar_sha256 != calendar.sha256:
        raise MarketDataValidationError("indicator adjustment calendar identity mismatch")
    if completed_15m.adjustment_scale.target_session != _aware(
        "as_of", as_of
    ).astimezone(NEW_YORK).date():
        raise MarketDataValidationError("indicator adjustment target session mismatch")
    assert_adjustment_scale_unchanged(completed_15m.adjustment_scale, pair)
    intraday = validate_completed_15m_bars(
        completed_15m.bars,
        symbol=symbol,
        as_of=as_of,
        calendar=calendar,
    )

    spy_raw = validate_daily_bars(
        spy_raw_daily,
        symbol=SPY_SYMBOL,
        scale=PriceScale.RAW,
        as_of=as_of,
        calendar=calendar,
    )
    spy_qfq = validate_daily_bars(
        spy_qfq_daily,
        symbol=SPY_SYMBOL,
        scale=PriceScale.QFQ,
        as_of=as_of,
        calendar=calendar,
    )
    spy_pair = validate_scale_alignment(spy_raw, spy_qfq)

    qfq_closes = tuple(bar.close for bar in pair.qfq)
    sma50_series = simple_moving_average(qfq_closes, 50)
    sma200_series = simple_moving_average(qfq_closes, 200)
    current_factor = pair.current_qfq_to_raw
    sma50 = sma50_series[-1]
    sma50_5d_ago = sma50_series[-6] if len(sma50_series) >= 6 else None
    sma200 = sma200_series[-1]

    spy_qfq_closes = tuple(bar.close for bar in spy_pair.qfq)
    spy_sma200_value = simple_moving_average(spy_qfq_closes, 200)[-1]
    liquid_window = pair.raw[-20:]
    median_dollar_volume = median(
        tuple(bar.close * bar.volume for bar in liquid_window)
    )

    return CandidateInput(
        symbol=symbol,
        priority=priority,
        instrument_kind=metadata.instrument_kind,
        prior_close=pair.raw[-1].close,
        median_dollar_volume_20d=median_dollar_volume,
        daily_bars=len(pair.raw),
        completed_15m_bars=len(intraday),
        is_halted=metadata.is_halted,
        has_delisting_risk=metadata.has_delisting_risk,
        instrument_status_known=metadata.status_known,
        adjustment_ok=True,
        sma200=None if sma200 is None else sma200 * current_factor,
        sma50=None if sma50 is None else sma50 * current_factor,
        sma50_5d_ago=(
            None if sma50_5d_ago is None else sma50_5d_ago * current_factor
        ),
        spy_close=spy_pair.raw[-1].close,
        spy_sma200=(
            None
            if spy_sma200_value is None
            else spy_sma200_value * spy_pair.current_qfq_to_raw
        ),
        metadata={
            "calendar_sha256": calendar.sha256,
            "daily_scale_contract": "RAW_LIQUIDITY_QFQ_TREND_CURRENT_RAW_EQUIVALENT",
        },
    )


def _table_records(value: Any) -> Tuple[Mapping[str, Any], ...]:
    try:
        records = value.to_dict("records")
    except (AttributeError, TypeError, ValueError) as exc:
        raise MarketDataUnavailable("quote response is not tabular") from exc
    if not isinstance(records, list) or not all(isinstance(row, Mapping) for row in records):
        raise MarketDataUnavailable("quote response rows are malformed")
    return tuple(records)


def _parse_provider_time(value: object) -> datetime:
    """Parse OpenD's documented exchange-local time without guessing formats."""

    if type(value) is not str or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value
    ):
        raise MarketDataUnavailable("provider timestamp format is ambiguous")
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=NEW_YORK)
    except ValueError as exc:
        raise MarketDataUnavailable("provider timestamp is invalid") from exc


def _provider_number(value: object, field: str) -> float:
    """Normalize provider scalar types without accepting bool or bad strings."""

    if type(value) is bool or value is None:
        raise MarketDataUnavailable("provider %s is not numeric" % field)
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MarketDataUnavailable("provider %s is not numeric" % field) from exc
    if not math.isfinite(result):
        raise MarketDataUnavailable("provider %s is non-finite" % field)
    return result


def _provider_market_state(value: object) -> MarketState:
    text = str(getattr(value, "value", value)).strip().upper()
    if text in {"MORNING", "AFTERNOON", "TRADING", "OPEN"}:
        return MarketState.OPEN
    if text in {"CLOSED", "REST", "PRE_MARKET_BEGIN", "AFTER_HOURS_BEGIN"}:
        return MarketState.CLOSED
    if text in {"HALTED", "SUSPENDED"}:
        return MarketState.HALTED
    return MarketState.UNKNOWN


class MoomooQuoteAdapter:
    """Narrow, quote-only OpenD adapter with an injected lazy SDK loader.

    The loader is deliberately mandatory.  Repository policy permits the
    optional SDK import only in the broker composition boundary, so runtime
    wiring must pass a reviewed loader rather than making this module another
    implicit import site.
    """

    def __init__(
        self,
        *,
        sdk_loader: Callable[[], Any],
        host: str = OPEND_HOST,
        port: int = OPEND_PORT,
    ) -> None:
        if host != OPEND_HOST or port != OPEND_PORT:
            raise MarketDataValidationError("OpenD endpoint must be 127.0.0.1:11111")
        if not callable(sdk_loader):
            raise MarketDataValidationError("sdk_loader must be callable")
        self._sdk_loader = sdk_loader
        self._host = host
        self._port = port

    def _open(self) -> Tuple[Any, Any]:
        try:
            sdk = self._sdk_loader()
            context = sdk.OpenQuoteContext(host=self._host, port=self._port)
        except Exception as exc:
            raise MarketDataUnavailable("quote-only OpenD context unavailable") from exc
        return sdk, context

    @staticmethod
    def _history_result(
        sdk: Any, result: Any, *, expected_code: str
    ) -> Tuple[Mapping[str, Any], ...]:
        if not isinstance(result, tuple) or len(result) != 3:
            raise MarketDataUnavailable("history response shape is ambiguous")
        ret, table, page_key = result
        if ret != getattr(sdk, "RET_OK", 0):
            raise MarketDataUnavailable("history request failed")
        if page_key not in (None, ""):
            raise MarketDataUnavailable("history response is truncated; implicit backfill forbidden")
        records = _table_records(table)
        if any(type(row.get("code")) is not str or row.get("code") != expected_code for row in records):
            raise MarketDataUnavailable("history row identity does not exactly match request")
        return records

    @staticmethod
    def _simple_result(sdk: Any, result: Any) -> Any:
        if not isinstance(result, tuple) or len(result) != 2:
            raise MarketDataUnavailable("quote response shape is ambiguous")
        ret, payload = result
        if ret != getattr(sdk, "RET_OK", 0):
            raise MarketDataUnavailable("quote request failed")
        return payload

    @staticmethod
    def _close(context: Any) -> None:
        try:
            context.close()
        except Exception as exc:
            raise MarketDataUnavailable("quote context did not close cleanly") from exc

    def _request_history(
        self,
        *,
        symbol: str,
        start: date,
        end: date,
        scale: PriceScale,
        intraday: bool,
    ) -> Tuple[Mapping[str, Any], ...]:
        code = normalize_us_symbol(symbol)
        if type(start) is not date or type(end) is not date or end < start:
            raise MarketDataValidationError("invalid history date range")
        if type(scale) is not PriceScale:
            raise MarketDataValidationError("scale must be an exact PriceScale")
        sdk, context = self._open()
        try:
            ktype = sdk.KLType.K_15M if intraday else sdk.KLType.K_DAY
            autype = sdk.AuType.NONE if scale is PriceScale.RAW else sdk.AuType.QFQ
            session = sdk.Session.RTH
            session_text = str(getattr(session, "value", session)).strip().upper()
            if session_text != "RTH":
                raise MarketDataUnavailable("SDK RTH session constant is ambiguous")
            expected_ktype = "K_15M" if intraday else "K_DAY"
            ktype_text = str(getattr(ktype, "value", ktype)).strip().upper()
            if ktype_text != expected_ktype:
                raise MarketDataUnavailable("SDK K-line type constant is ambiguous")
            expected_autype = "NONE" if scale is PriceScale.RAW else "QFQ"
            autype_text = str(getattr(autype, "value", autype)).strip().upper()
            if autype_text not in {expected_autype, "N/A" if scale is PriceScale.RAW else "QFQ"}:
                raise MarketDataUnavailable("SDK adjustment constant is ambiguous")
            result = context.request_history_kline(
                code,
                start=start.isoformat(),
                end=end.isoformat(),
                ktype=ktype,
                autype=autype,
                fields=[
                    sdk.KL_FIELD.DATE_TIME,
                    sdk.KL_FIELD.OPEN,
                    sdk.KL_FIELD.HIGH,
                    sdk.KL_FIELD.LOW,
                    sdk.KL_FIELD.CLOSE,
                    sdk.KL_FIELD.TRADE_VOL,
                ],
                max_count=1000,
                session=session,
                extended_time=False,
            )
            return self._history_result(sdk, result, expected_code=code)
        except MarketDataError:
            raise
        except Exception as exc:
            raise MarketDataUnavailable("history quote API failed") from exc
        finally:
            self._close(context)

    def completed_15m_bars(
        self,
        *,
        symbol: str,
        start: date,
        end: date,
        as_of: datetime,
        calendar: FrozenExchangeCalendar,
        adjustment_scale: FrozenAdjustmentScale,
        completion_lag: timedelta = timedelta(seconds=30),
    ) -> IndicatorBarSeries:
        code = normalize_us_symbol(symbol)
        if type(adjustment_scale) is not FrozenAdjustmentScale:
            raise MarketDataValidationError(
                "QFQ intraday history requires a FrozenAdjustmentScale"
            )
        if adjustment_scale.symbol != code:
            raise MarketDataValidationError("adjustment scale symbol mismatch")
        if adjustment_scale.calendar_sha256 != calendar.sha256:
            raise MarketDataValidationError("adjustment scale calendar identity mismatch")
        if adjustment_scale.target_session != _aware("as_of", as_of).astimezone(NEW_YORK).date():
            raise MarketDataValidationError("adjustment scale target session mismatch")
        records = self._request_history(
            symbol=code,
            start=start,
            end=end,
            scale=PriceScale.QFQ,
            intraday=True,
        )
        bars = []
        for row in records:
            # OpenD labels US 15-minute history by the bar's ending boundary:
            # 09:45 is the completed 09:30--09:45 RTH interval.
            bar_end = _parse_provider_time(row.get("time_key"))
            bar_start = bar_end - timedelta(minutes=15)
            try:
                session = calendar.session_on(bar_start.date())
            except ValueError as exc:
                raise MarketDataValidationError(str(exc)) from exc
            if (
                session is None
                or bar_start < session.open_at
                or bar_end > session.close_at
            ):
                raise MarketDataValidationError(
                    "provider intraday timestamp is outside frozen RTH session"
                )
            bars.append(
                CompletedBar15m(
                    symbol=code,
                    start=bar_start,
                    end=bar_end,
                    open=_provider_number(row.get("open"), "open"),
                    high=_provider_number(row.get("high"), "high"),
                    low=_provider_number(row.get("low"), "low"),
                    close=_provider_number(row.get("close"), "close"),
                    volume=_provider_number(row.get("volume"), "volume"),
                    complete=True,
                )
            )
        checked = validate_completed_15m_bars(
            tuple(bars),
            symbol=code,
            as_of=as_of,
            calendar=calendar,
            completion_lag=completion_lag,
        )
        return IndicatorBarSeries(
            bars=checked,
            scale=PriceScale.QFQ,
            adjustment_scale=adjustment_scale,
        )

    def daily_bars(
        self,
        *,
        symbol: str,
        start: date,
        end: date,
        scale: PriceScale,
        as_of: datetime,
        calendar: FrozenExchangeCalendar,
    ) -> Tuple[DailyBar, ...]:
        if type(scale) is not PriceScale:
            raise MarketDataValidationError("scale must be an exact PriceScale")
        records = self._request_history(
            symbol=symbol,
            start=start,
            end=end,
            scale=scale,
            intraday=False,
        )
        code = normalize_us_symbol(symbol)
        bars = tuple(
            DailyBar(
                symbol=code,
                session_date=_parse_provider_time(row.get("time_key")).date(),
                open=_provider_number(row.get("open"), "open"),
                high=_provider_number(row.get("high"), "high"),
                low=_provider_number(row.get("low"), "low"),
                close=_provider_number(row.get("close"), "close"),
                volume=_provider_number(row.get("volume"), "volume"),
                scale=scale,
            )
            for row in records
        )
        return validate_daily_bars(
            bars,
            symbol=code,
            scale=scale,
            as_of=as_of,
            calendar=calendar,
        )

    def quote_snapshot(
        self,
        *,
        symbol: str,
        as_of: datetime,
        calendar: FrozenExchangeCalendar,
        max_age: timedelta = timedelta(seconds=60),
    ) -> QuoteSnapshot:
        code = normalize_us_symbol(symbol)
        sdk, context = self._open()
        try:
            global_state = self._simple_result(sdk, context.get_global_state())
            if not isinstance(global_state, Mapping) or "market_us" not in global_state:
                raise MarketDataUnavailable("US market state is missing")
            state = _provider_market_state(global_state["market_us"])

            snapshot_rows = _table_records(
                self._simple_result(sdk, context.get_market_snapshot([code]))
            )
            matching = tuple(row for row in snapshot_rows if row.get("code") == code)
            if len(matching) != 1:
                raise MarketDataUnavailable("snapshot identity is missing or ambiguous")
            row = matching[0]

            book = self._simple_result(sdk, context.get_order_book(code, num=1))
            if not isinstance(book, Mapping):
                raise MarketDataUnavailable("top-of-book response is malformed")
            bid = self._book_price(book.get("Bid"), "Bid")
            ask = self._book_price(book.get("Ask"), "Ask")
            status_text = str(
                getattr(row.get("sec_status", ""), "value", row.get("sec_status", ""))
            ).upper()
            halted = state is MarketState.HALTED or status_text in {"SUSPENDED", "HALTED"}
            snapshot = QuoteSnapshot(
                symbol=code,
                observed_at=_parse_provider_time(row.get("update_time")),
                bid=_provider_number(bid, "bid"),
                ask=_provider_number(ask, "ask"),
                last=_provider_number(row.get("last_price"), "last_price"),
                market_state=state,
                halted=halted,
            )
            return validate_quote_snapshot(
                snapshot,
                symbol=code,
                as_of=as_of,
                calendar=calendar,
                max_age=max_age,
            )
        except MarketDataError:
            raise
        except Exception as exc:
            raise MarketDataUnavailable("snapshot quote APIs failed") from exc
        finally:
            self._close(context)

    @staticmethod
    def _book_price(levels: Any, side: str) -> Any:
        if not isinstance(levels, Sequence) or isinstance(levels, (str, bytes)):
            raise MarketDataUnavailable("%s top-of-book is missing" % side)
        if len(levels) != 1:
            raise MarketDataUnavailable("%s top-of-book is ambiguous" % side)
        level = levels[0]
        if isinstance(level, Mapping):
            if "price" not in level:
                raise MarketDataUnavailable("%s price is missing" % side)
            return level["price"]
        if isinstance(level, Sequence) and not isinstance(level, (str, bytes)) and level:
            return level[0]
        raise MarketDataUnavailable("%s top-of-book level is malformed" % side)


__all__ = [
    "DailyBar",
    "FrozenAdjustmentScale",
    "FrozenInstrumentClassifier",
    "IndicatorBarSeries",
    "InstrumentClassifier",
    "InstrumentMetadata",
    "MarketDataError",
    "MarketDataUnavailable",
    "MarketDataValidationError",
    "MarketState",
    "MoomooQuoteAdapter",
    "OPEND_HOST",
    "OPEND_PORT",
    "PriceScale",
    "QuoteSnapshot",
    "SPY_SYMBOL",
    "ScaleAlignedDailyHistory",
    "StaleMarketData",
    "build_candidate_facts",
    "build_market_gates",
    "assert_adjustment_scale_unchanged",
    "freeze_adjustment_scale",
    "instrument_metadata_payload_sha256",
    "validate_completed_15m_bars",
    "validate_daily_bars",
    "validate_quote_snapshot",
    "validate_scale_alignment",
]
