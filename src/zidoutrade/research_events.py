"""Strict, broker-free DTOs for the Q014 V2 prospective research ledger.

The types in this module describe research evidence only.  They deliberately
contain no execution, portfolio, or performance fields, and importing this
module has no side effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
import hashlib
import re
from types import MappingProxyType
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from .storage import canonical_json_bytes


class ResearchSchemaError(ValueError):
    """Raised when a research DTO is ambiguous or noncanonical."""


class EventType(str, Enum):
    PAIR_RESERVED = "PAIR_RESERVED"
    OBSERVED = "OBSERVED"
    OBSERVATION_MISSING = "OBSERVATION_MISSING"
    ARM_TERMINAL = "ARM_TERMINAL"
    SESSION_COMPLETE = "SESSION_COMPLETE"
    SESSION_MISSING = "SESSION_MISSING"


class ResearchArm(str, Enum):
    BASELINE = "BASELINE"
    CANDIDATE = "CANDIDATE"


class ArmOutcome(str, Enum):
    WAIT = "WAIT"
    NO_FILL = "NO_FILL"
    VIRTUAL_TERMINAL = "VIRTUAL_TERMINAL"
    DATA_QUALITY_TERMINAL = "DATA_QUALITY_TERMINAL"


class MissingReason(str, Enum):
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    CAPTURE_INCOMPLETE = "CAPTURE_INCOMPLETE"
    DATA_QUALITY = "DATA_QUALITY"


class SelectionKind(str, Enum):
    SELECTED_SYMBOL = "SELECTED_SYMBOL"
    NO_SELECTION = "NO_SELECTION"


PAIR_IDENTITY_DOMAIN = b"zidoutrade/q014-v2/pair-identity/v1\0"
EXPECTED_SESSIONS_DOMAIN = b"zidoutrade/q014-v2/expected-sessions/v1\0"
EXPECTED_LEDGER_DOMAIN = b"zidoutrade/q014-v2/expected-ledger/v1\0"
PAIR_RESERVATION_DOMAIN = b"zidoutrade/q014-v2/pair-reservation/v1\0"
RECORD_DOMAIN = b"zidoutrade/q014-v2/record/v1\0"

GENESIS_SHA256 = "0" * 64

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL = re.compile(r"^US\.[A-Z0-9][A-Z0-9._-]{0,31}$")
_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?Z$")


def _exact_keys(value: Mapping[str, Any], expected: Iterable[str], label: str) -> None:
    wanted = frozenset(expected)
    actual = frozenset(value)
    if actual != wanted:
        missing = sorted(wanted - actual, key=repr)
        extra = sorted(actual - wanted, key=repr)
        raise ResearchSchemaError(
            "%s fields do not match schema (missing=%r, extra=%r)"
            % (label, missing, extra)
        )


def _identifier(name: str, value: object) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ResearchSchemaError("%s is not a canonical identifier" % name)
    return value


def _sha256(name: str, value: object, *, permit_genesis: bool = False) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ResearchSchemaError("%s must be lowercase SHA-256 hex" % name)
    if not permit_genesis and value == GENESIS_SHA256:
        raise ResearchSchemaError("%s must not use the genesis sentinel" % name)
    return value


def canonical_session(value: object) -> str:
    if not isinstance(value, str):
        raise ResearchSchemaError("target_session must be text")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ResearchSchemaError("target_session is not an ISO date") from exc
    if parsed.isoformat() != value:
        raise ResearchSchemaError("target_session is not canonical")
    return value


def canonical_utc(value: object, *, name: str = "timestamp") -> str:
    """Return a strict UTC timestamp without consulting the local clock."""

    if not isinstance(value, str) or not _UTC.fullmatch(value):
        raise ResearchSchemaError("%s must be canonical UTC" % name)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ResearchSchemaError("%s is invalid" % name) from exc
    if parsed.tzinfo != timezone.utc:
        raise ResearchSchemaError("%s must use UTC" % name)
    timespec = "microseconds" if parsed.microsecond else "seconds"
    canonical = parsed.isoformat(timespec=timespec).replace("+00:00", "Z")
    if canonical != value:
        raise ResearchSchemaError("%s is not canonical" % name)
    return value


def parse_utc(value: str) -> datetime:
    canonical_utc(value)
    return datetime.fromisoformat(value[:-1] + "+00:00")


def domain_sha256(domain: bytes, value: Mapping[str, Any]) -> str:
    if not isinstance(domain, bytes) or not domain.endswith(b"\0"):
        raise ValueError("hash domain must be NUL-terminated bytes")
    return hashlib.sha256(domain + canonical_json_bytes(dict(value))).hexdigest()


@dataclass(frozen=True)
class PairIdentity:
    """Private preimage used to bind one selected symbol (or no selection)."""

    study_id: str
    protocol_id: str
    target_session: str
    selection_kind: SelectionKind
    selection_record_sha256: str
    selected_symbol: Optional[str] = None

    def __post_init__(self) -> None:
        _identifier("study_id", self.study_id)
        _identifier("protocol_id", self.protocol_id)
        canonical_session(self.target_session)
        try:
            kind = SelectionKind(self.selection_kind)
        except ValueError as exc:
            raise ResearchSchemaError("unknown selection_kind") from exc
        object.__setattr__(self, "selection_kind", kind)
        _sha256("selection_record_sha256", self.selection_record_sha256)
        if kind is SelectionKind.NO_SELECTION:
            if self.selected_symbol is not None:
                raise ResearchSchemaError(
                    "NO_SELECTION must not contain a selected symbol"
                )
        elif not isinstance(self.selected_symbol, str) or not _SYMBOL.fullmatch(
            self.selected_symbol
        ):
            raise ResearchSchemaError("selected symbol must use canonical US form")

    def payload(self) -> Dict[str, Any]:
        selected = (
            "NO_SELECTION"
            if self.selection_kind is SelectionKind.NO_SELECTION
            else self.selected_symbol
        )
        return {
            "protocol_id": self.protocol_id,
            "selected_symbol_or_no_selection": selected,
            "selection_kind": self.selection_kind.value,
            "selection_record_sha256": self.selection_record_sha256,
            "study_id": self.study_id,
            "target_session": self.target_session,
        }

    @property
    def pair_key(self) -> str:
        return domain_sha256(PAIR_IDENTITY_DOMAIN, self.payload())

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "PairIdentity":
        _exact_keys(
            value,
            {
                "protocol_id",
                "selected_symbol_or_no_selection",
                "selection_kind",
                "selection_record_sha256",
                "study_id",
                "target_session",
            },
            "pair identity",
        )
        try:
            kind = SelectionKind(value["selection_kind"])
        except (TypeError, ValueError) as exc:
            raise ResearchSchemaError("unknown selection_kind") from exc
        selected = value["selected_symbol_or_no_selection"]
        if kind is SelectionKind.NO_SELECTION:
            if selected != "NO_SELECTION":
                raise ResearchSchemaError("NO_SELECTION sentinel mismatch")
            symbol = None
        else:
            symbol = selected
        return cls(
            study_id=value["study_id"],
            protocol_id=value["protocol_id"],
            target_session=value["target_session"],
            selection_kind=kind,
            selection_record_sha256=value["selection_record_sha256"],
            selected_symbol=symbol,
        )


@dataclass(frozen=True)
class ExpectedSession:
    target_session: str
    capture_not_before_utc: str
    deadline_utc: str

    def __post_init__(self) -> None:
        canonical_session(self.target_session)
        canonical_utc(
            self.capture_not_before_utc, name="capture_not_before_utc"
        )
        canonical_utc(self.deadline_utc, name="deadline_utc")
        if parse_utc(self.capture_not_before_utc) >= parse_utc(self.deadline_utc):
            raise ResearchSchemaError(
                "capture_not_before_utc must be before deadline_utc"
            )

    def payload(self) -> Dict[str, str]:
        return {
            "capture_not_before_utc": self.capture_not_before_utc,
            "deadline_utc": self.deadline_utc,
            "target_session": self.target_session,
        }

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "ExpectedSession":
        _exact_keys(
            value,
            {"capture_not_before_utc", "deadline_utc", "target_session"},
            "expected session",
        )
        return cls(
            target_session=value["target_session"],
            capture_not_before_utc=value["capture_not_before_utc"],
            deadline_utc=value["deadline_utc"],
        )


@dataclass(frozen=True)
class ExpectedSessionLedger:
    study_id: str
    protocol_id: str
    manifest_sha256: str
    deadline_source_sha256: str
    created_at_utc: str
    sessions: Tuple[ExpectedSession, ...]
    schema: int = 1

    def __post_init__(self) -> None:
        if type(self.schema) is not int or self.schema != 1:
            raise ResearchSchemaError("unsupported expected-ledger schema")
        _identifier("study_id", self.study_id)
        _identifier("protocol_id", self.protocol_id)
        _sha256("manifest_sha256", self.manifest_sha256)
        _sha256("deadline_source_sha256", self.deadline_source_sha256)
        canonical_utc(self.created_at_utc, name="created_at_utc")
        try:
            normalized = tuple(self.sessions)
        except TypeError as exc:
            raise ResearchSchemaError("sessions must be an iterable") from exc
        if not normalized:
            raise ResearchSchemaError("expected session ledger must not be empty")
        if any(not isinstance(item, ExpectedSession) for item in normalized):
            raise ResearchSchemaError("sessions must contain ExpectedSession values")
        session_ids = tuple(item.target_session for item in normalized)
        if session_ids != tuple(sorted(session_ids)):
            raise ResearchSchemaError("expected sessions must be sorted")
        if len(set(session_ids)) != len(session_ids):
            raise ResearchSchemaError("expected sessions must be unique")
        created_at = parse_utc(self.created_at_utc)
        not_before_values = tuple(
            parse_utc(item.capture_not_before_utc) for item in normalized
        )
        deadlines = tuple(parse_utc(item.deadline_utc) for item in normalized)
        if any(not_before < created_at for not_before in not_before_values):
            raise ResearchSchemaError(
                "capture windows must not predate ledger creation"
            )
        if any(deadline <= created_at for deadline in deadlines):
            raise ResearchSchemaError(
                "every session deadline must be after ledger creation"
            )
        if not_before_values != tuple(sorted(not_before_values)) or len(
            set(not_before_values)
        ) != len(not_before_values):
            raise ResearchSchemaError(
                "session capture starts must be strictly increasing"
            )
        if deadlines != tuple(sorted(deadlines)) or len(set(deadlines)) != len(
            deadlines
        ):
            raise ResearchSchemaError(
                "session deadlines must be strictly increasing"
            )
        object.__setattr__(self, "sessions", normalized)

    @property
    def session_count(self) -> int:
        return len(self.sessions)

    @property
    def expected_sessions_root_sha256(self) -> str:
        return domain_sha256(
            EXPECTED_SESSIONS_DOMAIN,
            {"sessions": [item.payload() for item in self.sessions]},
        )

    def payload(self) -> Dict[str, Any]:
        return {
            "created_at_utc": self.created_at_utc,
            "deadline_source_sha256": self.deadline_source_sha256,
            "expected_session_count": self.session_count,
            "expected_sessions_root_sha256": self.expected_sessions_root_sha256,
            "manifest_sha256": self.manifest_sha256,
            "protocol_id": self.protocol_id,
            "schema": self.schema,
            "sessions": [item.payload() for item in self.sessions],
            "study_id": self.study_id,
        }

    @property
    def ledger_sha256(self) -> str:
        return domain_sha256(EXPECTED_LEDGER_DOMAIN, self.payload())

    def sealed_document(self) -> Dict[str, Any]:
        return {
            "expected_ledger": self.payload(),
            "expected_ledger_sha256": self.ledger_sha256,
            "kind": "SEALED_EXPECTED_SESSION_LEDGER",
        }

    @classmethod
    def from_sealed_document(
        cls, value: Mapping[str, Any]
    ) -> "ExpectedSessionLedger":
        _exact_keys(
            value,
            {"expected_ledger", "expected_ledger_sha256", "kind"},
            "sealed expected ledger",
        )
        if value["kind"] != "SEALED_EXPECTED_SESSION_LEDGER":
            raise ResearchSchemaError("unexpected expected-ledger kind")
        payload = value["expected_ledger"]
        if not isinstance(payload, Mapping):
            raise ResearchSchemaError("expected_ledger must be an object")
        _exact_keys(
            payload,
            {
                "created_at_utc",
                "deadline_source_sha256",
                "expected_session_count",
                "expected_sessions_root_sha256",
                "manifest_sha256",
                "protocol_id",
                "schema",
                "sessions",
                "study_id",
            },
            "expected ledger",
        )
        raw_sessions = payload["sessions"]
        if not isinstance(raw_sessions, list):
            raise ResearchSchemaError("sessions must be an array")
        sessions = tuple(
            ExpectedSession.from_payload(item)
            if isinstance(item, Mapping)
            else (_raise_schema("expected session must be an object"))
            for item in raw_sessions
        )
        ledger = cls(
            study_id=payload["study_id"],
            protocol_id=payload["protocol_id"],
            manifest_sha256=payload["manifest_sha256"],
            deadline_source_sha256=payload["deadline_source_sha256"],
            created_at_utc=payload["created_at_utc"],
            sessions=sessions,
            schema=payload["schema"],
        )
        if type(payload["expected_session_count"]) is not int:
            raise ResearchSchemaError("expected_session_count must be an integer")
        if payload["expected_session_count"] != ledger.session_count:
            raise ResearchSchemaError("expected session count mismatch")
        if payload["expected_sessions_root_sha256"] != ledger.expected_sessions_root_sha256:
            raise ResearchSchemaError("expected sessions root mismatch")
        if value["expected_ledger_sha256"] != ledger.ledger_sha256:
            raise ResearchSchemaError("expected ledger digest mismatch")
        return ledger


def _raise_schema(message: str) -> Any:
    raise ResearchSchemaError(message)


_PAYLOAD_KEYS = {
    EventType.PAIR_RESERVED: frozenset({"reservation_sha256"}),
    EventType.OBSERVED: frozenset({"observation_sha256"}),
    EventType.OBSERVATION_MISSING: frozenset({"reason"}),
    EventType.ARM_TERMINAL: frozenset({"arm", "evidence_sha256", "outcome"}),
    EventType.SESSION_COMPLETE: frozenset(),
    EventType.SESSION_MISSING: frozenset({"reason"}),
}


@dataclass(frozen=True)
class ResearchEvent:
    event_type: EventType
    occurred_at_utc: str
    target_session: str
    pair_key: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        try:
            event_type = EventType(self.event_type)
        except ValueError as exc:
            raise ResearchSchemaError("unknown event_type") from exc
        object.__setattr__(self, "event_type", event_type)
        canonical_utc(self.occurred_at_utc, name="occurred_at_utc")
        canonical_session(self.target_session)
        _sha256("pair_key", self.pair_key)
        if not isinstance(self.payload, Mapping):
            raise ResearchSchemaError("event payload must be an object")
        try:
            normalized = dict(self.payload)
        except (TypeError, ValueError) as exc:
            raise ResearchSchemaError("event payload must be a mapping") from exc
        _exact_keys(normalized, _PAYLOAD_KEYS[event_type], "event payload")
        if event_type is EventType.PAIR_RESERVED:
            _sha256("reservation_sha256", normalized["reservation_sha256"])
        elif event_type is EventType.OBSERVED:
            _sha256("observation_sha256", normalized["observation_sha256"])
        elif event_type in {
            EventType.OBSERVATION_MISSING,
            EventType.SESSION_MISSING,
        }:
            try:
                normalized["reason"] = MissingReason(normalized["reason"]).value
            except (TypeError, ValueError) as exc:
                raise ResearchSchemaError("unknown missing reason") from exc
        elif event_type is EventType.ARM_TERMINAL:
            try:
                normalized["arm"] = ResearchArm(normalized["arm"]).value
                normalized["outcome"] = ArmOutcome(normalized["outcome"]).value
            except (TypeError, ValueError) as exc:
                raise ResearchSchemaError("unknown arm or outcome") from exc
            _sha256("evidence_sha256", normalized["evidence_sha256"])
        object.__setattr__(self, "payload", MappingProxyType(normalized))

    def payload_document(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type.value,
            "occurred_at_utc": self.occurred_at_utc,
            "pair_key": self.pair_key,
            "payload": dict(self.payload),
            "target_session": self.target_session,
        }

    @classmethod
    def from_payload_document(cls, value: Mapping[str, Any]) -> "ResearchEvent":
        _exact_keys(
            value,
            {
                "event_type",
                "occurred_at_utc",
                "pair_key",
                "payload",
                "target_session",
            },
            "research event",
        )
        return cls(
            event_type=value["event_type"],
            occurred_at_utc=value["occurred_at_utc"],
            target_session=value["target_session"],
            pair_key=value["pair_key"],
            payload=value["payload"],
        )


@dataclass(frozen=True)
class ResearchRecord:
    study_id: str
    protocol_id: str
    manifest_sha256: str
    expected_ledger_sha256: str
    sequence: int
    previous_record_sha256: str
    event: ResearchEvent
    schema: int = 1

    def __post_init__(self) -> None:
        if type(self.schema) is not int or self.schema != 1:
            raise ResearchSchemaError("unsupported research-record schema")
        _identifier("study_id", self.study_id)
        _identifier("protocol_id", self.protocol_id)
        _sha256("manifest_sha256", self.manifest_sha256)
        _sha256("expected_ledger_sha256", self.expected_ledger_sha256)
        if type(self.sequence) is not int or self.sequence < 1:
            raise ResearchSchemaError("sequence must be a positive integer")
        _sha256(
            "previous_record_sha256",
            self.previous_record_sha256,
            permit_genesis=True,
        )
        if self.sequence == 1 and self.previous_record_sha256 != GENESIS_SHA256:
            raise ResearchSchemaError("first record must reference genesis")
        if self.sequence > 1 and self.previous_record_sha256 == GENESIS_SHA256:
            raise ResearchSchemaError("only the first record may reference genesis")
        if not isinstance(self.event, ResearchEvent):
            raise ResearchSchemaError("event must be a ResearchEvent")

    def body(self) -> Dict[str, Any]:
        return {
            "event": self.event.payload_document(),
            "expected_ledger_sha256": self.expected_ledger_sha256,
            "manifest_sha256": self.manifest_sha256,
            "previous_record_sha256": self.previous_record_sha256,
            "protocol_id": self.protocol_id,
            "schema": self.schema,
            "sequence": self.sequence,
            "study_id": self.study_id,
        }

    @property
    def record_sha256(self) -> str:
        return domain_sha256(RECORD_DOMAIN, self.body())

    def envelope(self) -> Dict[str, Any]:
        return {
            "record": self.body(),
            "record_sha256": self.record_sha256,
        }

    @classmethod
    def from_envelope(cls, value: Mapping[str, Any]) -> "ResearchRecord":
        _exact_keys(value, {"record", "record_sha256"}, "research record envelope")
        body = value["record"]
        if not isinstance(body, Mapping):
            raise ResearchSchemaError("record body must be an object")
        _exact_keys(
            body,
            {
                "event",
                "expected_ledger_sha256",
                "manifest_sha256",
                "previous_record_sha256",
                "protocol_id",
                "schema",
                "sequence",
                "study_id",
            },
            "research record",
        )
        event = body["event"]
        if not isinstance(event, Mapping):
            raise ResearchSchemaError("event must be an object")
        record = cls(
            study_id=body["study_id"],
            protocol_id=body["protocol_id"],
            manifest_sha256=body["manifest_sha256"],
            expected_ledger_sha256=body["expected_ledger_sha256"],
            sequence=body["sequence"],
            previous_record_sha256=body["previous_record_sha256"],
            event=ResearchEvent.from_payload_document(event),
            schema=body["schema"],
        )
        if value["record_sha256"] != record.record_sha256:
            raise ResearchSchemaError("research record digest mismatch")
        return record


__all__ = [
    "ArmOutcome",
    "EventType",
    "ExpectedSession",
    "ExpectedSessionLedger",
    "GENESIS_SHA256",
    "MissingReason",
    "PairIdentity",
    "ResearchArm",
    "ResearchEvent",
    "ResearchRecord",
    "ResearchSchemaError",
    "SelectionKind",
    "canonical_session",
    "canonical_utc",
    "domain_sha256",
    "parse_utc",
]
