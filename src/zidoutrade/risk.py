"""Frozen V1, fee- and slippage-aware risk gates and position sizing.

The public paper fee schedule is imported from :mod:`zidoutrade.fees` rather
than reconstructed as a caller-supplied formula.  V1 policy and stress values
are exact: constructing a wider policy is rejected before sizing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Tuple

from .fees import FeeSchedule, OrderSide, calculate_order_fees
from .models import require_int, require_number


PAPER_FEE_SCHEDULE = FeeSchedule.JP_US_PAPER_STOCK_2026_08_13


def _fraction(name: str, value: object) -> float:
    result = require_number(name, value, positive=True)
    if result > 1.0:
        raise ValueError("%s must not exceed 1" % name)
    return result


@dataclass(frozen=True)
class ExecutionStress:
    """Explicit stress assumptions frozen with the strategy configuration."""

    stop_slippage_fraction: float = 0.005
    stop_slippage_floor: float = 0.05
    minimum_price_increment: float = 0.01

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "stop_slippage_fraction",
            _fraction("stop_slippage_fraction", self.stop_slippage_fraction),
        )
        object.__setattr__(
            self,
            "stop_slippage_floor",
            require_number(
                "stop_slippage_floor", self.stop_slippage_floor, positive=True
            ),
        )
        object.__setattr__(
            self,
            "minimum_price_increment",
            require_number(
                "minimum_price_increment", self.minimum_price_increment, positive=True
            ),
        )
        expected = {
            "stop_slippage_fraction": 0.005,
            "stop_slippage_floor": 0.05,
            "minimum_price_increment": 0.01,
        }
        for field_name, frozen_value in expected.items():
            if getattr(self, field_name) != frozen_value:
                raise ValueError("V1 freezes %s at %s" % (field_name, frozen_value))


@dataclass(frozen=True)
class RiskPolicy:
    planned_risk_fraction: float = 0.0025
    daily_loss_fraction: float = 0.0075
    weekly_loss_fraction: float = 0.02
    max_notional_fraction: float = 0.10
    max_roundtrips_per_day: int = 1

    def __post_init__(self) -> None:
        for field_name in (
            "planned_risk_fraction",
            "daily_loss_fraction",
            "weekly_loss_fraction",
            "max_notional_fraction",
        ):
            object.__setattr__(self, field_name, _fraction(field_name, getattr(self, field_name)))
        limit = require_int(
            "max_roundtrips_per_day", self.max_roundtrips_per_day, nonnegative=True
        )
        if limit != 1:
            raise ValueError("V1 freezes max_roundtrips_per_day at exactly 1")
        expected = {
            "planned_risk_fraction": 0.0025,
            "daily_loss_fraction": 0.0075,
            "weekly_loss_fraction": 0.02,
            "max_notional_fraction": 0.10,
        }
        for field_name, frozen_value in expected.items():
            if getattr(self, field_name) != frozen_value:
                raise ValueError("V1 freezes %s at %s" % (field_name, frozen_value))


@dataclass(frozen=True)
class RiskState:
    """PnL state uses the session/weekly frozen equity anchors."""

    day_start_equity: float
    week_start_equity: float
    daily_pnl: float
    weekly_pnl: float
    completed_roundtrips_today: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "day_start_equity",
            require_number("day_start_equity", self.day_start_equity, positive=True),
        )
        object.__setattr__(
            self,
            "week_start_equity",
            require_number("week_start_equity", self.week_start_equity, positive=True),
        )
        object.__setattr__(self, "daily_pnl", require_number("daily_pnl", self.daily_pnl))
        object.__setattr__(self, "weekly_pnl", require_number("weekly_pnl", self.weekly_pnl))
        require_int(
            "completed_roundtrips_today",
            self.completed_roundtrips_today,
            nonnegative=True,
        )


class RiskBlockReason(str, Enum):
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    WEEKLY_LOSS_LIMIT = "WEEKLY_LOSS_LIMIT"
    ROUNDTRIP_LIMIT = "ROUNDTRIP_LIMIT"
    ONE_SHARE_EXCEEDS_NOTIONAL = "ONE_SHARE_EXCEEDS_NOTIONAL"
    ONE_SHARE_EXCEEDS_RISK = "ONE_SHARE_EXCEEDS_RISK"


@dataclass(frozen=True)
class RiskGateResult:
    allowed: bool
    reasons: Tuple[RiskBlockReason, ...]

    def __post_init__(self) -> None:
        if type(self.allowed) is not bool:
            raise TypeError("allowed must be an exact bool")
        if type(self.reasons) is not tuple:
            raise TypeError("reasons must be a tuple")
        if any(type(reason) is not RiskBlockReason for reason in self.reasons):
            raise TypeError("reasons must contain exact RiskBlockReason values")
        if self.allowed == bool(self.reasons):
            raise ValueError("allowed must be true exactly when reasons is empty")


def entry_allowed(
    state: RiskState, policy: RiskPolicy = RiskPolicy()
) -> RiskGateResult:
    if type(state) is not RiskState:
        raise TypeError("state must be an exact RiskState")
    if type(policy) is not RiskPolicy:
        raise TypeError("policy must be an exact RiskPolicy")
    reasons = []
    if state.daily_pnl / state.day_start_equity <= -policy.daily_loss_fraction:
        reasons.append(RiskBlockReason.DAILY_LOSS_LIMIT)
    if state.weekly_pnl / state.week_start_equity <= -policy.weekly_loss_fraction:
        reasons.append(RiskBlockReason.WEEKLY_LOSS_LIMIT)
    if state.completed_roundtrips_today >= policy.max_roundtrips_per_day:
        reasons.append(RiskBlockReason.ROUNDTRIP_LIMIT)
    return RiskGateResult(not reasons, tuple(reasons))


@dataclass(frozen=True)
class SizingRequest:
    state: RiskState
    entry_limit: float
    stop_trigger: float
    entry_fees: FeeSchedule = PAPER_FEE_SCHEDULE
    exit_fees: FeeSchedule = PAPER_FEE_SCHEDULE
    stress: ExecutionStress = ExecutionStress()
    policy: RiskPolicy = RiskPolicy()

    def __post_init__(self) -> None:
        if type(self.state) is not RiskState:
            raise TypeError("state must be an exact RiskState")
        entry = require_number("entry_limit", self.entry_limit, positive=True)
        stop = require_number("stop_trigger", self.stop_trigger, positive=True)
        if stop >= entry:
            raise ValueError("stop_trigger must be below entry_limit")
        object.__setattr__(self, "entry_limit", entry)
        object.__setattr__(self, "stop_trigger", stop)
        if type(self.entry_fees) is not FeeSchedule or type(self.exit_fees) is not FeeSchedule:
            raise TypeError("entry_fees and exit_fees must be exact FeeSchedule values")
        if self.entry_fees is not PAPER_FEE_SCHEDULE or self.exit_fees is not PAPER_FEE_SCHEDULE:
            raise ValueError("V1 sizing requires the frozen paper fee schedule")
        if type(self.stress) is not ExecutionStress:
            raise TypeError("stress must be an exact ExecutionStress")
        if type(self.policy) is not RiskPolicy:
            raise TypeError("policy must be an exact RiskPolicy")


@dataclass(frozen=True)
class StressComponents:
    qty: int
    stop_distance_per_share: float
    stop_slippage_per_share: float
    stressed_exit_price: float
    price_loss: float
    entry_fee: float
    exit_fee: float
    planned_total_loss: float

    def __post_init__(self) -> None:
        require_int("qty", self.qty, nonnegative=True)
        for field_name in (
            "stop_distance_per_share",
            "stop_slippage_per_share",
            "price_loss",
            "entry_fee",
            "exit_fee",
            "planned_total_loss",
        ):
            value = require_number(
                field_name, getattr(self, field_name), nonnegative=True
            )
            object.__setattr__(self, field_name, value)
        object.__setattr__(
            self,
            "stressed_exit_price",
            require_number(
                "stressed_exit_price", self.stressed_exit_price, positive=True
            ),
        )
        total = self.price_loss + self.entry_fee + self.exit_fee
        if not math.isclose(self.planned_total_loss, total, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("planned_total_loss must equal the explicit components")


@dataclass(frozen=True)
class SizingResult:
    qty: int
    components: StressComponents
    risk_budget: float
    notional_cap: float
    block_reasons: Tuple[RiskBlockReason, ...]

    def __post_init__(self) -> None:
        require_int("qty", self.qty, nonnegative=True)
        if type(self.components) is not StressComponents:
            raise TypeError("components must be an exact StressComponents")
        object.__setattr__(
            self, "risk_budget", require_number("risk_budget", self.risk_budget, positive=True)
        )
        object.__setattr__(
            self, "notional_cap", require_number("notional_cap", self.notional_cap, positive=True)
        )
        if type(self.block_reasons) is not tuple:
            raise TypeError("block_reasons must be a tuple")
        if any(type(reason) is not RiskBlockReason for reason in self.block_reasons):
            raise TypeError("block_reasons must contain exact RiskBlockReason values")
        if len(set(self.block_reasons)) != len(self.block_reasons):
            raise ValueError("block_reasons must not contain duplicates")
        if self.qty > 0 and self.components.qty != self.qty:
            raise ValueError("successful result quantity must match its components")
        if self.qty > 0 and self.block_reasons:
            raise ValueError("a positive quantity cannot have block reasons")
        if self.qty == 0 and not self.block_reasons:
            raise ValueError("a zero quantity must explain why it is blocked")

    @property
    def allowed(self) -> bool:
        return self.qty > 0 and not self.block_reasons


def _components(request: SizingRequest, qty: int) -> StressComponents:
    require_int("qty", qty, nonnegative=True)
    stress = request.stress
    slippage = max(
        stress.stop_slippage_fraction * request.stop_trigger,
        stress.stop_slippage_floor,
    )
    stressed_exit = max(
        stress.minimum_price_increment, request.stop_trigger - slippage
    )
    stop_distance = request.entry_limit - request.stop_trigger
    price_loss = qty * (stop_distance + slippage)

    if qty == 0:
        entry_fee = 0.0
        exit_fee = 0.0
    else:
        entry_fee = float(
            calculate_order_fees(
                request.entry_fees,
                OrderSide.BUY,
                qty,
                request.entry_limit,
            ).total
        )
        exit_fee = float(
            calculate_order_fees(
                request.exit_fees,
                OrderSide.SELL,
                qty,
                stressed_exit,
            ).total
        )
    planned_loss = math.fsum((price_loss, entry_fee, exit_fee))
    return StressComponents(
        qty=qty,
        stop_distance_per_share=stop_distance,
        stop_slippage_per_share=slippage,
        stressed_exit_price=stressed_exit,
        price_loss=price_loss,
        entry_fee=entry_fee,
        exit_fee=exit_fee,
        planned_total_loss=planned_loss,
    )


def size_position(request: SizingRequest) -> SizingResult:
    """Find the largest integer quantity satisfying risk and notional caps.

    The binary search is safe because all stress components are monotone in
    quantity, including cent-rounded fee schedules.
    """

    if type(request) is not SizingRequest:
        raise TypeError("request must be an exact SizingRequest")
    gate = entry_allowed(request.state, request.policy)
    risk_budget = request.policy.planned_risk_fraction * request.state.day_start_equity
    notional_cap = request.policy.max_notional_fraction * request.state.day_start_equity
    zero = _components(request, 0)
    if not gate.allowed:
        return SizingResult(0, zero, risk_budget, notional_cap, gate.reasons)

    notional_qty = int(math.floor(notional_cap / request.entry_limit))
    if notional_qty < 1:
        return SizingResult(
            0,
            zero,
            risk_budget,
            notional_cap,
            (RiskBlockReason.ONE_SHARE_EXCEEDS_NOTIONAL,),
        )

    one_share = _components(request, 1)
    if one_share.planned_total_loss > risk_budget:
        return SizingResult(
            0,
            one_share,
            risk_budget,
            notional_cap,
            (RiskBlockReason.ONE_SHARE_EXCEEDS_RISK,),
        )

    low, high = 1, notional_qty
    best = one_share
    while low <= high:
        middle = (low + high) // 2
        candidate = _components(request, middle)
        if candidate.planned_total_loss <= risk_budget:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return SizingResult(best.qty, best, risk_budget, notional_cap, ())
