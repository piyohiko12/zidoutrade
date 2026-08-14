"""Immutable local ledger and pure replay verifier for Q014 V2 research.

This module is intentionally separated from strategy, market data, runtime, and
execution code.  It provides only local, owner-controlled evidence.  Its hash
chain is explicitly ``LOCAL_CHAIN_ONLY`` and is not an external timestamp or a
proof that a real-world observation occurred.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .research_events import (
    EventType,
    ExpectedSessionLedger,
    GENESIS_SHA256,
    MissingReason,
    PAIR_RESERVATION_DOMAIN,
    PairIdentity,
    ResearchArm,
    ResearchEvent,
    ResearchRecord,
    ResearchSchemaError,
    canonical_utc,
    domain_sha256,
    parse_utc,
)
from .storage import canonical_json_bytes


class ResearchLedgerError(RuntimeError):
    """Base class for Q014 V2 ledger failures."""


class LedgerIntegrityError(ResearchLedgerError):
    """Persisted bytes, ownership, links, or chain evidence are invalid."""


class ReplayError(ResearchLedgerError):
    """The immutable events do not form the preregistered state machine."""


class QueryOnlyRecoveryRequired(ResearchLedgerError):
    """An immutable artifact already exists and must never be retried."""


class SealError(ResearchLedgerError):
    """A clean seal cannot be created from the supplied evidence."""


class ObservationBranch(str, Enum):
    NONE = "NONE"
    OBSERVED = "OBSERVED"
    OBSERVATION_MISSING = "OBSERVATION_MISSING"


class SessionTerminal(str, Enum):
    NONE = "NONE"
    SESSION_COMPLETE = "SESSION_COMPLETE"
    SESSION_MISSING = "SESSION_MISSING"


class VerificationVerdict(str, Enum):
    CLEAN = "CLEAN"
    PENDING = "PENDING"
    FAILURE = "FAILURE"


class FindingCode(str, Enum):
    TERMINAL_PENDING = "TERMINAL_PENDING"
    DEADLINE_TERMINAL_MISSING = "DEADLINE_TERMINAL_MISSING"
    SESSION_REPORTED_MISSING = "SESSION_REPORTED_MISSING"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


REPORT_DOMAIN = b"zidoutrade/q014-v2/verification-report/v1\0"
ANCHOR_DOMAIN = b"zidoutrade/q014-v2/retained-anchor-set/v1\0"
_RECORD_NAME = re.compile(r"^(\d{12})\.json$")
_PAIR_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")
_SOURCE_ROOT = Path(__file__).resolve().parents[2]
_WRITER_CAPABILITY = object()


def _exact_canonical_equal(actual: Any, expected: Any) -> bool:
    """Compare JSON-shaped values without Python's bool/int equivalence."""
    if type(actual) is not type(expected):
        return False
    if type(actual) is dict:
        if len(actual) != len(expected):
            return False
        if any(type(key) is not str for key in actual):
            return False
        if set(actual) != set(expected):
            return False
        return all(
            _exact_canonical_equal(actual[key], expected[key])
            for key in expected
        )
    if type(actual) is list:
        return len(actual) == len(expected) and all(
            _exact_canonical_equal(left, right)
            for left, right in zip(actual, expected)
        )
    return actual == expected


@dataclass(frozen=True)
class LedgerAnchor:
    sequence: int
    record_sha256: str

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("anchor sequence must be a nonnegative integer")
        if not re.fullmatch(r"[0-9a-f]{64}", self.record_sha256):
            raise ValueError("anchor digest must be lowercase SHA-256")
        if self.sequence == 0 and self.record_sha256 != GENESIS_SHA256:
            raise ValueError("sequence-zero anchor must be genesis")
        if self.sequence > 0 and self.record_sha256 == GENESIS_SHA256:
            raise ValueError("nonzero anchor must not be genesis")


@dataclass(frozen=True)
class PairReservation:
    """Private, immutable reservation keyed by one EXPECTED session."""

    identity: PairIdentity
    manifest_sha256: str
    expected_ledger_sha256: str
    next_sequence: int
    expected_previous_record_sha256: str
    schema: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.identity, PairIdentity):
            raise ResearchSchemaError("reservation identity is invalid")
        if type(self.schema) is not int or self.schema != 1:
            raise ResearchSchemaError("unsupported pair-reservation schema")
        for name, digest in (
            ("manifest_sha256", self.manifest_sha256),
            ("expected_ledger_sha256", self.expected_ledger_sha256),
            ("expected_previous_record_sha256", self.expected_previous_record_sha256),
        ):
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ResearchSchemaError("%s must be lowercase SHA-256" % name)
        if self.manifest_sha256 == GENESIS_SHA256:
            raise ResearchSchemaError("manifest digest cannot be genesis")
        if self.expected_ledger_sha256 == GENESIS_SHA256:
            raise ResearchSchemaError("ledger digest cannot be genesis")
        if type(self.next_sequence) is not int or self.next_sequence < 1:
            raise ResearchSchemaError("next_sequence must be positive")
        if self.next_sequence == 1:
            if self.expected_previous_record_sha256 != GENESIS_SHA256:
                raise ResearchSchemaError("first reservation must bind genesis")
        elif self.expected_previous_record_sha256 == GENESIS_SHA256:
            raise ResearchSchemaError("later reservation cannot bind genesis")

    def unsigned_payload(self) -> Dict[str, Any]:
        return {
            "expected_ledger_sha256": self.expected_ledger_sha256,
            "expected_previous_record_sha256": self.expected_previous_record_sha256,
            "identity": self.identity.payload(),
            "kind": "PAIR_RESERVATION",
            "manifest_sha256": self.manifest_sha256,
            "next_sequence": self.next_sequence,
            "pair_key": self.identity.pair_key,
            "schema": self.schema,
        }

    @property
    def reservation_sha256(self) -> str:
        return domain_sha256(PAIR_RESERVATION_DOMAIN, self.unsigned_payload())

    def document(self) -> Dict[str, Any]:
        result = self.unsigned_payload()
        result["reservation_sha256"] = self.reservation_sha256
        return result

    @classmethod
    def from_document(cls, value: Mapping[str, Any]) -> "PairReservation":
        expected = {
            "expected_ledger_sha256",
            "expected_previous_record_sha256",
            "identity",
            "kind",
            "manifest_sha256",
            "next_sequence",
            "pair_key",
            "reservation_sha256",
            "schema",
        }
        if set(value) != expected:
            raise ResearchSchemaError("pair reservation fields do not match schema")
        if value["kind"] != "PAIR_RESERVATION":
            raise ResearchSchemaError("unexpected reservation kind")
        identity_value = value["identity"]
        if not isinstance(identity_value, Mapping):
            raise ResearchSchemaError("reservation identity must be an object")
        reservation = cls(
            identity=PairIdentity.from_payload(identity_value),
            manifest_sha256=value["manifest_sha256"],
            expected_ledger_sha256=value["expected_ledger_sha256"],
            next_sequence=value["next_sequence"],
            expected_previous_record_sha256=value[
                "expected_previous_record_sha256"
            ],
            schema=value["schema"],
        )
        if not _exact_canonical_equal(dict(value), reservation.document()):
            raise ResearchSchemaError("reservation document binding mismatch")
        return reservation


@dataclass(frozen=True)
class SessionReplayState:
    target_session: str
    pair_key: Optional[str]
    branch: ObservationBranch
    terminal_arms: Tuple[ResearchArm, ...]
    terminal: SessionTerminal
    missing_reason: Optional[MissingReason]
    terminal_at_utc: Optional[str]


@dataclass(frozen=True)
class ReplayResult:
    sessions: Tuple[SessionReplayState, ...]
    record_count: int
    head_sha256: str

    def for_session(self, target_session: str) -> SessionReplayState:
        for item in self.sessions:
            if item.target_session == target_session:
                return item
        raise KeyError(target_session)


@dataclass(frozen=True)
class VerificationFinding:
    code: FindingCode
    target_session: Optional[str]

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "code", FindingCode(self.code))
        except ValueError as exc:
            raise ValueError("unknown verification finding") from exc
        if self.target_session is not None:
            try:
                canonical = datetime.fromisoformat(self.target_session).date().isoformat()
            except (TypeError, ValueError) as exc:
                raise ValueError("finding session is not canonical") from exc
            if canonical != self.target_session:
                raise ValueError("finding session is not canonical")

    def payload(self) -> Dict[str, Any]:
        return {"code": self.code.value, "target_session": self.target_session}


