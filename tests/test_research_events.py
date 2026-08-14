from dataclasses import FrozenInstanceError
import unittest

from zidoutrade.research_events import (
    ArmOutcome,
    EventType,
    ExpectedSession,
    ExpectedSessionLedger,
    PairIdentity,
    ResearchArm,
    ResearchEvent,
    ResearchRecord,
    ResearchSchemaError,
    SelectionKind,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def expected_ledger() -> ExpectedSessionLedger:
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
            ExpectedSession(
                "2026-08-16",
                "2026-08-16T00:00:00Z",
                "2026-08-17T00:00:00Z",
            ),
        ),
    )


class ResearchEventTests(unittest.TestCase):
    def test_pair_identity_is_domain_bound_and_no_selection_is_exact(self):
        selected = PairIdentity(
            study_id="STUDY_V1",
            protocol_id="Q014_V2",
            target_session="2026-08-15",
            selection_kind=SelectionKind.SELECTED_SYMBOL,
            selection_record_sha256=SHA_C,
            selected_symbol="US.TEST",
        )
        no_selection = PairIdentity(
            study_id="STUDY_V1",
            protocol_id="Q014_V2",
            target_session="2026-08-15",
            selection_kind=SelectionKind.NO_SELECTION,
            selection_record_sha256=SHA_C,
        )
        self.assertNotEqual(selected.pair_key, no_selection.pair_key)
        self.assertEqual(
            PairIdentity.from_payload(no_selection.payload()), no_selection
        )
        with self.assertRaises(ResearchSchemaError):
            PairIdentity(
                study_id="STUDY_V1",
                protocol_id="Q014_V2",
                target_session="2026-08-15",
                selection_kind=SelectionKind.NO_SELECTION,
                selection_record_sha256=SHA_C,
                selected_symbol="US.TEST",
            )

    def test_expected_ledger_is_sorted_sealed_and_deadlines_are_strict(self):
        ledger = expected_ledger()
        self.assertEqual(
            ExpectedSessionLedger.from_sealed_document(ledger.sealed_document()),
            ledger,
        )
        self.assertEqual(ledger.session_count, 2)
        with self.assertRaises(ResearchSchemaError):
            ExpectedSessionLedger(
                study_id="STUDY_V1",
                protocol_id="Q014_V2",
                manifest_sha256=SHA_A,
                deadline_source_sha256=SHA_B,
                created_at_utc="2026-08-14T00:00:00Z",
                sessions=(
                    ExpectedSession(
                        "2026-08-15",
                        "2026-08-15T00:00:00Z",
                        "2026-08-17T00:00:00Z",
                    ),
                    ExpectedSession(
                        "2026-08-16",
                        "2026-08-16T00:00:00Z",
                        "2026-08-16T12:00:00Z",
                    ),
                ),
            )
        with self.assertRaises(ResearchSchemaError):
            ExpectedSessionLedger(
                study_id="STUDY_V1",
                protocol_id="Q014_V2",
                manifest_sha256=SHA_A,
                deadline_source_sha256=SHA_B,
                created_at_utc="2026-08-14T00:00:00Z",
                sessions=(
                    ExpectedSession(
                        "2026-08-15",
                        "2026-08-13T00:00:00Z",
                        "2026-08-14T00:00:00Z",
                    ),
                ),
            )

    def test_event_payload_is_strict_and_transitively_immutable(self):
        identity = PairIdentity(
            study_id="STUDY_V1",
            protocol_id="Q014_V2",
            target_session="2026-08-15",
            selection_kind=SelectionKind.SELECTED_SYMBOL,
            selection_record_sha256=SHA_C,
            selected_symbol="US.TEST",
        )
        event = ResearchEvent(
            EventType.ARM_TERMINAL,
            "2026-08-15T01:00:00Z",
            "2026-08-15",
            identity.pair_key,
            {
                "arm": ResearchArm.BASELINE.value,
                "evidence_sha256": SHA_A,
                "outcome": ArmOutcome.WAIT.value,
            },
        )
        record = ResearchRecord(
            study_id="STUDY_V1",
            protocol_id="Q014_V2",
            manifest_sha256=SHA_A,
            expected_ledger_sha256=expected_ledger().ledger_sha256,
            sequence=1,
            previous_record_sha256="0" * 64,
            event=event,
        )
        digest = record.record_sha256
        with self.assertRaises(TypeError):
            event.payload["outcome"] = ArmOutcome.NO_FILL.value
        self.assertEqual(record.record_sha256, digest)
        with self.assertRaises(ResearchSchemaError):
            ResearchEvent(
                EventType.OBSERVED,
                "2026-08-15T01:00:00Z",
                "2026-08-15",
                identity.pair_key,
                {"observation_sha256": SHA_A, "price": 1},
            )
        with self.assertRaises(FrozenInstanceError):
            event.target_session = "2026-08-16"

    def test_unknown_event_extra_record_field_and_noncanonical_time_fail(self):
        identity = PairIdentity(
            study_id="STUDY_V1",
            protocol_id="Q014_V2",
            target_session="2026-08-15",
            selection_kind=SelectionKind.NO_SELECTION,
            selection_record_sha256=SHA_C,
        )
        with self.assertRaises(ResearchSchemaError):
            ResearchEvent(
                "UNKNOWN",
                "2026-08-15T01:00:00Z",
                "2026-08-15",
                identity.pair_key,
                {},
            )
        with self.assertRaises(ResearchSchemaError):
            ResearchEvent(
                EventType.SESSION_COMPLETE,
                "2026-08-15T01:00:00.000000Z",
                "2026-08-15",
                identity.pair_key,
                {},
            )


if __name__ == "__main__":
    unittest.main()
