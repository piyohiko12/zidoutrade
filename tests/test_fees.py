from decimal import Decimal
import unittest

from zidoutrade.fees import FeeSchedule, OrderSide, calculate_order_fees


class FeeEngineTests(unittest.TestCase):
    def test_basic_course_rate_floor_and_cap(self):
        minimum = calculate_order_fees(
            FeeSchedule.JP_US_BASIC_CASH_2026_08_13, OrderSide.BUY, 1, 1
        )
        self.assertEqual(minimum.total, Decimal("0.01"))
        ordinary = calculate_order_fees(
            FeeSchedule.JP_US_BASIC_CASH_2026_08_13, OrderSide.BUY, 10, 100
        )
        self.assertEqual(ordinary.total, Decimal("1.32"))
        capped = calculate_order_fees(
            FeeSchedule.JP_US_BASIC_CASH_2026_08_13, OrderSide.SELL, 1000, 100
        )
        self.assertEqual(capped.total, Decimal("22.00"))

    def test_paper_buy_and_sell_are_componentized(self):
        buy = calculate_order_fees(
            FeeSchedule.JP_US_PAPER_STOCK_2026_08_13, OrderSide.BUY, 10, 100
        )
        sell = calculate_order_fees(
            FeeSchedule.JP_US_PAPER_STOCK_2026_08_13, OrderSide.SELL, 10, 100
        )
        self.assertEqual(buy.components["system_usage"], Decimal("1.00"))
        self.assertEqual(buy.components["settlement"], Decimal("0.03"))
        self.assertNotIn("sec", buy.components)
        self.assertEqual(sell.components["sec"], Decimal("0.01"))
        self.assertEqual(sell.components["trading_activity"], Decimal("0.01"))
        self.assertEqual(sell.total, Decimal("1.05"))

    def test_conservative_subcent_rounding(self):
        fee = calculate_order_fees(
            FeeSchedule.JP_US_PAPER_STOCK_2026_08_13, OrderSide.BUY, 3, 50
        )
        self.assertEqual(fee.components["settlement"], Decimal("0.01"))

    def test_invalid_values_fail_closed(self):
        with self.assertRaises(ValueError):
            calculate_order_fees(
                FeeSchedule.JP_US_PAPER_STOCK_2026_08_13, OrderSide.BUY, 0, 10
            )
        with self.assertRaises(ValueError):
            calculate_order_fees(
                FeeSchedule.JP_US_PAPER_STOCK_2026_08_13,
                OrderSide.BUY,
                1,
                float("nan"),
            )


if __name__ == "__main__":
    unittest.main()
