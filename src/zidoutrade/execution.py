"""Hash-bound, short-lived execution decisions.

Strategy output, risk sizing, a fresh executable quote, and the reviewed
exchange session are recomputed and bound into one local-HMAC-authenticated
decision.  The runner consumes that decision directly, so a caller cannot
replace its symbol, side, quantity, or limit price between review and dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import math
import re
from typing import Any, Dict, Mapping, Optional

from .activation import (
    ActivationError,
    ActivationProof,
    ActivationVerifier,
    utc_timestamp,
)
from .broker import Side
from .exchange_calendar import FrozenExchangeCalendar
from .models import DecisionAction, ReasonCode, StrategyContext
from .risk import (
    ExecutionStress,
    PAPER_FEE_SCHEDULE,
    RiskPolicy,
    RiskState,
    SizingRequest,
    entry_allowed,
    size_position,
)
from .storage import canonical_sha256
from .strategy import evaluate_strategy


MAX_QUOTE_AGE = timedelta(seconds=2)
MAX_RELATIVE_SPREAD = Decimal("0.0010")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL = re.compile(r"^US\.[A-Z0-9][A-Z0-9._-]{0,31}$")
_BUILDER_TOKEN = object()
_UNSIGNED_KEYS = {
    "activation_binding_sha256",
    "completed_roundtrips_today",
    "created_at",
    "entry_dispatches_today",
    "exit_dispatches_today",
    "kind",
    "limit_price",
    "quantity",
    "quote_sha256",
    "risk_sha256",
    "rth_verifier_sha256",
    "schema",
    "session_id",
    "side",
    "signal_bar_end",
    "strategy_sha256",
    "symbol",
    "valid_until",
}


class ExecutionEvidenceError(RuntimeError):
    """The complete decision-to-order evidence cannot be established."""


def _decimal(name: str, value: object, *, positive: bool = True) -> Decimal:
    if isinstance(value, bool):
        raise ExecutionEvidenceError("%s must be numeric" % name)
    try:
        result = value if type(value) is Decimal else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ExecutionEvidenceError("%s must be numeric" % name) from exc
    if not result.is_finite() or (positive and result <= 0):
        raise ExecutionEvidenceError("%s must be finite and positive" % name)
    return result


def _timestamp(name: str, value: object) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise ExecutionEvidenceError("%s must be a canonical UTC timestamp" % name)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ExecutionEvidenceError("%s is invalid" % name) from exc
    if utc_timestamp(parsed) != value:
        raise ExecutionEvidenceError("%s is not canonical" % name)
    return parsed


def _exact_nonnegative_int(name: str, value: object) -> int:
    if type(value) is not int or value < 0:
        raise ExecutionEvidenceError("%s must be an exact nonnegative int" % name)
    return value


@dataclass(frozen=True)
class QuoteSnapshot:
    """A minimal top-of-book snapshot from the local market-data boundary."""

    symbol: str
    bid: Decimal
    ask: Decimal
    observed_at: datetime
    source: str = "OPEND_LOCAL"

    def __post_init__(self) -> None:
        if type(self.symbol) is not str or _SYMBOL.fullmatch(self.symbol) is None:
            raise ExecutionEvidenceError("quote symbol is not canonical")
        bid = _decimal("bid", self.bid)
        ask = _decimal("ask", self.ask)
        if ask < bid:
            raise ExecutionEvidenceError("quote ask is below bid")
        if type(self.observed_at) is not datetime or self.observed_at.tzinfo is None:
            raise ExecutionEvidenceError("quote observed_at must be timezone-aware")
        if self.observed_at.utcoffset() is None:
            raise ExecutionEvidenceError("quote observed_at must be timezone-aware")
        if self.source != "OPEND_LOCAL":
            raise ExecutionEvidenceError("quote source must be OPEND_LOCAL")
        object.__setattr__(self, "bid", bid)
        object.__setattr__(self, "ask", ask)

    @property
    def relative_spread(self) -> Decimal:
        midpoint = (self.bid + self.ask) / Decimal(2)
        return (self.ask - self.bid) / midpoint

    def payload(self) -> Dict[str, str]:
        return {
            "ask": str(self.ask),
            "bid": str(self.bid),
            "observed_at": utc_timestamp(self.observed_at),
            "source": self.source,
            "symbol": self.symbol,
        }


class DecisionKind(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"


@dataclass(frozen=True)
class ExecutionDecision:
    """Immutable order fields plus authenticated evidence hashes."""

    activation_binding_sha256: str
    kind: DecisionKind
    session_id: str
    symbol: str
    side: Side
    quantity: int
    limit_price: Decimal
    created_at: str
    valid_until: str
    quote_sha256: str
    strategy_sha256: str
    risk_sha256: str
    rth_verifier_sha256: str
    signal_bar_end: Optional[str]
    completed_roundtrips_today: int
    entry_dispatches_today: int
    exit_dispatches_today: int
    execution_hmac_sha256: str

    def __post_init__(self) -> None:
        if type(self.kind) is not DecisionKind:
            raise ExecutionEvidenceError("kind must be an exact DecisionKind")
        if type(self.side) is not Side:
            raise ExecutionEvidenceError("side must be an exact Side")
        if (self.kind is DecisionKind.ENTRY) != (self.side is Side.BUY):
            raise ExecutionEvidenceError("decision kind and order side disagree")
        if type(self.session_id) is not str:
            raise ExecutionEvidenceError("session_id must be a string")
        try:
            canonical_session = datetime.strptime(self.session_id, "%Y-%m-%d").date().isoformat()
        except ValueError as exc:
            raise ExecutionEvidenceError("session_id must be exact YYYY-MM-DD") from exc
        if canonical_session != self.session_id:
            raise ExecutionEvidenceError("session_id must be canonical")
        if type(self.symbol) is not str or _SYMBOL.fullmatch(self.symbol) is None:
            raise ExecutionEvidenceError("execution symbol is not canonical")
        if type(self.quantity) is not int or isinstance(self.quantity, bool) or self.quantity <= 0:
            raise ExecutionEvidenceError("execution quantity must be a positive exact int")
        price = _decimal("limit_price", self.limit_price)
        object.__setattr__(self, "limit_price", price)
        created = _timestamp("created_at", self.created_at)
        expires = _timestamp("valid_until", self.valid_until)
        if expires <= created or expires - created > timedelta(seconds=5):
            raise ExecutionEvidenceError("execution decision validity exceeds five seconds")
        for name in (
            "activation_binding_sha256",
            "quote_sha256",
            "strategy_sha256",
            "risk_sha256",
            "rth_verifier_sha256",
            "execution_hmac_sha256",
        ):
            value = getattr(self, name)
            if type(value) is not str or _SHA256.fullmatch(value) is None:
                raise ExecutionEvidenceError("%s must be a lowercase SHA-256" % name)
        if self.signal_bar_end is not None:
            _timestamp("signal_bar_end", self.signal_bar_end)
        _exact_nonnegative_int(
            "completed_roundtrips_today", self.completed_roundtrips_today
        )
        _exact_nonnegative_int("entry_dispatches_today", self.entry_dispatches_today)
        _exact_nonnegative_int("exit_dispatches_today", self.exit_dispatches_today)
        if self.completed_roundtrips_today > 1:
            raise ExecutionEvidenceError("one-round-trip policy exceeded")
        if self.kind is DecisionKind.ENTRY:
            if self.completed_roundtrips_today != 0 or self.entry_dispatches_today != 0:
                raise ExecutionEvidenceError("entry dispatch already consumed for this session")
            if self.exit_dispatches_today != 0:
                raise ExecutionEvidenceError("cannot enter after an exit dispatch")
        elif self.entry_dispatches_today != 1 or self.exit_dispatches_today >= 2:
            raise ExecutionEvidenceError("exit dispatch counters violate V1 policy")

    def unsigned_payload(self) -> Dict[str, Any]:
        return {
            "activation_binding_sha256": self.activation_binding_sha256,
            "completed_roundtrips_today": self.completed_roundtrips_today,
            "created_at": self.created_at,
            "entry_dispatches_today": self.entry_dispatches_today,
            "exit_dispatches_today": self.exit_dispatches_today,
            "kind": self.kind.value,
            "limit_price": str(self.limit_price),
            "quantity": self.quantity,
            "quote_sha256": self.quote_sha256,
            "risk_sha256": self.risk_sha256,
            "rth_verifier_sha256": self.rth_verifier_sha256,
            "schema": 1,
            "session_id": self.session_id,
            "side": self.side.value,
            "signal_bar_end": self.signal_bar_end,
            "strategy_sha256": self.strategy_sha256,
            "symbol": self.symbol,
            "valid_until": self.valid_until,
        }

    @property
    def sha256(self) -> str:
        return canonical_sha256(
            {
                **self.unsigned_payload(),
                "execution_hmac_sha256": self.execution_hmac_sha256,
            }
        )


def _validate_unsigned_schema(
    payload: Mapping[str, Any], *, proof: ActivationProof
) -> None:
    """Validate the exact evidence envelope before any local HMAC operation."""

    if type(payload) is not dict or set(payload) != _UNSIGNED_KEYS:
        raise ExecutionEvidenceError("execution evidence schema mismatch")
    if payload.get("schema") != 1:
        raise ExecutionEvidenceError("execution evidence schema version mismatch")
    if (
        payload.get("activation_binding_sha256") != proof.binding_sha256
        or payload.get("session_id") != proof.session_id
        or payload.get("symbol") != proof.selected_symbol
        or payload.get("rth_verifier_sha256") != proof.rth_verifier_sha256
    ):
        raise ExecutionEvidenceError("execution evidence activation identity mismatch")
    try:
        kind = DecisionKind(payload.get("kind"))
        side = Side(payload.get("side"))
    except (TypeError, ValueError) as exc:
        raise ExecutionEvidenceError("execution evidence kind/side is invalid") from exc
    # Constructing the exact public value validates every other field and the
    # cross-field counter policy.  A placeholder authenticator is sufficient
    # because this function runs before sealing.
    ExecutionDecision(
        activation_binding_sha256=str(payload["activation_binding_sha256"]),
        kind=kind,
        session_id=str(payload["session_id"]),
        symbol=str(payload["symbol"]),
        side=side,
        quantity=payload["quantity"],
        limit_price=payload["limit_price"],
        created_at=payload["created_at"],
        valid_until=payload["valid_until"],
        quote_sha256=payload["quote_sha256"],
        strategy_sha256=payload["strategy_sha256"],
        risk_sha256=payload["risk_sha256"],
        rth_verifier_sha256=payload["rth_verifier_sha256"],
        signal_bar_end=payload["signal_bar_end"],
        completed_roundtrips_today=payload["completed_roundtrips_today"],
        entry_dispatches_today=payload["entry_dispatches_today"],
        exit_dispatches_today=payload["exit_dispatches_today"],
        execution_hmac_sha256="0" * 64,
    )


def _risk_payload(state: RiskState, policy: RiskPolicy) -> Dict[str, Any]:
    if type(state) is not RiskState or type(policy) is not RiskPolicy:
        raise ExecutionEvidenceError("exact risk state and policy are required")
    return {
        **policy.evidence_payload(),
        "completed_roundtrips_today": state.completed_roundtrips_today,
        "daily_loss_limit_passed": (
            state.daily_pnl / state.day_start_equity > -policy.daily_loss_fraction
        ),
        "daily_pnl_fraction": state.daily_pnl / state.day_start_equity,
        "day_start_equity": state.day_start_equity,
        "week_start_equity": state.week_start_equity,
        "weekly_loss_limit_passed": (
            state.weekly_pnl / state.week_start_equity > -policy.weekly_loss_fraction
        ),
        "weekly_pnl_fraction": state.weekly_pnl / state.week_start_equity,
    }


def _strategy_payload(context: StrategyContext) -> Dict[str, Any]:
    decision = evaluate_strategy(context)
    return {
        "action": decision.action.value,
        "active_symbol": context.active_symbol,
        "latest_bar_id": context.bars[-1].bar_id if context.bars else None,
        "reasons": [reason.value for reason in decision.reasons],
        "selected_symbol": context.selected_symbol,
        "signal_bar_end": (
            utc_timestamp(decision.signal_bar_end)
            if decision.signal_bar_end is not None
            else None
        ),
        "strategy_now": utc_timestamp(context.now),
    }


def _require_common(
    *,
    verifier: ActivationVerifier,
    proof: ActivationProof,
    calendar: FrozenExchangeCalendar,
    context: StrategyContext,
    quote: QuoteSnapshot,
) -> datetime:
    if type(verifier) is not ActivationVerifier:
        raise ExecutionEvidenceError("an exact ActivationVerifier is required")
    if type(proof) is not ActivationProof:
        raise ExecutionEvidenceError("an exact ActivationProof is required")
    if type(calendar) is not FrozenExchangeCalendar:
        raise ExecutionEvidenceError("a frozen exchange calendar is required")
    if type(context) is not StrategyContext or type(quote) is not QuoteSnapshot:
        raise ExecutionEvidenceError("exact strategy context and quote types are required")
    try:
        verifier.verify_proof(proof)
    except ActivationError as exc:
        raise ExecutionEvidenceError("activation proof is invalid") from exc
    if calendar.sha256 != proof.rth_verifier_sha256:
        raise ExecutionEvidenceError("calendar hash differs from activation proof")
    now = verifier.current_time().astimezone(timezone.utc)
    try:
        active = calendar.active_session(now)
    except Exception as exc:
        raise ExecutionEvidenceError("RTH calendar verification failed") from exc
    if active is None or active.session_date.isoformat() != proof.session_id:
        raise ExecutionEvidenceError("decision time is outside the locked RTH session")
    if (
        quote.symbol != proof.selected_symbol
        or context.active_symbol != proof.selected_symbol
        or context.selected_symbol != proof.selected_symbol
    ):
        raise ExecutionEvidenceError("decision evidence differs from locked symbol")
    observed = quote.observed_at.astimezone(timezone.utc)
    if observed > now or now - observed > MAX_QUOTE_AGE:
        raise ExecutionEvidenceError("quote is future-dated or stale")
    context_time = context.now.astimezone(timezone.utc)
    if context_time > now or now - context_time > MAX_QUOTE_AGE:
        raise ExecutionEvidenceError("strategy evaluation is future-dated or stale")
    return now


def _finish(
    *,
    verifier: ActivationVerifier,
    proof: ActivationProof,
    kind: DecisionKind,
    quantity: int,
    limit_price: Decimal,
    now: datetime,
    quote: QuoteSnapshot,
    strategy_payload: Mapping[str, Any],
    risk_payload: Mapping[str, Any],
    signal_bar_end: Optional[datetime],
    completed_roundtrips_today: int,
    entry_dispatches_today: int,
    exit_dispatches_today: int,
) -> ExecutionDecision:
    valid_until = min(
        now + timedelta(seconds=2),
        _timestamp("proof valid_until", proof.valid_until),
    )
    unsigned = {
        "activation_binding_sha256": proof.binding_sha256,
        "completed_roundtrips_today": completed_roundtrips_today,
        "created_at": utc_timestamp(now),
        "entry_dispatches_today": entry_dispatches_today,
        "exit_dispatches_today": exit_dispatches_today,
        "kind": kind.value,
        "limit_price": str(limit_price),
        "quantity": quantity,
        "quote_sha256": canonical_sha256(quote.payload()),
        "risk_sha256": canonical_sha256(dict(risk_payload)),
        "rth_verifier_sha256": proof.rth_verifier_sha256,
        "schema": 1,
        "session_id": proof.session_id,
        "side": Side.BUY.value if kind is DecisionKind.ENTRY else Side.SELL.value,
        "signal_bar_end": utc_timestamp(signal_bar_end) if signal_bar_end is not None else None,
        "strategy_sha256": canonical_sha256(dict(strategy_payload)),
        "symbol": proof.selected_symbol,
        "valid_until": utc_timestamp(valid_until),
    }
    try:
        authenticator = verifier._seal_execution_evidence(
            proof, unsigned, builder_token=_BUILDER_TOKEN
        )
    except ActivationError as exc:
        raise ExecutionEvidenceError("execution evidence could not be sealed") from exc
    return ExecutionDecision(
        execution_hmac_sha256=authenticator,
        **{
            **{
                key: value
                for key, value in unsigned.items()
                if key not in {"schema", "kind", "side"}
            },
            "kind": kind,
            "side": Side.BUY if kind is DecisionKind.ENTRY else Side.SELL,
        },
    )


def build_entry_decision(
    *,
    verifier: ActivationVerifier,
    proof: ActivationProof,
    calendar: FrozenExchangeCalendar,
    context: StrategyContext,
    sizing_request: SizingRequest,
    atr_raw: float,
    quote: QuoteSnapshot,
    entry_dispatches_today: int,
    exit_dispatches_today: int,
) -> ExecutionDecision:
    """Recompute every entry gate and produce one exact BUY decision."""

    now = _require_common(
        verifier=verifier, proof=proof, calendar=calendar, context=context, quote=quote
    )
    entry_count = _exact_nonnegative_int("entry_dispatches_today", entry_dispatches_today)
    exit_count = _exact_nonnegative_int("exit_dispatches_today", exit_dispatches_today)
    if entry_count != 0 or exit_count != 0:
        raise ExecutionEvidenceError("one-round-trip entry budget is already consumed")
    if quote.relative_spread > MAX_RELATIVE_SPREAD or context.gates.spread_ok is not True:
        raise ExecutionEvidenceError("entry spread gate failed")
    decision = evaluate_strategy(context)
    if (
        decision.action is not DecisionAction.ENTER
        or decision.reasons != (ReasonCode.ENTRY_SIGNAL_CONFIRMED,)
        or not context.bars
    ):
        raise ExecutionEvidenceError("strategy did not produce the exact entry signal")
    try:
        expected_bar_end = calendar.latest_completed_bar_end(now)
    except Exception as exc:
        raise ExecutionEvidenceError("latest completed bar cannot be verified") from exc
    if context.bars[-1].end != expected_bar_end or decision.signal_bar_end != expected_bar_end:
        raise ExecutionEvidenceError("strategy does not use the latest completed RTH bar")
    if type(sizing_request) is not SizingRequest:
        raise ExecutionEvidenceError("an exact SizingRequest is required")
    if (
        sizing_request.stress != ExecutionStress()
        or sizing_request.entry_fees is not PAPER_FEE_SCHEDULE
        or sizing_request.exit_fees is not PAPER_FEE_SCHEDULE
    ):
        raise ExecutionEvidenceError(
            "entry sizing stress/fees must match the frozen execution contract"
        )
    if sizing_request.state.completed_roundtrips_today != context.traded_roundtrips_today:
        raise ExecutionEvidenceError("strategy and risk round-trip counters disagree")
    if context.traded_roundtrips_today != 0:
        raise ExecutionEvidenceError("one-round-trip policy is already consumed")
    if not math.isfinite(atr_raw) or atr_raw <= 0:
        raise ExecutionEvidenceError("ATR must be finite and positive")
    expected_stop = float(quote.ask) - 1.5 * float(atr_raw)
    if not math.isclose(sizing_request.entry_limit, float(quote.ask), abs_tol=1e-12):
        raise ExecutionEvidenceError("risk sizing entry differs from executable ask")
    if not math.isclose(sizing_request.stop_trigger, expected_stop, abs_tol=1e-12):
        raise ExecutionEvidenceError("risk sizing stop is not entry minus 1.5 ATR")
    sized = size_position(sizing_request)
    if not sized.allowed or not entry_allowed(sizing_request.state, sizing_request.policy).allowed:
        raise ExecutionEvidenceError("daily/weekly/round-trip risk gate blocked entry")
    risk_payload = {
        **_risk_payload(sizing_request.state, sizing_request.policy),
        "allowed_quantity": sized.qty,
        "entry_cash_required": sized.components.entry_cash_required,
        "entry_fee": sized.components.entry_fee,
        "entry_limit": sizing_request.entry_limit,
        "exit_fee": sized.components.exit_fee,
        "fee_schedule": PAPER_FEE_SCHEDULE.value,
        "notional_cap": sized.notional_cap,
        "investment_cap_cents": sized.investment_cap_cents,
        "max_relative_spread": str(MAX_RELATIVE_SPREAD),
        "observed_relative_spread": str(quote.relative_spread),
        "planned_total_loss": sized.components.planned_total_loss,
        "risk_budget": sized.risk_budget,
        "stop_trigger": sizing_request.stop_trigger,
    }
    return _finish(
        verifier=verifier,
        proof=proof,
        kind=DecisionKind.ENTRY,
        quantity=sized.qty,
        limit_price=quote.ask,
        now=now,
        quote=quote,
        strategy_payload=_strategy_payload(context),
        risk_payload=risk_payload,
        signal_bar_end=decision.signal_bar_end,
        completed_roundtrips_today=context.traded_roundtrips_today,
        entry_dispatches_today=entry_count,
        exit_dispatches_today=exit_count,
    )


def build_exit_decision(
    *,
    verifier: ActivationVerifier,
    proof: ActivationProof,
    calendar: FrozenExchangeCalendar,
    context: StrategyContext,
    risk_state: RiskState,
    quote: QuoteSnapshot,
    known_position_quantity: int,
    entry_dispatches_today: int,
    exit_dispatches_today: int,
    risk_policy: RiskPolicy,
) -> ExecutionDecision:
    """Produce one exact full-position SELL decision; risk limits never block exits."""

    now = _require_common(
        verifier=verifier, proof=proof, calendar=calendar, context=context, quote=quote
    )
    quantity = _exact_nonnegative_int("known_position_quantity", known_position_quantity)
    entry_count = _exact_nonnegative_int("entry_dispatches_today", entry_dispatches_today)
    exit_count = _exact_nonnegative_int("exit_dispatches_today", exit_dispatches_today)
    if quantity <= 0 or entry_count != 1 or exit_count >= 2:
        raise ExecutionEvidenceError("exit position/order counters violate V1 policy")
    if type(risk_state) is not RiskState:
        raise ExecutionEvidenceError("an exact RiskState is required")
    if type(risk_policy) is not RiskPolicy:
        raise ExecutionEvidenceError("an exact RiskPolicy is required")
    if risk_state.completed_roundtrips_today not in (0, 1):
        raise ExecutionEvidenceError("one-round-trip policy exceeded")
    if context.position is None:
        raise ExecutionEvidenceError("exit requires a strategy position snapshot")
    if not math.isclose(context.position.current_raw_price, float(quote.bid), abs_tol=1e-12):
        raise ExecutionEvidenceError("exit strategy price differs from executable bid")
    decision = evaluate_strategy(context)
    if decision.action is not DecisionAction.EXIT:
        raise ExecutionEvidenceError("strategy did not produce an exit signal")
    # Bar-driven exits must use the latest completed bar.  Emergency/stop exits
    # are allowed without waiting for a new bar but still require a fresh quote.
    immediate = decision.reasons[0] in (ReasonCode.EMERGENCY_EXIT, ReasonCode.STOP_LOSS)
    if not immediate:
        if not context.bars:
            raise ExecutionEvidenceError("bar-driven exit has no completed bar")
        try:
            expected_bar_end = calendar.latest_completed_bar_end(now)
        except Exception as exc:
            raise ExecutionEvidenceError("latest completed bar cannot be verified") from exc
        if context.bars[-1].end != expected_bar_end or decision.signal_bar_end != expected_bar_end:
            raise ExecutionEvidenceError("exit does not use the latest completed RTH bar")
    return _finish(
        verifier=verifier,
        proof=proof,
        kind=DecisionKind.EXIT,
        quantity=quantity,
        limit_price=quote.bid,
        now=now,
        quote=quote,
        strategy_payload=_strategy_payload(context),
        risk_payload={
            **_risk_payload(risk_state, risk_policy),
            "exit_limits_blocking": False,
        },
        signal_bar_end=decision.signal_bar_end,
        completed_roundtrips_today=risk_state.completed_roundtrips_today,
        entry_dispatches_today=entry_count,
        exit_dispatches_today=exit_count,
    )


def verify_execution_decision(
    *,
    verifier: ActivationVerifier,
    proof: ActivationProof,
    decision: ExecutionDecision,
    expected_kind: DecisionKind,
    expected_session_id: str,
    expected_symbol: str,
    expected_position_quantity: Optional[int] = None,
    expected_exit_dispatches: Optional[int] = None,
) -> None:
    """Final, side-effect-free verification immediately before intent reserve."""

    if type(verifier) is not ActivationVerifier or type(proof) is not ActivationProof:
        raise ExecutionEvidenceError("exact activation verifier/proof types are required")
    if type(decision) is not ExecutionDecision or type(expected_kind) is not DecisionKind:
        raise ExecutionEvidenceError("exact execution decision/kind types are required")
    if (
        decision.kind is not expected_kind
        or decision.session_id != expected_session_id
        or decision.symbol != expected_symbol
        or decision.activation_binding_sha256 != proof.binding_sha256
        or decision.rth_verifier_sha256 != proof.rth_verifier_sha256
    ):
        raise ExecutionEvidenceError("execution decision differs from durable runtime identity")
    now = verifier.current_time().astimezone(timezone.utc)
    if now < _timestamp("created_at", decision.created_at) or now >= _timestamp(
        "valid_until", decision.valid_until
    ):
        raise ExecutionEvidenceError("execution decision expired")
    if expected_kind is DecisionKind.ENTRY:
        if decision.entry_dispatches_today != 0 or decision.exit_dispatches_today != 0:
            raise ExecutionEvidenceError("entry dispatch counters changed")
    else:
        if expected_position_quantity is None or decision.quantity != expected_position_quantity:
            raise ExecutionEvidenceError("exit quantity differs from reconciled position")
        if expected_exit_dispatches is None or decision.exit_dispatches_today != expected_exit_dispatches:
            raise ExecutionEvidenceError("exit dispatch counter changed")
    try:
        verifier.verify_execution_payload(
            proof, decision.unsigned_payload(), decision.execution_hmac_sha256
        )
    except ActivationError as exc:
        raise ExecutionEvidenceError("execution decision authenticator is invalid") from exc


__all__ = [
    "MAX_QUOTE_AGE",
    "MAX_RELATIVE_SPREAD",
    "DecisionKind",
    "ExecutionDecision",
    "ExecutionEvidenceError",
    "QuoteSnapshot",
    "build_entry_decision",
    "build_exit_decision",
    "verify_execution_decision",
]