@dataclass(frozen=True)
class VerificationReport:
    study_id: str
    protocol_id: str
    manifest_sha256: str
    expected_ledger_sha256: str
    expected_sessions_root_sha256: str
    expected_session_count: int
    as_of_utc: str
    verdict: VerificationVerdict
    findings: Tuple[VerificationFinding, ...]
    record_count: int
    head_sha256: str
    complete_session_count: int
    missing_session_count: int
    local_chain_only: bool = True
    automatic_append_performed: bool = False

    def __post_init__(self) -> None:
        canonical_utc(self.as_of_utc, name="as_of_utc")
        for name, value in (
            ("study_id", self.study_id),
            ("protocol_id", self.protocol_id),
        ):
            if not isinstance(value, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value
            ):
                raise ValueError("%s is not canonical" % name)
        for name, value in (
            ("manifest_sha256", self.manifest_sha256),
            ("expected_ledger_sha256", self.expected_ledger_sha256),
            ("expected_sessions_root_sha256", self.expected_sessions_root_sha256),
            ("head_sha256", self.head_sha256),
        ):
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError("%s must be lowercase SHA-256" % name)
        try:
            object.__setattr__(self, "verdict", VerificationVerdict(self.verdict))
        except ValueError as exc:
            raise ValueError("unknown verification verdict") from exc
        try:
            normalized_findings = tuple(self.findings)
        except TypeError as exc:
            raise ValueError("findings must be an iterable") from exc
        if any(not isinstance(item, VerificationFinding) for item in normalized_findings):
            raise ValueError("findings must contain VerificationFinding values")
        object.__setattr__(self, "findings", normalized_findings)
        for name, value in (
            ("expected_session_count", self.expected_session_count),
            ("record_count", self.record_count),
            ("complete_session_count", self.complete_session_count),
            ("missing_session_count", self.missing_session_count),
        ):
            if type(value) is not int or value < 0:
                raise ValueError("%s must be a nonnegative integer" % name)
        if self.complete_session_count + self.missing_session_count > self.expected_session_count:
            raise ValueError("terminal session counts exceed expected sessions")
        failure_codes = {
            FindingCode.DEADLINE_TERMINAL_MISSING,
            FindingCode.INTEGRITY_FAILURE,
        }
        codes = {item.code for item in normalized_findings}
        finding_pairs = tuple(
            (item.code, item.target_session) for item in normalized_findings
        )
        if len(set(finding_pairs)) != len(finding_pairs):
            raise ValueError("verification findings must not be duplicated")
        for item in normalized_findings:
            if item.code is FindingCode.INTEGRITY_FAILURE:
                if item.target_session is not None:
                    raise ValueError("integrity finding must not guess a session")
            elif item.target_session is None:
                raise ValueError("session finding requires a target session")
        reported_missing_targets = {
            item.target_session
            for item in normalized_findings
            if item.code is FindingCode.SESSION_REPORTED_MISSING
        }
        if len(reported_missing_targets) != self.missing_session_count:
            raise ValueError("reported-missing findings/count do not agree")
        if self.verdict is VerificationVerdict.CLEAN:
            if codes & (failure_codes | {FindingCode.TERMINAL_PENDING}):
                raise ValueError("CLEAN report contains a non-clean finding")
            if (
                self.complete_session_count + self.missing_session_count
                != self.expected_session_count
            ):
                raise ValueError("CLEAN report requires every expected terminal")
            if self.expected_session_count == 0 or self.record_count == 0:
                raise ValueError("CLEAN report requires nonempty sealed evidence")
        elif self.verdict is VerificationVerdict.PENDING:
            if FindingCode.TERMINAL_PENDING not in codes or codes & failure_codes:
                raise ValueError("PENDING report has inconsistent findings")
        elif not codes & failure_codes:
            raise ValueError("FAILURE report requires a failure finding")
        if self.record_count == 0 and self.head_sha256 != GENESIS_SHA256:
            raise ValueError("empty report must use genesis head")
        if (
            self.record_count > 0
            and self.head_sha256 == GENESIS_SHA256
            and not (
                self.verdict is VerificationVerdict.FAILURE
                and any(
                    item.code is FindingCode.INTEGRITY_FAILURE
                    for item in normalized_findings
                )
            )
        ):
            raise ValueError("nonempty valid report must not use genesis head")
        if self.local_chain_only is not True:
            raise ValueError("research report must remain LOCAL_CHAIN_ONLY")
        if self.automatic_append_performed is not False:
            raise ValueError("verifier must not append collector events")

    def payload(self) -> Dict[str, Any]:
        return {
            "as_of_utc": self.as_of_utc,
            "automatic_append_performed": self.automatic_append_performed,
            "complete_session_count": self.complete_session_count,
            "expected_ledger_sha256": self.expected_ledger_sha256,
            "expected_session_count": self.expected_session_count,
            "expected_sessions_root_sha256": self.expected_sessions_root_sha256,
            "findings": [item.payload() for item in self.findings],
            "head_sha256": self.head_sha256,
            "local_chain_only": self.local_chain_only,
            "manifest_sha256": self.manifest_sha256,
            "missing_session_count": self.missing_session_count,
            "protocol_id": self.protocol_id,
            "record_count": self.record_count,
            "study_id": self.study_id,
            "verdict": self.verdict.value,
        }

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "VerificationReport":
        expected_fields = {
            "as_of_utc",
            "automatic_append_performed",
            "complete_session_count",
            "expected_ledger_sha256",
            "expected_session_count",
            "expected_sessions_root_sha256",
            "findings",
            "head_sha256",
            "local_chain_only",
            "manifest_sha256",
            "missing_session_count",
            "protocol_id",
            "record_count",
            "study_id",
            "verdict",
        }
        if not isinstance(value, Mapping) or set(value) != expected_fields:
            raise ValueError("verification report fields do not match schema")
        raw_findings = value["findings"]
        if not isinstance(raw_findings, list):
            raise ValueError("verification findings must be an array")
        findings = []
        for raw in raw_findings:
            if not isinstance(raw, Mapping) or set(raw) != {"code", "target_session"}:
                raise ValueError("verification finding fields do not match schema")
            findings.append(
                VerificationFinding(
                    code=raw["code"], target_session=raw["target_session"]
                )
            )
        return cls(
            study_id=value["study_id"],
            protocol_id=value["protocol_id"],
            manifest_sha256=value["manifest_sha256"],
            expected_ledger_sha256=value["expected_ledger_sha256"],
            expected_sessions_root_sha256=value[
                "expected_sessions_root_sha256"
            ],
            expected_session_count=value["expected_session_count"],
            as_of_utc=value["as_of_utc"],
            verdict=value["verdict"],
            findings=tuple(findings),
            record_count=value["record_count"],
            head_sha256=value["head_sha256"],
            complete_session_count=value["complete_session_count"],
            missing_session_count=value["missing_session_count"],
            local_chain_only=value["local_chain_only"],
            automatic_append_performed=value["automatic_append_performed"],
        )

    @property
    def report_sha256(self) -> str:
        return domain_sha256(REPORT_DOMAIN, self.payload())


@dataclass(frozen=True)
class ArtifactReceipt:
    path: Path
    sha256: str
    byte_length: int


@dataclass
class _MutableSession:
    pair_key: Optional[str] = None
    branch: ObservationBranch = ObservationBranch.NONE
    terminal_arms: Optional[Dict[ResearchArm, bool]] = None
    terminal: SessionTerminal = SessionTerminal.NONE
    missing_reason: Optional[MissingReason] = None
    terminal_at_utc: Optional[str] = None

    def __post_init__(self) -> None:
        if self.terminal_arms is None:
            self.terminal_arms = {}


def _validate_anchors(records: Sequence[ResearchRecord], anchors: Sequence[LedgerAnchor]) -> None:
    if any(not isinstance(record, ResearchRecord) for record in records):
        raise ReplayError("record has the wrong type")
    seen: Dict[int, str] = {}
    for anchor in anchors:
        if not isinstance(anchor, LedgerAnchor):
            raise ReplayError("retained anchor has the wrong type")
        previous = seen.get(anchor.sequence)
        if previous is not None and previous != anchor.record_sha256:
            raise ReplayError("retained anchors fork at one sequence")
        seen[anchor.sequence] = anchor.record_sha256
        if anchor.sequence == 0:
            continue
        if anchor.sequence > len(records):
            raise ReplayError("record tail is shorter than a retained anchor")
        if records[anchor.sequence - 1].record_sha256 != anchor.record_sha256:
            raise ReplayError("record chain disagrees with a retained anchor")


