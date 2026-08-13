from datetime import date, datetime, timedelta
import math
import unittest
from zoneinfo import ZoneInfo

from zidoutrade.candidates import InstrumentKind
from zidoutrade.exchange_calendar import (
    FrozenExchangeCalendar,
    FrozenSession,
    calendar_payload_sha256,
)
from zidoutrade.market_data import (
    DailyBar,
    FrozenAdjustmentScale,
    FrozenInstrumentClassifier,
    IndicatorBarSeries,
    InstrumentMetadata,
    MarketDataUnavailable,
    MarketDataValidationError,
    MarketState,
    MoomooQuoteAdapter,
    PriceScale,
    QuoteSnapshot,
    StaleMarketData,
    assert_adjustment_scale_unchanged,
    build_candidate_facts,
    build_market_gates,
    freeze_adjustment_scale,
    instrument_metadata_payload_sha256,
    validate_completed_15m_bars,
    validate_daily_bars,
    validate_quote_snapshot,
    validate_scale_alignment,
)
from zidoutrade.models import CompletedBar15m


NY = ZoneInfo("America/New_York")


def sessions_from_days(days):
    return tuple(
        FrozenSession(
            day,
            datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY),
            datetime(day.year, day.month, day.day, 16, 0, tzinfo=NY),
        )
        for day in days
    )


def calendar_from_days(days, covered_from=None, covered_through=None):
    sessions = sessions_from_days(days)
    first = covered_from or days[0]
    last = covered_through or days[-1]
    digest = calendar_payload_sha256(
        source_revision="synthetic-market-data-v1",
        covered_from=first,
        covered_through=last,
        sessions=sessions,
    )
    return FrozenExchangeCalendar(
        "synthetic-market-data-v1", first, last, sessions, digest
    )


def intraday_bar(symbol, start, close=100.0):
    return CompletedBar15m(
        symbol=symbol,
        start=start,
        end=start + timedelta(minutes=15),
        open=close,
        high=close + 1,
        low=close - 1,
        close=close + 0.25,
        volume=1000,
    )


def all_intraday_bars(calendar, symbol="US.ABC"):
    starts = tuple(start for session in calendar.sessions for start in session.bar_starts())
    return tuple(
        intraday_bar(symbol, start, 100.0 + index / 100.0)
        for index, start in enumerate(starts)
    )


def adjustment_scale(calendar, symbol="US.ABC", target_session=None):
    target = target_session or calendar.sessions[-1].session_date
    prior = tuple(
        session.session_date
        for session in calendar.sessions
        if session.session_date < target
    )[-1]
    session = calendar.session_on(target)
    return FrozenAdjustmentScale(
        symbol=symbol,
        target_session=target,
        source_daily_session=prior,
        qfq_to_raw=1.0,
        frozen_at=session.open_at,
        calendar_sha256=calendar.sha256,
    )


def daily_bars(days, symbol="US.ABC", scale=PriceScale.RAW, start_price=100.0):
    return tuple(
        DailyBar(
            symbol=symbol,
            session_date=day,
            open=start_price + index,
            high=start_price + index + 1,
            low=start_price + index - 1,
            close=start_price + index + 0.5,
            volume=1_000_000,
            scale=scale,
        )
        for index, day in enumerate(days)
    )


