from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import patch
import unittest
from zoneinfo import ZoneInfo

from zidoutrade.backtest import BacktestVariant
from zidoutrade.backtest_io import BacktestInputBundle, HistoricalBar, HistoricalSession
from zidoutrade.backtest_runner import prepare_daily_trends, run_attested_backtest
from zidoutrade.risk import RiskPolicy


NY = ZoneInfo("America/New_York")
TARGET = date(2025, 10, 1)


def _daily(day: date, close: float, *, volume: int = 250_000, turnover: float = 0.0):
    return HistoricalBar(
        datetime.combine(day, time(0, 0), tzinfo=NY),
        close - 0.5,
        close + 1.0,
        close - 1.0,
        close,
        volume,
        turnover,
    )


def _intraday(day: date, index: int):
    end = datetime.combine(day, time(9, 45), tzinfo=NY) + timedelta(
        minutes=15 * index
    )
    return HistoricalBar(end, 100.0, 101.0, 99.0, 100.5, 1_000_000, 100_000_000.0)


def _bundle(*, intraday_count: int = 100, include_unknown_future: bool = False):
    dates = tuple(TARGET - timedelta(days=offset) for offset in range(260, 0, -1))
    symbol_qfq = tuple(_daily(day, 100.0 + index) for index, day in enumerate(dates))
    symbol_raw = tuple(_daily(day, 100.0 + index, turnover=0.0) for index, day in enumerate(dates))
    spy_qfq = tuple(_daily(day, 300.0 + index) for index, day in enumerate(dates))
    if include_unknown_future:
        # A same-session daily close is not knowable before that session and
        # must not influence its candidate/trend eligibility.
        symbol_qfq += (_daily(TARGET, 1.0),)
        symbol_raw += (_daily(TARGET, 1.0, volume=1),)
        spy_qfq += (_daily(TARGET, 1.0),)
    intraday = tuple(
        _intraday(
            TARGET - timedelta(days=4 - index // 26),
            index % 26,
        )
        for index in range(intraday_count)
    )
    session = HistoricalSession(
        TARGET,
        datetime.combine(TARGET, time(9, 30), tzinfo=NY),
        datetime.combine(TARGET, time(16, 0), tzinfo=NY),
        "WHOLE",
    )
    return BacktestInputBundle(
        strategy_version="RSI_AUTOPILOT_V1",
        classification="EXPLORATORY_ONLY",
        provider="MOOMOO_OPEND",
        moomoo_sdk="10.9.6908",
        opend_gui="10.9.6918",
        symbol="US.AAPL",
        benchmark_symbol="US.SPY",
        acquired_at_utc=datetime(2026, 8, 13, tzinfo=timezone.utc),
        input_start=TARGET,
        input_end=TARGET,
        daily_start=dates[0],
        daily_end=TARGET,
        intraday_qfq=intraday,
        intraday_raw=intraday,
        symbol_daily_qfq=symbol_qfq,
        symbol_daily_raw=symbol_raw,
        benchmark_daily_qfq=spy_qfq,
        sessions=(session,),
        attestations=(),
        manifest_sha256="a" * 64,
        manifest_byte_length=1,
    )


class BacktestRunnerTests(unittest.TestCase):
    def test_daily_gate_uses_only_prior_data_and_close_times_volume(self):
        trend = prepare_daily_trends(_bundle(include_unknown_future=True))[0].eligibility
        self.assertTrue(trend.daily_data_ok)
        self.assertTrue(trend.symbol_daily_ok)
        self.assertTrue(trend.spy_daily_ok)

    def test_candidate_intraday_warmup_is_not_silently_bypassed(self):
        trend = prepare_daily_trends(_bundle(intraday_count=99))[0].eligibility
        self.assertFalse(trend.daily_data_ok)
        self.assertTrue(trend.symbol_daily_ok)
        self.assertTrue(trend.spy_daily_ok)

    def test_adapter_passes_typed_bars_sessions_trends_and_policy(self):
        bundle = _bundle()
        sentinel = object()
        policy = RiskPolicy(maximum_investment_cents=1_000_000)
        with patch(
            "zidoutrade.backtest_runner.run_candle_backtest", return_value=sentinel
        ) as engine:
            result = run_attested_backtest(
                bundle, initial_equity=100_000.0, risk_policy=policy
            )
        self.assertIs(result, sentinel)
        call = engine.call_args
        self.assertEqual(len(call.kwargs["qfq_bars"]), 100)
        self.assertEqual(call.kwargs["qfq_bars"][0].symbol, "US.AAPL")
        self.assertEqual(call.kwargs["sessions"][0].session_date, TARGET)
        self.assertIs(call.kwargs["config"].risk_policy, policy)
        self.assertIs(
            call.kwargs["config"].strategy_variant,
            BacktestVariant.BASELINE,
        )

    def test_adapter_propagates_fixed_variant_by_keyword_only(self):
        bundle = _bundle()
        sentinel = object()
        policy = RiskPolicy(maximum_investment_cents=1_000_000)
        with patch(
            "zidoutrade.backtest_runner.run_candle_backtest", return_value=sentinel
        ) as engine:
            result = run_attested_backtest(
                bundle,
                initial_equity=100_000.0,
                risk_policy=policy,
                strategy_variant=BacktestVariant.Q013_ATR_CAP_0050_SHADOW,
            )

        self.assertIs(result, sentinel)
        self.assertIs(
            engine.call_args.kwargs["config"].strategy_variant,
            BacktestVariant.Q013_ATR_CAP_0050_SHADOW,
        )

    def test_adapter_propagates_q015_as_a_typed_research_variant(self):
        bundle = _bundle()
        sentinel = object()
        policy = RiskPolicy(maximum_investment_cents=1_000_000)
        with patch(
            "zidoutrade.backtest_runner.run_candle_backtest", return_value=sentinel
        ) as engine:
            result = run_attested_backtest(
                bundle,
                initial_equity=100_000.0,
                risk_policy=policy,
                strategy_variant=(
                    BacktestVariant.Q015_PRIOR_CLOSE_NET_REWARD_RISK_GATE_V1
                ),
            )

        self.assertIs(result, sentinel)
        self.assertIs(
            engine.call_args.kwargs["config"].strategy_variant,
            BacktestVariant.Q015_PRIOR_CLOSE_NET_REWARD_RISK_GATE_V1,
        )

    def test_adapter_rejects_untyped_variant(self):
        with self.assertRaisesRegex(TypeError, "exact BacktestVariant"):
            run_attested_backtest(
                _bundle(),
                initial_equity=100_000.0,
                risk_policy=RiskPolicy(maximum_investment_cents=1_000_000),
                strategy_variant="RSI_AUTOPILOT_V1_Q013_ATR_CAP_0050_SHADOW",
            )


if __name__ == "__main__":
    unittest.main()