def _normalized_anchors(anchors: Sequence[LedgerAnchor]) -> Tuple[LedgerAnchor, ...]:
    by_sequence: Dict[int, LedgerAnchor] = {}
    for anchor in anchors:
        if not isinstance(anchor, LedgerAnchor):
            raise ReplayError("retained anchor has the wrong type")
        existing = by_sequence.get(anchor.sequence)
        if existing is not None and existing.record_sha256 != anchor.record_sha256:
            raise ReplayError("retained anchors fork at one sequence")
        by_sequence[anchor.sequence] = anchor
    return tuple(by_sequence[key] for key in sorted(by_sequence))


def _anchor_set_root(
    anchors: Sequence[LedgerAnchor], expected_ledger: ExpectedSessionLedger
) -> str:
    normalized = _normalized_anchors(anchors)
    return domain_sha256(
        ANCHOR_DOMAIN,
        {
            "anchors": [
                {
                    "record_sha256": item.record_sha256,
                    "sequence": item.sequence,
                }
                for item in normalized
            ],
            "expected_ledger_sha256": expected_ledger.ledger_sha256,
            "manifest_sha256": expected_ledger.manifest_sha256,
            "protocol_id": expected_ledger.protocol_id,
            "study_id": expected_ledger.study_id,
        },
    )


def replay_records(
    expected_ledger: ExpectedSessionLedger,
    records: Sequence[ResearchRecord],
    reservations: Sequence[PairReservation],
    *,
    retained_anchors: Sequence[LedgerAnchor] = (),
) -> ReplayResult:
    """Purely replay a complete synthetic or persisted event sequence."""

    if not isinstance(expected_ledger, ExpectedSessionLedger):
        raise ReplayError("expected ledger has the wrong type")
    try:
        records = tuple(records)
        reservations = tuple(reservations)
        anchors = tuple(retained_anchors)
    except TypeError as exc:
        raise ReplayError("replay inputs must be iterable") from exc
    _validate_anchors(records, anchors)

    expected = {item.target_session: item for item in expected_ledger.sessions}
    reservation_by_session: Dict[str, PairReservation] = {}
    pair_keys: Dict[str, str] = {}
    for reservation in reservations:
        if not isinstance(reservation, PairReservation):
            raise ReplayError("reservation has the wrong type")
        identity = reservation.identity
        if identity.target_session not in expected:
            raise ReplayError("reservation session is not EXPECTED")
        if identity.study_id != expected_ledger.study_id:
            raise ReplayError("reservation study identity mismatch")
        if identity.protocol_id != expected_ledger.protocol_id:
            raise ReplayError("reservation protocol identity mismatch")
        if reservation.manifest_sha256 != expected_ledger.manifest_sha256:
            raise ReplayError("reservation manifest mismatch")
        if reservation.expected_ledger_sha256 != expected_ledger.ledger_sha256:
            raise ReplayError("reservation expected-ledger mismatch")
        if identity.target_session in reservation_by_session:
            raise ReplayError("multiple pair reservations for one session")
        if identity.pair_key in pair_keys:
            raise ReplayError("one pair key is reused across sessions")
        reservation_by_session[identity.target_session] = reservation
        pair_keys[identity.pair_key] = identity.target_session

    states = {session: _MutableSession() for session in expected}
    previous_hash = GENESIS_SHA256
    previous_time: Optional[datetime] = None
    ledger_created = parse_utc(expected_ledger.created_at_utc)

    for expected_sequence, record in enumerate(records, start=1):
        if not isinstance(record, ResearchRecord):
            raise ReplayError("record has the wrong type")
        if record.sequence != expected_sequence:
            raise ReplayError("record sequence discontinuity")
        if record.previous_record_sha256 != previous_hash:
            raise ReplayError("record predecessor mismatch")
        if record.study_id != expected_ledger.study_id:
            raise ReplayError("record study identity mismatch")
        if record.protocol_id != expected_ledger.protocol_id:
            raise ReplayError("record protocol identity mismatch")
        if record.manifest_sha256 != expected_ledger.manifest_sha256:
            raise ReplayError("record manifest mismatch")
        if record.expected_ledger_sha256 != expected_ledger.ledger_sha256:
            raise ReplayError("record expected-ledger mismatch")
        event = record.event
        if event.target_session not in expected:
            raise ReplayError("event target_session is not EXPECTED")
        occurred = parse_utc(event.occurred_at_utc)
        if occurred < ledger_created:
            raise ReplayError("event predates the sealed expected ledger")
        if occurred < parse_utc(
            expected[event.target_session].capture_not_before_utc
        ):
            raise ReplayError("event predates the sealed session capture window")
        if previous_time is not None and occurred < previous_time:
            raise ReplayError("event timestamps decrease along the sequence")
        if occurred > parse_utc(expected[event.target_session].deadline_utc):
            raise ReplayError("event occurs after the sealed session deadline")
        previous_time = occurred

        reservation = reservation_by_session.get(event.target_session)
        if reservation is None:
            raise ReplayError("event has no validated pair reservation")
        if event.pair_key != reservation.identity.pair_key:
            raise ReplayError("event pair key does not match reservation preimage")
        state = states[event.target_session]
        if state.terminal is not SessionTerminal.NONE:
            raise ReplayError("event appears after the session terminal")

        if event.event_type is EventType.PAIR_RESERVED:
            if state.pair_key is not None:
                raise ReplayError("PAIR_RESERVED is duplicated")
            if record.sequence != reservation.next_sequence:
                raise ReplayError("PAIR_RESERVED sequence disagrees with reservation")
            if record.previous_record_sha256 != reservation.expected_previous_record_sha256:
                raise ReplayError("PAIR_RESERVED predecessor disagrees with reservation")
            if event.payload["reservation_sha256"] != reservation.reservation_sha256:
                raise ReplayError("PAIR_RESERVED digest mismatch")
            state.pair_key = event.pair_key
        else:
            if state.pair_key is None:
                raise ReplayError("event occurs before PAIR_RESERVED")
            if event.event_type is EventType.OBSERVED:
                if state.branch is not ObservationBranch.NONE:
                    raise ReplayError("observation branch is duplicated")
                state.branch = ObservationBranch.OBSERVED
            elif event.event_type is EventType.OBSERVATION_MISSING:
                if state.branch is not ObservationBranch.NONE:
                    raise ReplayError("observation branches are mutually exclusive")
                state.branch = ObservationBranch.OBSERVATION_MISSING
                state.missing_reason = MissingReason(event.payload["reason"])
            elif event.event_type is EventType.ARM_TERMINAL:
                if state.branch is not ObservationBranch.OBSERVED:
                    raise ReplayError("ARM_TERMINAL requires OBSERVED")
                arm = ResearchArm(event.payload["arm"])
                assert state.terminal_arms is not None
                if arm in state.terminal_arms:
                    raise ReplayError("arm terminal is duplicated")
                state.terminal_arms[arm] = True
            elif event.event_type is EventType.SESSION_COMPLETE:
                if state.branch is not ObservationBranch.OBSERVED:
                    raise ReplayError("SESSION_COMPLETE requires OBSERVED")
                assert state.terminal_arms is not None
                if set(state.terminal_arms) != set(ResearchArm):
                    raise ReplayError(
                        "SESSION_COMPLETE requires both arm terminals"
                    )
                state.terminal = SessionTerminal.SESSION_COMPLETE
                state.terminal_at_utc = event.occurred_at_utc
            elif event.event_type is EventType.SESSION_MISSING:
                if state.branch is not ObservationBranch.OBSERVATION_MISSING:
                    raise ReplayError(
                        "SESSION_MISSING requires OBSERVATION_MISSING"
                    )
                reason = MissingReason(event.payload["reason"])
                if reason is not state.missing_reason:
                    raise ReplayError("missing reasons do not match")
                state.terminal = SessionTerminal.SESSION_MISSING
                state.terminal_at_utc = event.occurred_at_utc
            else:  # pragma: no cover - EventType is closed above.
                raise ReplayError("unknown research event")
        previous_hash = record.record_sha256

    outstanding: List[PairReservation] = []
    for reservation in reservations:
        reserved_sequence = reservation.next_sequence
        if reserved_sequence <= len(records):
            reserved_record = records[reserved_sequence - 1]
            if (
                reserved_record.event.event_type is not EventType.PAIR_RESERVED
                or reserved_record.event.target_session
                != reservation.identity.target_session
                or reserved_record.event.pair_key != reservation.identity.pair_key
            ):
                raise ReplayError("a reserved sequence was consumed by another effect")
        else:
            if reserved_sequence != len(records) + 1:
                raise ReplayError("pair reservation leaves a sequence gap")
            outstanding.append(reservation)
    if len(outstanding) > 1:
        raise ReplayError("multiple pair reservations fork the next sequence")

    frozen = tuple(
        SessionReplayState(
            target_session=session,
            pair_key=states[session].pair_key,
            branch=states[session].branch,
            terminal_arms=tuple(
                sorted(
                    (states[session].terminal_arms or {}).keys(),
                    key=lambda item: item.value,
                )
            ),
            terminal=states[session].terminal,
            missing_reason=states[session].missing_reason,
            terminal_at_utc=states[session].terminal_at_utc,
        )
        for session in sorted(states)
    )
    return ReplayResult(
        sessions=frozen,
        record_count=len(records),
        head_sha256=records[-1].record_sha256 if records else GENESIS_SHA256,
    )


