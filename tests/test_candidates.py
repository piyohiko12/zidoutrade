from __future__ import annotations

import unittest

from zidoutrade.candidates import (
    CandidateInput,
    CandidatePolicy,
    EligibilityStatus,
    InstrumentKind,
    ReasonCode,
    evaluate_candidate,
    evaluate_candidates,
    normalize_master_watchlist,
    normalize_us_symbol,
)


def eligible_input(symbol: str = "US.AAPL", priority: int = 1, **changes):
    values = {
        "symbol": symbol,
        "priority": priority,
        "instrument_kind": InstrumentKind.ORDINARY_EQUITY,
        "prior_close": 200.0,
        "median_dollar_volume_20d": 200_000_000.0,
        "daily_bars": 500,
        "completed_15m_bars": 300,
        "is_halted": False,
        "has_delisting_risk": False,
        "instrument_status_known": True,
        "adjustment_ok": True,
        "sma200": 180.0,
        "sma50": 195.0,
        "sma50_5d_ago": 192.0,
        "spy_close": 650.0,
        "spy_sma200": 600.0,
    }
    values.update(changes)
    return CandidateInput(**values)


class CandidateTests(unittest.TestCase):
    def test_normalizes_us_symbols_without_accepting_other_markets(self):
        self.assertEqual(normalize_us_symbol(" aapl "), "US.AAPL")
        self.assertEqual(normalize_us_symbol("us.brk.b"), "US.BRK.B")
        with self.assertRaisesRegex(ValueError, "INVALID_SYMBOL"):
            normalize_us_symbol("HK.00700")
        with self.assertRaisesRegex(ValueError, "INVALID_SYMBOL"):
            normalize_us_symbol("../AAPL")

    def test_master_watchlist_is_unique_and_limited_to_twenty(self):
        self.assertEqual(normalize_master_watchlist(["aapl", "US.MSFT"]), ("US.AAPL", "US.MSFT"))
        with self.assertRaisesRegex(ValueError, "DUPLICATE_SYMBOL"):
            normalize_master_watchlist(["AAPL", "US.AAPL"])
        with self.assertRaisesRegex(ValueError, "WATCHLIST_LIMIT_EXCEEDED"):
            normalize_master_watchlist(["S%02d" % index for index in range(21)])

    def test_complete_candidate_passes(self):
        result = evaluate_candidate(eligible_input())
        self.assertEqual(result.status, EligibilityStatus.PASS)
        self.assertTrue(result.eligible)
        self.assertEqual(result.reason_codes, ())

    def test_thresholds_are_inclusive_except_trend_must_be_above(self):
        result = evaluate_candidate(
            eligible_input(
                prior_close=5.0,
                median_dollar_volume_20d=50_000_000.0,
                daily_bars=252,
                completed_15m_bars=100,
                sma200=4.99,
                sma50=5.0,
                sma50_5d_ago=4.99,
                spy_close=600.01,
                spy_sma200=600.0,
            )
        )
        self.assertTrue(result.eligible)

        equal_trend = evaluate_candidate(eligible_input(prior_close=180.0, sma200=180.0))
        self.assertIn(ReasonCode.PRICE_NOT_ABOVE_SMA200, equal_trend.reason_codes)

    def test_every_gate_returns_a_stable_reason_code(self):
        result = evaluate_candidate(
            eligible_input(
                prior_close=4.99,
                median_dollar_volume_20d=49_999_999.0,
                daily_bars=251,
                completed_15m_bars=99,
                is_halted=True,
                has_delisting_risk=True,
                instrument_status_known=False,
                adjustment_ok=False,
                sma200=6.0,
                sma50=4.0,
                sma50_5d_ago=4.0,
                spy_close=500.0,
                spy_sma200=500.0,
            )
        )
        self.assertEqual(result.status, EligibilityStatus.FAIL)
        self.assertEqual(
            set(result.reason_codes),
            {
                ReasonCode.PRIOR_CLOSE_BELOW_MINIMUM,
                ReasonCode.MEDIAN_DOLLAR_VOLUME_BELOW_MINIMUM,
                ReasonCode.INSUFFICIENT_DAILY_BARS,
                ReasonCode.INSUFFICIENT_COMPLETED_15M_BARS,
                ReasonCode.TRADING_HALTED,
                ReasonCode.DELISTING_RISK,
                ReasonCode.INSTRUMENT_STATUS_UNKNOWN,
                ReasonCode.ADJUSTMENT_ISSUE,
                ReasonCode.PRICE_NOT_ABOVE_SMA200,
                ReasonCode.SMA50_NOT_RISING,
                ReasonCode.SPY_NOT_ABOVE_SMA200,
            },
        )

    def test_missing_values_are_fail_closed(self):
        result = evaluate_candidate(
            eligible_input(
                prior_close=None,
                median_dollar_volume_20d=float("nan"),
                sma200=None,
                sma50=None,
                sma50_5d_ago=None,
                spy_close=None,
                spy_sma200=float("inf"),
            )
        )
        self.assertFalse(result.eligible)
        self.assertEqual(
            set(result.reason_codes),
            {
                ReasonCode.MISSING_PRIOR_CLOSE,
                ReasonCode.MISSING_MEDIAN_DOLLAR_VOLUME_20D,
                ReasonCode.MISSING_SMA200,
                ReasonCode.MISSING_SMA50,
                ReasonCode.MISSING_SMA50_5D_AGO,
                ReasonCode.MISSING_SPY_CLOSE,
                ReasonCode.MISSING_SPY_SMA200,
            },
        )

    def test_risky_instrument_classes_are_blocked_by_default_flags(self):
        cases = {
            InstrumentKind.LEVERAGED_ETF: ReasonCode.LEVERAGED_ETF_BLOCKED,
            InstrumentKind.INVERSE_ETF: ReasonCode.INVERSE_ETF_BLOCKED,
            InstrumentKind.OTC_EQUITY: ReasonCode.OTC_BLOCKED,
            InstrumentKind.OPTION: ReasonCode.OPTION_BLOCKED,
            InstrumentKind.UNKNOWN: ReasonCode.UNKNOWN_INSTRUMENT_BLOCKED,
        }
        for kind, expected in cases.items():
            with self.subTest(kind=kind):
                result = evaluate_candidate(eligible_input(instrument_kind=kind))
                self.assertIn(expected, result.reason_codes)
                self.assertFalse(result.eligible)

        with self.assertRaises(ValueError):
            CandidatePolicy(allow_leveraged_etf=True)
        with self.assertRaises(ValueError):
            CandidatePolicy(allow_options="false")
        with self.assertRaises(ValueError):
            CandidatePolicy(min_prior_close=4.0)

    def test_deterministic_order_is_user_priority_then_ticker_not_return_rank(self):
        batch = evaluate_candidates(
            [
                eligible_input("NVDA", 5, metadata={"predicted_return": 99}),
                eligible_input("MSFT", 1, metadata={"predicted_return": -99}),
                eligible_input("AAPL", 1, prior_close=4.0, metadata={"predicted_return": 1_000}),
            ]
        )
        self.assertEqual([item.symbol for item in batch.evaluations], ["US.AAPL", "US.MSFT", "US.NVDA"])
        self.assertEqual(len(batch.evaluations), 3)
        self.assertEqual(len(batch.eligible), 2)
        self.assertEqual(len(batch.ineligible), 1)
        fields = set(batch.evaluations[0].__dataclass_fields__)
        self.assertTrue(fields.isdisjoint({"rank", "score", "expected_return", "predicted_return"}))
        self.assertNotIn("ranking_or_return_score", batch.to_dict())


if __name__ == "__main__":
    unittest.main()
