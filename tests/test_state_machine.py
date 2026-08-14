from pathlib import Path
import tempfile
import unittest

from zidoutrade.state_machine import (
    CONTROL_TRANSITIONS,
    EXPOSURE_TRANSITIONS,
    ControlState,
    ExposureState,
    RuntimeState,
    RuntimeStateStore,
    StaleRevisionError,
    StateTransitionError,
    evolve_state,
    transition_control,
    transition_exposure,
)
from zidoutrade.storage import IntegrityError, atomic_write_json, read_json


class StateMachineTests(unittest.TestCase):
    def test_every_state_has_an_explicit_transition_set(self):
        self.assertEqual(set(CONTROL_TRANSITIONS), set(ControlState))
        self.assertEqual(set(EXPOSURE_TRANSITIONS), set(ExposureState))

    def test_control_and_exposure_are_orthogonal(self):
        state = RuntimeState(
            control=ControlState.ARMED,
            exposure=ExposureState.LONG_GUARDED_LOCAL_ONLY,
            selected_symbol="US.TEST",
            reconciled_position_qty=3,
        )
        paused = transition_control(state, ControlState.PAUSED)
        self.assertEqual(paused.exposure, ExposureState.LONG_GUARDED_LOCAL_ONLY)
        self.assertEqual(paused.reconciled_position_qty, 3)
        exiting = transition_exposure(paused, ExposureState.EXIT_INTENT_DURABLE)
        self.assertEqual(exiting.control, ControlState.PAUSED)

    def test_illegal_transition_is_rejected(self):
        state = RuntimeState()
        with self.assertRaises(StateTransitionError):
            transition_exposure(state, ExposureState.EXIT_PENDING)
        emergency = transition_control(state, ControlState.EMERGENCY)
        with self.assertRaises(StateTransitionError):
            transition_control(emergency, ControlState.ARMED)

    def test_recovery_requires_an_explicit_evidence_transition(self):
        state = RuntimeState(
            exposure=ExposureState.ENTRY_PENDING,
            selected_symbol="US.TEST",
        )
        recovery = transition_exposure(state, ExposureState.RECOVERY_REQUIRED)
        reconciled = transition_exposure(
            recovery, ExposureState.ENTRY_RECONCILING
        )
        self.assertEqual(reconciled.exposure, ExposureState.ENTRY_RECONCILING)

    def test_runtime_state_rejects_short_and_excess_exit_dispatches(self):
        with self.assertRaises(ValueError):
            RuntimeState(reconciled_position_qty=-1)
        with self.assertRaises(ValueError):
            RuntimeState(exit_dispatches=3)

    def test_state_store_hash_and_compare_and_swap(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = RuntimeStateStore(path)
            first = RuntimeState(selected_symbol="US.TEST")
            store.save(first, expected_revision=None)
            second = evolve_state(first, session_id="session-1")
            store.save(second, expected_revision=0)
            self.assertEqual(store.load(), second)
            with self.assertRaises(StaleRevisionError):
                store.save(evolve_state(second), expected_revision=0)

            envelope = read_json(path)
            envelope["state"]["session_id"] = "tampered"
            atomic_write_json(path, envelope)
            with self.assertRaises(IntegrityError):
                store.load()


if __name__ == "__main__":
    unittest.main()