def verify_dataset(
    expected_ledger: ExpectedSessionLedger,
    records: Sequence[ResearchRecord],
    reservations: Sequence[PairReservation],
    *,
    as_of_utc: str,
    retained_anchors: Sequence[LedgerAnchor] = (),
) -> VerificationReport:
    """Pure verifier; ``as_of_utc`` and all deadlines are injected inputs."""

    canonical_utc(as_of_utc, name="as_of_utc")
    if not isinstance(expected_ledger, ExpectedSessionLedger):
        return VerificationReport(
            study_id="INVALID_INPUT",
            protocol_id="INVALID_INPUT",
            manifest_sha256="f" * 64,
            expected_ledger_sha256="e" * 64,
            expected_sessions_root_sha256="d" * 64,
            expected_session_count=0,
            as_of_utc=as_of_utc,
            verdict=VerificationVerdict.FAILURE,
            findings=(VerificationFinding(FindingCode.INTEGRITY_FAILURE, None),),
            record_count=0,
            head_sha256=GENESIS_SHA256,
            complete_session_count=0,
            missing_session_count=0,
        )
    as_of = parse_utc(as_of_utc)
    if as_of < parse_utc(expected_ledger.created_at_utc):
        raise ResearchSchemaError("as_of_utc predates the expected ledger")
    try:
        normalized_records = tuple(records)
        normalized_reservations = tuple(reservations)
        normalized_anchors = tuple(retained_anchors)
    except TypeError:
        normalized_records = ()
        normalized_reservations = ()
        normalized_anchors = ()
        invalid_container = True
    else:
        invalid_container = False
    safe_head = GENESIS_SHA256
    if normalized_records and isinstance(normalized_records[-1], ResearchRecord):
        safe_head = normalized_records[-1].record_sha256
    try:
        if invalid_container:
            raise ReplayError("verification input is not iterable")
        replay = replay_records(
            expected_ledger,
            normalized_records,
            normalized_reservations,
            retained_anchors=normalized_anchors,
        )
        if any(
            parse_utc(record.event.occurred_at_utc) > as_of
            for record in normalized_records
        ):
            raise ReplayError("event occurs after verifier as_of_utc")
    except (ReplayError, ResearchSchemaError):
        return VerificationReport(
            study_id=expected_ledger.study_id,
            protocol_id=expected_ledger.protocol_id,
            manifest_sha256=expected_ledger.manifest_sha256,
            expected_ledger_sha256=expected_ledger.ledger_sha256,
            expected_sessions_root_sha256=expected_ledger.expected_sessions_root_sha256,
            expected_session_count=expected_ledger.session_count,
            as_of_utc=as_of_utc,
            verdict=VerificationVerdict.FAILURE,
            findings=(VerificationFinding(FindingCode.INTEGRITY_FAILURE, None),),
            record_count=len(normalized_records),
            head_sha256=safe_head if all(
                isinstance(item, ResearchRecord) for item in normalized_records
            ) else GENESIS_SHA256,
            complete_session_count=0,
            missing_session_count=0,
        )

    deadline_by_session = {
        item.target_session: parse_utc(item.deadline_utc)
        for item in expected_ledger.sessions
    }
    findings: List[VerificationFinding] = []
    complete_count = 0
    missing_count = 0
    for state in replay.sessions:
        if state.terminal is SessionTerminal.SESSION_COMPLETE:
            complete_count += 1
        elif state.terminal is SessionTerminal.SESSION_MISSING:
            missing_count += 1
            findings.append(
                VerificationFinding(
                    FindingCode.SESSION_REPORTED_MISSING, state.target_session
                )
            )
        elif as_of > deadline_by_session[state.target_session]:
            findings.append(
                VerificationFinding(
                    FindingCode.DEADLINE_TERMINAL_MISSING, state.target_session
                )
            )
        else:
            findings.append(
                VerificationFinding(FindingCode.TERMINAL_PENDING, state.target_session)
            )

    if any(item.code is FindingCode.DEADLINE_TERMINAL_MISSING for item in findings):
        verdict = VerificationVerdict.FAILURE
    elif any(item.code is FindingCode.TERMINAL_PENDING for item in findings):
        verdict = VerificationVerdict.PENDING
    else:
        verdict = VerificationVerdict.CLEAN
    return VerificationReport(
        study_id=expected_ledger.study_id,
        protocol_id=expected_ledger.protocol_id,
        manifest_sha256=expected_ledger.manifest_sha256,
        expected_ledger_sha256=expected_ledger.ledger_sha256,
        expected_sessions_root_sha256=expected_ledger.expected_sessions_root_sha256,
        expected_session_count=expected_ledger.session_count,
        as_of_utc=as_of_utc,
        verdict=verdict,
        findings=tuple(findings),
        record_count=replay.record_count,
        head_sha256=replay.head_sha256,
        complete_session_count=complete_count,
        missing_session_count=missing_count,
    )


def _storage_failure_report(
    expected_ledger: ExpectedSessionLedger, *, as_of_utc: str
) -> VerificationReport:
    canonical_utc(as_of_utc, name="as_of_utc")
    return VerificationReport(
        study_id=expected_ledger.study_id,
        protocol_id=expected_ledger.protocol_id,
        manifest_sha256=expected_ledger.manifest_sha256,
        expected_ledger_sha256=expected_ledger.ledger_sha256,
        expected_sessions_root_sha256=expected_ledger.expected_sessions_root_sha256,
        expected_session_count=expected_ledger.session_count,
        as_of_utc=as_of_utc,
        verdict=VerificationVerdict.FAILURE,
        findings=(VerificationFinding(FindingCode.INTEGRITY_FAILURE, None),),
        record_count=0,
        head_sha256=GENESIS_SHA256,
        complete_session_count=0,
        missing_session_count=0,
    )


def _duplicate_rejecting_object(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LedgerIntegrityError("duplicate JSON key")
        result[key] = value
    return result


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(str(directory), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _validate_directory(path: Path, label: str) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise LedgerIntegrityError("%s does not exist" % label) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise LedgerIntegrityError("%s must be a real directory" % label)
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise LedgerIntegrityError("%s must be owned by the current user" % label)
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise LedgerIntegrityError("%s must have owner-only mode 0700" % label)
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LedgerIntegrityError("cannot resolve %s" % label) from exc


def _scandir_entries(directory: Path, label: str) -> Tuple[os.DirEntry, ...]:
    try:
        with os.scandir(str(directory)) as entries:
            return tuple(sorted(entries, key=lambda item: item.name))
    except OSError as exc:
        raise LedgerIntegrityError("cannot scan %s" % label) from exc


def _read_canonical_artifact(
    path: Path, label: str
) -> Tuple[Mapping[str, Any], ArtifactReceipt]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode):
            raise LedgerIntegrityError("%s must not be a symlink" % label)
        fd = os.open(str(path), flags)
    except LedgerIntegrityError:
        raise
    except OSError as exc:
        raise LedgerIntegrityError("cannot open %s safely" % label) from exc
    try:
        info = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino):
            raise LedgerIntegrityError("%s changed while opening" % label)
        if not stat.S_ISREG(info.st_mode):
            raise LedgerIntegrityError("%s must be a regular file" % label)
        if info.st_nlink != 1:
            raise LedgerIntegrityError("%s must have exactly one hard link" % label)
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise LedgerIntegrityError("%s must be owned by the current user" % label)
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise LedgerIntegrityError("%s must have mode 0600" % label)
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > 16 * 1024 * 1024:
                raise LedgerIntegrityError("%s exceeds the size limit" % label)
            chunks.append(chunk)
        if total != info.st_size:
            raise LedgerIntegrityError("%s changed while reading" % label)
        raw = b"".join(chunks)
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_duplicate_rejecting_object
        )
    except LedgerIntegrityError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LedgerIntegrityError("%s is not valid UTF-8 JSON" % label) from exc
    finally:
        os.close(fd)
    if not isinstance(value, dict):
        raise LedgerIntegrityError("%s must contain a JSON object" % label)
    try:
        canonical = canonical_json_bytes(value)
    except Exception as exc:
        raise LedgerIntegrityError("%s is not canonical JSON" % label) from exc
    if raw != canonical:
        raise LedgerIntegrityError("%s is not canonical JSON" % label)
    return value, ArtifactReceipt(
        path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_length=len(raw),
    )