class DailyValidationTests(unittest.TestCase):
    def setUp(self):
        self.days = [date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 13)]
        self.calendar = calendar_from_days(
            self.days,
            covered_from=date(2026, 8, 10),
            covered_through=date(2026, 8, 13),
        )
        self.as_of = datetime(2026, 8, 13, 16, 1, tzinfo=NY)

    def test_closed_date_gap_is_not_a_missing_session(self):
        bars = daily_bars(self.days)
        self.assertEqual(
            validate_daily_bars(
                bars,
                symbol="US.ABC",
                scale=PriceScale.RAW,
                as_of=self.as_of,
                calendar=self.calendar,
            ),
            bars,
        )

    def test_missing_trading_session_is_rejected(self):
        bars = daily_bars([self.days[0], self.days[2]])
        with self.assertRaisesRegex(MarketDataValidationError, "missing"):
            validate_daily_bars(
                bars,
                symbol="US.ABC",
                scale=PriceScale.RAW,
                as_of=self.as_of,
                calendar=self.calendar,
            )

    def test_stale_daily_history_is_rejected(self):
        bars = daily_bars(self.days[:-1])
        with self.assertRaises(StaleMarketData):
            validate_daily_bars(
                bars,
                symbol="US.ABC",
                scale=PriceScale.RAW,
                as_of=self.as_of,
                calendar=self.calendar,
            )

    def test_nonfinite_price_is_rejected_at_model_boundary(self):
        with self.assertRaisesRegex(MarketDataValidationError, "finite"):
            DailyBar(
                "US.ABC",
                self.days[0],
                100.0,
                math.inf,
                99.0,
                100.0,
                1_000.0,
                PriceScale.RAW,
            )


class ScaleValidationTests(unittest.TestCase):
    def test_aligned_raw_and_qfq_are_accepted(self):
        days = [date(2026, 8, 10), date(2026, 8, 11)]
        raw = daily_bars(days)
        qfq = daily_bars(days, scale=PriceScale.QFQ)
        pair = validate_scale_alignment(raw, qfq)
        self.assertAlmostEqual(pair.current_qfq_to_raw, 1.0)

    def test_reversed_scale_tags_are_rejected(self):
        days = [date(2026, 8, 10)]
        with self.assertRaisesRegex(MarketDataValidationError, "tags"):
            validate_scale_alignment(
                daily_bars(days, scale=PriceScale.QFQ),
                daily_bars(days, scale=PriceScale.RAW),
            )

    def test_inconsistent_ohlc_adjustment_is_rejected(self):
        day = date(2026, 8, 10)
        raw = daily_bars([day])
        qfq = (
            DailyBar("US.ABC", day, 100.0, 100.5, 99.0, 100.5, 1_000_000, PriceScale.QFQ),
        )
        with self.assertRaisesRegex(MarketDataValidationError, "ratio"):
            validate_scale_alignment(raw, qfq)

    def test_latest_qfq_must_be_on_current_raw_scale(self):
        day = date(2026, 8, 10)
        raw = daily_bars([day])
        qfq = (
            DailyBar("US.ABC", day, 50.0, 50.5, 49.5, 50.25, 1_000_000, PriceScale.QFQ),
        )
        with self.assertRaisesRegex(MarketDataValidationError, "current raw scale"):
            validate_scale_alignment(raw, qfq)

    def test_session_scale_freezes_at_open_and_converts_atr(self):
        days = [date(2026, 8, 10), date(2026, 8, 11)]
        calendar = calendar_from_days(days)
        raw = daily_bars(days[:1])
        qfq = daily_bars(days[:1], scale=PriceScale.QFQ)
        pair = validate_scale_alignment(raw, qfq)
        frozen = freeze_adjustment_scale(
            pair,
            target_session=days[-1],
            frozen_at=calendar.session_on(days[-1]).open_at,
            calendar=calendar,
        )
        self.assertEqual(frozen.raw_value(1.5), 1.5)
        assert_adjustment_scale_unchanged(frozen, pair)

    def test_mid_session_scale_change_is_rejected(self):
        days = [date(2026, 8, 10), date(2026, 8, 11)]
        calendar = calendar_from_days(days)
        pair = validate_scale_alignment(
            daily_bars(days[:1]), daily_bars(days[:1], scale=PriceScale.QFQ)
        )
        frozen = freeze_adjustment_scale(
            pair,
            target_session=days[-1],
            frozen_at=calendar.session_on(days[-1]).open_at,
            calendar=calendar,
        )
        changed_qfq = (
            DailyBar(
                "US.ABC",
                days[0],
                80.0,
                80.8,
                79.2,
                80.4,
                1_000_000,
                PriceScale.QFQ,
            ),
        )
        changed = type(pair)(pair.raw, changed_qfq, 1.25)
        with self.assertRaisesRegex(MarketDataValidationError, "changed mid-session"):
            assert_adjustment_scale_unchanged(frozen, changed)

    def test_scale_cannot_freeze_after_the_open_boundary(self):
        days = [date(2026, 8, 10), date(2026, 8, 11)]
        calendar = calendar_from_days(days)
        pair = validate_scale_alignment(
            daily_bars(days[:1]), daily_bars(days[:1], scale=PriceScale.QFQ)
        )
        with self.assertRaisesRegex(MarketDataValidationError, "official session open"):
            freeze_adjustment_scale(
                pair,
                target_session=days[-1],
                frozen_at=calendar.session_on(days[-1]).open_at + timedelta(seconds=1),
                calendar=calendar,
            )


