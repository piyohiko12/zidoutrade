import ast
import json
from pathlib import Path
import subprocess
import sys
import unittest

from zidoutrade.research_events import (
    EventType,
    ExpectedSession,
    ExpectedSessionLedger,
    PairIdentity,
    ResearchEvent,
    SelectionKind,
)
from zidoutrade.research_ledger import verify_dataset


ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = (
    ROOT / "src" / "zidoutrade" / "research_events.py",
    ROOT / "src" / "zidoutrade" / "research_ledger.py",
)


class ResearchBoundaryTests(unittest.TestCase):
    def test_research_modules_do_not_import_runtime_or_external_sdk_layers(self):
        forbidden = {
            "zidoutrade.backtest",
            "zidoutrade.broker",
            "zidoutrade.cli",
            "zidoutrade.dashboard",
            "zidoutrade.market_data",
            "zidoutrade.runner",
            "zidoutrade.strategy",
            "moomoo",
            "futu",
        }
        imported = set()
        for path in SOURCE_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if node.level:
                        module = "zidoutrade." + module
                    imported.add(module.rstrip("."))
        self.assertTrue(imported.isdisjoint(forbidden), imported & forbidden)

    def test_import_is_inert_and_does_not_load_forbidden_modules(self):
        code = (
            "import json,sys; "
            "import zidoutrade.research_events,zidoutrade.research_ledger; "
            "print(json.dumps(sorted(k for k in sys.modules "
            "if k in {'moomoo','futu','zidoutrade.broker','zidoutrade.runner',"
            "'zidoutrade.market_data'})))"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", code],
            cwd=ROOT,
            env={"PYTHONPATH": str(ROOT / "src")},
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(result.stdout), [])

    def test_verification_public_payload_has_no_market_execution_or_performance_fields(self):
        ledger = ExpectedSessionLedger(
            study_id="STUDY_V1",
            protocol_id="Q014_V2",
            manifest_sha256="a" * 64,
            deadline_source_sha256="b" * 64,
            created_at_utc="2026-08-14T00:00:00Z",
            sessions=(
                ExpectedSession(
                    "2026-08-15",
                    "2026-08-15T00:00:00Z",
                    "2026-08-16T00:00:00Z",
                ),
            ),
        )
        report = verify_dataset(
            ledger, (), (), as_of_utc="2026-08-15T12:00:00Z"
        )
        serialized = json.dumps(report.payload(), sort_keys=True).lower()
        forbidden = (
            "account_id",
            "balance",
            "order_id",
            "position",
            "price",
            "pnl",
            "profit",
            "return",
            "performance",
        )
        for field in forbidden:
            self.assertNotIn(field, serialized)

    def test_research_event_rejects_unallowlisted_sensitive_payload(self):
        identity = PairIdentity(
            study_id="STUDY_V1",
            protocol_id="Q014_V2",
            target_session="2026-08-15",
            selection_kind=SelectionKind.NO_SELECTION,
            selection_record_sha256="c" * 64,
        )
        for field in ("account_id", "order_id", "position", "price", "pnl"):
            with self.subTest(field=field):
                with self.assertRaises(Exception):
                    ResearchEvent(
                        EventType.OBSERVED,
                        "2026-08-15T01:00:00Z",
                        "2026-08-15",
                        identity.pair_key,
                        {"observation_sha256": "d" * 64, field: "synthetic"},
                    )


if __name__ == "__main__":
    unittest.main()
