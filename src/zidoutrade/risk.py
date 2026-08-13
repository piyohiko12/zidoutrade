"""Versioned, fee- and slippage-aware risk gates and position sizing.

The public paper fee schedule is imported from :mod:`zidoutrade.fees` rather
than reconstructed as a caller-supplied formula.  Risk percentages are stored
as exact integer basis points, are bounded by hard product ceilings, and are
included in the execution evidence.  A missing absolute investment limit is a
deliberate fail-closed state: it never means "unlimited".
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
import math
from typing import Optional, Tuple

from .fees import FeeSchedule, OrderSide, calculate_order_fees
from .models import require_int, require_number


PAPER_FEE_SCHEDULE = FeeSchedule.JP_US_PAPER_STOCK_2026_08_13
RISK_POLICY_VERSION = "RSI_RISK_POLICY_V2"
DEFAULT_PLANNED_RISK_BASIS_POINTS = 25
DEFAULT_DAILY_LOSS_LIMIT_BASIS_POINTS = 75
DEFAULT_WEEKLY_LOSS_LIMIT_BASIS_POINTS = 200
MAX_PLANNED_RISK_BASIS_POINTS = 100
MAX_DAILY_LOSS_LIMIT_BASIS_POINTS = 200
MAX_WEEKLY_LOSS_LIMIT_BASIS_POINTS = 500


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
    """Configurable entry-risk policy with non-configurable safety ceilings.

    The three loss values are *entry sizing and new-entry stop* controls.  They
    cannot guarantee realised losses because gaps, slippage, fees, or exchange
    failures can exceed a planned amount.  They never block an exit.
    """

    risk_policy_version: str = RISK_POLICY_VERSION
    planned_risk_basis_points: int = DEFAULT_PLANNED_RISK_BASIS_POINTS
    daily_loss_limit_basis_points: int = DEFAULT_DAILY_LOSS_LIMIT_BASIS_POINTS
    weekly_loss_limit_basis_points: int = DEFAULT_WEEKLY_LOSS_LIMIT_BASIS_POINTS
    maximum_investment_cents: Optional[int] = None
    max_notional_fraction: float = 0.10
    max_roundtrips_per_day: int = 1

    def __post_init__(self) -> None:
        if self.risk_policy_version != RISK_POLICY_VERSION:
            raise ValueError("risk_policy_version does not match the supported policy")
        basis_points = {
            "planned_risk_basis_points": self.planned_risk_basis_points,
            "daily_loss_limit_basis_points": self.daily_loss_limit_basis_points,
            "weekly_loss_limit_basis_points": self.weekly_loss_limit_basis_points,
        }
        for name, value in basis_points.items():
            if type(value) is not int:
                raise TypeError("%s must be an exact int" % name)
        if not 0 < self.planned_risk_basis_points <= MAX_PLANNED_RISK_BASIS_POINTS:
            raise ValueError("planned_risk_basis_points exceeds its hard limit")
        if not (
            self.planned_risk_basis_points
            <= self.daily_loss_limit_basis_points
            <= MAX_DAILY_LOSS_LIMIT_BASIS_POINTS
        ):
            raise ValueError(
                "daily_loss_limit_basis_points must be between per-trade risk and 200"
            )
        if not (
            self.daily_loss_limit_basis_points
            <= self.weekly_loss_limit_basis_points
            <= MAX_WEEKLY_LOSS_LIMIT_BASIS_POINTS
        ):
            raise ValueError(
                "weekly_loss_limit_basis_points must be between daily loss and 500"
            )
        if self.maximum_investment_cents is not None:
            if type(self.maximum_investment_cents) is not int:
                raise TypeError(
                    "maximum_investment_cents must be a positive exact int or None"
                )
            if self.maximum_investment_cents <= 0:
                raise ValueError("maximum_investment_cents must be positive")
        object.__setattr__(
            self,
            "max_notional_fraction",
            _fraction("max_notional_fraction", self.max_notional_fraction),
        )
        limit = require_int(
            "max_roundtrips_per_day", self.max_roundtrips_per_day, nonnegative=True
        )
        if limit != 1:
            raise ValueError("V1 freezes max_roundtrips_per_day at exactly 1")
        if self.max_notional_fraction != 0.10:
            raise ValueError("V2 freezes max_notional_fraction at 0.1")

    @property
    def planned_risk_fraction(self) -> float:
        return self.planned_risk_basis_points / 10_000.0

    @property
    def daily_loss_fraction(self) -> float:
        return self.daily_loss_limit_basis_points / 10_000.0

    @property
    def weekly_loss_fraction(self) -> float:
        return self.weekly_loss_limit_basis_points / 10_000.0

    def evidence_payload(self) -> dict:
        """Return the complete policy fields bound by execution evidence."""

        return {
            "risk_policy_version": self.risk_policy_version,
            "planned_risk_basis_points": self.planned_risk_basis_points,
            "daily_loss_limit_basis_points": self.daily_loss_limit_basis_points,
            "weekly_loss_limit_basis_points": self.weekly_loss_limit_basis_points,
            "maximum_investment_cents": self.maximum_investment_cents,
            "max_notional_fraction": self.max_notional_fraction,
            "max_roundtrips_per_day": self.max_roundtrips_per_day,
        }


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
    INVESTMENT_LIMIT_NOT_SET = "INVESTMENT_LIMIT_NOT_SET"
    ONE_SHARE_EXCEEDS_NOTIONAL = "ONE_SHARE_EXCEEDS_NOTIONAL"
    ONE_SHARE_EXCEEDS_INVESTMENT = "ONE_SHARE_EXCEEDS_INVESTMENT"
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
    entry_notional: float
    entry_cash_required: float
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
            "entry_notional",
            "entry_cash_required",
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
        if not math.isclose(
            self.entry_cash_required,
            self.entry_notional + self.entry_fee,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise ValueError("entry_cash_required must equal entry notional plus BUY fee")


@dataclass(frozen=True)
class SizingResult:
    qty: int
    components: StressComponents
    risk_budget: float
    notional_cap: float
    investment_cap_cents: Optional[int]
    block_reasons: Tuple[RiskBlockReason, ...]

    def __post_init__(self) -> None:
        require_int("qty", self.qty, nonnegative=True)
        if type(self.components) is not StressComponents:
            raise TypeError("components must be an exact StressComponents")
        object.__setattr__(
            self,
            "risk_budget",
            require_number("risk_budget", self.risk_budget, nonnegative=True),
        )
        object.__setattr__(
            self, "notional_cap", require_number("notional_cap", self.notional_cap, positive=True)
        )
        if self.investment_cap_cents is not None and (
            type(self.investment_cap_cents) is not int
            or self.investment_cap_cents <= 0
        ):
            raise TypeError("investment_cap_cents must be a positive exact int or None")
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
    entry_notional = qty * request.entry_limit
    return StressComponents(
        qty=qty,
        entry_notional=entry_notional,
        entry_cash_required=entry_notional + entry_fee,
        stop_distance_per_share=stop_distance,
        stop_slippage_per_share=slippage,
        stressed_exit_price=stressed_exit,
        price_loss=price_loss,
        entry_fee=entry_fee,
        exit_fee=exit_fee,
        planned_total_loss=planned_loss,
    )


def _entry_cash_required_decimal(request: SizingRequest, qty: int) -> Decimal:
    """Return exact BUY notional plus the cent-rounded versioned BUY fee."""

    require_int("qty", qty, nonnegative=True)
    if qty == 0:
        return Decimal("0")
    entry_price = Decimal(str(request.entry_limit))
    fee = calculate_order_fees(
        request.entry_fees,
        OrderSide.BUY,
        qty,
        request.entry_limit,
    ).total
    return entry_price * qty + fee


def size_position(request: SizingRequest) -> SizingResult:
    """Find the largest integer quantity satisfying risk and notional caps.

    The binary search is safe because all stress components are monotone in
    quantity, including cent-rounded fee schedules.
    """

    if type(request) is not SizingRequest:
        raise TypeError("request must be an exact SizingRequest")
    gate = entry_allowed(request.state, request.policy)
    planned_budget = request.policy.planned_risk_fraction * request.state.day_start_equity
    daily_remaining = (
        request.policy.daily_loss_fraction * request.state.day_start_equity
        + request.state.daily_pnl
    )
    weekly_remaining = (
        request.policy.weekly_loss_fraction * request.state.week_start_equity
        + request.state.weekly_pnl
    )
    risk_budget = max(0.0, min(planned_budget, daily_remaining, weekly_remaining))
    notional_cap = request.policy.max_notional_fraction * request.state.day_start_equity
    investment_cap_cents = request.policy.maximum_investment_cents
    zero = _components(request, 0)
    if not gate.allowed:
        return SizingResult(
            0,
            zero,
            risk_budget,
            notional_cap,
            investment_cap_cents,
            gate.reasons,
        )

    if investment_cap_cents is None:
        return SizingResult(
            0,
            zero,
            risk_budget,
            notional_cap,
            None,
            (RiskBlockReason.INVESTMENT_LIMIT_NOT_SET,),
        )

    investment_cap = Decimal(investment_cap_cents) / Decimal(100)
    entry_price = Decimal(str(request.entry_limit))
    notional_qty = min(
        int(math.floor(notional_cap / request.entry_limit)),
        int(investment_cap / entry_price),
    )
    if notional_qty < 1:
        reason = (
            RiskBlockReason.ONE_SHARE_EXCEEDS_NOTIONAL
            if request.entry_limit > notional_cap
            else RiskBlockReason.ONE_SHARE_EXCEEDS_INVESTMENT
        )
        return SizingResult(
            0,
            zero,
            risk_budget,
            notional_cap,
            investment_cap_cents,
            (reason,),
        )

    one_share = _components(request, 1)
    if _entry_cash_required_decimal(request, 1) > investment_cap:
        return SizingResult(
            0,
            one_share,
            risk_budget,
            notional_cap,
            investment_cap_cents,
            (RiskBlockReason.ONE_SHARE_EXCEEDS_INVESTMENT,),
        )
    if one_share.planned_total_loss > risk_budget:
        return SizingResult(
            0,
            one_share,
            risk_budget,
            notional_cap,
            investment_cap_cents,
            (RiskBlockReason.ONE_SHARE_EXCEEDS_RISK,),
        )

    low, high = 1, notional_qty
    best = one_share
    while low <= high:
        middle = (low + high) // 2
        candidate = _components(request, middle)
        if (
            candidate.planned_total_loss <= risk_budget
            and _entry_cash_required_decimal(request, middle) <= investment_cap
        ):
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return SizingResult(
        best.qty,
        best,
        risk_budget,
        notional_cap,
        investment_cap_cents,
        (),
    )
