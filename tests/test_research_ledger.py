import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import zidoutrade.research_ledger as research_ledger_module

from zidoutrade.research_events import (
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
from zidoutrade.research_ledger import (
    FindingCode,
    LedgerAnchor,
    LedgerIntegrityError,
    PairReservation,
    QueryOnlyRecoveryRequired,
    ReplayError,
    ResearchLedger,
    SealError,
    SessionTerminal,
    VerificationFinding,
    VerificationReport,
    VerificationVerdict,
    replay_records,
    verify_dataset,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def make_ledger() -> ExpectedSessionLedger:
    return ExpectedSessionLedger(
        study_id="STUDY_V1",
        protocol_id="Q014_V2",
        manifest_sha256=SHA_A,
        deadline_source_sha256=SHA_B,
        created_at_utc="2026-08-14T00:00:00Z",
        sessions=(
            ExpectedSession(
                "2026-08-15",
                "2026-08-15T00:00:00Z",
                "2026-08-16T00:00:00Z",
            ),
        ),
    )


def make_identity() -> PairIdentity:
    return PairIdentity(
        study_id="STUDY_V1",
        protocol_id="Q014_V2",
        target_session="2026-08-15",
        selection_kind=SelectionKind.SELECTED_SYMBOL,
        selection_record_sha256=SHA_C,
        selected_symbol="US.TEST",
    )


def make_reservation(ledger: ExpectedSessionLedger) -> PairReservation:
    return PairReservation(
        identity=make_identity(),
        manifest_sha256=ledger.manifest_sha256,
        expected_ledger_sha256=ledger.ledger_sha256,
        next_sequence=1,
        expected_previous_record_sha256=GENESIS_SHA256,
    )


def event(
    event_type: EventType,
    occurred_at_utc: str,
    payload,
    identity: PairIdentity = None,
) -> ResearchEvent:
    identity = identity or make_identity()
    return ResearchEvent(
        event_type=event_type,
        occurred_at_utc=occurred_at_utc,
        target_session=identity.target_session,
        pair_key=identity.pair_key,
        payload=payload,
    )


def build_records(
    ledger: ExpectedSessionLedger, events
) -> tuple:
    records = []
    previous = GENESIS_SHA256
    for sequence, item in enumerate(events, start=1):
        record = ResearchRecord(
            study_id=ledger.study_id,
            protocol_id=ledger.protocol_id,
            manifest_sha256=ledger.manifest_sha256,
            expected_ledger_sha256=ledger.ledger_sha256,
            sequence=sequence,
            previous_record_sha256=previous,
            event=item,
        )
        records.append(record)
        previous = record.record_sha256
    return tuple(records)


def complete_stream(ledger: ExpectedSessionLedger, reservation: PairReservation):
    return build_records(
        ledger,
        (
            event(
                EventType.PAIR_RESERVED,
                "2026-08-15T00:00:00Z",
                {"reservation_sha256": reservation.reservation_sha256},
            ),
            event(
                EventType.OBSERVED,
                "2026-08-15T01:00:00Z",
                {"observation_sha256": SHA_D},
            ),
            event(
                EventType.ARM_TERMINAL,
                "2026-08-15T02:00:00Z",
                {
                    "arm": ResearchArm.BASELINE.value,
                    "evidence_sha256": SHA_A,
                    "outcome": ArmOutcome.WAIT.value,
                },
            ),
            event(
                EventType.ARM_TERMINAL,
                "2026-08-15T03:00:00Z",
                {
                    "arm": ResearchArm.CANDIDATE.value,
                    "evidence_sha256": SHA_B,
                    "outcome": ArmOutcome.NO_FILL.value,
                },
            ),
            event(EventType.SESSION_COMPLETE, "2026-08-15T04:00:00Z", {}),
        ),
    )


class PureReplayTests(unittest.TestCase):
    def test_observed_branch_requires_both_arms_and_exactly_one_terminal(self):
        ledger = make_ledger()
        reservation = make_reservation(ledger)
        records = complete_stream(ledger, reservation)
        replay = replay_records(ledger, records, (reservation,))
        self.assertEqual(
            replay.for_session("2026-08-15").terminal,
            SessionTerminal.SESSION_COMPLETE,
        )
        premature = build_records(
            ledger,
            (
                records[0].event,
                records[1].event,
                records[2].event,
                event(EventType.SESSION_COMPLETE, "2026-08-15T03:00:00Z", {}),
            ),
        )
        with self.assertRaises(ReplayError):
            replay_records(ledger, premature, (reservation,))
        duplicate = build_records(
            ledger,
            tuple(item.event for item in records)
            + (event(EventType.SESSION_COMPLETE, "2026-08-15T05:00:00Z", {}),),
        )
        with self.assertRaises(ReplayError):
            replay_records(ledger, duplicate, (reservation,))

    def test_missing_branch_is_terminal_but_reason_must_match(self):
        ledger = make_ledger()
        reservation = make_reservation(ledger)
        missing = build_records(
            ledger,
            (
                event(
                    EventType.PAIR_RESERVED,
                    "2026-08-15T00:00:00Z",
                    {"reservation_sha256": reservation.reservation_sha256},
                ),
                event(
                    EventType.OBSERVATION_MISSING,
                    "2026-08-15T01:00:00Z",
                    {"reason": MissingReason.SOURCE_UNAVAILABLE.value},
                ),
                event(
                    EventType.SESSION_MISSING,
                    "2026-08-15T02:00:00Z",
                    {"reason": MissingReason.SOURCE_UNAVAILABLE.value},
                ),
            ),
        )
        report = verify_dataset(
            ledger,
            missing,
            (reservation,),
            as_of_utc="2026-08-16T01:00:00Z",
        )
        self.assertEqual(report.verdict, VerificationVerdict.CLEAN)
        self.assertEqual(report.missing_session_count, 1)
        self.assertEqual(report.findings[0].code, FindingCode.SESSION_REPORTED_MISSING)
        wrong_reason = build_records(
            ledger,
            (
                missing[0].event,
                missing[1].event,
                event(
                    EventType.SESSION_MISSING,
                    "2026-08-15T02:00:00Z",
                    {"reason": MissingReason.DATA_QUALITY.value},
                ),
            ),
        )
        with self.assertRaises(ReplayError):
            replay_records(ledger, wrong_reason, (reservation,))

    def test_verifier_pending_overdue_future_event_and_anchors(self):
        ledger = make_ledger()
        reservation = make_reservation(ledger)
        pending = verify_dataset(
            ledger, (), (), as_of_utc="2026-08-15T12:00:00Z"
        )
        self.assertEqual(pending.verdict, VerificationVerdict.PENDING)
        overdue = verify_dataset(
            ledger, (), (), as_of_utc="2026-08-16T00:00:01Z"
        )
        self.assertEqual(overdue.verdict, VerificationVerdict.FAILURE)
        complete = complete_stream(ledger, reservation)
        future = verify_dataset(
            ledger,
            complete,
            (reservation,),
            as_of_utc="2026-08-15T03:30:00Z",
        )
        self.assertEqual(future.verdict, VerificationVerdict.FAILURE)
        tail_anchor = LedgerAnchor(len(complete), complete[-1].record_sha256)
        truncated = verify_dataset(
            ledger,
            complete[:-1],
            (reservation,),
            as_of_utc="2026-08-15T05:00:00Z",
            retained_anchors=(tail_anchor,),
        )
        self.assertEqual(truncated.verdict, VerificationVerdict.FAILURE)

    def test_capture_window_deadline_and_decreasing_times_fail(self):
        ledger = make_ledger()
        reservation = make_reservation(ledger)
        before = build_records(
            ledger,
            (
                event(
                    EventType.PAIR_RESERVED,
                    "2026-08-14T23:59:59Z",
                    {"reservation_sha256": reservation.reservation_sha256},
                ),
            ),
        )
        with self.assertRaises(ReplayError):
            replay_records(ledger, before, (reservation,))
        late = build_records(
            ledger,
            (
                event(
                    EventType.PAIR_RESERVED,
                    "2026-08-16T00:00:01Z",
                    {"reservation_sha256": reservation.reservation_sha256},
                ),
            ),
        )
        with self.assertRaises(ReplayError):
            replay_records(ledger, late, (reservation,))
        decreasing = build_records(
            ledger,
            (
                event(
                    EventType.PAIR_RESERVED,
                    "2026-08-15T02:00:00Z",
                    {"reservation_sha256": reservation.reservation_sha256},
                ),
                event(
                    EventType.OBSERVED,
                    "2026-08-15T01:00:00Z",
                    {"observation_sha256": SHA_D},
                ),
            ),
        )
        with self.assertRaises(ReplayError):
            replay_records(ledger, decreasing, (reservation,))

    def test_verification_report_rejects_duplicate_and_incoherent_findings(self):
        ledger = make_ledger()
        pending = verify_dataset(
            ledger, (), (), as_of_utc="2026-08-15T12:00:00Z"
        )
        duplicate = pending.payload()
        duplicate["findings"].append(dict(duplicate["findings"][0]))
        with self.assertRaises(ValueError):
            VerificationReport.from_payload(duplicate)

        reservation = make_reservation(ledger)
        missing_records = build_records(
            ledger,
            (
                event(
                    EventType.PAIR_RESERVED,
                    "2026-08-15T00:00:00Z",
                    {"reservation_sha256": reservation.reservation_sha256},
                ),
                event(
                    EventType.OBSERVATION_MISSING,
                    "2026-08-15T01:00:00Z",
                    {"reason": MissingReason.DATA_QUALITY.value},
                ),
                event(
                    EventType.SESSION_MISSING,
                    "2026-08-15T02:00:00Z",
                    {"reason": MissingReason.DATA_QUALITY.value},
                ),
            ),
        )
        missing = verify_dataset(
            ledger,
            missing_records,
            (reservation,),
            as_of_utc="2026-08-16T01:00:00Z",
        ).payload()
        missing["missing_session_count"] = 0
        with self.assertRaises(ValueError):
            VerificationReport.from_payload(missing)

        with self.assertRaises(ValueError):
            VerificationReport(
                study_id="STUDY_V1",
                protocol_id="Q014_V2",
                manifest_sha256=SHA_A,
                expected_ledger_sha256=ledger.ledger_sha256,
                expected_sessions_root_sha256=ledger.expected_sessions_root_sha256,
                expected_session_count=1,
                as_of_utc="2026-08-16T01:00:00Z",
                verdict=VerificationVerdict.CLEAN,
                findings=(),
                record_count=0,
                head_sha256=GENESIS_SHA256,
                complete_session_count=1,
                missing_session_count=0,
            )


class PersistentLedgerTests(unittest.TestCase):
    def _initialized(self, directory: str):
        root = Path(directory) / "owner-runtime"
        root.mkdir(mode=0o700)
        return ResearchLedger.initialize(root, make_ledger())

    def _append(self, ledger: ResearchLedger, item: ResearchEvent):
        records = ledger.read_records()
        previous = records[-1].record_sha256 if records else GENESIS_SHA256
        return ledger.append_event(
            item,
            expected_sequence=len(records) + 1,
            expected_previous_record_sha256=previous,
        )

    def test_o_excl_records_anchors_clean_report_and_query_only_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self._initialized(directory)
            reservation = ledger.reserve_pair(
                make_identity(),
                expected_sequence=1,
                expected_previous_record_sha256=GENESIS_SHA256,
            )
            for item in complete_stream(make_ledger(), reservation):
                self._append(ledger, item.event)
            report = ledger.verify(as_of_utc="2026-08-16T01:00:00Z")
            self.assertEqual(report.verdict, VerificationVerdict.CLEAN)
            seal = ledger.publish_verdict(report)
            self.assertEqual(seal.path.name, ResearchLedger.CLEAN_SEAL_NAME)
            self.assertTrue(
                (ledger.reports_directory / ResearchLedger.CLEAN_REPORT_NAME).is_file()
            )
            self.assertEqual(len(ledger.read_retained_anchors()), 5)
            with self.assertRaises(QueryOnlyRecoveryRequired):
                ledger.reserve_pair(
                    make_identity(),
                    expected_sequence=6,
                    expected_previous_record_sha256=report.head_sha256,
                )
            reopened = ResearchLedger.open(ledger.root)
            with self.assertRaises(QueryOnlyRecoveryRequired):
                reopened.append_event(
                    event(EventType.SESSION_COMPLETE, "2026-08-15T05:00:00Z", {}),
                    expected_sequence=6,
                    expected_previous_record_sha256=report.head_sha256,
                )

    def test_publish_rejects_external_anchor_before_creating_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self._initialized(directory)
            reservation = ledger.reserve_pair(
                make_identity(),
                expected_sequence=1,
                expected_previous_record_sha256=GENESIS_SHA256,
            )
            for item in complete_stream(make_ledger(), reservation):
                self._append(ledger, item.event)
            report = ledger.verify(as_of_utc="2026-08-16T01:00:00Z")
            external_anchor = LedgerAnchor(
                report.record_count, report.head_sha256
            )

            with self.assertRaises(SealError):
                ledger.publish_verdict(
                    report, retained_anchors=(external_anchor,)
                )

            self.assertEqual(list(ledger.reports_directory.iterdir()), [])
            seal = ledger.publish_verdict(report)
            self.assertEqual(seal.path.name, ResearchLedger.CLEAN_SEAL_NAME)

    def test_bool_for_int_artifact_tampering_is_rejected_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self._initialized(directory)
            ledger.reserve_pair(
                make_identity(),
                expected_sequence=1,
                expected_previous_record_sha256=GENESIS_SHA256,
            )
            slot_path = ledger.reservation_slots_directory / ("%012d.json" % 1)
            slot = json.loads(slot_path.read_text(encoding="utf-8"))
            slot["next_sequence"] = True
            slot_path.write_text(
                json.dumps(slot, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(LedgerIntegrityError):
                ledger.read_reservations()

        with tempfile.TemporaryDirectory() as directory:
            ledger = self._initialized(directory)
            reservation = ledger.reserve_pair(
                make_identity(),
                expected_sequence=1,
                expected_previous_record_sha256=GENESIS_SHA256,
            )
            for item in complete_stream(make_ledger(), reservation):
                self._append(ledger, item.event)
            report = ledger.verify(as_of_utc="2026-08-16T01:00:00Z")
            ledger.publish_verdict(report)
            seal_path = ledger.reports_directory / ResearchLedger.CLEAN_SEAL_NAME
            seal = json.loads(seal_path.read_text(encoding="utf-8"))
            seal["schema"] = True
            seal_path.write_text(
                json.dumps(seal, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(LedgerIntegrityError):
                ResearchLedger.open(ledger.root)
            self.assertEqual(
                ledger.verify(as_of_utc="2026-08-16T01:00:00Z").verdict,
                VerificationVerdict.FAILURE,
            )

    def test_published_verdict_requires_the_exact_verifier_instant(self):
        for publish_clean in (True, False):
            with self.subTest(publish_clean=publish_clean):
                with tempfile.TemporaryDirectory() as directory:
                    ledger = self._initialized(directory)
                    if publish_clean:
                        reservation = ledger.reserve_pair(
                            make_identity(),
                            expected_sequence=1,
                            expected_previous_record_sha256=GENESIS_SHA256,
                        )
                        for item in complete_stream(make_ledger(), reservation):
                            self._append(ledger, item.event)
                    published_at = "2026-08-16T01:00:00Z"
                    report = ledger.verify(as_of_utc=published_at)
                    self.assertEqual(
                        report.verdict,
                        VerificationVerdict.CLEAN
                        if publish_clean
                        else VerificationVerdict.FAILURE,
                    )
                    ledger.publish_verdict(report)
                    self.assertEqual(
                        ledger.verify(as_of_utc=published_at).payload(),
                        report.payload(),
                    )

                    for requested in (
                        "2026-08-16T00:30:00Z",
                        "2026-08-16T02:00:00Z",
                    ):
                        mismatch = ledger.verify(as_of_utc=requested)
                        self.assertEqual(
                            mismatch.verdict, VerificationVerdict.FAILURE
                        )
                        self.assertEqual(mismatch.as_of_utc, requested)
                        self.assertEqual(
                            mismatch.findings,
                            (
                                VerificationFinding(
                                    FindingCode.INTEGRITY_FAILURE, None
                                ),
                            ),
                        )
                    with self.assertRaises(ResearchSchemaError):
                        ledger.verify(as_of_utc="2026-08-16 01:00:00Z")

                    if publish_clean:
                        contradictory = ledger.verify(
                            as_of_utc=published_at,
                            retained_anchors=(LedgerAnchor(1, "f" * 64),),
                        )
                        self.assertEqual(
                            contradictory.verdict, VerificationVerdict.FAILURE
                        )
                        self.assertEqual(
                            contradictory.findings[0].code,
                            FindingCode.INTEGRITY_FAILURE,
                        )

    def test_common_verdict_latch_serializes_clean_failure_interleavings(self):
        for nested_failure_wins in (True, False):
            with self.subTest(nested_failure_wins=nested_failure_wins):
                with tempfile.TemporaryDirectory() as directory:
                    ledger = self._initialized(directory)
                    reservation = ledger.reserve_pair(
                        make_identity(),
                        expected_sequence=1,
                        expected_previous_record_sha256=GENESIS_SHA256,
                    )
                    for item in complete_stream(make_ledger(), reservation):
                        self._append(ledger, item.event)
                    clean = ledger.verify(as_of_utc="2026-08-16T01:00:00Z")
                    failure = ledger.verify(as_of_utc="2026-08-15T03:30:00Z")
                    self.assertEqual(clean.verdict, VerificationVerdict.CLEAN)
                    self.assertEqual(failure.verdict, VerificationVerdict.FAILURE)
                    nested = failure if nested_failure_wins else clean
                    outer = clean if nested_failure_wins else failure
                    real_write = research_ledger_module._write_exclusive
                    nested_receipts = []
                    interleaved = False

                    def interleaving_write(path, document):
                        nonlocal interleaved
                        if (
                            path.name == ResearchLedger.VERDICT_LATCH_NAME
                            and not interleaved
                        ):
                            interleaved = True
                            nested_receipts.append(ledger.publish_verdict(nested))
                        return real_write(path, document)

                    with mock.patch.object(
                        research_ledger_module,
                        "_write_exclusive",
                        side_effect=interleaving_write,
                    ):
                        with self.assertRaises(QueryOnlyRecoveryRequired):
                            ledger.publish_verdict(outer)

                    self.assertEqual(len(nested_receipts), 1)
                    latch_path = (
                        ledger.reports_directory / ResearchLedger.VERDICT_LATCH_NAME
                    )
                    latch = json.loads(latch_path.read_text(encoding="utf-8"))
                    self.assertEqual(latch["verdict"], nested.verdict.value)
                    self.assertEqual(
                        latch["verification_report_sha256"], nested.report_sha256
                    )
                    clean_paths = (
                        ledger.reports_directory / ResearchLedger.CLEAN_REPORT_NAME,
                        ledger.reports_directory / ResearchLedger.CLEAN_SEAL_NAME,
                    )
                    failure_path = (
                        ledger.reports_directory / ResearchLedger.FAILURE_REPORT_NAME
                    )
                    if nested_failure_wins:
                        self.assertTrue(failure_path.is_file())
                        self.assertTrue(all(not path.exists() for path in clean_paths))
                    else:
                        self.assertFalse(failure_path.exists())
                        self.assertTrue(all(path.is_file() for path in clean_paths))

    def test_existing_reservation_is_not_retried_and_wrong_constructor_cannot_write(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self._initialized(directory)
            ledger.reserve_pair(
                make_identity(),
                expected_sequence=1,
                expected_previous_record_sha256=GENESIS_SHA256,
            )
            with self.assertRaises(QueryOnlyRecoveryRequired):
                ledger.reserve_pair(
                    make_identity(),
                    expected_sequence=1,
                    expected_previous_record_sha256=GENESIS_SHA256,
                )
            direct = ResearchLedger(ledger.root, make_ledger())
            with self.assertRaises(QueryOnlyRecoveryRequired):
                direct.append_event(
                    event(EventType.SESSION_COMPLETE, "2026-08-15T01:00:00Z", {}),
                    expected_sequence=1,
                    expected_previous_record_sha256=GENESIS_SHA256,
                )

    def test_second_outstanding_session_reservation_cannot_fork_same_head(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "owner-runtime"
            root.mkdir(mode=0o700)
            definition = ExpectedSessionLedger(
                study_id="STUDY_V1",
                protocol_id="Q014_V2",
                manifest_sha256=SHA_A,
                deadline_source_sha256=SHA_B,
                created_at_utc="2026-08-14T00:00:00Z",
                sessions=(
                    ExpectedSession(
                        "2026-08-15",
                        "2026-08-15T00:00:00Z",
                        "2026-08-16T00:00:00Z",
                    ),
                    ExpectedSession(
                        "2026-08-16",
                        "2026-08-16T00:00:00Z",
                        "2026-08-17T00:00:00Z",
                    ),
                ),
            )
            ledger = ResearchLedger.initialize(root, definition)
            first = ledger.reserve_pair(
                make_identity(),
                expected_sequence=1,
                expected_previous_record_sha256=GENESIS_SHA256,
            )
            second = PairIdentity(
                study_id="STUDY_V1",
                protocol_id="Q014_V2",
                target_session="2026-08-16",
                selection_kind=SelectionKind.NO_SELECTION,
                selection_record_sha256=SHA_D,
            )
            with self.assertRaises(ReplayError):
                ledger.reserve_pair(
                    second,
                    expected_sequence=1,
                    expected_previous_record_sha256=GENESIS_SHA256,
                )
            self.assertEqual(len(ledger.read_reservations()), 1)
            first_events = complete_stream(definition, first)
            self._append(ledger, first_events[0].event)
            with self.assertRaises(ReplayError):
                ledger.reserve_pair(
                    second,
                    expected_sequence=2,
                    expected_previous_record_sha256=ledger.read_records()[-1].record_sha256,
                )
            for item in first_events[1:]:
                self._append(ledger, item.event)
            head = ledger.read_records()[-1].record_sha256
            second_reservation = ledger.reserve_pair(
                second,
                expected_sequence=6,
                expected_previous_record_sha256=head,
            )
            self.assertEqual(second_reservation.identity.target_session, "2026-08-16")
            self.assertEqual(len(ledger.read_reservations()), 2)

    def test_common_reservation_slot_serializes_interleaved_pair_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self._initialized(directory)
            outer_identity = make_identity()
            nested_identity = PairIdentity(
                study_id="STUDY_V1",
                protocol_id="Q014_V2",
                target_session="2026-08-15",
                selection_kind=SelectionKind.NO_SELECTION,
                selection_record_sha256=SHA_D,
            )
            real_write = research_ledger_module._write_exclusive
            nested_reservations = []
            interleaved = False

            def interleaving_write(path, document):
                nonlocal interleaved
                if (
                    path.parent == ledger.reservation_slots_directory
                    and not interleaved
                ):
                    interleaved = True
                    nested_reservations.append(
                        ledger.reserve_pair(
                            nested_identity,
                            expected_sequence=1,
                            expected_previous_record_sha256=GENESIS_SHA256,
                        )
                    )
                return real_write(path, document)

            with mock.patch.object(
                research_ledger_module,
                "_write_exclusive",
                side_effect=interleaving_write,
            ):
                with self.assertRaises(QueryOnlyRecoveryRequired):
                    ledger.reserve_pair(
                        outer_identity,
                        expected_sequence=1,
                        expected_previous_record_sha256=GENESIS_SHA256,
                    )
            self.assertEqual(len(nested_reservations), 1)
            persisted = ledger.read_reservations()
            self.assertEqual(len(persisted), 1)
            self.assertEqual(persisted[0].identity, nested_identity)
            self.assertEqual(
                len(tuple(ledger.reservation_slots_directory.iterdir())), 1
            )

    def test_storage_corruption_becomes_separate_failure_and_latches_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self._initialized(directory)
            reservation = ledger.reserve_pair(
                make_identity(),
                expected_sequence=1,
                expected_previous_record_sha256=GENESIS_SHA256,
            )
            first = self._append(
                ledger,
                event(
                    EventType.PAIR_RESERVED,
                    "2026-08-15T00:00:00Z",
                    {"reservation_sha256": reservation.reservation_sha256},
                ),
            )
            record_path = ledger.records_directory / ("%012d.json" % 1)
            record_path.write_bytes(record_path.read_bytes()[:-1])
            report = ledger.verify(as_of_utc="2026-08-15T01:00:00Z")
            self.assertEqual(report.verdict, VerificationVerdict.FAILURE)
            self.assertEqual(report.findings[0].code, FindingCode.INTEGRITY_FAILURE)
            receipt = ledger.publish_verdict(report)
            self.assertEqual(receipt.path.name, ResearchLedger.FAILURE_REPORT_NAME)
            reopened = ResearchLedger.open(ledger.root)
            self.assertEqual(
                reopened.verify(as_of_utc="2026-08-16T01:00:00Z").verdict,
                VerificationVerdict.FAILURE,
            )
            with self.assertRaises(QueryOnlyRecoveryRequired):
                ledger.append_event(
                    event(
                        EventType.OBSERVED,
                        "2026-08-15T02:00:00Z",
                        {"observation_sha256": SHA_D},
                    ),
                    expected_sequence=2,
                    expected_previous_record_sha256=first.record_sha256,
                )

    def test_verify_converts_resolve_and_scandir_faults_to_integrity_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self._initialized(directory)
            with mock.patch.object(Path, "resolve", side_effect=OSError("fault")):
                resolve_report = ledger.verify(
                    as_of_utc="2026-08-15T01:00:00Z"
                )
            self.assertEqual(resolve_report.verdict, VerificationVerdict.FAILURE)
            self.assertEqual(
                resolve_report.findings,
                (VerificationFinding(FindingCode.INTEGRITY_FAILURE, None),),
            )
            with mock.patch.object(
                research_ledger_module.os,
                "scandir",
                side_effect=OSError("fault"),
            ):
                scan_report = ledger.verify(as_of_utc="2026-08-15T01:00:00Z")
            self.assertEqual(scan_report.verdict, VerificationVerdict.FAILURE)
            self.assertEqual(scan_report.findings[0].code, FindingCode.INTEGRITY_FAILURE)

    def test_incomplete_or_tampered_terminal_publication_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self._initialized(directory)
            reservation = ledger.reserve_pair(
                make_identity(),
                expected_sequence=1,
                expected_previous_record_sha256=GENESIS_SHA256,
            )
            for item in complete_stream(make_ledger(), reservation):
                self._append(ledger, item.event)
            clean = ledger.verify(as_of_utc="2026-08-16T01:00:00Z")
            real_write = research_ledger_module._write_exclusive

            def interrupt_after_latch(path, document):
                if path.name == ResearchLedger.CLEAN_REPORT_NAME:
                    raise OSError("synthetic publication interruption")
                return real_write(path, document)

            with mock.patch.object(
                research_ledger_module,
                "_write_exclusive",
                side_effect=interrupt_after_latch,
            ):
                with self.assertRaises(OSError):
                    ledger.publish_verdict(clean)
            self.assertTrue(
                (ledger.reports_directory / ResearchLedger.VERDICT_LATCH_NAME).is_file()
            )
            self.assertFalse(
                (ledger.reports_directory / ResearchLedger.CLEAN_SEAL_NAME).exists()
            )
            with self.assertRaises(LedgerIntegrityError):
                ResearchLedger.open(ledger.root)
            incomplete = ledger.verify(as_of_utc="2026-08-16T01:00:00Z")
            self.assertEqual(incomplete.verdict, VerificationVerdict.FAILURE)

        with tempfile.TemporaryDirectory() as directory:
            ledger = self._initialized(directory)
            reservation = ledger.reserve_pair(
                make_identity(),
                expected_sequence=1,
                expected_previous_record_sha256=GENESIS_SHA256,
            )
            for item in complete_stream(make_ledger(), reservation):
                self._append(ledger, item.event)
            clean = ledger.verify(as_of_utc="2026-08-16T01:00:00Z")
            ledger.publish_verdict(clean)
            seal_path = ledger.reports_directory / ResearchLedger.CLEAN_SEAL_NAME
            seal = json.loads(seal_path.read_text(encoding="utf-8"))
            seal["pre_seal_record_count"] += 1
            seal_path.write_text(
                json.dumps(seal, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(LedgerIntegrityError):
                ResearchLedger.open(ledger.root)
            tampered = ledger.verify(as_of_utc="2026-08-16T01:00:00Z")
            self.assertEqual(tampered.verdict, VerificationVerdict.FAILURE)

    def test_terminal_artifact_link_and_permission_substitution_is_rejected(self):
        for mutation in ("hardlink", "symlink", "permission"):
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as directory:
                    ledger = self._initialized(directory)
                    reservation = ledger.reserve_pair(
                        make_identity(),
                        expected_sequence=1,
                        expected_previous_record_sha256=GENESIS_SHA256,
                    )
                    for item in complete_stream(make_ledger(), reservation):
                        self._append(ledger, item.event)
                    clean = ledger.verify(as_of_utc="2026-08-16T01:00:00Z")
                    ledger.publish_verdict(clean)
                    latch = (
                        ledger.reports_directory / ResearchLedger.VERDICT_LATCH_NAME
                    )
                    seal = ledger.reports_directory / ResearchLedger.CLEAN_SEAL_NAME
                    if mutation == "hardlink":
                        os.link(latch, ledger.root / "synthetic-second-link")
                    elif mutation == "symlink":
                        seal.unlink()
                        seal.symlink_to(
                            ledger.reports_directory / ResearchLedger.CLEAN_REPORT_NAME
                        )
                    else:
                        os.chmod(latch, 0o644)
                    with self.assertRaises(LedgerIntegrityError):
                        ResearchLedger.open(ledger.root)

    def test_hardlink_symlink_and_permission_changes_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self._initialized(directory)
            expected_path = ledger.root / ResearchLedger.EXPECTED_LEDGER_NAME
            hard = ledger.root / "second-ledger.json"
            os.link(expected_path, hard)
            with self.assertRaises(LedgerIntegrityError):
                ResearchLedger.open(ledger.root)
            hard.unlink()
            os.chmod(expected_path, 0o644)
            with self.assertRaises(LedgerIntegrityError):
                ResearchLedger.open(ledger.root)
            os.chmod(expected_path, 0o600)
            link_root = Path(directory) / "linked-root"
            link_root.symlink_to(ledger.root, target_is_directory=True)
            with self.assertRaises(LedgerIntegrityError):
                ResearchLedger.open(link_root)
            os.chmod(ledger.records_directory, 0o755)
            report = ledger.verify(as_of_utc="2026-08-15T01:00:00Z")
            self.assertEqual(report.verdict, VerificationVerdict.FAILURE)

    def test_duplicate_json_and_missing_durable_anchor_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = self._initialized(directory)
            reservation = ledger.reserve_pair(
                make_identity(),
                expected_sequence=1,
                expected_previous_record_sha256=GENESIS_SHA256,
            )
            self._append(
                ledger,
                event(
                    EventType.PAIR_RESERVED,
                    "2026-08-15T00:00:00Z",
                    {"reservation_sha256": reservation.reservation_sha256},
                ),
            )
            anchor_path = ledger.anchors_directory / ("%012d.json" % 1)
            anchor_path.unlink()
            missing_anchor = ledger.verify(as_of_utc="2026-08-15T01:00:00Z")
            self.assertEqual(missing_anchor.verdict, VerificationVerdict.FAILURE)

        with tempfile.TemporaryDirectory() as directory:
            ledger = self._initialized(directory)
            reservation = ledger.reserve_pair(
                make_identity(),
                expected_sequence=1,
                expected_previous_record_sha256=GENESIS_SHA256,
            )
            self._append(
                ledger,
                event(
                    EventType.PAIR_RESERVED,
                    "2026-08-15T00:00:00Z",
                    {"reservation_sha256": reservation.reservation_sha256},
                ),
            )
            record_path = ledger.records_directory / ("%012d.json" % 1)
            original = json.loads(record_path.read_text(encoding="utf-8"))
            duplicate = (
                '{"record":'
                + json.dumps(original["record"], sort_keys=True, separators=(",", ":"))
                + ',"record":'
                + json.dumps(original["record"], sort_keys=True, separators=(",", ":"))
                + ',"record_sha256":"'
                + original["record_sha256"]
                + '"}\n'
            )
            record_path.write_text(duplicate, encoding="utf-8")
            duplicate_report = ledger.verify(as_of_utc="2026-08-15T01:00:00Z")
            self.assertEqual(duplicate_report.verdict, VerificationVerdict.FAILURE)

    def test_initialize_requires_existing_absolute_external_0700_root(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            with self.assertRaises(LedgerIntegrityError):
                ResearchLedger.initialize(missing, make_ledger())
            root = Path(directory) / "wide"
            root.mkdir(mode=0o700)
            os.chmod(root, 0o755)
            with self.assertRaises(LedgerIntegrityError):
                ResearchLedger.initialize(root, make_ledger())
            with self.assertRaises(LedgerIntegrityError):
                ResearchLedger.initialize(Path("relative-root"), make_ledger())


if __name__ == "__main__":
    unittest.main()
