from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
import math
import unittest
from zoneinfo import ZoneInfo

from zidoutrade.indicators import (
    simple_moving_average,
    validate_bar_series,
    wilder_atr,
    wilder_rsi,
)
from zidoutrade.models import CompletedBar15m


ET = ZoneInfo("America/New_York")


def bar(day, hour, minute, close, *, symbol="US.TEST", open_price=None, spread=1.0):
    start = datetime(day[0], day[1], day[2], hour, minute, tzinfo=ET)
    opening = close if open_price is None else open_price
    return CompletedBar15m(
        symbol=symbol,
        start=start,
        end=start + timedelta(minutes=15),
        open=opening,
        high=max(opening, close) + spread,
        low=min(opening, close) - spread,
        close=close,
        volume=1000,
    )


def session_bars(closes, day=(2026, 8, 10), start=(9, 30)):
    initial = datetime(*day, *start, tzinfo=ET)
    return tuple(
        bar(day, (initial + timedelta(minutes=15 * i)).hour,
            (initial + timedelta(minutes=15 * i)).minute, close)
        for i, close in enumerate(closes)
    )


class CompletedBarTests(unittest.TestCase):
    def test_is_immutable(self):
        value = bar((2026, 8, 10), 9, 30, 100)
        with self.assertRaises(FrozenInstanceError):
            value.close = 101

    def test_rejects_incomplete_and_nonfinite(self):
        start = datetime(2026, 8, 10, 9, 30, tzinfo=ET)
        kwargs = dict(
            symbol="US.TEST", start=start, end=start + timedelta(minutes=15),
            open=100, high=101, low=99, close=100, volume=10,
        )
        with self.assertRaisesRegex(ValueError, "incomplete"):
            CompletedBar15m(**kwargs, complete=False)
        kwargs["close"] = math.nan
        with self.assertRaisesRegex(ValueError, "finite"):
            CompletedBar15m(**kwargs)

    def test_rejects_non_rth_and_wrong_duration(self):
        with self.assertRaisesRegex(ValueError, "pre-market"):
            bar((2026, 8, 10), 9, 15, 100)
        start = datetime(2026, 8, 10, 9, 30, tzinfo=ET)
        with self.assertRaisesRegex(ValueError, "exactly 15"):
            CompletedBar15m(
                "US.TEST", start, start + timedelta(minutes=10),
                100, 101, 99, 100, 10,
            )

    def test_rejects_bool_as_number(self):
        start = datetime(2026, 8, 10, 9, 30, tzinfo=ET)
        with self.assertRaises(TypeError):
            CompletedBar15m(
                "US.TEST", start, start + timedelta(minutes=15),
                True, 101, 99, 100, 10,
            )


class WilderRsiTests(unittest.TestCase):
    def test_flat_is_50(self):
        values = wilder_rsi(session_bars([100] * 15))
        self.assertEqual(values[:14], (None,) * 14)
        self.assertEqual(values[14], 50.0)

    def test_only_gains_is_100_and_only_losses_is_zero(self):
        up = wilder_rsi(session_bars(range(100, 115)))
        down = wilder_rsi(session_bars(range(115, 100, -1)))
        self.assertEqual(up[14], 100.0)
        self.assertEqual(down[14], 0.0)

    def test_wilder_seed_and_smoothing_known_vector(self):
        # period=3: seed gains=(1+0+2)/3=1, losses=(0+1+0)/3=1/3,
        # RSI=75. Next loss=1 -> gains=2/3, losses=5/9, RSI=54.5454...
        values = wilder_rsi(session_bars([10, 11, 10, 12, 11]), period=3)
        self.assertAlmostEqual(values[3], 75.0, places=12)
        self.assertAlmostEqual(values[4], 54.54545454545455, places=12)

    def test_does_not_reset_and_includes_overnight_close_change(self):
        bars = (
            bar((2026, 8, 10), 15, 30, 100),
            bar((2026, 8, 10), 15, 45, 100),
            bar((2026, 8, 11), 9, 30, 110),
        )
        values = wilder_rsi(bars, period=2)
        self.assertEqual(values, (None, None, 100.0))

    def test_rejects_out_of_order_duplicate_and_intraday_gap(self):
        bars = session_bars([100, 101, 102])
        with self.assertRaisesRegex(ValueError, "chronological"):
            wilder_rsi((bars[1], bars[0]), period=2)
        with self.assertRaisesRegex(ValueError, "chronological"):
            validate_bar_series((bars[0], bars[0]))
        gap = (bars[0], bars[2])
        with self.assertRaisesRegex(ValueError, "missing"):
            wilder_rsi(gap, period=2)

    def test_rejects_mixed_symbols_and_invalid_period_type(self):
        first = bar((2026, 8, 10), 9, 30, 100)
        second = bar((2026, 8, 10), 9, 45, 101, symbol="US.OTHER")
        with self.assertRaisesRegex(ValueError, "same symbol"):
            wilder_rsi((first, second), period=2)
        with self.assertRaises(TypeError):
            wilder_rsi(session_bars([100, 101, 102]), period=True)


class WilderAtrTests(unittest.TestCase):
    def test_seed_uses_true_range_and_explicit_prior_regular_close(self):
        bars = session_bars([100, 102], start=(9, 30))
        # First bar H/L = 101/99; prior close 90 makes TR=11.
        # Second bar H/L = 103/101 and previous close=100 makes TR=3.
        values = wilder_atr(bars, period=2, prior_regular_close=90)
        self.assertEqual(values[0], None)
        self.assertAlmostEqual(values[1], 7.0)

    def test_overnight_gap_uses_prior_rth_close(self):
        bars = (
            bar((2026, 8, 10), 15, 45, 100),
            bar((2026, 8, 11), 9, 30, 110),
        )
        # TRs are 2 and 11, not 2 and 2.
        self.assertAlmostEqual(wilder_atr(bars, period=2)[1], 6.5)

    def test_wilder_smoothing(self):
        bars = session_bars([100, 102, 101], start=(9, 30))
        values = wilder_atr(bars, period=2)
        self.assertAlmostEqual(values[1], 2.5)  # TR 2, 3
        self.assertAlmostEqual(values[2], 2.25)  # prior ATR*1 + TR 2, /2

    def test_invalid_prior_close_fails_closed(self):
        with self.assertRaises(ValueError):
            wilder_atr(session_bars([100, 101]), period=2, prior_regular_close=0)


class SmaTests(unittest.TestCase):
    def test_sma_alignment(self):
        self.assertEqual(simple_moving_average([1, 2, 3, 4], 3), (None, None, 2.0, 3.0))

    def test_sma_rejects_nonfinite(self):
        with self.assertRaises(ValueError):
            simple_moving_average([1, math.inf], 2)


if __name__ == "__main__":
    unittest.main()