class IntradayValidationTests(unittest.TestCase):
    def setUp(self):
        self.days = [date(2026, 8, 10), date(2026, 8, 11)]
        self.calendar = calendar_from_days(self.days)
        self.bars = all_intraday_bars(self.calendar)
        self.as_of = datetime(2026, 8, 11, 16, 1, tzinfo=NY)

    def test_complete_contiguous_rth_bars_are_accepted(self):
        self.assertEqual(
            validate_completed_15m_bars(
                self.bars,
                symbol="US.ABC",
                as_of=self.as_of,
                calendar=self.calendar,
            ),
            self.bars,
        )

    def test_missing_bar_is_rejected(self):
        with self.assertRaisesRegex(MarketDataValidationError, "missing"):
            validate_completed_15m_bars(
                self.bars[:5] + self.bars[6:],
                symbol="US.ABC",
                as_of=self.as_of,
                calendar=self.calendar,
            )

    def test_duplicate_bar_is_rejected(self):
        with self.assertRaisesRegex(MarketDataValidationError, "strictly chronological"):
            validate_completed_15m_bars(
                self.bars[:2] + (self.bars[1],) + self.bars[2:],
                symbol="US.ABC",
                as_of=self.as_of,
                calendar=self.calendar,
            )

    def test_latest_missing_bar_is_stale(self):
        with self.assertRaises(StaleMarketData):
            validate_completed_15m_bars(
                self.bars[:-1],
                symbol="US.ABC",
                as_of=self.as_of,
                calendar=self.calendar,
            )

    def test_forming_bar_is_rejected(self):
        as_of = datetime(2026, 8, 11, 10, 1, tzinfo=NY)
        first = self.bars[26:28]
        forming = intraday_bar("US.ABC", datetime(2026, 8, 11, 10, 0, tzinfo=NY))
        with self.assertRaisesRegex(MarketDataValidationError, "future or forming"):
            validate_completed_15m_bars(
                first + (forming,),
                symbol="US.ABC",
                as_of=as_of,
                calendar=self.calendar,
            )


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.day = date(2026, 8, 11)
        self.calendar = calendar_from_days([self.day])
        self.as_of = datetime(2026, 8, 11, 10, 0, tzinfo=NY)

    def snapshot(self, observed_at=None, state=MarketState.OPEN):
        return QuoteSnapshot(
            "US.ABC",
            observed_at or self.as_of - timedelta(seconds=5),
            99.99,
            100.01,
            100.0,
            state,
        )

    def test_fresh_snapshot_and_spread_gate(self):
        gates = build_market_gates(
            self.snapshot(),
            symbol="US.ABC",
            as_of=self.as_of,
            calendar=self.calendar,
            max_spread_bps=3.0,
        )
        self.assertTrue(gates.data_ok)
        self.assertTrue(gates.spread_ok)
        self.assertTrue(gates.market_open)

    def test_provider_open_state_cannot_override_calendar(self):
        as_of = datetime(2026, 8, 11, 16, 1, tzinfo=NY)
        gates = build_market_gates(
            self.snapshot(observed_at=as_of),
            symbol="US.ABC",
            as_of=as_of,
            calendar=self.calendar,
            max_spread_bps=3.0,
        )
        self.assertFalse(gates.market_open)

    def test_stale_and_future_quotes_fail_closed(self):
        with self.assertRaises(StaleMarketData):
            validate_quote_snapshot(
                self.snapshot(self.as_of - timedelta(minutes=2)),
                symbol="US.ABC",
                as_of=self.as_of,
                calendar=self.calendar,
            )
        with self.assertRaisesRegex(MarketDataValidationError, "future"):
            validate_quote_snapshot(
                self.snapshot(self.as_of + timedelta(seconds=3)),
                symbol="US.ABC",
                as_of=self.as_of,
                calendar=self.calendar,
            )

    def test_crossed_book_is_rejected(self):
        with self.assertRaisesRegex(MarketDataValidationError, "crossed"):
            QuoteSnapshot(
                "US.ABC", self.as_of, 100.01, 99.99, 100.0, MarketState.OPEN
            )


