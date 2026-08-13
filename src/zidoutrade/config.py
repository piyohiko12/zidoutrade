"""Strict, side-effect-free system configuration.

Configuration describes policy; it never acts as an activation marker.  A
valid PAPER_SIMULATE configuration remains disarmed unless a separate, local,
hash-bound activation workflow is completed.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
import json
import math
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from . import PROGRAM_ID
from .risk import (
    MAX_DAILY_LOSS_LIMIT_BASIS_POINTS,
    MAX_PLANNED_RISK_BASIS_POINTS,
    MAX_WEEKLY_LOSS_LIMIT_BASIS_POINTS,
    RISK_POLICY_VERSION,
    RiskPolicy,
)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class SystemConfig:
    program_id: str
    mode: str
    operating_mode: str
    broker_environment: str
    opend_host: str
    opend_port: int
    session: str
    maximum_watchlist_symbols: int
    maximum_active_symbols: int
    maximum_round_trips_per_day: int
    maximum_exit_dispatches_per_round_trip: int
    risk_policy_version: str
    planned_risk_basis_points: int
    daily_loss_limit_basis_points: int
    weekly_loss_limit_basis_points: int
    maximum_investment_cents: Optional[int]
    maximum_symbol_notional_fraction: float
    maximum_total_notional_fraction: float
    rsi_period: int
    rsi_oversold: float
    rsi_recovery: float
    rsi_exit: float
    atr_period: int
    atr_stop_multiple: float
    maximum_holding_bars: int
    fee_schedule: str = "JP_US_PAPER_STOCK_2026_08_13"

    def __post_init__(self) -> None:
        if self.program_id != PROGRAM_ID:
            raise ConfigError("PROGRAM_ID_MISMATCH")
        if self.mode not in {"SHADOW", "PAPER_SIMULATE"}:
            raise ConfigError("MODE_MUST_BE_SHADOW_OR_PAPER_SIMULATE")
        if self.operating_mode != "SUPERVISED_ONLY":
            raise ConfigError("UNATTENDED_MODE_IS_FORBIDDEN")
        if self.broker_environment != "SIMULATE":
            raise ConfigError("BROKER_ENVIRONMENT_MUST_BE_SIMULATE")
        if self.opend_host != "127.0.0.1" or type(self.opend_port) is not int or self.opend_port != 11111:
            raise ConfigError("OPEND_ENDPOINT_MUST_BE_127_0_0_1_11111")
        if self.session != "RTH":
            raise ConfigError("SESSION_MUST_BE_RTH")
        if self.fee_schedule != "JP_US_PAPER_STOCK_2026_08_13":
            raise ConfigError("FEE_SCHEDULE_VERSION_MISMATCH")
        if self.risk_policy_version != RISK_POLICY_VERSION:
            raise ConfigError("RISK_POLICY_VERSION_MISMATCH")

        exact_ints = {
            "maximum_watchlist_symbols": (self.maximum_watchlist_symbols, 1, 20),
            "maximum_active_symbols": (self.maximum_active_symbols, 1, 1),
            "maximum_round_trips_per_day": (self.maximum_round_trips_per_day, 1, 1),
            "maximum_exit_dispatches_per_round_trip": (
                self.maximum_exit_dispatches_per_round_trip,
                1,
                2,
            ),
            "rsi_period": (self.rsi_period, 14, 14),
            "atr_period": (self.atr_period, 14, 14),
            "maximum_holding_bars": (self.maximum_holding_bars, 8, 8),
        }
        for name, (value, lower, upper) in exact_ints.items():
            if type(value) is not int or not lower <= value <= upper:
                raise ConfigError(f"INVALID_{name.upper()}")

        for name, value in (
            ("planned_risk_basis_points", self.planned_risk_basis_points),
            ("daily_loss_limit_basis_points", self.daily_loss_limit_basis_points),
            ("weekly_loss_limit_basis_points", self.weekly_loss_limit_basis_points),
        ):
            if type(value) is not int:
                raise ConfigError(f"INVALID_{name.upper()}")
        if not (
            0
            < self.planned_risk_basis_points
            <= MAX_PLANNED_RISK_BASIS_POINTS
        ):
            raise ConfigError("PLANNED_RISK_BASIS_POINTS_EXCEEDS_HARD_LIMIT")
        if not (
            self.planned_risk_basis_points
            <= self.daily_loss_limit_basis_points
            <= MAX_DAILY_LOSS_LIMIT_BASIS_POINTS
        ):
            raise ConfigError("DAILY_LOSS_LIMIT_BASIS_POINTS_EXCEEDS_HARD_LIMIT")
        if not (
            self.daily_loss_limit_basis_points
            <= self.weekly_loss_limit_basis_points
            <= MAX_WEEKLY_LOSS_LIMIT_BASIS_POINTS
        ):
            raise ConfigError("WEEKLY_LOSS_LIMIT_BASIS_POINTS_EXCEEDS_HARD_LIMIT")
        if self.maximum_investment_cents is None:
            if self.mode != "SHADOW":
                raise ConfigError("MAXIMUM_INVESTMENT_REQUIRED_FOR_PAPER_SIMULATE")
        elif (
            type(self.maximum_investment_cents) is not int
            or self.maximum_investment_cents <= 0
        ):
            raise ConfigError("INVALID_MAXIMUM_INVESTMENT_CENTS")

        exact_numbers = {
            "maximum_symbol_notional_fraction": (
                self.maximum_symbol_notional_fraction,
                0.1,
            ),
            "maximum_total_notional_fraction": (self.maximum_total_notional_fraction, 0.2),
            "rsi_oversold": (self.rsi_oversold, 30.0),
            "rsi_recovery": (self.rsi_recovery, 35.0),
            "rsi_exit": (self.rsi_exit, 60.0),
            "atr_stop_multiple": (self.atr_stop_multiple, 1.5),
        }
        for name, (value, expected) in exact_numbers.items():
            if type(value) not in (int, float) or not math.isfinite(float(value)):
                raise ConfigError(f"INVALID_{name.upper()}")
            if float(value) != expected:
                raise ConfigError(f"FROZEN_{name.upper()}_MISMATCH")

    def to_dict(self) -> Dict[str, Any]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    def to_risk_policy(self) -> RiskPolicy:
        """Build the exact runtime policy represented by this configuration."""

        return RiskPolicy(
            risk_policy_version=self.risk_policy_version,
            planned_risk_basis_points=self.planned_risk_basis_points,
            daily_loss_limit_basis_points=self.daily_loss_limit_basis_points,
            weekly_loss_limit_basis_points=self.weekly_loss_limit_basis_points,
            maximum_investment_cents=self.maximum_investment_cents,
            max_notional_fraction=self.maximum_symbol_notional_fraction,
            max_roundtrips_per_day=self.maximum_round_trips_per_day,
        )


def parse_system_config(value: Mapping[str, Any]) -> SystemConfig:
    if not isinstance(value, Mapping):
        raise ConfigError("CONFIG_MUST_BE_OBJECT")
    expected = {field.name for field in fields(SystemConfig)}
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ConfigError(f"CONFIG_FIELDS_MISMATCH missing={missing} extra={extra}")
    try:
        return SystemConfig(**dict(value))
    except TypeError as exc:
        raise ConfigError("CONFIG_TYPE_ERROR") from exc


def load_system_config(path: Path) -> SystemConfig:
    try:
        raw = Path(path).read_text(encoding="utf-8")
        value = json.loads(raw, parse_constant=lambda token: (_ for _ in ()).throw(ConfigError(token)))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError("CONFIG_READ_FAILED") from exc
    return parse_system_config(value)


__all__ = ["ConfigError", "SystemConfig", "load_system_config", "parse_system_config"]
