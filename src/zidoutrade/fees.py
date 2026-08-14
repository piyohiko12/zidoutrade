"""Versioned, conservative fee models used by risk and shadow evaluation.

The paper schedule models moomoo Japan's published U.S. stock demo rules.  The
basic cash schedule is report-only: this project never enables real trading.
Every component rounds upward to a cent so the risk estimate is not improved by
unknown sub-cent broker rounding.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from enum import Enum
from typing import Dict


VERIFIED_ON = "2026-08-13"
JP_PRICING_URL = "https://www.moomoo.com/jp/pricing"
JP_FEE_DETAILS_URL = "https://www.moomoo.com/jp/support/topic7_184"
JP_PAPER_RULES_URL = "https://www.moomoo.com/jp/support/topic7_320"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class FeeSchedule(str, Enum):
    JP_US_PAPER_STOCK_2026_08_13 = "JP_US_PAPER_STOCK_2026_08_13"
    JP_US_BASIC_CASH_2026_08_13 = "JP_US_BASIC_CASH_2026_08_13"


CENT = Decimal("0.01")


def _decimal_number(name: str, value: object, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise TypeError(f"{name} must be numeric")
    result = Decimal(str(value))
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'non-negative'}")
    return result


def ceil_cent(value: Decimal) -> Decimal:
    if not value.is_finite() or value < 0:
        raise ValueError("fee must be finite and non-negative")
    return value.quantize(CENT, rounding=ROUND_CEILING)


@dataclass(frozen=True)
class FeeBreakdown:
    schedule: FeeSchedule
    side: OrderSide
    quantity: int
    price: Decimal
    components: Dict[str, Decimal]

    @property
    def total(self) -> Decimal:
        return sum(self.components.values(), Decimal("0"))


def calculate_order_fees(
    schedule: FeeSchedule,
    side: OrderSide,
    quantity: int,
    price: object,
) -> FeeBreakdown:
    if not isinstance(schedule, FeeSchedule):
        raise TypeError("schedule must be FeeSchedule")
    if not isinstance(side, OrderSide):
        raise TypeError("side must be OrderSide")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise ValueError("quantity must be a positive integer")
    px = _decimal_number("price", price, positive=True)
    qty = Decimal(quantity)
    notional = qty * px

    if schedule is FeeSchedule.JP_US_BASIC_CASH_2026_08_13:
        # Published total including Japanese consumption tax: 0.132%, minimum
        # handling of $0.01 and maximum $22 per order. Local clearing is paid by
        # the broker for this course.
        trading = min(max(notional * Decimal("0.00132"), CENT), Decimal("22"))
        components = {"trading_and_system": ceil_cent(trading)}
    else:
        # Published U.S. stock demo rules. These are simulated costs and can
        # differ from real-account fees.
        system = min(max(qty * Decimal("0.005"), Decimal("1")), notional * Decimal("0.005"))
        settlement = qty * Decimal("0.003")
        components = {
            "system_usage": ceil_cent(system),
            "settlement": ceil_cent(settlement),
        }
        if side is OrderSide.SELL:
            sec = max(notional * Decimal("0.000008"), CENT)
            activity = min(
                max(qty * Decimal("0.000166"), CENT),
                Decimal("8.30"),
            )
            components["sec"] = ceil_cent(sec)
            components["trading_activity"] = ceil_cent(activity)

    return FeeBreakdown(schedule, side, quantity, px, components)


__all__ = [
    "FeeBreakdown",
    "FeeSchedule",
    "JP_FEE_DETAILS_URL",
    "JP_PAPER_RULES_URL",
    "JP_PRICING_URL",
    "OrderSide",
    "VERIFIED_ON",
    "calculate_order_fees",
    "ceil_cent",
]