class CandidateFactTests(unittest.TestCase):
    def test_builds_raw_liquidity_qfq_trend_and_spy_regime_facts(self):
        days = []
        current = date(2025, 8, 1)
        while len(days) < 260:
            if current.weekday() < 5:
                days.append(current)
            current += timedelta(days=1)
        calendar = calendar_from_days(
            days,
            covered_from=days[0],
            covered_through=days[-1],
        )
        as_of = datetime(
            days[-1].year, days[-1].month, days[-1].day, 16, 1, tzinfo=NY
        )
        candidate_raw = daily_bars(days, start_price=20.0)
        candidate_qfq = daily_bars(days, scale=PriceScale.QFQ, start_price=20.0)
        spy_raw = daily_bars(days, symbol="US.SPY", start_price=300.0)
        spy_qfq = daily_bars(
            days, symbol="US.SPY", scale=PriceScale.QFQ, start_price=300.0
        )
        intraday = IndicatorBarSeries(
            bars=all_intraday_bars(calendar)[-104:],
            scale=PriceScale.QFQ,
            adjustment_scale=adjustment_scale(calendar),
        )
        facts = build_candidate_facts(
            priority=2,
            metadata=InstrumentMetadata(
                "US.ABC", InstrumentKind.ORDINARY_EQUITY
            ),
            raw_daily=candidate_raw,
            qfq_daily=candidate_qfq,
            completed_15m=intraday,
            spy_raw_daily=spy_raw,
            spy_qfq_daily=spy_qfq,
            as_of=as_of,
            calendar=calendar,
        )
        self.assertEqual(facts.symbol, "US.ABC")
        self.assertEqual(facts.daily_bars, 260)
        self.assertEqual(facts.completed_15m_bars, 104)
        self.assertGreater(facts.median_dollar_volume_20d, 50_000_000)
        self.assertGreater(facts.sma50, facts.sma50_5d_ago)
        self.assertGreater(facts.spy_close, facts.spy_sma200)
        self.assertEqual(facts.metadata["calendar_sha256"], calendar.sha256)


