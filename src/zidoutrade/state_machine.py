"""Explicit two-axis runtime state machine.

Control intent (armed/paused/emergency) and broker exposure are deliberately
independent.  For example, pausing entries cannot erase a pending exit.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
import re
from typing import Any, Dict, FrozenSet, Mapping, Optional

from .storage import IntegrityError, atomic_write_json, canonical_sha256, read_json


class StateTransitionError(RuntimeError):
    """Raised when a transition is not explicitly allowed."""


class StaleRevisionError(StateTransitionError):
    """Raised when a caller attempts to overwrite a newer state snapshot."""


class ControlState(str, Enum):
    DISARMED = "DISARMED"
    ARMED = "ARMED"
    PAUSED = "PAUSED"
    HALTED = "HALTED"
    EMERGENCY = "EMERGENCY"


class ExposureState(str, Enum):
    FLAT = "FLAT"
    ENTRY_INTENT_DURABLE = "ENTRY_INTENT_DURABLE"
    ENTRY_PENDING = "ENTRY_PENDING"
    ENTRY_RECONCILING = "ENTRY_RECONCILING"
    LONG_UNPROTECTED = "LONG_UNPROTECTED"
    LONG_GUARDED_LOCAL_ONLY = "LONG_GUARDED_LOCAL_ONLY"
    PARTIAL_POSITION = "PARTIAL_POSITION"
    EXIT_INTENT_DURABLE = "EXIT_INTENT_DURABLE"
    EXIT_PENDING = "EXIT_PENDING"
    EXIT_RECONCILING = "EXIT_RECONCILING"
    CANCEL_PENDING = "CANCEL_PENDING"
    FLATTEN_INTENT_DURABLE = "FLATTEN_INTENT_DURABLE"
    FLATTEN_PENDING = "FLATTEN_PENDING"
    FLATTEN_RECONCILING = "FLATTEN_RECONCILING"
    COMPLETE = "COMPLETE"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


CONTROL_TRANSITIONS: Mapping[ControlState, FrozenSet[ControlState]] = {
    ControlState.DISARMED: frozenset(
        {ControlState.ARMED, ControlState.HALTED, ControlState.EMERGENCY}
    ),
    ControlState.ARMED: frozenset(
        {
            ControlState.DISARMED,
            ControlState.PAUSED,
            ControlState.HALTED,
            ControlState.EMERGENCY,
        }
    ),
    ControlState.PAUSED: frozenset(
        {
            ControlState.ARMED,
            ControlState.DISARMED,
            ControlState.HALTED,
            ControlState.EMERGENCY,
        }
    ),
    ControlState.HALTED: frozenset(
        {ControlState.DISARMED, ControlState.EMERGENCY}
    ),
    ControlState.EMERGENCY: frozenset({ControlState.HALTED}),
}


EXPOSURE_TRANSITIONS: Mapping[ExposureState, FrozenSet[ExposureState]] = {
    ExposureState.FLAT: frozenset(
        {ExposureState.ENTRY_INTENT_DURABLE, ExposureState.RECOVERY_REQUIRED}
    ),
    ExposureState.ENTRY_INTENT_DURABLE: frozenset(
        {
            ExposureState.ENTRY_PENDING,
            ExposureState.ENTRY_RECONCILING,
            ExposureState.LONG_GUARDED_LOCAL_ONLY,
            ExposureState.PARTIAL_POSITION,
            ExposureState.RECOVERY_REQUIRED,
        }
    ),
    ExposureState.ENTRY_PENDING: frozenset(
        {
            ExposureState.ENTRY_RECONCILING,
            ExposureState.LONG_UNPROTECTED,
            ExposureState.LONG_GUARDED_LOCAL_ONLY,
            ExposureState.PARTIAL_POSITION,
            ExposureState.CANCEL_PENDING,
            ExposureState.RECOVERY_REQUIRED,
        }
    ),
    ExposureState.ENTRY_RECONCILING: frozenset(
        {
            ExposureState.FLAT,
            ExposureState.ENTRY_PENDING,
            ExposureState.LONG_UNPROTECTED,
            ExposureState.LONG_GUARDED_LOCAL_ONLY,
            ExposureState.PARTIAL_POSITION,
            ExposureState.CANCEL_PENDING,
            ExposureState.RECOVERY_REQUIRED,
        }
    ),
    ExposureState.LONG_UNPROTECTED: frozenset(
        {
            ExposureState.LONG_GUARDED_LOCAL_ONLY,
            ExposureState.EXIT_INTENT_DURABLE,
            ExposureState.FLATTEN_INTENT_DURABLE,
            ExposureState.RECOVERY_REQUIRED,
        }
    ),
    ExposureState.LONG_GUARDED_LOCAL_ONLY: frozenset(
        {
            ExposureState.EXIT_INTENT_DURABLE,
            ExposureState.FLATTEN_INTENT_DURABLE,
            ExposureState.RECOVERY_REQUIRED,
        }
    ),
    ExposureState.PARTIAL_POSITION: frozenset(
        {
            ExposureState.CANCEL_PENDING,
            ExposureState.PARTIAL_POSITION,
            ExposureState.EXIT_INTENT_DURABLE,
            ExposureState.FLATTEN_INTENT_DURABLE,
            ExposureState.RECOVERY_REQUIRED,
        }
    ),
    ExposureState.EXIT_INTENT_DURABLE: frozenset(
        {
            ExposureState.EXIT_PENDING,
            ExposureState.EXIT_RECONCILING,
            ExposureState.COMPLETE,
            ExposureState.PARTIAL_POSITION,
            ExposureState.RECOVERY_REQUIRED,
        }
    ),
    ExposureState.EXIT_PENDING: frozenset(
        {
            ExposureState.EXIT_RECONCILING,
            ExposureState.COMPLETE,
            ExposureState.PARTIAL_POSITION,
            ExposureState.CANCEL_PENDING,
            ExposureState.RECOVERY_REQUIRED,
        }
    ),
    ExposureState.EXIT_RECONCILING: frozenset(
        {
            ExposureState.COMPLETE,
            ExposureState.PARTIAL_POSITION,
            ExposureState.CANCEL_PENDING,
            ExposureState.RECOVERY_REQUIRED,
        }
    ),
    ExposureState.CANCEL_PENDING: frozenset(
        {
            ExposureState.ENTRY_RECONCILING,
            ExposureState.EXIT_RECONCILING,
            ExposureState.FLATTEN_RECONCILING,
            ExposureState.LONG_UNPROTECTED,
            ExposureState.LONG_GUARDED_LOCAL_ONLY,
            ExposureState.PARTIAL_POSITION,
            ExposureState.COMPLETE,
            ExposureState.RECOVERY_REQUIRED,
        }
    ),
    ExposureState.FLATTEN_INTENT_DURABLE: frozenset(
        {
            ExposureState.FLATTEN_PENDING,
            ExposureState.FLATTEN_RECONCILING,
            ExposureState.COMPLETE,
            ExposureState.PARTIAL_POSITION,
            ExposureState.RECOVERY_REQUIRED,
        }
    ),
    ExposureState.FLATTEN_PENDING: frozenset(
        {
            ExposureState.FLATTEN_RECONCILING,
            ExposureState.COMPLETE,
            ExposureState.PARTIAL_POSITION,
            ExposureState.CANCEL_PENDING,
            ExposureState.RECOVERY_REQUIRED,
        }
    ),
    ExposureState.FLATTEN_RECONCILING: frozenset(
        {
            ExposureState.COMPLETE,
            ExposureState.PARTIAL_POSITION,
            ExposureState.CANCEL_PENDING,
            ExposureState.RECOVERY_REQUIRED,
        }
    ),
    ExposureState.COMPLETE: frozenset(
        {ExposureState.FLAT, ExposureState.RECOVERY_REQUIRED}
    ),
    ExposureState.RECOVERY_REQUIRED: frozenset(
        {
            ExposureState.FLAT,
            ExposureState.ENTRY_RECONCILING,
            ExposureState.EXIT_RECONCILING,
            ExposureState.FLATTEN_RECONCILING,
            ExposureState.LONG_UNPROTECTED,
            ExposureState.LONG_GUARDED_LOCAL_ONLY,
            ExposureState.PARTIAL_POSITION,
            ExposureState.COMPLETE,
        }
    ),
}


_SYMBOL = re.compile(r"^US\.[A-Z0-9][A-Z0-9._-]{0,31}$")


@dataclass(frozen=True)
class RuntimeState:
    control: ControlState = ControlState.DISARMED
    exposure: ExposureState = ExposureState.FLAT
    revision: int = 0
    session_id: Optional[str] = None
    selected_symbol: Optional[str] = None
    active_intent_id: Optional[str] = None
    exit_dispatches: int = 0
    reconciled_position_qty: int = 0
    account_fingerprint: Optional[str] = None
    last_order_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise ValueError("revision cannot be negative")
        if self.selected_symbol is not None and not _SYMBOL.fullmatch(
            self.selected_symbol
        ):
            raise ValueError("selected_symbol must be a normalized US symbol")
        if self.exit_dispatches < 0 or self.exit_dispatches > 2:
            raise ValueError("exit_dispatches must be between zero and two")
        if self.reconciled_position_qty < 0:
            raise ValueError("short/negative positions are unsupported")
        if self.account_fingerprint is not None and not re.fullmatch(
            r"[0-9a-f]{64}", self.account_fingerprint
        ):
            raise ValueError("account_fingerprint must be a SHA-256 hex digest")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account_fingerprint": self.account_fingerprint,
            "active_intent_id": self.active_intent_id,
            "control": self.control.value,
            "exit_dispatches": self.exit_dispatches,
            "exposure": self.exposure.value,
            "last_order_id": self.last_order_id,
            "reconciled_position_qty": self.reconciled_position_qty,
            "revision": self.revision,
            "selected_symbol": self.selected_symbol,
            "session_id": self.session_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RuntimeState":
        try:
            return cls(
                control=ControlState(value["control"]),
                exposure=ExposureState(value["exposure"]),
                revision=int(value["revision"]),
                session_id=value.get("session_id"),
                selected_symbol=value.get("selected_symbol"),
                active_intent_id=value.get("active_intent_id"),
                exit_dispatches=int(value.get("exit_dispatches", 0)),
                reconciled_position_qty=int(
                    value.get("reconciled_position_qty", 0)
                ),
                account_fingerprint=value.get("account_fingerprint"),
                last_order_id=value.get("last_order_id"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise IntegrityError("invalid runtime state") from exc


def transition_control(state: RuntimeState, target: ControlState) -> RuntimeState:
    target = ControlState(target)
    if target not in CONTROL_TRANSITIONS[state.control]:
        raise StateTransitionError(
            f"illegal control transition: {state.control.value} -> {target.value}"
        )
    return replace(state, control=target, revision=state.revision + 1)


def transition_exposure(state: RuntimeState, target: ExposureState) -> RuntimeState:
    target = ExposureState(target)
    if target not in EXPOSURE_TRANSITIONS[state.exposure]:
        raise StateTransitionError(
            f"illegal exposure transition: {state.exposure.value} -> {target.value}"
        )
    return replace(state, exposure=target, revision=state.revision + 1)


def evolve_state(state: RuntimeState, **changes: Any) -> RuntimeState:
    """Update metadata while monotonically advancing the durable revision."""

    if "revision" in changes:
        raise ValueError("revision is managed by the state machine")
    return replace(state, revision=state.revision + 1, **changes)


class RuntimeStateStore:
    SCHEMA = 1

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> RuntimeState:
        envelope = read_json(self.path)
        if not isinstance(envelope, dict) or envelope.get("schema") != self.SCHEMA:
            raise IntegrityError("invalid runtime state envelope")
        state_value = envelope.get("state")
        if not isinstance(state_value, dict):
            raise IntegrityError("runtime state payload is missing")
        expected = canonical_sha256(state_value)
        if envelope.get("state_hash") != expected:
            raise IntegrityError("runtime state hash mismatch")
        return RuntimeState.from_dict(state_value)

    def load_or_initialize(self, initial: Optional[RuntimeState] = None) -> RuntimeState:
        if self.path.exists():
            return self.load()
        state = initial or RuntimeState()
        self.save(state, expected_revision=None)
        return state

    def save(
        self, state: RuntimeState, *, expected_revision: Optional[int]
    ) -> None:
        if self.path.exists() and expected_revision is not None:
            current = self.load()
            if current.revision != expected_revision:
                raise StaleRevisionError(
                    f"expected revision {expected_revision}, found {current.revision}"
                )
        state_value = state.to_dict()
        envelope = {
            "schema": self.SCHEMA,
            "state": state_value,
            "state_hash": canonical_sha256(state_value),
        }
        atomic_write_json(self.path, envelope)


__all__ = [
    "CONTROL_TRANSITIONS",
    "EXPOSURE_TRANSITIONS",
    "ControlState",
    "ExposureState",
    "RuntimeState",
    "RuntimeStateStore",
    "StaleRevisionError",
    "StateTransitionError",
    "evolve_state",
    "transition_control",
    "transition_exposure",
]
