"""Pure, order-free evaluation helpers for prospective shadow comparisons.

Nothing in this module connects to a broker.  It evaluates already sealed,
synthetic or locally collected trade outcomes and keeps unfilled signals in the
denominator used for the fill-rate report.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Iterable, Optional, Tuple


class ShadowStrategy(str, Enum):
    USER_SELECTED = "RSI_USER_SELECTED"
    ALL_CANDIDATES_EQUAL = "RSI_ALL_CANDIDATES_EQUAL"
    FIXED_BASELINE = "RSI_FIXED_BASELINE"


@dataclass(frozen=True)
class ExecutionCosts:
    commission: float = 0.0
    regulatory_fees: float = 0.0
    spread: float = 0.0
    slippage: float = 0.0
    other: float = 0.0

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be a number")
            if not math.isfinite(float(value)) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")

    @property
    def total(self) -> float:
        return float(sum(self.__dict__.values()))


@dataclass(frozen=True)
class ShadowTrade:
    """One sealed flat-to-flat opportunity, including no-fill outcomes."""

    strategy: ShadowStrategy
    opportunity_id: str
    symbol: str
    filled: bool
    quantity: int = 0
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    costs: ExecutionCosts = ExecutionCosts()

    def __post_init__(self) -> None:
        if not isinstance(self.strategy, ShadowStrategy):
            raise TypeError("strategy must be ShadowStrategy")
        if not self.opportunity_id or not isinstance(self.opportunity_id, str):
            raise ValueError("opportunity_id is required")
        if not isinstance(self.symbol, str) or not self.symbol.startswith("US."):
            raise ValueError("symbol must use US.<TICKER> format")
        if type(self.filled) is not bool:
            raise TypeError("filled must be bool")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int):
            raise TypeError("quantity must be int")
        if self.filled:
            if self.quantity <= 0:
                raise ValueError("filled trade requires positive quantity")
            for name, value in (("entry_price", self.entry_price), ("exit_price", self.exit_price)):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise TypeError(f"{name} must be numeric for a filled trade")
                if not math.isfinite(float(value)) or value <= 0:
                    raise ValueError(f"{name} must be finite and positive")
        elif self.quantity != 0 or self.entry_price is not None or self.exit_price is not None:
            raise ValueError("unfilled opportunities cannot contain execution values")

    @property
    def gross_pnl(self) -> float:
        if not self.filled:
            return 0.0
        assert self.entry_price is not None and self.exit_price is not None
        return self.quantity * (self.exit_price - self.entry_price)

    @property
    def net_pnl(self) -> float:
        if not self.filled:
            return 0.0
        return self.gross_pnl - self.costs.total


@dataclass(frozen=True)
class ShadowSummary:
    strategy: ShadowStrategy
    opportunity_count: int
    filled_trade_count: int
    fill_rate: float
    winning_trades: int
    losing_trades: int
    flat_trades: int
    win_rate_excluding_flat: Optional[float]
    total_gross_pnl: float
    total_costs: float
    total_net_pnl: float
    average_net_win: Optional[float]
    average_net_loss: Optional[float]
    profit_factor: Optional[float]
    maximum_single_trade_loss: Optional[float]


def summarize_shadow(
    trades: Iterable[ShadowTrade], strategy: ShadowStrategy
) -> ShadowSummary:
    if not isinstance(strategy, ShadowStrategy):
        raise TypeError("strategy must be ShadowStrategy")
    selected: Tuple[ShadowTrade, ...] = tuple(t for t in trades if t.strategy is strategy)
    if len({t.opportunity_id for t in selected}) != len(selected):
        raise ValueError("duplicate opportunity_id within strategy")

    filled = tuple(t for t in selected if t.filled)
    net = tuple(t.net_pnl for t in filled)
    wins = tuple(x for x in net if x > 0)
    losses = tuple(x for x in net if x < 0)
    flat = tuple(x for x in net if x == 0)
    decided = len(wins) + len(losses)
    total_wins = sum(wins)
    total_losses = abs(sum(losses))

    return ShadowSummary(
        strategy=strategy,
        opportunity_count=len(selected),
        filled_trade_count=len(filled),
        fill_rate=(len(filled) / len(selected)) if selected else 0.0,
        winning_trades=len(wins),
        losing_trades=len(losses),
        flat_trades=len(flat),
        win_rate_excluding_flat=(len(wins) / decided) if decided else None,
        total_gross_pnl=sum(t.gross_pnl for t in filled),
        total_costs=sum(t.costs.total for t in filled),
        total_net_pnl=sum(net),
        average_net_win=(total_wins / len(wins)) if wins else None,
        average_net_loss=(sum(losses) / len(losses)) if losses else None,
        profit_factor=(total_wins / total_losses) if losses else None,
        maximum_single_trade_loss=min(losses) if losses else None,
    )
