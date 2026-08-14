#!/usr/bin/env python3
"""Run a fixed synthetic-only structural replay of the Q014 V2 ledger.

This is not a strategy or performance backtest.  It imports only the isolated
research schema/replay modules, uses synthetic identifiers and timestamps, and
prints a redacted allowlisted integrity summary.  It never reads historical
quotes, runtime state, accounts, positions, orders, or performance artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Callable, Mapping, Sequence, Tuple, Type


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from zidoutrade.research_events import (  # noqa: E402
    ArmOutcome,
    EventType,
    ExpectedSession,
    ExpectedSessionLedger,
    GENESIS_SHA256,
    MissingReason,
    PairIdentity,
    ResearchArm,
    ResearchEvent,
    ResearchRecord,
    ResearchSchemaError,
    SelectionKind,
)
from zidoutrade.research_ledger import (  # noqa: E402
    FindingCode,
    LedgerAnchor,
    PairReservation,
    ReplayError,
    SessionTerminal,
    VerificationVerdict,
    replay_records,
    verify_dataset,
)


REPORT_SCHEMA = "Q014_V2_SYNTHETIC_STRUCTURAL_REPLAY_REPORT_V1"
CLASSIFICATIONS = (
    "STRUCTURAL_REPLAY_ONLY",
    "SYNTHETIC_ONLY",
    "RESEARCH_INFRASTRUCTURE_ONLY",
    "NO_PERFORMANCE_EVIDENCE",
    "NOT_A_STRATEGY_BACKTEST",
    "NOT_OOS",
    "ORDER_IMPACT_ZERO",
)
STUDY_ID = "Q014_V2_SYNTHETIC_STRUCTURAL_REPLAY_V1"
PROTOCOL_ID = "Q014_PROSPECTIVE_PAIRED_SHADOW_V2"
MANIFEST_SHA256 = "a" * 64
DEADLINE_SOURCE_SHA256 = "b" * 64
PUBLIC_KEYS = frozenset(
    {
        "classification",
        "classifications",
        "integrity_verdict",
        "ok",
        "production_order_impact",
        "rejected_fault_count",
        "reported_missing_structurally_terminal",
        "report_sha256",
        "scenario_count",
        "schema_version",
        "seal_eligible_for_complete_fixture",
        "unreported_deadline_missing_failed_closed",
    }
)


class StructuralReplayError(RuntimeError):
    """The fixed synthetic matrix did not fail closed as preregistered."""


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise StructuralReplayError("summary is not canonical JSON") from exc
    return (text + "\n").encode("utf-8")


def _expected_ledger(
    sessions: Tuple[ExpectedSession, ...], *, created_at_utc: str
) -> ExpectedSessionLedger:
    return ExpectedSessionLedger(
        study_id=STUDY_ID,
        protocol_id=PROTOCOL_ID,
        manifest_sha256=MANIFEST_SHA256,
        deadline_source_sha256=DEADLINE_SOURCE_SHA256,
        created_at_utc=created_at_utc,
        sessions=sessions,
    )


def _reservation(
    ledger: ExpectedSessionLedger,
    identity: PairIdentity,
    *,
    next_sequence: int,
    previous_record_sha256: str,
) -> PairReservation:
    return PairReservation(
        identity=identity,
        manifest_sha256=ledger.manifest_sha256,
        expected_ledger_sha256=ledger.ledger_sha256,
        next_sequence=next_sequence,
        expected_previous_record_sha256=previous_record_sha256,
    )


def _records(
    ledger: ExpectedSessionLedger,
    events: Sequence[ResearchEvent],
    *,
    start_sequence: int = 1,
    previous_record_sha256: str = GENESIS_SHA256,
) -> Tuple[ResearchRecord, ...]:
    result = []
    previous = previous_record_sha256
    for offset, event in enumerate(events):
        record = ResearchRecord(
            study_id=ledger.study_id,
            protocol_id=ledger.protocol_id,
            manifest_sha256=ledger.manifest_sha256,
            expected_ledger_sha256=ledger.ledger_sha256,
            sequence=start_sequence + offset,
            previous_record_sha256=previous,
            event=event,
        )
        result.append(record)
        previous = record.record_sha256
    return tuple(result)


def _complete_events(
    reservation: PairReservation,
    *,
    timestamp_prefix: str,
    baseline_outcome: ArmOutcome,
    candidate_outcome: ArmOutcome,
    evidence_digit: str,
) -> Tuple[ResearchEvent, ...]:
    session = reservation.identity.target_session
    pair_key = reservation.identity.pair_key
    return (
        ResearchEvent(
            EventType.PAIR_RESERVED,
            timestamp_prefix + "00Z",
            session,
            pair_key,
            {"reservation_sha256": reservation.reservation_sha256},
        ),
        ResearchEvent(
            EventType.OBSERVED,
            timestamp_prefix + "01Z",
            session,
            pair_key,
            {"observation_sha256": evidence_digit * 64},
        ),
        ResearchEvent(
            EventType.ARM_TERMINAL,
            timestamp_prefix + "02Z",
            session,
            pair_key,
            {
                "arm": ResearchArm.BASELINE.value,
                "evidence_sha256": str(int(evidence_digit) + 1) * 64,
                "outcome": baseline_outcome.value,
            },
        ),
        ResearchEvent(
            EventType.ARM_TERMINAL,
            timestamp_prefix + "03Z",
            session,
            pair_key,
            {
                "arm": ResearchArm.CANDIDATE.value,
                "evidence_sha256": str(int(evidence_digit) + 2) * 64,
                "outcome": candidate_outcome.value,
            },
        ),
        ResearchEvent(
            EventType.SESSION_COMPLETE,
            timestamp_prefix + "04Z",
            session,
            pair_key,
            {},
        ),
    )


def _complete_fixture() -> tuple[
    ExpectedSessionLedger,
    Tuple[ResearchRecord, ...],
    Tuple[PairReservation, ...],
]:
    ledger = _expected_ledger(
        (
            ExpectedSession(
                "2030-01-02",
                "2030-01-02T13:59:59Z",
                "2030-01-02T23:59:59Z",
            ),
            ExpectedSession(
                "2030-01-03",
                "2030-01-03T13:59:59Z",
                "2030-01-03T23:59:59Z",
            ),
        ),
        created_at_utc="2030-01-01T00:00:00Z",
    )
    selected = PairIdentity(
        STUDY_ID,
        PROTOCOL_ID,
        "2030-01-02",
        SelectionKind.SELECTED_SYMBOL,
        "1" * 64,
        "US.TEST",
    )
    first_reservation = _reservation(
        ledger,
        selected,
        next_sequence=1,
        previous_record_sha256=GENESIS_SHA256,
    )
    first = _records(
        ledger,
        _complete_events(
            first_reservation,
            timestamp_prefix="2030-01-02T14:00:",
            baseline_outcome=ArmOutcome.WAIT,
            candidate_outcome=ArmOutcome.NO_FILL,
            evidence_digit="3",
        ),
    )
    no_selection = PairIdentity(
        STUDY_ID,
        PROTOCOL_ID,
        "2030-01-03",
        SelectionKind.NO_SELECTION,
        "6" * 64,
        None,
    )
    second_reservation = _reservation(
        ledger,
        no_selection,
        next_sequence=len(first) + 1,
        previous_record_sha256=first[-1].record_sha256,
    )
    second = _records(
        ledger,
        _complete_events(
            second_reservation,
            timestamp_prefix="2030-01-03T14:00:",
            baseline_outcome=ArmOutcome.WAIT,
            candidate_outcome=ArmOutcome.WAIT,
            evidence_digit="7",
        ),
        start_sequence=len(first) + 1,
        previous_record_sha256=first[-1].record_sha256,
    )
    return ledger, first + second, (first_reservation, second_reservation)


def _missing_fixture() -> tuple[
    ExpectedSessionLedger,
    Tuple[ResearchRecord, ...],
    Tuple[PairReservation, ...],
]:
    ledger = _expected_ledger(
        (
            ExpectedSession(
                "2031-02-03",
                "2031-02-03T13:59:59Z",
                "2031-02-03T23:59:59Z",
            ),
        ),
        created_at_utc="2031-02-01T00:00:00Z",
    )
    identity = PairIdentity(
        STUDY_ID,
        PROTOCOL_ID,
        "2031-02-03",
        SelectionKind.SELECTED_SYMBOL,
        "1" * 64,
        "US.TEST",
    )
    reservation = _reservation(
        ledger,
        identity,
        next_sequence=1,
        previous_record_sha256=GENESIS_SHA256,
    )
    events = (
        ResearchEvent(
            EventType.PAIR_RESERVED,
            "2031-02-03T14:00:00Z",
            identity.target_session,
            identity.pair_key,
            {"reservation_sha256": reservation.reservation_sha256},
        ),
        ResearchEvent(
            EventType.OBSERVATION_MISSING,
            "2031-02-03T14:00:01Z",
            identity.target_session,
            identity.pair_key,
            {"reason": MissingReason.CAPTURE_INCOMPLETE.value},
        ),
        ResearchEvent(
            EventType.SESSION_MISSING,
            "2031-02-03T14:00:02Z",
            identity.target_session,
            identity.pair_key,
            {"reason": MissingReason.CAPTURE_INCOMPLETE.value},
        ),
    )
    return ledger, _records(ledger, events), (reservation,)


def _must_raise(expected: Type[Exception], action: Callable[[], object]) -> int:
    try:
        action()
    except expected:
        return 1
    except Exception as exc:
        raise StructuralReplayError("fault raised an unexpected exception") from exc
    raise StructuralReplayError("fault was not rejected")


def _fault_matrix(
    ledger: ExpectedSessionLedger,
    records: Tuple[ResearchRecord, ...],
    reservations: Tuple[PairReservation, ...],
) -> int:
    first_reservation = reservations[0]
    pair_key = first_reservation.identity.pair_key
    session = first_reservation.identity.target_session
    reserved = ResearchEvent(
        EventType.PAIR_RESERVED,
        "2030-01-02T14:00:00Z",
        session,
        pair_key,
        {"reservation_sha256": first_reservation.reservation_sha256},
    )
    observed = ResearchEvent(
        EventType.OBSERVED,
        "2030-01-02T14:00:01Z",
        session,
        pair_key,
        {"observation_sha256": "3" * 64},
    )
    baseline = ResearchEvent(
        EventType.ARM_TERMINAL,
        "2030-01-02T14:00:02Z",
        session,
        pair_key,
        {
            "arm": ResearchArm.BASELINE.value,
            "evidence_sha256": "4" * 64,
            "outcome": ArmOutcome.WAIT.value,
        },
    )
    early_complete = ResearchEvent(
        EventType.SESSION_COMPLETE,
        "2030-01-02T14:00:03Z",
        session,
        pair_key,
        {},
    )
    conflicting_missing = ResearchEvent(
        EventType.OBSERVATION_MISSING,
        "2030-01-02T14:00:02Z",
        session,
        pair_key,
        {"reason": MissingReason.SOURCE_UNAVAILABLE.value},
    )
    duplicate_baseline = ResearchEvent(
        EventType.ARM_TERMINAL,
        "2030-01-02T14:00:03Z",
        session,
        pair_key,
        {
            "arm": ResearchArm.BASELINE.value,
            "evidence_sha256": "5" * 64,
            "outcome": ArmOutcome.NO_FILL.value,
        },
    )

    rejected = 0
    rejected += _must_raise(
        ReplayError,
        lambda: replay_records(
            ledger,
            _records(ledger, (reserved, observed, baseline, early_complete)),
            reservations,
        ),
    )
    rejected += _must_raise(
        ReplayError,
        lambda: replay_records(
            ledger,
            _records(ledger, (reserved, observed, conflicting_missing)),
            reservations,
        ),
    )
    rejected += _must_raise(
        ReplayError,
        lambda: replay_records(
            ledger,
            _records(ledger, (reserved, observed, baseline, duplicate_baseline)),
            reservations,
        ),
    )
    before_capture = ResearchEvent(
        EventType.PAIR_RESERVED,
        "2030-01-02T13:59:58Z",
        session,
        pair_key,
        {"reservation_sha256": first_reservation.reservation_sha256},
    )
    rejected += _must_raise(
        ReplayError,
        lambda: replay_records(
            ledger,
            _records(ledger, (before_capture,)),
            reservations,
        ),
    )
    decreasing_observed = ResearchEvent(
        EventType.OBSERVED,
        "2030-01-02T14:00:00Z",
        session,
        pair_key,
        {"observation_sha256": "3" * 64},
    )
    reserved_late = ResearchEvent(
        EventType.PAIR_RESERVED,
        "2030-01-02T14:00:01Z",
        session,
        pair_key,
        {"reservation_sha256": first_reservation.reservation_sha256},
    )
    rejected += _must_raise(
        ReplayError,
        lambda: replay_records(
            ledger,
            _records(ledger, (reserved_late, decreasing_observed)),
            reservations,
        ),
    )

    after_terminal = ResearchRecord(
        study_id=ledger.study_id,
        protocol_id=ledger.protocol_id,
        manifest_sha256=ledger.manifest_sha256,
        expected_ledger_sha256=ledger.ledger_sha256,
        sequence=len(records) + 1,
        previous_record_sha256=records[-1].record_sha256,
        event=ResearchEvent(
            EventType.OBSERVED,
            "2030-01-03T14:00:05Z",
            reservations[-1].identity.target_session,
            reservations[-1].identity.pair_key,
            {"observation_sha256": "9" * 64},
        ),
    )
    rejected += _must_raise(
        ReplayError,
        lambda: replay_records(ledger, records + (after_terminal,), reservations),
    )

    gap = ResearchRecord(
        study_id=ledger.study_id,
        protocol_id=ledger.protocol_id,
        manifest_sha256=ledger.manifest_sha256,
        expected_ledger_sha256=ledger.ledger_sha256,
        sequence=3,
        previous_record_sha256=records[0].record_sha256,
        event=records[1].event,
    )
    rejected += _must_raise(
        ReplayError,
        lambda: replay_records(ledger, (records[0], gap), reservations),
    )
    wrong_predecessor = ResearchRecord(
        study_id=ledger.study_id,
        protocol_id=ledger.protocol_id,
        manifest_sha256=ledger.manifest_sha256,
        expected_ledger_sha256=ledger.ledger_sha256,
        sequence=2,
        previous_record_sha256="f" * 64,
        event=records[1].event,
    )
    rejected += _must_raise(
        ReplayError,
        lambda: replay_records(
            ledger, (records[0], wrong_predecessor), reservations
        ),
    )
    rejected += _must_raise(
        ReplayError,
        lambda: replay_records(
            ledger, records, reservations + (reservations[0],)
        ),
    )

    unexpected_identity = PairIdentity(
        STUDY_ID,
        PROTOCOL_ID,
        "2030-01-04",
        SelectionKind.NO_SELECTION,
        "1" * 64,
        None,
    )
    unexpected_reservation = _reservation(
        ledger,
        unexpected_identity,
        next_sequence=1,
        previous_record_sha256=GENESIS_SHA256,
    )
    rejected += _must_raise(
        ReplayError,
        lambda: replay_records(ledger, (), (unexpected_reservation,)),
    )
    rejected += _must_raise(
        ReplayError,
        lambda: replay_records(
            ledger,
            records[:1],
            reservations,
            retained_anchors=(
                LedgerAnchor(len(records), records[-1].record_sha256),
            ),
        ),
    )
    rejected += _must_raise(
        ReplayError,
        lambda: replay_records(
            ledger,
            records,
            reservations,
            retained_anchors=(
                LedgerAnchor(1, records[0].record_sha256),
                LedgerAnchor(1, "f" * 64),
            ),
        ),
    )

    changed_envelope = records[0].envelope()
    changed_envelope["record_sha256"] = "f" * 64
    rejected += _must_raise(
        ResearchSchemaError,
        lambda: ResearchRecord.from_envelope(changed_envelope),
    )
    unknown_event = records[0].event.payload_document()
    unknown_event["event_type"] = "UNKNOWN_EVENT"
    rejected += _must_raise(
        ResearchSchemaError,
        lambda: ResearchEvent.from_payload_document(unknown_event),
    )
    extra_field = records[0].event.payload_document()
    extra_field["unexpected"] = True
    rejected += _must_raise(
        ResearchSchemaError,
        lambda: ResearchEvent.from_payload_document(extra_field),
    )

    overdue = verify_dataset(
        ledger,
        (),
        (),
        as_of_utc="2030-01-04T00:00:00Z",
    )
    if overdue.verdict is not VerificationVerdict.FAILURE or not any(
        finding.code is FindingCode.DEADLINE_TERMINAL_MISSING
        for finding in overdue.findings
    ):
        raise StructuralReplayError("deadline omission did not fail closed")
    rejected += 1
    future_event = verify_dataset(
        ledger,
        records[:1],
        (reservations[0],),
        as_of_utc="2030-01-02T13:59:59Z",
    )
    if future_event.verdict is not VerificationVerdict.FAILURE or not any(
        finding.code is FindingCode.INTEGRITY_FAILURE
        for finding in future_event.findings
    ):
        raise StructuralReplayError("future event relative to as-of was accepted")
    rejected += 1
    return rejected


def run_structural_replay() -> Mapping[str, Any]:
    ledger, records, reservations = _complete_fixture()
    replay = replay_records(ledger, records, reservations)
    complete_report = verify_dataset(
        ledger,
        records,
        reservations,
        as_of_utc="2030-01-03T22:00:00Z",
    )
    if (
        replay.record_count != len(records)
        or any(
            state.terminal is not SessionTerminal.SESSION_COMPLETE
            for state in replay.sessions
        )
        or complete_report.verdict is not VerificationVerdict.CLEAN
    ):
        raise StructuralReplayError("complete fixture did not verify cleanly")

    missing_ledger, missing_records, missing_reservations = _missing_fixture()
    missing_replay = replay_records(
        missing_ledger, missing_records, missing_reservations
    )
    missing_report = verify_dataset(
        missing_ledger,
        missing_records,
        missing_reservations,
        as_of_utc="2031-02-03T22:00:00Z",
    )
    missing_is_terminal = (
        len(missing_replay.sessions) == 1
        and missing_replay.sessions[0].terminal is SessionTerminal.SESSION_MISSING
        and missing_report.verdict is VerificationVerdict.CLEAN
        and any(
            finding.code is FindingCode.SESSION_REPORTED_MISSING
            for finding in missing_report.findings
        )
    )
    if not missing_is_terminal:
        raise StructuralReplayError("reported missing was not retained as a terminal")

    rejected = _fault_matrix(ledger, records, reservations)
    payload = {
        "classification": CLASSIFICATIONS[0],
        "classifications": list(CLASSIFICATIONS),
        "integrity_verdict": "STRUCTURALLY_VALID",
        "ok": True,
        "production_order_impact": 0,
        "rejected_fault_count": rejected,
        "reported_missing_structurally_terminal": True,
        "scenario_count": rejected + 2,
        "schema_version": REPORT_SCHEMA,
        "seal_eligible_for_complete_fixture": True,
        "unreported_deadline_missing_failed_closed": True,
    }
    if frozenset(payload) | {"report_sha256"} != PUBLIC_KEYS:
        raise StructuralReplayError("public summary allowlist drifted")
    payload["report_sha256"] = hashlib.sha256(
        _canonical_json_bytes(payload)
    ).hexdigest()
    return payload


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fixed synthetic Q014 V2 structural replay; no strategy or data inputs."
        )
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    _parse_args(argv)
    try:
        report = run_structural_replay()
    except (ResearchSchemaError, ReplayError, StructuralReplayError, ValueError):
        failure = {
            "classification": CLASSIFICATIONS[0],
            "classifications": list(CLASSIFICATIONS),
            "integrity_verdict": "FAIL_CLOSED",
            "ok": False,
            "production_order_impact": 0,
            "rejected_fault_count": 0,
            "reported_missing_structurally_terminal": False,
            "scenario_count": 0,
            "schema_version": REPORT_SCHEMA,
            "seal_eligible_for_complete_fixture": False,
            "unreported_deadline_missing_failed_closed": False,
        }
        failure["report_sha256"] = hashlib.sha256(
            _canonical_json_bytes(failure)
        ).hexdigest()
        print(_canonical_json_bytes(failure).decode("utf-8"), end="")
        return 2
    print(_canonical_json_bytes(report).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
