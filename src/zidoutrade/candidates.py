"""Deterministic candidate eligibility for the RSI autopilot.

This module deliberately answers only *whether* an instrument is eligible.  It
does not calculate an expected return, score candidates, or rank one candidate
above another.  The user supplies an explicit display priority; ticker is the
stable tie-breaker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
import re
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple


MAX_WATCHLIST_SIZE = 20
MIN_PRIOR_CLOSE = 5.0
MIN_MEDIAN_DOLLAR_VOLUME_20D = 50_000_000.0
MIN_DAILY_BARS = 252
MIN_COMPLETED_15M_BARS = 100

_US_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")


class InstrumentKind(str, Enum):
    """Instrument classification supplied by a trusted metadata source."""

    ORDINARY_EQUITY = "ORDINARY_EQUITY"
    NONLEVERAGED_ETF = "NONLEVERAGED_ETF"
    LEVERAGED_ETF = "LEVERAGED_ETF"
    INVERSE_ETF = "INVERSE_ETF"
    OTC_EQUITY = "OTC_EQUITY"
    OPTION = "OPTION"
    UNKNOWN = "UNKNOWN"


class EligibilityStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


class ReasonCode(str, Enum):
    """Stable, machine-readable reasons for an eligibility failure."""

    INVALID_SYMBOL = "INVALID_SYMBOL"
    LEVERAGED_ETF_BLOCKED = "LEVERAGED_ETF_BLOCKED"
    INVERSE_ETF_BLOCKED = "INVERSE_ETF_BLOCKED"
    OTC_BLOCKED = "OTC_BLOCKED"
    OPTION_BLOCKED = "OPTION_BLOCKED"
    UNKNOWN_INSTRUMENT_BLOCKED = "UNKNOWN_INSTRUMENT_BLOCKED"
    UNSUPPORTED_INSTRUMENT = "UNSUPPORTED_INSTRUMENT"
    MISSING_PRIOR_CLOSE = "MISSING_PRIOR_CLOSE"
    PRIOR_CLOSE_BELOW_MINIMUM = "PRIOR_CLOSE_BELOW_MINIMUM"
    MISSING_MEDIAN_DOLLAR_VOLUME_20D = "MISSING_MEDIAN_DOLLAR_VOLUME_20D"
    MEDIAN_DOLLAR_VOLUME_BELOW_MINIMUM = "MEDIAN_DOLLAR_VOLUME_BELOW_MINIMUM"
    INSUFFICIENT_DAILY_BARS = "INSUFFICIENT_DAILY_BARS"
    INSUFFICIENT_COMPLETED_15M_BARS = "INSUFFICIENT_COMPLETED_15M_BARS"
    TRADING_HALTED = "TRADING_HALTED"
    DELISTING_RISK = "DELISTING_RISK"
    INSTRUMENT_STATUS_UNKNOWN = "INSTRUMENT_STATUS_UNKNOWN"
    ADJUSTMENT_ISSUE = "ADJUSTMENT_ISSUE"
    MISSING_SMA200 = "MISSING_SMA200"
    PRICE_NOT_ABOVE_SMA200 = "PRICE_NOT_ABOVE_SMA200"
    MISSING_SMA50 = "MISSING_SMA50"
    MISSING_SMA50_5D_AGO = "MISSING_SMA50_5D_AGO"
    SMA50_NOT_RISING = "SMA50_NOT_RISING"
    MISSING_SPY_CLOSE = "MISSING_SPY_CLOSE"
    MISSING_SPY_SMA200 = "MISSING_SPY_SMA200"
    SPY_NOT_ABOVE_SMA200 = "SPY_NOT_ABOVE_SMA200"


@dataclass(frozen=True)
class CandidatePolicy:
    """Eligibility policy.

    The four expansion flags default to ``False``.  Production version 1 is
    expected to leave them false, making ordinary US equities and
    non-leveraged ETFs the only supported instruments.
    """

    allow_leveraged_etf: bool = False
    allow_inverse_etf: bool = False
    allow_otc: bool = False
    allow_options: bool = False
    min_prior_close: float = MIN_PRIOR_CLOSE
    min_median_dollar_volume_20d: float = MIN_MEDIAN_DOLLAR_VOLUME_20D
    min_daily_bars: int = MIN_DAILY_BARS
    min_completed_15m_bars: int = MIN_COMPLETED_15M_BARS

    def __post_init__(self) -> None:
        # V1 has no runtime switch that may widen the instrument universe.
        # Any such product change requires a new strategy/version and review.
        for name in (
            "allow_leveraged_etf",
            "allow_inverse_etf",
            "allow_otc",
            "allow_options",
        ):
            value = getattr(self, name)
            if type(value) is not bool or value is not False:
                raise ValueError("%s_IS_FROZEN_FALSE" % name.upper())
        frozen = {
            "min_prior_close": MIN_PRIOR_CLOSE,
            "min_median_dollar_volume_20d": MIN_MEDIAN_DOLLAR_VOLUME_20D,
            "min_daily_bars": MIN_DAILY_BARS,
            "min_completed_15m_bars": MIN_COMPLETED_15M_BARS,
        }
        for name, expected in frozen.items():
            value = getattr(self, name)
            if type(value) is not type(expected) or value != expected:
                raise ValueError("%s_IS_FROZEN" % name.upper())


@dataclass(frozen=True)
class CandidateInput:
    """Point-in-time facts used for candidate eligibility.

    Prices and moving averages must be based only on completed bars available
    at the decision cutoff.  ``priority`` is a user-defined presentation order,
    not a model score.
    """

    symbol: str
    priority: int
    instrument_kind: InstrumentKind
    prior_close: Optional[float]
    median_dollar_volume_20d: Optional[float]
    daily_bars: int
    completed_15m_bars: int
    is_halted: bool = False
    has_delisting_risk: bool = False
    instrument_status_known: bool = True
    adjustment_ok: bool = True
    sma200: Optional[float] = None
    sma50: Optional[float] = None
    sma50_5d_ago: Optional[float] = None
    spy_close: Optional[float] = None
    spy_sma200: Optional[float] = None
    metadata: Mapping[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        kind = self.instrument_kind
        if not isinstance(kind, InstrumentKind):
            try:
                kind = InstrumentKind(kind)
            except (TypeError, ValueError):
                kind = InstrumentKind.UNKNOWN
            object.__setattr__(self, "instrument_kind", kind)
        if isinstance(self.priority, bool) or not isinstance(self.priority, int) or self.priority < 0:
            raise ValueError("INVALID_USER_PRIORITY")
        for name in ("daily_bars", "completed_15m_bars"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("INVALID_%s" % name.upper())
        for name in (
            "is_halted",
            "has_delisting_risk",
            "instrument_status_known",
            "adjustment_ok",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError("INVALID_%s" % name.upper())
        if not isinstance(self.metadata, Mapping):
            raise ValueError("INVALID_METADATA")


@dataclass(frozen=True)
class CandidateEvaluation:
    symbol: str
    priority: int
    instrument_kind: InstrumentKind
    status: EligibilityStatus
    reason_codes: Tuple[ReasonCode, ...]

    @property
    def eligible(self) -> bool:
        return self.status is EligibilityStatus.PASS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "priority": self.priority,
            "instrument_kind": self.instrument_kind.value,
            "status": self.status.value,
            "eligible": self.eligible,
            "reason_codes": [reason.value for reason in self.reason_codes],
        }


@dataclass(frozen=True)
class CandidateBatch:
    """A complete, deterministic snapshot including passes and failures."""

    evaluations: Tuple[CandidateEvaluation, ...]

    @property
    def eligible(self) -> Tuple[CandidateEvaluation, ...]:
        return tuple(item for item in self.evaluations if item.eligible)

    @property
    def ineligible(self) -> Tuple[CandidateEvaluation, ...]:
        return tuple(item for item in self.evaluations if not item.eligible)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ordering": "USER_PRIORITY_THEN_TICKER",
            "candidates": [item.to_dict() for item in self.evaluations],
        }


def normalize_us_symbol(raw_symbol: str) -> str:
    """Return a canonical ``US.TICKER`` or raise ``ValueError``.

    Symbols from other markets are rejected rather than silently rewritten.
    """

    if not isinstance(raw_symbol, str):
        raise ValueError(ReasonCode.INVALID_SYMBOL.value)
    value = raw_symbol.strip().upper()
    if value.startswith("US."):
        ticker = value[3:]
    elif "." in value and value.split(".", 1)[0] in {
        "HK",
        "SH",
        "SZ",
        "JP",
        "SG",
        "AU",
        "CA",
    }:
        raise ValueError(ReasonCode.INVALID_SYMBOL.value)
    else:
        ticker = value
    if not _US_SYMBOL.fullmatch(ticker):
        raise ValueError(ReasonCode.INVALID_SYMBOL.value)
    return "US." + ticker


def normalize_master_watchlist(symbols: Sequence[str]) -> Tuple[str, ...]:
    """Validate and normalize a user master watchlist (maximum 20 symbols)."""

    if isinstance(symbols, (str, bytes)):
        raise TypeError("watchlist must be a sequence of symbols")
    if len(symbols) > MAX_WATCHLIST_SIZE:
        raise ValueError("WATCHLIST_LIMIT_EXCEEDED")
    normalized = tuple(normalize_us_symbol(symbol) for symbol in symbols)
    if len(set(normalized)) != len(normalized):
        raise ValueError("DUPLICATE_SYMBOL")
    return normalized


def _finite(value: Optional[float]) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _instrument_reasons(kind: InstrumentKind, policy: CandidatePolicy) -> Iterable[ReasonCode]:
    if kind in (InstrumentKind.ORDINARY_EQUITY, InstrumentKind.NONLEVERAGED_ETF):
        return ()
    if kind is InstrumentKind.LEVERAGED_ETF:
        return (ReasonCode.LEVERAGED_ETF_BLOCKED,)
    if kind is InstrumentKind.INVERSE_ETF:
        return (ReasonCode.INVERSE_ETF_BLOCKED,)
    if kind is InstrumentKind.OTC_EQUITY:
        return (ReasonCode.OTC_BLOCKED,)
    if kind is InstrumentKind.OPTION:
        return (ReasonCode.OPTION_BLOCKED,)
    if kind is InstrumentKind.UNKNOWN:
        return (ReasonCode.UNKNOWN_INSTRUMENT_BLOCKED,)
    return (ReasonCode.UNSUPPORTED_INSTRUMENT,)


def evaluate_candidate(
    candidate: CandidateInput, policy: CandidatePolicy = CandidatePolicy()
) -> CandidateEvaluation:
    """Evaluate every rule and return all applicable reason codes."""

    reasons = list(_instrument_reasons(candidate.instrument_kind, policy))
    try:
        symbol = normalize_us_symbol(candidate.symbol)
    except ValueError:
        symbol = str(candidate.symbol).strip().upper()
        reasons.append(ReasonCode.INVALID_SYMBOL)

    if not _finite(candidate.prior_close):
        reasons.append(ReasonCode.MISSING_PRIOR_CLOSE)
    elif float(candidate.prior_close) < policy.min_prior_close:
        reasons.append(ReasonCode.PRIOR_CLOSE_BELOW_MINIMUM)

    if not _finite(candidate.median_dollar_volume_20d):
        reasons.append(ReasonCode.MISSING_MEDIAN_DOLLAR_VOLUME_20D)
    elif float(candidate.median_dollar_volume_20d) < policy.min_median_dollar_volume_20d:
        reasons.append(ReasonCode.MEDIAN_DOLLAR_VOLUME_BELOW_MINIMUM)

    if candidate.daily_bars < policy.min_daily_bars:
        reasons.append(ReasonCode.INSUFFICIENT_DAILY_BARS)
    if candidate.completed_15m_bars < policy.min_completed_15m_bars:
        reasons.append(ReasonCode.INSUFFICIENT_COMPLETED_15M_BARS)
    if candidate.is_halted:
        reasons.append(ReasonCode.TRADING_HALTED)
    if candidate.has_delisting_risk:
        reasons.append(ReasonCode.DELISTING_RISK)
    if not candidate.instrument_status_known:
        reasons.append(ReasonCode.INSTRUMENT_STATUS_UNKNOWN)
    if not candidate.adjustment_ok:
        reasons.append(ReasonCode.ADJUSTMENT_ISSUE)

    if not _finite(candidate.sma200):
        reasons.append(ReasonCode.MISSING_SMA200)
    elif _finite(candidate.prior_close) and float(candidate.prior_close) <= float(candidate.sma200):
        reasons.append(ReasonCode.PRICE_NOT_ABOVE_SMA200)

    if not _finite(candidate.sma50):
        reasons.append(ReasonCode.MISSING_SMA50)
    if not _finite(candidate.sma50_5d_ago):
        reasons.append(ReasonCode.MISSING_SMA50_5D_AGO)
    if _finite(candidate.sma50) and _finite(candidate.sma50_5d_ago):
        if float(candidate.sma50) <= float(candidate.sma50_5d_ago):
            reasons.append(ReasonCode.SMA50_NOT_RISING)

    if not _finite(candidate.spy_close):
        reasons.append(ReasonCode.MISSING_SPY_CLOSE)
    if not _finite(candidate.spy_sma200):
        reasons.append(ReasonCode.MISSING_SPY_SMA200)
    if _finite(candidate.spy_close) and _finite(candidate.spy_sma200):
        if float(candidate.spy_close) <= float(candidate.spy_sma200):
            reasons.append(ReasonCode.SPY_NOT_ABOVE_SMA200)

    # De-duplicate without losing stable evaluation order.
    reason_tuple = tuple(dict.fromkeys(reasons))
    return CandidateEvaluation(
        symbol=symbol,
        priority=candidate.priority,
        instrument_kind=candidate.instrument_kind,
        status=EligibilityStatus.FAIL if reason_tuple else EligibilityStatus.PASS,
        reason_codes=reason_tuple,
    )


def evaluate_candidates(
    candidates: Sequence[CandidateInput], policy: CandidatePolicy = CandidatePolicy()
) -> CandidateBatch:
    """Evaluate a complete watchlist and order it without predictive ranking."""

    if len(candidates) > MAX_WATCHLIST_SIZE:
        raise ValueError("WATCHLIST_LIMIT_EXCEEDED")
    normalized_seen = set()
    for candidate in candidates:
        try:
            identity = normalize_us_symbol(candidate.symbol)
        except ValueError:
            # Invalid instruments remain visible as FAIL records instead of
            # disappearing from the complete candidate snapshot.
            identity = "INVALID:" + repr(candidate.symbol)
        if identity in normalized_seen:
            raise ValueError("DUPLICATE_SYMBOL")
        normalized_seen.add(identity)
        if isinstance(candidate.priority, bool) or not isinstance(candidate.priority, int):
            raise ValueError("INVALID_USER_PRIORITY")
        if candidate.priority < 0:
            raise ValueError("INVALID_USER_PRIORITY")
    evaluated = tuple(evaluate_candidate(candidate, policy) for candidate in candidates)
    ordered = tuple(sorted(evaluated, key=lambda item: (item.priority, item.symbol)))
    return CandidateBatch(ordered)


__all__ = [
    "CandidateBatch",
    "CandidateEvaluation",
    "CandidateInput",
    "CandidatePolicy",
    "EligibilityStatus",
    "InstrumentKind",
    "MAX_WATCHLIST_SIZE",
    "ReasonCode",
    "evaluate_candidate",
    "evaluate_candidates",
    "normalize_master_watchlist",
    "normalize_us_symbol",
]