class FrozenInstrumentClassifierTests(unittest.TestCase):
    def setUp(self):
        self.observed = datetime(2026, 8, 11, 8, 0, tzinfo=NY)
        self.valid = datetime(2026, 8, 11, 16, 0, tzinfo=NY)
        self.items = (
            InstrumentMetadata("US.ABC", InstrumentKind.ORDINARY_EQUITY),
            InstrumentMetadata("US.SPY", InstrumentKind.NONLEVERAGED_ETF),
        )
        digest = instrument_metadata_payload_sha256(
            source_revision="synthetic-reference-data-v1",
            observed_at=self.observed,
            valid_through=self.valid,
            instruments=self.items,
        )
        self.classifier = FrozenInstrumentClassifier(
            "synthetic-reference-data-v1",
            self.observed,
            self.valid,
            self.items,
            digest,
        )

    def test_exact_known_classes_are_returned(self):
        self.assertIs(
            self.classifier.classify("US.SPY", as_of=self.observed).instrument_kind,
            InstrumentKind.NONLEVERAGED_ETF,
        )

    def test_absent_symbol_is_unknown_and_status_unknown(self):
        result = self.classifier.classify("US.XYZ", as_of=self.observed)
        self.assertIs(result.instrument_kind, InstrumentKind.UNKNOWN)
        self.assertFalse(result.status_known)

    def test_stale_and_tampered_snapshots_fail_closed(self):
        with self.assertRaises(StaleMarketData):
            self.classifier.classify(
                "US.ABC", as_of=self.valid + timedelta(seconds=1)
            )
        with self.assertRaisesRegex(MarketDataValidationError, "SHA-256 mismatch"):
            FrozenInstrumentClassifier(
                "synthetic-reference-data-v1",
                self.observed,
                self.valid,
                self.items,
                "0" * 64,
            )

    def test_unsorted_or_duplicate_instruments_are_rejected(self):
        with self.assertRaisesRegex(MarketDataValidationError, "unique and sorted"):
            FrozenInstrumentClassifier(
                "synthetic-reference-data-v1",
                self.observed,
                self.valid,
                tuple(reversed(self.items)),
                "0" * 64,
            )


class FakeFrame:
    def __init__(self, rows):
        self.rows = rows

    def to_dict(self, orientation):
        if orientation != "records":
            raise AssertionError("unexpected orientation")
        return list(self.rows)


class FakeQuoteContext:
    def __init__(self, *, history_rows=None, snapshot_rows=None, page_key=None):
        self.history_rows = history_rows or []
        self.snapshot_rows = snapshot_rows or []
        self.page_key = page_key
        self.closed = False
        self.history_calls = []

    def request_history_kline(self, code, **kwargs):
        self.history_calls.append((code, kwargs))
        return 0, FakeFrame(self.history_rows), self.page_key

    def get_global_state(self):
        return 0, {"market_us": "MORNING"}

    def get_market_snapshot(self, codes):
        return 0, FakeFrame(self.snapshot_rows)

    def get_order_book(self, code, num):
        return 0, {"Bid": [(99.99, 10)], "Ask": [(100.01, 20)]}

    def close(self):
        self.closed = True


class FakeSDK:
    RET_OK = 0

    class KLType:
        K_15M = "K_15M"
        K_DAY = "K_DAY"

    class AuType:
        NONE = "NONE"
        QFQ = "QFQ"

    class Session:
        RTH = "RTH"

    class KL_FIELD:
        DATE_TIME = "time_key"
        OPEN = "open"
        HIGH = "high"
        LOW = "low"
        CLOSE = "close"
        TRADE_VOL = "volume"

    def __init__(self, context):
        self.context = context
        self.open_calls = []

    def OpenQuoteContext(self, *, host, port):
        self.open_calls.append((host, port))
        return self.context


