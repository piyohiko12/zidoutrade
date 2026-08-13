"""Pure composition from attested moomoo history to the candle proxy engine.

This module never imports the moomoo SDK.  It converts already-pinned quote
history into the exact daily trend inputs and 15-minute bars consumed by the
headless exploratory backtest.
"""

from __future__ import annotations

from datetime import date
import math
from statistics import median
from typing import Dict, Tuple

from .backtest import (
    BacktestConfig,
    BacktestReport,
    DatedTrend,
    SessionBoundary,
    run_candle_backtest,
)
from .backtest_io import BacktestInputBundle, HistoricalBar
from .candidates import MIN_COMPLETED_15M_BARS
from .models import CompletedBar15m, TrendEligibility
from .risk import RiskPolicy


MINIMUM_RAW_CLOSE = 5.0
MINIMUM_MEDIAN_DOLLAR_TURNOVER = 50_000_000.0
DAILY_HISTORY_MINIMUM = 252
DAILY_TURNOVER_LOOKBACK = 20
SMA_LONG_PERIOD = 200
SMA_TREND_PERIOD = 50
SMA_TREND_LAG = 5


def _mean(values: Tuple[float, ...]) -> float:
    if not values:
        raise ValueError("cannot average an empty sequence")
    return math.fsum(values) / len(values)


def _daily_by_date(
    bars: Tuple[HistoricalBar, ...], *, before: date
) -> Dict[date, HistoricalBar]:
    return {bar.session_date: bar for bar in bars if bar.session_date < before}


def _trend_for_session(
    bundle: BacktestInputBundle, session_date: date
) -> TrendEligibility:
    symbol_qfq = _daily_by_date(bundle.symbol_daily_qfq, before=session_date)
    symbol_raw = _daily_by_date(bundle.symbol_daily_raw, before=session_date)
    benchmark = _daily_by_date(bundle.benchmark_daily_qfq, before=session_date)
    dates = tuple(sorted(symbol_qfq))
    if (
        len(dates) < DAILY_HISTORY_MINIMUM
        or tuple(sorted(symbol_raw)) != dates
        or tuple(sorted(benchmark)) != dates
    ):
        return TrendEligibility(False, False, False)

    qfq_close = tuple(symbol_qfq[item].close for item in dates)
    raw_close = tuple(symbol_raw[item].close for item in dates)
    # Match the production candidate gate exactly: the reviewed liquidity
    # measure is RAW close * volume, not the provider's separate turnover
    # field (whose historical definition is not part of this input contract).
    dollar_turnover = tuple(
        symbol_raw[item].close * symbol_raw[item].volume for item in dates
    )
    benchmark_close = tuple(benchmark[item].close for item in dates)
    if len(qfq_close) < SMA_TREND_PERIOD + SMA_TREND_LAG:
        return TrendEligibility(False, False, False)

    latest_sma50 = _mean(qfq_close[-SMA_TREND_PERIOD:])
    lagged_sma50 = _mean(
        qfq_close[-(SMA_TREND_PERIOD + SMA_TREND_LAG) : -SMA_TREND_LAG]
    )
    symbol_ok = (
        raw_close[-1] >= MINIMUM_RAW_CLOSE
        and median(dollar_turnover[-DAILY_TURNOVER_LOOKBACK:])
        >= MINIMUM_MEDIAN_DOLLAR_TURNOVER
        and qfq_close[-1] > _mean(qfq_close[-SMA_LONG_PERIOD:])
        and latest_sma50 > lagged_sma50
    )
    benchmark_ok = benchmark_close[-1] > _mean(
        benchmark_close[-SMA_LONG_PERIOD:]
    )
    completed_intraday_before_session = sum(
        1 for bar in bundle.intraday_qfq if bar.session_date < session_date
    )
    intraday_history_ok = (
        completed_intraday_before_session >= MIN_COMPLETED_15M_BARS
    )
    return TrendEligibility(symbol_ok, benchmark_ok, intraday_history_ok)


def prepare_daily_trends(bundle: BacktestInputBundle) -> Tuple[DatedTrend, ...]:
    """Compute every daily gate using data strictly before that session."""

    if type(bundle) is not BacktestInputBundle:
        raise TypeError("bundle must be an exact BacktestInputBundle")
    return tuple(
        DatedTrend(
            session_date=session.session_date,
            eligibility=_trend_for_session(bundle, session.session_date),
        )
        for session in bundle.sessions
        if bundle.input_start <= session.session_date <= bundle.input_end
    )


def _completed_bars(
    bundle: BacktestInputBundle, source: Tuple[HistoricalBar, ...]
) -> Tuple[CompletedBar15m, ...]:
    return tuple(
        CompletedBar15m(
            symbol=bundle.symbol,
            start=bar.start,
            end=bar.time,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            complete=True,
        )
        for bar in source
    )


def run_attested_backtest(
    bundle: BacktestInputBundle,
    *,
    initial_equity: float,
    risk_policy: RiskPolicy,
) -> BacktestReport:
    """Run the pure candle proxy over one fully-attested input bundle."""

    if type(bundle) is not BacktestInputBundle:
        raise TypeError("bundle must be an exact BacktestInputBundle")
    if type(risk_policy) is not RiskPolicy:
        raise TypeError("risk_policy must be an exact RiskPolicy")
    sessions = tuple(
        SessionBoundary(item.session_date, item.close_at)
        for item in bundle.sessions
        if bundle.input_start <= item.session_date <= bundle.input_end
    )
    config = BacktestConfig(
        symbol=bundle.symbol,
        initial_equity=initial_equity,
        risk_policy=risk_policy,
    )
    return run_candle_backtest(
        qfq_bars=_completed_bars(bundle, bundle.intraday_qfq),
        raw_bars=_completed_bars(bundle, bundle.intraday_raw),
        daily_trends=prepare_daily_trends(bundle),
        sessions=sessions,
        config=config,
    )


__all__ = [
    "DAILY_HISTORY_MINIMUM",
    "DAILY_TURNOVER_LOOKBACK",
    "MINIMUM_MEDIAN_DOLLAR_TURNOVER",
    "MINIMUM_RAW_CLOSE",
    "SMA_LONG_PERIOD",
    "SMA_TREND_LAG",
    "SMA_TREND_PERIOD",
    "prepare_daily_trends",
    "run_attested_backtest",
]
