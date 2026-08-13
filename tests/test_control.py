from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from zidoutrade.control import ARM_CONFIRMATION, SelectionController
from zidoutrade.selection import PresentedCandidate, SelectionError, SelectionState


NOW = datetime(2026, 8, 13, 5, 0, tzinfo=timezone.utc)


class SelectionControllerTests(unittest.TestCase):
    def controller(self, runtime, repository):
        return SelectionController(
            runtime_root=runtime,
            repository_root=repository,
            candidates_provider=lambda: (
                PresentedCandidate("US.AAPL", 1, True),
                PresentedCandidate("US.BAD", 2, False, ("TRADING_HALTED",)),
            ),
            clock=lambda: NOW,
        )

    def test_draft_validate_and_arm_are_distinct_hash_bound_steps(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repository = base / "repo"
            repository.mkdir()
            controller = self.controller(base / "runtime", repository)
            draft = controller.draft("US.AAPL", "2026-08-14")
            self.assertEqual(draft["state"], "DRAFT")
            with self.assertRaises(SelectionError):
                controller.arm(draft["record_sha256"], ARM_CONFIRMATION)
            validated = controller.validate(draft["record_sha256"])
            self.assertEqual(validated["state"], "VALIDATED")
            with self.assertRaisesRegex(SelectionError, "STALE_SELECTION_REVISION"):
                controller.arm(draft["record_sha256"], ARM_CONFIRMATION)
            with self.assertRaisesRegex(SelectionError, "EXACT_ARM_CONFIRMATION_REQUIRED"):
                controller.arm(validated["record_sha256"], "yes")
            armed = controller.arm(validated["record_sha256"], ARM_CONFIRMATION)
            self.assertEqual(armed["state"], "ARMED_NEXT_SESSION")
            self.assertEqual(controller.store.load_latest().state, SelectionState.ARMED_NEXT_SESSION)

    def test_no_trade_can_be_armed_but_never_permits_trade(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repository = base / "repo"
            repository.mkdir()
            controller = self.controller(base / "runtime", repository)
            draft = controller.draft(None, "2026-08-14")
            validated = controller.validate(draft["record_sha256"])
            controller.arm(validated["record_sha256"], ARM_CONFIRMATION)
            self.assertFalse(controller.store.load_latest().trade_permitted)


if __name__ == "__main__":
    unittest.main()