class QuoteAdapterTests(unittest.TestCase):
    def setUp(self):
        self.days = [date(2026, 8, 10), date(2026, 8, 11)]
        self.calendar = calendar_from_days(self.days)

    def test_adapter_is_lazy_loopback_and_quote_only(self):
        rows = [
            {
                "code": "US.ABC",
                "time_key": "2026-08-11 09:30:00",
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 1000.0,
            },
            {
                "code": "US.ABC",
                "time_key": "2026-08-11 09:45:00",
                "open": 100.5,
                "high": 101.5,
                "low": 100.0,
                "close": 101.0,
                "volume": 1100.0,
            },
        ]
        context = FakeQuoteContext(history_rows=rows)
        sdk = FakeSDK(context)
        loads = []
        adapter = MoomooQuoteAdapter(sdk_loader=lambda: loads.append(True) or sdk)
        self.assertEqual(loads, [])
        history = adapter.completed_15m_bars(
            symbol="US.ABC",
            start=self.days[-1],
            end=self.days[-1],
            as_of=datetime(2026, 8, 11, 10, 1, tzinfo=NY),
            calendar=self.calendar,
            adjustment_scale=adjustment_scale(self.calendar),
        )
        self.assertEqual(len(history.bars), 2)
        self.assertIs(history.scale, PriceScale.QFQ)
        self.assertEqual(sdk.open_calls, [("127.0.0.1", 11111)])
        self.assertTrue(context.closed)
        self.assertFalse(context.history_calls[0][1]["extended_time"])
        self.assertEqual(context.history_calls[0][1]["session"], "RTH")
        self.assertEqual(context.history_calls[0][1]["autype"], "QFQ")

    def test_paginated_history_fails_instead_of_implicit_backfill(self):
        context = FakeQuoteContext(page_key="more")
        adapter = MoomooQuoteAdapter(sdk_loader=lambda: FakeSDK(context))
        with self.assertRaisesRegex(MarketDataUnavailable, "backfill"):
            adapter.completed_15m_bars(
                symbol="US.ABC",
                start=self.days[-1],
                end=self.days[-1],
                as_of=datetime(2026, 8, 11, 10, 1, tzinfo=NY),
                calendar=self.calendar,
                adjustment_scale=adjustment_scale(self.calendar),
            )

    def test_history_row_for_another_symbol_is_never_relabeled(self):
        rows = [
            {
                "code": "US.XYZ",
                "time_key": "2026-08-11 09:30:00",
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 1000.0,
            }
        ]
        context = FakeQuoteContext(history_rows=rows)
        adapter = MoomooQuoteAdapter(sdk_loader=lambda: FakeSDK(context))
        with self.assertRaisesRegex(MarketDataUnavailable, "identity"):
            adapter.completed_15m_bars(
                symbol="US.ABC",
                start=self.days[-1],
                end=self.days[-1],
                as_of=datetime(2026, 8, 11, 9, 46, tzinfo=NY),
                calendar=self.calendar,
                adjustment_scale=adjustment_scale(self.calendar),
            )
        self.assertTrue(context.closed)

    def test_ambiguous_sdk_rth_constant_fails_before_history_rpc(self):
        context = FakeQuoteContext()
        sdk = FakeSDK(context)
        sdk.Session = type("Session", (), {"RTH": "ALL"})
        adapter = MoomooQuoteAdapter(sdk_loader=lambda: sdk)
        with self.assertRaisesRegex(MarketDataUnavailable, "RTH"):
            adapter.completed_15m_bars(
                symbol="US.ABC",
                start=self.days[-1],
                end=self.days[-1],
                as_of=datetime(2026, 8, 11, 9, 46, tzinfo=NY),
                calendar=self.calendar,
                adjustment_scale=adjustment_scale(self.calendar),
            )
        self.assertEqual(context.history_calls, [])
        self.assertTrue(context.closed)

    def test_snapshot_combines_market_state_snapshot_and_book(self):
        context = FakeQuoteContext(
            snapshot_rows=[
                {
                    "code": "US.ABC",
                    "update_time": "2026-08-11 10:00:30",
                    "last_price": 100.0,
                    "sec_status": "NORMAL",
                }
            ]
        )
        adapter = MoomooQuoteAdapter(sdk_loader=lambda: FakeSDK(context))
        snapshot = adapter.quote_snapshot(
            symbol="US.ABC",
            as_of=datetime(2026, 8, 11, 10, 1, tzinfo=NY),
            calendar=self.calendar,
        )
        self.assertIs(snapshot.market_state, MarketState.OPEN)
        self.assertIs(snapshot.scale, PriceScale.RAW)
        self.assertAlmostEqual(snapshot.spread_bps, 2.0)
        self.assertTrue(context.closed)

    def test_non_loopback_endpoint_is_rejected_before_sdk_load(self):
        loads = []
        with self.assertRaisesRegex(MarketDataValidationError, "127.0.0.1"):
            MoomooQuoteAdapter(
                sdk_loader=lambda: loads.append(True), host="localhost"
            )
        self.assertEqual(loads, [])


if __name__ == "__main__":
    unittest.main()