def _read_canonical(path: Path, label: str) -> Mapping[str, Any]:
    value, _receipt = _read_canonical_artifact(path, label)
    return value


def _write_exclusive(path: Path, document: Mapping[str, Any]) -> ArtifactReceipt:
    payload = canonical_json_bytes(dict(document))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(path), flags, 0o600)
    except FileExistsError as exc:
        raise QueryOnlyRecoveryRequired(
            "immutable artifact already exists: %s" % path.name
        ) from exc
    try:
        os.fchmod(fd, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise LedgerIntegrityError("short immutable-artifact write")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_directory(path.parent)
    return ArtifactReceipt(
        path=path,
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_length=len(payload),
    )


class ResearchLedger:
    EXPECTED_LEDGER_NAME = "expected-ledger.json"
    VERDICT_LATCH_NAME = "verdict-publication.json"
    CLEAN_SEAL_NAME = "clean-seal.json"
    CLEAN_REPORT_NAME = "verification-clean.json"
    FAILURE_REPORT_NAME = "failure-report.json"

    def __init__(
        self,
        root: Path,
        expected_ledger: ExpectedSessionLedger,
        *,
        _capability: object = None,
    ) -> None:
        self.root = _validate_directory(Path(root), "research root")
        if self.root == _SOURCE_ROOT or _SOURCE_ROOT in self.root.parents:
            raise LedgerIntegrityError("research root must be outside the repository")
        self.records_directory = _validate_directory(
            self.root / "records", "records directory"
        )
        self.pairs_directory = _validate_directory(
            self.root / "pairs", "pairs directory"
        )
        self.reports_directory = _validate_directory(
            self.root / "reports", "reports directory"
        )
        self.anchors_directory = _validate_directory(
            self.root / "anchors", "anchors directory"
        )
        self.reservation_slots_directory = _validate_directory(
            self.root / "reservation-slots", "reservation-slots directory"
        )
        persisted_value = _read_canonical(
            self.root / self.EXPECTED_LEDGER_NAME, "sealed expected ledger"
        )
        try:
            persisted_ledger = ExpectedSessionLedger.from_sealed_document(
                persisted_value
            )
        except ResearchSchemaError as exc:
            raise LedgerIntegrityError("sealed expected ledger is invalid") from exc
        if not _exact_canonical_equal(
            dict(persisted_value), persisted_ledger.sealed_document()
        ):
            raise LedgerIntegrityError("sealed expected ledger is not exact")
        if not _exact_canonical_equal(
            persisted_ledger.sealed_document(), expected_ledger.sealed_document()
        ):
            raise LedgerIntegrityError("constructor ledger does not match sealed bytes")
        self.expected_ledger = persisted_ledger
        self._writer_enabled = _capability is _WRITER_CAPABILITY

    def _validate_layout(self) -> None:
        validated_root = _validate_directory(self.root, "research root")
        if validated_root != self.root:
            raise LedgerIntegrityError("research root identity changed")
        for path, label in (
            (self.records_directory, "records directory"),
            (self.pairs_directory, "pairs directory"),
            (self.reports_directory, "reports directory"),
            (self.anchors_directory, "anchors directory"),
            (self.reservation_slots_directory, "reservation-slots directory"),
        ):
            if _validate_directory(path, label) != path:
                raise LedgerIntegrityError("%s identity changed" % label)
        value = _read_canonical(
            self.root / self.EXPECTED_LEDGER_NAME, "sealed expected ledger"
        )
        try:
            persisted = ExpectedSessionLedger.from_sealed_document(value)
        except ResearchSchemaError as exc:
            raise LedgerIntegrityError("sealed expected ledger is invalid") from exc
        if not _exact_canonical_equal(dict(value), persisted.sealed_document()):
            raise LedgerIntegrityError("sealed expected ledger is not exact")
        if not _exact_canonical_equal(
            persisted.sealed_document(), self.expected_ledger.sealed_document()
        ):
            raise LedgerIntegrityError("sealed expected ledger changed")

    def _validate_report_identity(self, report: VerificationReport) -> None:
        if report.study_id != self.expected_ledger.study_id:
            raise LedgerIntegrityError("published report study mismatch")
        if report.protocol_id != self.expected_ledger.protocol_id:
            raise LedgerIntegrityError("published report protocol mismatch")
        if report.manifest_sha256 != self.expected_ledger.manifest_sha256:
            raise LedgerIntegrityError("published report manifest mismatch")
        if report.expected_ledger_sha256 != self.expected_ledger.ledger_sha256:
            raise LedgerIntegrityError("published report ledger mismatch")
        if (
            report.expected_sessions_root_sha256
            != self.expected_ledger.expected_sessions_root_sha256
        ):
            raise LedgerIntegrityError("published expected-session root mismatch")
        if report.expected_session_count != self.expected_ledger.session_count:
            raise LedgerIntegrityError("published expected-session count mismatch")
        expected_sessions = {
            item.target_session for item in self.expected_ledger.sessions
        }
        if any(
            item.target_session is not None
            and item.target_session not in expected_sessions
            for item in report.findings
        ):
            raise LedgerIntegrityError("published finding names an unknown session")

    def _validate_publication_state(self) -> Optional[VerificationReport]:
        entries = _scandir_entries(
            self.reports_directory, "terminal report directory"
        )
        names = frozenset(item.name for item in entries)
        if not names:
            return None
        failure_set = frozenset(
            {self.VERDICT_LATCH_NAME, self.FAILURE_REPORT_NAME}
        )
        clean_set = frozenset({
            self.VERDICT_LATCH_NAME,
            self.CLEAN_REPORT_NAME,
            self.CLEAN_SEAL_NAME,
        })
        if names not in {failure_set, clean_set}:
            raise LedgerIntegrityError(
                "terminal publication is incomplete, conflicting, or has extra files"
            )

        latch, latch_receipt = _read_canonical_artifact(
            self.reports_directory / self.VERDICT_LATCH_NAME,
            "verdict publication reservation",
        )
        latch_fields = {
            "as_of_utc",
            "expected_ledger_sha256",
            "expected_session_count",
            "expected_sessions_root_sha256",
            "head_sha256",
            "kind",
            "manifest_sha256",
            "protocol_id",
            "record_count",
            "schema",
            "study_id",
            "verdict",
            "verification_report_sha256",
        }
        if set(latch) != latch_fields:
            raise LedgerIntegrityError("verdict reservation fields do not match schema")
        if (
            latch["kind"] != "VERDICT_PUBLICATION_RESERVATION"
            or type(latch["schema"]) is not int
            or latch["schema"] != 1
        ):
            raise LedgerIntegrityError("verdict reservation kind/schema mismatch")

        branch_name = (
            self.FAILURE_REPORT_NAME
            if names == failure_set
            else self.CLEAN_REPORT_NAME
        )
        branch, branch_receipt = _read_canonical_artifact(
            self.reports_directory / branch_name, "published verification report"
        )
        branch_fields = {
            "kind",
            "report",
            "verdict_publication_artifact_sha256",
            "verification_report_sha256",
        }
        if set(branch) != branch_fields:
            raise LedgerIntegrityError("published report fields do not match schema")
        expected_kind = (
            "FAILURE_REPORT"
            if names == failure_set
            else "CLEAN_VERIFICATION_REPORT"
        )
        if branch["kind"] != expected_kind:
            raise LedgerIntegrityError("published report kind mismatch")
        raw_report = branch["report"]
        try:
            report = VerificationReport.from_payload(raw_report)
        except (TypeError, ValueError, ResearchSchemaError) as exc:
            raise LedgerIntegrityError("published verification report is invalid") from exc
        self._validate_report_identity(report)
        if report.report_sha256 != branch["verification_report_sha256"]:
            raise LedgerIntegrityError("published verification report digest mismatch")
        if branch["verdict_publication_artifact_sha256"] != latch_receipt.sha256:
            raise LedgerIntegrityError("published report latch binding mismatch")

        latch_expected = {
            "as_of_utc": report.as_of_utc,
            "expected_ledger_sha256": self.expected_ledger.ledger_sha256,
            "expected_session_count": self.expected_ledger.session_count,
            "expected_sessions_root_sha256": self.expected_ledger.expected_sessions_root_sha256,
            "head_sha256": report.head_sha256,
            "kind": "VERDICT_PUBLICATION_RESERVATION",
            "manifest_sha256": self.expected_ledger.manifest_sha256,
            "protocol_id": self.expected_ledger.protocol_id,
            "record_count": report.record_count,
            "schema": 1,
            "study_id": self.expected_ledger.study_id,
            "verdict": report.verdict.value,
            "verification_report_sha256": report.report_sha256,
        }
        if not _exact_canonical_equal(dict(latch), latch_expected):
            raise LedgerIntegrityError("verdict reservation/report binding mismatch")
        if names == failure_set and report.verdict is not VerificationVerdict.FAILURE:
            raise LedgerIntegrityError("failure artifact does not contain FAILURE")
        if names == clean_set and report.verdict is not VerificationVerdict.CLEAN:
            raise LedgerIntegrityError("clean artifact does not contain CLEAN")

        try:
            records = self.read_records()
            anchors = self._effective_anchors(records, ())
            reservations = self.read_reservations()
        except (LedgerIntegrityError, ReplayError, ResearchSchemaError):
            fresh = _storage_failure_report(
                self.expected_ledger, as_of_utc=report.as_of_utc
            )
            records = ()
            anchors = ()
        else:
            fresh = verify_dataset(
                self.expected_ledger,
                records,
                reservations,
                as_of_utc=report.as_of_utc,
                retained_anchors=anchors,
            )
        if not _exact_canonical_equal(fresh.payload(), report.payload()):
            raise LedgerIntegrityError("published report does not match replay state")

        branch_expected = {
            "kind": expected_kind,
            "report": report.payload(),
            "verdict_publication_artifact_sha256": latch_receipt.sha256,
            "verification_report_sha256": report.report_sha256,
        }
        if not _exact_canonical_equal(dict(branch), branch_expected):
            raise LedgerIntegrityError("published report bindings are not exact")

        if names == clean_set:
            seal, _seal_receipt = _read_canonical_artifact(
                self.reports_directory / self.CLEAN_SEAL_NAME, "clean study seal"
            )
            seal_fields = {
                "clean_report_artifact_sha256",
                "expected_ledger_sha256",
                "expected_session_count",
                "expected_sessions_root_sha256",
                "kind",
                "local_chain_only",
                "manifest_sha256",
                "pre_seal_head_sha256",
                "pre_seal_record_count",
                "protocol_id",
                "retained_anchor_count",
                "retained_anchor_root_sha256",
                "schema",
                "study_id",
                "verdict",
                "verdict_publication_artifact_sha256",
                "verification_report_sha256",
            }
            if set(seal) != seal_fields:
                raise LedgerIntegrityError("clean seal fields do not match schema")
            expected_seal = {
                "clean_report_artifact_sha256": branch_receipt.sha256,
                "expected_ledger_sha256": self.expected_ledger.ledger_sha256,
                "expected_session_count": self.expected_ledger.session_count,
                "expected_sessions_root_sha256": self.expected_ledger.expected_sessions_root_sha256,
                "kind": "CLEAN_STUDY_SEAL",
                "local_chain_only": True,
                "manifest_sha256": self.expected_ledger.manifest_sha256,
                "pre_seal_head_sha256": report.head_sha256,
                "pre_seal_record_count": report.record_count,
                "protocol_id": self.expected_ledger.protocol_id,
                "retained_anchor_count": len(anchors),
                "retained_anchor_root_sha256": _anchor_set_root(
                    anchors, self.expected_ledger
                ),
                "schema": 1,
                "study_id": self.expected_ledger.study_id,
                "verdict": VerificationVerdict.CLEAN.value,
                "verdict_publication_artifact_sha256": latch_receipt.sha256,
                "verification_report_sha256": report.report_sha256,
            }
            if not _exact_canonical_equal(dict(seal), expected_seal):
                raise LedgerIntegrityError("clean seal bindings do not match artifacts")
        return report

    @classmethod
    def initialize(
        cls, root: Path, expected_ledger: ExpectedSessionLedger
    ) -> "ResearchLedger":
        requested = Path(root)
        if not requested.is_absolute():
            raise LedgerIntegrityError("research root must be an absolute path")
        resolved_candidate = requested.resolve(strict=False)
        if resolved_candidate == _SOURCE_ROOT or _SOURCE_ROOT in resolved_candidate.parents:
            raise LedgerIntegrityError("research root must be outside the repository")
        if not requested.exists():
            raise LedgerIntegrityError(
                "research root must already exist with owner-only mode 0700"
            )
        root_path = _validate_directory(requested, "research root")
        existing_entries = _scandir_entries(root_path, "research root")
        if existing_entries:
            raise QueryOnlyRecoveryRequired(
                "research root is not empty; initialization is never retried"
            )
        for name in (
            "records",
            "pairs",
            "reports",
            "anchors",
            "reservation-slots",
        ):
            child = root_path / name
            try:
                child.mkdir(mode=0o700)
            except FileExistsError:
                pass
            _validate_directory(child, "%s directory" % name)
        expected_path = root_path / cls.EXPECTED_LEDGER_NAME
        _write_exclusive(expected_path, expected_ledger.sealed_document())
        return cls(root_path, expected_ledger, _capability=_WRITER_CAPABILITY)

    @classmethod
    def open(cls, root: Path) -> "ResearchLedger":
        requested = Path(root)
        if not requested.is_absolute():
            raise LedgerIntegrityError("research root must be an absolute path")
        root_path = _validate_directory(requested, "research root")
        value = _read_canonical(
            root_path / cls.EXPECTED_LEDGER_NAME, "sealed expected ledger"
        )
        try:
            expected = ExpectedSessionLedger.from_sealed_document(value)
        except ResearchSchemaError as exc:
            raise LedgerIntegrityError("sealed expected ledger is invalid") from exc
        instance = cls(root_path, expected)
        instance._validate_publication_state()
        return instance

    def _require_writer(self) -> None:
        self._validate_layout()
        if not self._writer_enabled:
            raise QueryOnlyRecoveryRequired(
                "reopened ledger is query-only; collector effects are never retried"
            )
        artifacts = _scandir_entries(
            self.reports_directory, "terminal report directory"
        )
        if artifacts:
            raise QueryOnlyRecoveryRequired(
                "terminal report or seal exists; the collector is permanently latched"
            )

    def read_reservations(self) -> Tuple[PairReservation, ...]:
        self._validate_layout()
        result: List[PairReservation] = []
        seen_names = set()
        for entry in _scandir_entries(
            self.pairs_directory, "pair-reservation directory"
        ):
            if not _PAIR_NAME.fullmatch(entry.name):
                raise LedgerIntegrityError("unexpected pair-reservation artifact")
            if entry.name in seen_names:
                raise LedgerIntegrityError("duplicate pair-reservation filename")
            seen_names.add(entry.name)
            value = _read_canonical(Path(entry.path), "pair reservation")
            try:
                reservation = PairReservation.from_document(value)
            except ResearchSchemaError as exc:
                raise LedgerIntegrityError("invalid pair reservation") from exc
            if entry.name != reservation.identity.target_session + ".json":
                raise LedgerIntegrityError("reservation filename/session mismatch")
            result.append(reservation)
        slots = _scandir_entries(
            self.reservation_slots_directory, "reservation-slots directory"
        )
        if len(slots) != len(result):
            raise LedgerIntegrityError("reservation/slot count mismatch")
        reservations_by_sequence = {item.next_sequence: item for item in result}
        if len(reservations_by_sequence) != len(result):
            raise LedgerIntegrityError("pair reservations fork one sequence")
        for entry in slots:
            matched = _RECORD_NAME.fullmatch(entry.name)
            if matched is None:
                raise LedgerIntegrityError("unexpected reservation-slot artifact")
            value = _read_canonical(Path(entry.path), "reservation slot")
            expected_fields = {
                "expected_ledger_sha256",
                "expected_previous_record_sha256",
                "kind",
                "manifest_sha256",
                "next_sequence",
                "pair_key",
                "protocol_id",
                "reservation_sha256",
                "schema",
                "study_id",
                "target_session",
            }
            if set(value) != expected_fields:
                raise LedgerIntegrityError("reservation-slot fields do not match schema")
            sequence = int(matched.group(1))
            reservation = reservations_by_sequence.get(sequence)
            if reservation is None:
                raise LedgerIntegrityError("reservation slot has no pair reservation")
            expected_value = {
                "expected_ledger_sha256": self.expected_ledger.ledger_sha256,
                "expected_previous_record_sha256": reservation.expected_previous_record_sha256,
                "kind": "PAIR_RESERVATION_SLOT",
                "manifest_sha256": self.expected_ledger.manifest_sha256,
                "next_sequence": reservation.next_sequence,
                "pair_key": reservation.identity.pair_key,
                "protocol_id": self.expected_ledger.protocol_id,
                "reservation_sha256": reservation.reservation_sha256,
                "schema": 1,
                "study_id": self.expected_ledger.study_id,
                "target_session": reservation.identity.target_session,
            }
            if not _exact_canonical_equal(dict(value), expected_value):
                raise LedgerIntegrityError("reservation slot binding mismatch")
        return tuple(result)

    def read_records(self) -> Tuple[ResearchRecord, ...]:
        self._validate_layout()
        result: List[ResearchRecord] = []
        for entry in _scandir_entries(self.records_directory, "records directory"):
            matched = _RECORD_NAME.fullmatch(entry.name)
            if matched is None:
                raise LedgerIntegrityError("unexpected record artifact")
            sequence = int(matched.group(1))
            if sequence != len(result) + 1:
                raise LedgerIntegrityError("record filename sequence discontinuity")
            value = _read_canonical(Path(entry.path), "research record")
            try:
                record = ResearchRecord.from_envelope(value)
            except ResearchSchemaError as exc:
                raise LedgerIntegrityError("invalid research record") from exc
            if not _exact_canonical_equal(dict(value), record.envelope()):
                raise LedgerIntegrityError("research record document is not exact")
            if record.sequence != sequence:
                raise LedgerIntegrityError("record filename/body sequence mismatch")
            result.append(record)
        return tuple(result)

    def read_retained_anchors(
        self, *, expected_record_count: Optional[int] = None
    ) -> Tuple[LedgerAnchor, ...]:
        self._validate_layout()
        result: List[LedgerAnchor] = []
        for entry in _scandir_entries(
            self.anchors_directory, "retained-anchor directory"
        ):
            matched = _RECORD_NAME.fullmatch(entry.name)
            if matched is None:
                raise LedgerIntegrityError("unexpected retained-anchor artifact")
            value = _read_canonical(Path(entry.path), "retained anchor")
            expected_fields = {
                "expected_ledger_sha256",
                "head_sha256",
                "kind",
                "manifest_sha256",
                "protocol_id",
                "record_sha256",
                "schema",
                "sequence",
                "study_id",
            }
            if set(value) != expected_fields:
                raise LedgerIntegrityError("retained-anchor fields do not match schema")
            try:
                anchor = LedgerAnchor(
                    sequence=value["sequence"],
                    record_sha256=value["record_sha256"],
                )
            except (TypeError, ValueError) as exc:
                raise LedgerIntegrityError("invalid retained anchor") from exc
            expected_value = {
                "expected_ledger_sha256": self.expected_ledger.ledger_sha256,
                "head_sha256": anchor.record_sha256,
                "kind": "RETAINED_PREDECESSOR_RECEIPT",
                "manifest_sha256": self.expected_ledger.manifest_sha256,
                "protocol_id": self.expected_ledger.protocol_id,
                "record_sha256": anchor.record_sha256,
                "schema": 1,
                "sequence": anchor.sequence,
                "study_id": self.expected_ledger.study_id,
            }
            if not _exact_canonical_equal(dict(value), expected_value):
                raise LedgerIntegrityError("retained-anchor binding mismatch")
            filename_sequence = int(matched.group(1))
            if anchor.sequence != filename_sequence or anchor.sequence != len(result) + 1:
                raise LedgerIntegrityError("retained-anchor sequence discontinuity")
            result.append(anchor)
        if expected_record_count is not None and len(result) != expected_record_count:
            raise LedgerIntegrityError("record/retained-anchor count mismatch")
        return tuple(result)

    def _effective_anchors(
        self,
        records: Sequence[ResearchRecord],
        supplied: Sequence[LedgerAnchor],
    ) -> Tuple[LedgerAnchor, ...]:
        durable = self.read_retained_anchors(expected_record_count=len(records))
        return _normalized_anchors(tuple(durable) + tuple(supplied))

    def reserve_pair(
        self,
        identity: PairIdentity,
        *,
        expected_sequence: int,
        expected_previous_record_sha256: str,
        retained_anchors: Sequence[LedgerAnchor] = (),
    ) -> PairReservation:
        self._require_writer()
        if identity.study_id != self.expected_ledger.study_id:
            raise ReplayError("pair study identity mismatch")
        if identity.protocol_id != self.expected_ledger.protocol_id:
            raise ReplayError("pair protocol identity mismatch")
        if identity.target_session not in {
            item.target_session for item in self.expected_ledger.sessions
        }:
            raise ReplayError("pair session is not EXPECTED")
        records = self.read_records()
        reservations = self.read_reservations()
        effective_anchors = self._effective_anchors(records, retained_anchors)
        replay = replay_records(
            self.expected_ledger,
            records,
            reservations,
            retained_anchors=effective_anchors,
        )
        earliest_unfinished = next(
            (
                state.target_session
                for state in replay.sessions
                if state.terminal is SessionTerminal.NONE
            ),
            None,
        )
        if earliest_unfinished is None:
            raise ReplayError("all EXPECTED sessions are already terminal")
        if identity.target_session != earliest_unfinished:
            raise ReplayError(
                "pair reservation must target the earliest unfinished session"
            )
        if any(
            item.identity.target_session == identity.target_session
            for item in reservations
        ):
            raise QueryOnlyRecoveryRequired(
                "session already has an immutable pair reservation"
            )
        represented_sessions = {
            record.event.target_session
            for record in records
            if record.event.event_type is EventType.PAIR_RESERVED
        }
        if any(
            item.identity.target_session not in represented_sessions
            for item in reservations
        ):
            raise QueryOnlyRecoveryRequired(
                "an existing pair reservation is query-only after recovery"
            )
        if expected_sequence != len(records) + 1:
            raise ReplayError("reservation expected_sequence is stale")
        actual_head = records[-1].record_sha256 if records else GENESIS_SHA256
        if expected_previous_record_sha256 != actual_head:
            raise ReplayError("reservation predecessor is stale")
        reservation = PairReservation(
            identity=identity,
            manifest_sha256=self.expected_ledger.manifest_sha256,
            expected_ledger_sha256=self.expected_ledger.ledger_sha256,
            next_sequence=expected_sequence,
            expected_previous_record_sha256=expected_previous_record_sha256,
        )
        replay_records(
            self.expected_ledger,
            records,
            reservations + (reservation,),
            retained_anchors=effective_anchors,
        )
        _write_exclusive(
            self.reservation_slots_directory / ("%012d.json" % expected_sequence),
            {
                "expected_ledger_sha256": self.expected_ledger.ledger_sha256,
                "expected_previous_record_sha256": expected_previous_record_sha256,
                "kind": "PAIR_RESERVATION_SLOT",
                "manifest_sha256": self.expected_ledger.manifest_sha256,
                "next_sequence": expected_sequence,
                "pair_key": identity.pair_key,
                "protocol_id": self.expected_ledger.protocol_id,
                "reservation_sha256": reservation.reservation_sha256,
                "schema": 1,
                "study_id": self.expected_ledger.study_id,
                "target_session": identity.target_session,
            },
        )
        _write_exclusive(
            self.pairs_directory / (identity.target_session + ".json"),
            reservation.document(),
        )
        self.read_reservations()
        return reservation

    def append_event(
        self,
        event: ResearchEvent,
        *,
        expected_sequence: int,
        expected_previous_record_sha256: str,
        retained_anchors: Sequence[LedgerAnchor] = (),
    ) -> ResearchRecord:
        self._require_writer()
        records = self.read_records()
        reservations = self.read_reservations()
        effective_anchors = self._effective_anchors(records, retained_anchors)
        replay_records(
            self.expected_ledger,
            records,
            reservations,
            retained_anchors=effective_anchors,
        )
        if expected_sequence != len(records) + 1:
            raise ReplayError("append sequence is stale")
        actual_head = records[-1].record_sha256 if records else GENESIS_SHA256
        if expected_previous_record_sha256 != actual_head:
            raise ReplayError("append predecessor is stale")
        record = ResearchRecord(
            study_id=self.expected_ledger.study_id,
            protocol_id=self.expected_ledger.protocol_id,
            manifest_sha256=self.expected_ledger.manifest_sha256,
            expected_ledger_sha256=self.expected_ledger.ledger_sha256,
            sequence=expected_sequence,
            previous_record_sha256=expected_previous_record_sha256,
            event=event,
        )
        replay_records(
            self.expected_ledger,
            records + (record,),
            reservations,
            retained_anchors=effective_anchors,
        )
        _write_exclusive(
            self.records_directory / ("%012d.json" % expected_sequence),
            record.envelope(),
        )
        _write_exclusive(
            self.anchors_directory / ("%012d.json" % expected_sequence),
            {
                "expected_ledger_sha256": self.expected_ledger.ledger_sha256,
                "head_sha256": record.record_sha256,
                "kind": "RETAINED_PREDECESSOR_RECEIPT",
                "manifest_sha256": self.expected_ledger.manifest_sha256,
                "protocol_id": self.expected_ledger.protocol_id,
                "record_sha256": record.record_sha256,
                "schema": 1,
                "sequence": record.sequence,
                "study_id": self.expected_ledger.study_id,
            },
        )
        return record

    def verify(
        self,
        *,
        as_of_utc: str,
        retained_anchors: Sequence[LedgerAnchor] = (),
    ) -> VerificationReport:
        canonical_utc(as_of_utc, name="as_of_utc")
        if tuple(retained_anchors):
            return _storage_failure_report(
                self.expected_ledger, as_of_utc=as_of_utc
            )
        try:
            self._validate_layout()
            published = self._validate_publication_state()
            if published is not None:
                if published.as_of_utc != as_of_utc:
                    return _storage_failure_report(
                        self.expected_ledger, as_of_utc=as_of_utc
                    )
                return published
            records = self.read_records()
            anchors = self._effective_anchors(records, ())
            reservations = self.read_reservations()
        except (LedgerIntegrityError, ReplayError, ResearchSchemaError):
            return _storage_failure_report(
                self.expected_ledger, as_of_utc=as_of_utc
            )
        return verify_dataset(
            self.expected_ledger,
            records,
            reservations,
            as_of_utc=as_of_utc,
            retained_anchors=anchors,
        )

    def publish_verdict(
        self,
        report: VerificationReport,
        *,
        retained_anchors: Sequence[LedgerAnchor] = (),
    ) -> ArtifactReceipt:
        if tuple(retained_anchors):
            raise SealError(
                "persistent publication accepts durable retained anchors only"
            )
        self._validate_layout()
        terminal_artifacts = _scandir_entries(
            self.reports_directory, "terminal report directory"
        )
        if terminal_artifacts:
            raise QueryOnlyRecoveryRequired(
                "a terminal report artifact already exists; publication is query-only"
            )
        if not isinstance(report, VerificationReport):
            raise SealError("report has the wrong type")
        if report.study_id != self.expected_ledger.study_id:
            raise SealError("verification report is for another study")
        if report.protocol_id != self.expected_ledger.protocol_id:
            raise SealError("verification report is for another protocol")
        if report.manifest_sha256 != self.expected_ledger.manifest_sha256:
            raise SealError("verification report is for another manifest")
        if report.expected_ledger_sha256 != self.expected_ledger.ledger_sha256:
            raise SealError("verification report is for another ledger")
        if report.expected_sessions_root_sha256 != self.expected_ledger.expected_sessions_root_sha256:
            raise SealError("verification report expected-session root mismatch")
        if report.expected_session_count != self.expected_ledger.session_count:
            raise SealError("verification report expected-session count mismatch")
        if report.verdict is VerificationVerdict.PENDING:
            raise SealError("a pending study cannot publish a terminal artifact")
        fresh = self.verify(
            as_of_utc=report.as_of_utc,
        )
        if not _exact_canonical_equal(fresh.payload(), report.payload()):
            raise SealError("report does not match current immutable evidence")
        verdict_latch = _write_exclusive(
            self.reports_directory / self.VERDICT_LATCH_NAME,
            {
                "as_of_utc": report.as_of_utc,
                "expected_ledger_sha256": self.expected_ledger.ledger_sha256,
                "expected_session_count": self.expected_ledger.session_count,
                "expected_sessions_root_sha256": self.expected_ledger.expected_sessions_root_sha256,
                "head_sha256": report.head_sha256,
                "kind": "VERDICT_PUBLICATION_RESERVATION",
                "manifest_sha256": self.expected_ledger.manifest_sha256,
                "protocol_id": self.expected_ledger.protocol_id,
                "record_count": report.record_count,
                "schema": 1,
                "study_id": self.expected_ledger.study_id,
                "verdict": report.verdict.value,
                "verification_report_sha256": report.report_sha256,
            },
        )
        if report.verdict is VerificationVerdict.FAILURE:
            document = {
                "kind": "FAILURE_REPORT",
                "report": report.payload(),
                "verdict_publication_artifact_sha256": verdict_latch.sha256,
                "verification_report_sha256": report.report_sha256,
            }
            receipt = _write_exclusive(
                self.reports_directory / self.FAILURE_REPORT_NAME, document
            )
            self._validate_publication_state()
            return receipt
        if (self.reports_directory / self.FAILURE_REPORT_NAME).exists():
            raise SealError("failure report exists; dataset is burned")
        if fresh.verdict is not VerificationVerdict.CLEAN:
            raise SealError("clean report does not match current immutable evidence")
        clean_report_receipt = _write_exclusive(
            self.reports_directory / self.CLEAN_REPORT_NAME,
            {
                "kind": "CLEAN_VERIFICATION_REPORT",
                "report": report.payload(),
                "verdict_publication_artifact_sha256": verdict_latch.sha256,
                "verification_report_sha256": report.report_sha256,
            },
        )
        records = self.read_records()
        anchors = self._effective_anchors(records, ())
        document = {
            "clean_report_artifact_sha256": clean_report_receipt.sha256,
            "expected_ledger_sha256": self.expected_ledger.ledger_sha256,
            "expected_session_count": self.expected_ledger.session_count,
            "expected_sessions_root_sha256": self.expected_ledger.expected_sessions_root_sha256,
            "kind": "CLEAN_STUDY_SEAL",
            "local_chain_only": True,
            "manifest_sha256": self.expected_ledger.manifest_sha256,
            "pre_seal_head_sha256": report.head_sha256,
            "pre_seal_record_count": report.record_count,
            "protocol_id": self.expected_ledger.protocol_id,
            "schema": 1,
            "study_id": self.expected_ledger.study_id,
            "retained_anchor_count": len(anchors),
            "retained_anchor_root_sha256": _anchor_set_root(
                anchors, self.expected_ledger
            ),
            "verification_report_sha256": report.report_sha256,
            "verdict_publication_artifact_sha256": verdict_latch.sha256,
            "verdict": report.verdict.value,
        }
        receipt = _write_exclusive(
            self.reports_directory / self.CLEAN_SEAL_NAME, document
        )
        self._validate_publication_state()
        return receipt


__all__ = [
    "ArtifactReceipt",
    "FindingCode",
    "LedgerAnchor",
    "LedgerIntegrityError",
    "ObservationBranch",
    "PairReservation",
    "QueryOnlyRecoveryRequired",
    "ReplayError",
    "ReplayResult",
    "ResearchLedger",
    "ResearchLedgerError",
    "SealError",
    "SessionReplayState",
    "SessionTerminal",
    "VerificationFinding",
    "VerificationReport",
    "VerificationVerdict",
    "replay_records",
    "verify_dataset",
]
