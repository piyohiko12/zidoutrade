"""Fail-closed orchestration for deterministic signals and a paper broker.

The runner provides at-most-once *dispatch attempts*.  It does not claim
broker-side exactly-once execution: a lost acknowledgement moves the durable
state to ``RECOVERY_REQUIRED`` and is never guessed or automatically retried.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from pathlib import Path
import os
import re
import secrets
import stat
from typing import Optional

from .activation import (
    ActivationError,
    ActivationProof,
    ActivationVerifier,
)

from .broker import (
    AmbiguousBrokerResponse,
    BrokerError,
    BrokerSafetyError,
    LimitOrderRequest,
    OrderRecord,
    PaperBroker,
    ReconciliationSnapshot,
    Side,
)
from .execution import (
    DecisionKind,
    ExecutionDecision,
    ExecutionEvidenceError,
    verify_execution_decision,
)
from .state_machine import (
    ControlState,
    ExposureState,
    RuntimeState,
    RuntimeStateStore,
    StateTransitionError,
    evolve_state,
    transition_control,
    transition_exposure,
)
from .storage import (
    ExclusiveFileLock,
    HashChainJournal,
    IntentStore,
    LockAlreadyHeld,
)


class RunnerError(RuntimeError):
    """Base class for lifecycle/orchestration failures."""


class RunnerSafetyError(RunnerError):
    """Raised before a forbidden or unproven action is attempted."""


class ExecutionMode(str, Enum):
    SHADOW = "SHADOW"
    PAPER_SIMULATE = "PAPER_SIMULATE"
    # Backward-compatible spelling for early internal tests; both names are
    # the same enum member and match SystemConfig's persisted policy value.
    SIMULATE_ACTIVE = "PAPER_SIMULATE"


@dataclass(frozen=True)
class RuntimePaths:
    root: Path

    @property
    def state(self) -> Path:
        return self.root / "state.json"

    @property
    def journal(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def intents(self) -> Path:
        return self.root / "intents"

    @property
    def writer_lock(self) -> Path:
        return self.root / "writer.lock"


_PENDING_STATUS_PARTS = (
    "WAITING_SUBMIT",
    "SUBMITTING",
    "SUBMITTED",
    "CANCELLING_PART",
    "CANCELLING_ALL",
)
_FILLED_STATUS_PARTS = ("FILLED_ALL",)
_TERMINAL_FAILURE_PARTS = (
    "SUBMIT_FAILED",
    "CANCELLED_PART",
    "CANCELLED_ALL",
    "FAILED",
    "DISABLED",
    "DELETED",
    "FILL_CANCELLED",
)


def _status_has(status: str, pieces: tuple[str, ...]) -> bool:
    return type(status) is str and status.strip().upper() in pieces


class TradingRunner:
    """One-writer runtime coordinator for a single frozen symbol/session."""

    def __init__(
        self,
        *,
        runtime_root: Path,
        broker: PaperBroker,
        mode: ExecutionMode = ExecutionMode.SHADOW,
        activation_verifier: Optional[ActivationVerifier] = None,
    ) -> None:
        try:
            self.mode = ExecutionMode(mode)
        except ValueError as exc:
            raise RunnerSafetyError("unsupported execution mode") from exc
        requested_root = Path(runtime_root).expanduser()
        if requested_root.is_symlink():
            raise RunnerSafetyError("runtime root must not be a symlink")
        resolved_root = requested_root.resolve(strict=False)
        for ancestor in (resolved_root, *resolved_root.parents):
            if (ancestor / ".git").exists():
                raise RunnerSafetyError("runtime root must be outside a Git checkout")
        resolved_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        root_stat = resolved_root.stat()
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or root_stat.st_uid != os.getuid()
            or stat.S_IMODE(root_stat.st_mode) & 0o077
        ):
            raise RunnerSafetyError(
                "runtime root must be owned by this user with mode 0700"
            )
        self.paths = RuntimePaths(resolved_root)
        self.broker = broker
        if (
            activation_verifier is not None
            and type(activation_verifier) is not ActivationVerifier
        ):
            raise RunnerSafetyError(
                "activation_verifier must be an exact ActivationVerifier"
            )
        if (
            activation_verifier is not None
            and activation_verifier.runtime_root != resolved_root
        ):
            raise RunnerSafetyError(
                "activation verifier and runner runtime roots must match exactly"
            )
        self.activation_verifier = activation_verifier
        self.state_store = RuntimeStateStore(self.paths.state)
        self.intent_store = IntentStore(self.paths.intents)
        self.journal = HashChainJournal(self.paths.journal)
        self.writer_lock = ExclusiveFileLock(
            self.paths.writer_lock, purpose="trading-runner"
        )
        self.state: Optional[RuntimeState] = None
        self._started = False

    def __enter__(self) -> "TradingRunner":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _require_started(self) -> RuntimeState:
        if not self._started or self.state is None:
            raise RunnerError("runner has not completed startup reconciliation")
        return self.state

    def _save(self, next_state: RuntimeState, event_type: str, payload: dict) -> None:
        previous = self.state
        expected = previous.revision if previous is not None else None
        self.state_store.save(next_state, expected_revision=expected)
        self.state = next_state
        self.journal.append(
            event_type,
            {
                **payload,
                "control": next_state.control.value,
                "exposure": next_state.exposure.value,
                "revision": next_state.revision,
                "session_id": next_state.session_id,
                "symbol": next_state.selected_symbol,
                "active_intent_id": next_state.active_intent_id,
            },
        )

    def _has_unjournaled_intent(self) -> bool:
        """Detect a crash after O_EXCL reservation but before state publication."""

        state = self._require_started()
        referenced = {
            str(record.get("payload", {}).get("active_intent_id"))
            for record in self.journal.read_all()
            if isinstance(record.get("payload"), dict)
            and record["payload"].get("active_intent_id") is not None
        }
        if state.active_intent_id is not None:
            referenced.add(state.active_intent_id)
        return any(
            reservation.intent_id not in referenced
            for reservation in self.intent_store.all()
        )

    def _move_exposure(
        self, target: ExposureState, event_type: str, **metadata: object
    ) -> RuntimeState:
        state = self._require_started()
        if target is state.exposure:
            next_state = evolve_state(state, **metadata)
        else:
            next_state = transition_exposure(state, target)
            if metadata:
                next_state = evolve_state(next_state, **metadata)
        self._save(next_state, event_type, {"target": target.value})
        return next_state

    @staticmethod
    def _validate_identity(
        session_id: str, symbol: str, account_fingerprint: str
    ) -> None:
        if type(session_id) is not str:
            raise RunnerSafetyError("invalid session id")
        try:
            parsed_session = date.fromisoformat(session_id)
        except ValueError as exc:
            raise RunnerSafetyError("session id must be an exact YYYY-MM-DD date") from exc
        if parsed_session.isoformat() != session_id:
            raise RunnerSafetyError("session id must be an exact YYYY-MM-DD date")
        if not re.fullmatch(r"US\.[A-Z0-9][A-Z0-9._-]{0,31}", symbol):
            raise RunnerSafetyError("invalid selected symbol")
        if not re.fullmatch(r"[0-9a-f]{64}", account_fingerprint):
            raise RunnerSafetyError("invalid keyed account fingerprint")

    def start(
        self,
        *,
        session_id: str,
        selected_symbol: str,
        account_fingerprint: str,
        activation_proof: Optional[ActivationProof] = None,
    ) -> RuntimeState:
        """Acquire the sole-writer lock and reconcile before accepting actions."""

        if self._started:
            raise RunnerError("runner is already started")
        self._validate_identity(session_id, selected_symbol, account_fingerprint)
        if self.mode is ExecutionMode.SIMULATE_ACTIVE:
            self._verify_activation_identity(
                activation_proof,
                session_id=session_id,
                selected_symbol=selected_symbol,
                account_fingerprint=account_fingerprint,
            )
        self.writer_lock.acquire()
        try:
            if self.paths.state.exists():
                existing = self.state_store.load()
                self.state = existing
                # Startup always queries the symbol represented by durable state
                # before any new session/selection can replace it.
                if existing.selected_symbol is None:
                    raise RunnerSafetyError("durable state has no selected symbol")
                self._started = True
                if self._has_unjournaled_intent():
                    self._move_exposure(
                        ExposureState.RECOVERY_REQUIRED,
                        "UNJOURNALED_INTENT_DETECTED",
                    )
                self.reconcile(reason="STARTUP")
                existing = self._require_started()
                identity_changed = (
                    existing.session_id != session_id
                    or existing.selected_symbol != selected_symbol
                    or existing.account_fingerprint != account_fingerprint
                )
                if identity_changed:
                    if existing.session_id == session_id:
                        raise RunnerSafetyError(
                            "session account and symbol are immutable until the next date"
                        )
                    if (
                        existing.exposure not in {ExposureState.FLAT, ExposureState.COMPLETE}
                        or existing.reconciled_position_qty != 0
                        or existing.active_intent_id is not None
                    ):
                        self._move_exposure(
                            ExposureState.RECOVERY_REQUIRED,
                            "SESSION_IDENTITY_MISMATCH",
                        )
                        raise RunnerSafetyError(
                            "cannot replace session/account/symbol with unresolved state"
                        )
                    if existing.exposure is ExposureState.COMPLETE:
                        self._move_exposure(
                            ExposureState.FLAT, "PREPARE_NEXT_SESSION"
                        )
                    existing = self._require_started()
                    next_state = evolve_state(
                        existing,
                        session_id=session_id,
                        selected_symbol=selected_symbol,
                        account_fingerprint=account_fingerprint,
                        active_intent_id=None,
                        exit_dispatches=0,
                        reconciled_position_qty=0,
                        last_order_id=None,
                    )
                    self._save(next_state, "SESSION_STARTED", {})
                    self.reconcile(reason="SESSION")
                return self._require_started()

            initial = RuntimeState(
                control=ControlState.DISARMED,
                exposure=ExposureState.FLAT,
                revision=0,
                session_id=session_id,
                selected_symbol=selected_symbol,
                account_fingerprint=account_fingerprint,
            )
            self.state_store.save(initial, expected_revision=None)
            self.state = initial
            self._started = True
            self.journal.append(
                "RUNTIME_INITIALIZED",
                {
                    "control": initial.control.value,
                    "exposure": initial.exposure.value,
                    "revision": initial.revision,
                    "session_id": session_id,
                    "symbol": selected_symbol,
                },
            )
            if self._has_unjournaled_intent():
                self._move_exposure(
                    ExposureState.RECOVERY_REQUIRED,
                    "UNJOURNALED_INTENT_DETECTED",
                )
            return self.reconcile(reason="STARTUP")
        except BaseException:
            if self._started:
                self._started = False
            self.state = None
            self.writer_lock.release()
            raise

    def close(self) -> None:
        if self.writer_lock.token is not None:
            self.writer_lock.release()
        self._started = False
        self.state = None

    def set_control(self, target: ControlState) -> RuntimeState:
        state = self._require_started()
        try:
            next_state = transition_control(state, ControlState(target))
        except (ValueError, StateTransitionError) as exc:
            raise RunnerSafetyError(str(exc)) from exc
        self._save(next_state, "CONTROL_CHANGED", {"target": next_state.control.value})
        return next_state

    def _matching_active_order(
        self, snapshot: ReconciliationSnapshot
    ) -> Optional[OrderRecord]:
        state = self._require_started()
        if state.active_intent_id is None:
            return None
        marker = state.active_intent_id
        reservation = self.intent_store.get(marker)
        if reservation is None:
            return None
        expected_remark = reservation.document.get("remark")
        if not isinstance(expected_remark, str):
            return None
        matches = [order for order in snapshot.orders if order.remark == expected_remark]
        if len(matches) > 1:
            raise AmbiguousBrokerResponse("multiple orders match one durable intent")
        return matches[0] if matches else None

    def _has_unknown_live_order(
        self, snapshot: ReconciliationSnapshot, matched: Optional[OrderRecord]
    ) -> bool:
        known_remarks = {
            str(reservation.document.get("remark"))
            for reservation in self.intent_store.all()
            if isinstance(reservation.document.get("remark"), str)
        }
        for order in snapshot.orders:
            if matched is not None and order.order_id == matched.order_id:
                continue
            # Any selected-symbol order not backed by a durable local intent is
            # ownership ambiguity, including a completed external round trip
            # whose net position is flat.  Ignoring filled unknowns would reset
            # the one-round-trip/day boundary.
            if order.remark not in known_remarks:
                return True
        return False

    def reconcile(self, *, reason: str = "POLL") -> RuntimeState:
        """Query both selected-symbol orders and position; never dispatch here."""

        state = self._require_started()
        assert state.selected_symbol is not None
        try:
            snapshot = self.broker.reconcile(state.selected_symbol)
            if (
                snapshot.symbol != state.selected_symbol
                or snapshot.position.symbol != state.selected_symbol
                or snapshot.position.quantity < 0
            ):
                raise AmbiguousBrokerResponse("reconciliation identity mismatch")
            matched = self._matching_active_order(snapshot)
            if self._has_unknown_live_order(snapshot, matched):
                return self._move_exposure(
                    ExposureState.RECOVERY_REQUIRED,
                    "RECONCILE_UNKNOWN_ORDER",
                    reconciled_position_qty=snapshot.position.quantity,
                )

            quantity = snapshot.position.quantity
            if state.active_intent_id is not None:
                reservation = self.intent_store.get(state.active_intent_id)
                if reservation is None:
                    return self._move_exposure(
                        ExposureState.RECOVERY_REQUIRED,
                        "RECONCILE_MISSING_INTENT",
                        reconciled_position_qty=quantity,
                    )
                side = Side(str(reservation.document.get("side")))
                expected_qty = int(reservation.document.get("quantity", -1))
                if state.exposure is ExposureState.RECOVERY_REQUIRED:
                    if side is Side.BUY:
                        reconciling = ExposureState.ENTRY_RECONCILING
                    elif bool(reservation.document.get("flatten")):
                        reconciling = ExposureState.FLATTEN_RECONCILING
                    else:
                        reconciling = ExposureState.EXIT_RECONCILING
                    self._move_exposure(
                        reconciling,
                        "RECOVERY_QUERY_ONLY_RECONCILIATION",
                        reconciled_position_qty=quantity,
                    )
                    state = self._require_started()
                if matched is None:
                    return self._move_exposure(
                        ExposureState.RECOVERY_REQUIRED,
                        "RECONCILE_MISSING_ORDER",
                        reconciled_position_qty=quantity,
                    )
                if (
                    matched.symbol != state.selected_symbol
                    or matched.side is not side
                    or matched.quantity != expected_qty
                    or matched.remark != reservation.document.get("remark")
                ):
                    return self._move_exposure(
                        ExposureState.RECOVERY_REQUIRED,
                        "RECONCILE_INTENT_MISMATCH",
                        reconciled_position_qty=quantity,
                    )
                terminal_failure = _status_has(
                    matched.status, _TERMINAL_FAILURE_PARTS
                )
                # Terminal status always wins over overlapping text such as
                # SUBMIT_FAILED; it must never remain pending by substring.
                pending = not terminal_failure and _status_has(
                    matched.status, _PENDING_STATUS_PARTS
                )
                fully_filled = not terminal_failure and _status_has(
                    matched.status, _FILLED_STATUS_PARTS
                )
                if matched.filled_quantity not in (0, matched.quantity):
                    return self._move_exposure(
                        ExposureState.PARTIAL_POSITION,
                        "RECONCILE_PARTIAL",
                        reconciled_position_qty=quantity,
                        last_order_id=matched.order_id,
                    )
                if side is Side.BUY:
                    if fully_filled and matched.filled_quantity == expected_qty and quantity == expected_qty:
                        return self._move_exposure(
                            ExposureState.LONG_GUARDED_LOCAL_ONLY,
                            "ENTRY_FILLED",
                            reconciled_position_qty=quantity,
                            active_intent_id=None,
                            last_order_id=matched.order_id,
                        )
                    if pending and matched.filled_quantity == 0 and quantity == 0:
                        return self._move_exposure(
                            ExposureState.ENTRY_PENDING,
                            "ENTRY_STILL_PENDING",
                            reconciled_position_qty=0,
                            last_order_id=matched.order_id,
                        )
                else:
                    if fully_filled and matched.filled_quantity == expected_qty and quantity == 0:
                        return self._move_exposure(
                            ExposureState.COMPLETE,
                            "EXIT_FILLED",
                            reconciled_position_qty=0,
                            active_intent_id=None,
                            last_order_id=matched.order_id,
                        )
                    if fully_filled and matched.filled_quantity == expected_qty and quantity > 0:
                        return self._move_exposure(
                            ExposureState.PARTIAL_POSITION,
                            "EXIT_SLICE_FILLED",
                            reconciled_position_qty=quantity,
                            active_intent_id=None,
                            last_order_id=matched.order_id,
                        )
                    if pending and matched.filled_quantity == 0 and quantity > 0:
                        target = (
                            ExposureState.FLATTEN_PENDING
                            if state.exposure
                            in {
                                ExposureState.FLATTEN_INTENT_DURABLE,
                                ExposureState.FLATTEN_PENDING,
                                ExposureState.FLATTEN_RECONCILING,
                            }
                            else ExposureState.EXIT_PENDING
                        )
                        return self._move_exposure(
                            target,
                            "EXIT_STILL_PENDING",
                            reconciled_position_qty=quantity,
                            last_order_id=matched.order_id,
                        )
                if terminal_failure or not pending:
                    return self._move_exposure(
                        ExposureState.RECOVERY_REQUIRED,
                        "RECONCILE_TERMINAL_OR_AMBIGUOUS",
                        reconciled_position_qty=quantity,
                        last_order_id=matched.order_id,
                    )

            if quantity == 0:
                if state.exposure is ExposureState.COMPLETE:
                    next_state = evolve_state(state, reconciled_position_qty=0)
                    self._save(next_state, "RECONCILED", {"reason": reason})
                    return next_state
                if state.exposure is ExposureState.FLAT:
                    next_state = evolve_state(state, reconciled_position_qty=0)
                    self._save(next_state, "RECONCILED", {"reason": reason})
                    return next_state
                return self._move_exposure(
                    ExposureState.RECOVERY_REQUIRED,
                    "RECONCILE_UNEXPECTED_FLAT",
                    reconciled_position_qty=0,
                )

            if state.exposure in {
                ExposureState.LONG_UNPROTECTED,
                ExposureState.LONG_GUARDED_LOCAL_ONLY,
                ExposureState.PARTIAL_POSITION,
            }:
                if state.exposure is ExposureState.PARTIAL_POSITION:
                    return self._move_exposure(
                        ExposureState.PARTIAL_POSITION,
                        "RECONCILED_PARTIAL_POSITION",
                        reconciled_position_qty=quantity,
                    )
                return self._move_exposure(
                    ExposureState.LONG_GUARDED_LOCAL_ONLY,
                    "RECONCILED_LONG",
                    reconciled_position_qty=quantity,
                )
            return self._move_exposure(
                ExposureState.RECOVERY_REQUIRED,
                "RECONCILE_UNEXPECTED_POSITION",
                reconciled_position_qty=quantity,
            )
        except BrokerError:
            try:
                return self._move_exposure(
                    ExposureState.RECOVERY_REQUIRED, "RECONCILIATION_FAILED"
                )
            except StateTransitionError:
                raise RunnerSafetyError("reconciliation failed and recovery is required")

    def poll_pending(self) -> RuntimeState:
        """Required poll hook for every pending/reconciling lifecycle tick."""

        return self.reconcile(reason="PENDING_POLL")

    def _verify_activation_identity(
        self,
        proof: Optional[ActivationProof],
        *,
        session_id: str,
        selected_symbol: str,
        account_fingerprint: str,
    ) -> ActivationProof:
        if type(self.activation_verifier) is not ActivationVerifier:
            raise RunnerSafetyError("local activation verifier is absent")
        if type(proof) is not ActivationProof:
            raise RunnerSafetyError("dispatch requires an exact ActivationProof")
        if (
            proof.runtime_root != str(self.paths.root)
            or proof.session_id != session_id
            or proof.selected_symbol != selected_symbol
            or proof.account_fingerprint != account_fingerprint
        ):
            raise RunnerSafetyError("activation proof differs from runtime identity")
        try:
            self.activation_verifier.verify_proof(proof)
            self.activation_verifier.verify_broker_account(proof, self.broker)
        except ActivationError as exc:
            raise RunnerSafetyError("activation/account proof could not be verified") from exc
        return proof

    def _require_dispatch_enabled(
        self, proof: Optional[ActivationProof]
    ) -> tuple[RuntimeState, ActivationProof]:
        state = self._require_started()
        if self.mode is not ExecutionMode.SIMULATE_ACTIVE:
            raise RunnerSafetyError("SHADOW mode cannot dispatch orders")
        # STOP-SHIP boundary mirrored by MoomooPaperBroker.place_limit.  Typed
        # proofs/decisions are implemented for offline verification, but this
        # release deliberately cannot cross into a broker order RPC until the
        # remaining ownership-ledger and fresh-process attestation work lands.
        raise RunnerSafetyError(
            "SIMULATE order dispatch is disabled in this reviewed release"
        )
        # The unreachable code is retained as the exact future verification
        # sequence and remains covered by pure unit tests.
        assert state.session_id is not None
        assert state.selected_symbol is not None
        assert state.account_fingerprint is not None
        verified = self._verify_activation_identity(
            proof,
            session_id=state.session_id,
            selected_symbol=state.selected_symbol,
            account_fingerprint=state.account_fingerprint,
        )
        return state, verified

    def _verify_decision(
        self,
        *,
        proof: ActivationProof,
        decision: ExecutionDecision,
        expected_kind: DecisionKind,
        expected_position_quantity: Optional[int] = None,
        expected_exit_dispatches: Optional[int] = None,
    ) -> None:
        state = self._require_started()
        assert state.session_id is not None and state.selected_symbol is not None
        if type(self.activation_verifier) is not ActivationVerifier:
            raise RunnerSafetyError("local activation verifier is absent")
        try:
            verify_execution_decision(
                verifier=self.activation_verifier,
                proof=proof,
                decision=decision,
                expected_kind=expected_kind,
                expected_session_id=state.session_id,
                expected_symbol=state.selected_symbol,
                expected_position_quantity=expected_position_quantity,
                expected_exit_dispatches=expected_exit_dispatches,
            )
        except ExecutionEvidenceError as exc:
            raise RunnerSafetyError("execution decision could not be verified") from exc

    @staticmethod
    def new_intent_id(session_id: str, side: Side) -> str:
        compact = re.sub(r"[^A-Za-z0-9]", "", session_id)[:16] or "SESSION"
        return f"{compact}-{side.value}-{secrets.token_hex(8)}"

    def _reserve_intent(
        self,
        *,
        side: Side,
        quantity: int,
        limit_price: Decimal,
        intent_id: Optional[str],
        flatten: bool,
        activation_proof_sha256: str,
        execution_decision_sha256: str,
    ) -> LimitOrderRequest:
        state = self._require_started()
        assert state.session_id is not None and state.selected_symbol is not None
        chosen_id = intent_id or self.new_intent_id(state.session_id, side)
        remark = f"RSI1-{chosen_id}"
        request = LimitOrderRequest(
            intent_id=chosen_id,
            symbol=state.selected_symbol,
            side=side,
            quantity=quantity,
            limit_price=Decimal(str(limit_price)),
            remark=remark,
        )
        document = {
            "account_fingerprint": state.account_fingerprint,
            "activation_proof_sha256": activation_proof_sha256,
            "execution_decision_sha256": execution_decision_sha256,
            "flatten": flatten,
            "limit_price": str(request.limit_price),
            "quantity": request.quantity,
            "remark": request.remark,
            "session_id": state.session_id,
            "side": side.value,
            "symbol": state.selected_symbol,
        }
        self.intent_store.reserve(chosen_id, document)
        return request

    def dispatch_entry(
        self,
        *,
        activation_proof: ActivationProof,
        decision: ExecutionDecision,
        intent_id: Optional[str] = None,
    ) -> OrderRecord:
        """Reconcile, reserve durably, then attempt one BUY dispatch."""

        _, proof = self._require_dispatch_enabled(activation_proof)
        state = self.reconcile(reason="ENTRY_ACTION")
        if state.control is not ControlState.ARMED:
            raise RunnerSafetyError("new entries require ARMED control state")
        if state.exposure is not ExposureState.FLAT or state.reconciled_position_qty != 0:
            raise RunnerSafetyError("entry requires proven FLAT exposure")
        if state.active_intent_id is not None:
            raise RunnerSafetyError("an existing intent is query-only")
        self._verify_decision(
            proof=proof,
            decision=decision,
            expected_kind=DecisionKind.ENTRY,
        )
        request = self._reserve_intent(
            side=Side.BUY,
            quantity=decision.quantity,
            limit_price=decision.limit_price,
            intent_id=intent_id,
            flatten=False,
            activation_proof_sha256=proof.sha256,
            execution_decision_sha256=decision.sha256,
        )
        self._move_exposure(
            ExposureState.ENTRY_INTENT_DURABLE,
            "ENTRY_INTENT_RESERVED",
            active_intent_id=request.intent_id,
        )
        try:
            # Re-read GLOBAL_STOP and every hash/account binding after the
            # durable reservation, immediately before the sole broker call.
            self._require_dispatch_enabled(proof)
            self._verify_decision(
                proof=proof,
                decision=decision,
                expected_kind=DecisionKind.ENTRY,
            )
            state = self._require_started()
            assert state.selected_symbol is not None
            acknowledged = self.broker.place_limit(
                request, selected_symbol=state.selected_symbol
            )
        except Exception as exc:
            self._move_exposure(
                ExposureState.RECOVERY_REQUIRED, "ENTRY_ACK_AMBIGUOUS"
            )
            raise RunnerSafetyError(
                "entry acknowledgement is ambiguous; intent will not be retried"
            ) from exc
        self._move_exposure(
            ExposureState.ENTRY_PENDING,
            "ENTRY_ACKNOWLEDGED",
            last_order_id=acknowledged.order_id,
        )
        # Action boundary post-reconciliation; this is still query-only.
        self.reconcile(reason="ENTRY_POST_ACTION")
        return acknowledged

    def dispatch_exit(
        self,
        *,
        activation_proof: ActivationProof,
        decision: ExecutionDecision,
        intent_id: Optional[str] = None,
    ) -> OrderRecord:
        """Send one bounded SELL attempt; PAUSED/EMERGENCY do not suppress exits."""

        _, proof = self._require_dispatch_enabled(activation_proof)
        state = self.reconcile(reason="EXIT_ACTION")
        assert state.selected_symbol is not None
        sellable_snapshot = self.broker.reconcile(state.selected_symbol)
        if (
            sellable_snapshot.symbol != state.selected_symbol
            or sellable_snapshot.position.symbol != state.selected_symbol
            or sellable_snapshot.position.quantity != state.reconciled_position_qty
        ):
            self._move_exposure(
                ExposureState.RECOVERY_REQUIRED,
                "EXIT_SELLABLE_RECONCILIATION_MISMATCH",
            )
            raise RunnerSafetyError("exit sellable quantity is ambiguous")
        if state.control not in {
            ControlState.DISARMED,
            ControlState.ARMED,
            ControlState.PAUSED,
            ControlState.HALTED,
            ControlState.EMERGENCY,
        }:
            raise RunnerSafetyError("exit requires an active or safety control state")
        if state.active_intent_id is not None:
            reservation = self.intent_store.get(state.active_intent_id)
            if (
                state.exposure is ExposureState.PARTIAL_POSITION
                and reservation is not None
                and reservation.document.get("side") == Side.SELL.value
            ):
                next_state = evolve_state(state, active_intent_id=None)
                self._save(next_state, "FILLED_EXIT_INTENT_RETIRED", {})
                state = next_state
            else:
                raise RunnerSafetyError("an existing intent is query-only")
        if state.exposure not in {
            ExposureState.LONG_UNPROTECTED,
            ExposureState.LONG_GUARDED_LOCAL_ONLY,
            ExposureState.PARTIAL_POSITION,
        }:
            raise RunnerSafetyError("exit requires a reconciled long position")
        self._verify_decision(
            proof=proof,
            decision=decision,
            expected_kind=DecisionKind.EXIT,
            expected_position_quantity=state.reconciled_position_qty,
            expected_exit_dispatches=state.exit_dispatches,
        )
        if decision.quantity > sellable_snapshot.position.sellable_quantity:
            raise RunnerSafetyError("exit quantity exceeds the broker-sellable position")
        if state.exit_dispatches >= 2:
            raise RunnerSafetyError("shared exit/flatten dispatch cap reached")
        request = self._reserve_intent(
            side=Side.SELL,
            quantity=decision.quantity,
            limit_price=decision.limit_price,
            intent_id=intent_id,
            flatten=True,
            activation_proof_sha256=proof.sha256,
            execution_decision_sha256=decision.sha256,
        )
        target = ExposureState.FLATTEN_INTENT_DURABLE
        self._move_exposure(
            target,
            "EXIT_INTENT_RESERVED",
            active_intent_id=request.intent_id,
            exit_dispatches=state.exit_dispatches + 1,
        )
        try:
            self._require_dispatch_enabled(proof)
            self._verify_decision(
                proof=proof,
                decision=decision,
                expected_kind=DecisionKind.EXIT,
                expected_position_quantity=state.reconciled_position_qty,
                expected_exit_dispatches=state.exit_dispatches,
            )
            state = self._require_started()
            assert state.selected_symbol is not None
            acknowledged = self.broker.place_limit(
                request, selected_symbol=state.selected_symbol
            )
        except Exception as exc:
            self._move_exposure(
                ExposureState.RECOVERY_REQUIRED, "EXIT_ACK_AMBIGUOUS"
            )
            raise RunnerSafetyError(
                "exit acknowledgement is ambiguous; intent will not be retried"
            ) from exc
        pending = ExposureState.FLATTEN_PENDING
        self._move_exposure(
            pending,
            "EXIT_ACKNOWLEDGED",
            last_order_id=acknowledged.order_id,
        )
        self.reconcile(reason="EXIT_POST_ACTION")
        return acknowledged


__all__ = [
    "ExecutionMode",
    "RunnerError",
    "RunnerSafetyError",
    "RuntimePaths",
    "TradingRunner",
]
