from __future__ import annotations

import ast
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_q014_v2_structural_replay.py"
SPEC = importlib.util.spec_from_file_location("q014_v2_structural_replay", SCRIPT)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import contract
    raise RuntimeError("Q014 V2 structural replay could not be loaded")
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class Q014V2StructuralReplayTests(unittest.TestCase):
    def test_fixed_matrix_is_structural_only_and_fully_rejected(self):
        report = dict(RUNNER.run_structural_replay())

        self.assertEqual(set(report), set(RUNNER.PUBLIC_KEYS))
        self.assertEqual(report["classification"], "STRUCTURAL_REPLAY_ONLY")
        self.assertEqual(
            report["classifications"],
            [
                "STRUCTURAL_REPLAY_ONLY",
                "SYNTHETIC_ONLY",
                "RESEARCH_INFRASTRUCTURE_ONLY",
                "NO_PERFORMANCE_EVIDENCE",
                "NOT_A_STRATEGY_BACKTEST",
                "NOT_OOS",
                "ORDER_IMPACT_ZERO",
            ],
        )
        self.assertTrue(report["ok"])
        self.assertEqual(report["integrity_verdict"], "STRUCTURALLY_VALID")
        self.assertTrue(report["seal_eligible_for_complete_fixture"])
        self.assertTrue(report["reported_missing_structurally_terminal"])
        self.assertTrue(report["unreported_deadline_missing_failed_closed"])
        self.assertEqual(report["production_order_impact"], 0)
        self.assertGreaterEqual(report["rejected_fault_count"], 14)
        self.assertEqual(
            report["scenario_count"], report["rejected_fault_count"] + 2
        )

        digest = report.pop("report_sha256")
        self.assertEqual(
            digest,
            hashlib.sha256(RUNNER._canonical_json_bytes(report)).hexdigest(),
        )

    def test_main_stdout_is_canonical_and_contains_only_redacted_fields(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result = RUNNER.main([])

        self.assertEqual(result, 0)
        raw = output.getvalue().encode("utf-8")
        document = json.loads(raw.decode("utf-8"))
        self.assertEqual(raw, RUNNER._canonical_json_bytes(document))
        self.assertEqual(set(document), set(RUNNER.PUBLIC_KEYS))
        forbidden_keys = {
            "account",
            "evidence",
            "manifest",
            "order",
            "pair",
            "pnl",
            "position",
            "price",
            "session",
            "symbol",
            "trade",
        }
        self.assertTrue(forbidden_keys.isdisjoint(document))

    def test_cli_accepts_no_data_strategy_risk_or_output_arguments(self):
        RUNNER._parse_args([])
        for option in (
            "--manifest",
            "--symbol",
            "--strategy",
            "--threshold",
            "--risk",
            "--report",
        ):
            with self.subTest(option=option), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    RUNNER._parse_args([option, "synthetic"])

    def test_script_has_no_historical_runtime_or_execution_imports(self):
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"), filename=str(SCRIPT))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        forbidden = (
            "moomoo",
            "zidoutrade.backtest",
            "zidoutrade.backtest_io",
            "zidoutrade.backtest_runner",
            "zidoutrade.broker",
            "zidoutrade.cli",
            "zidoutrade.dashboard",
            "zidoutrade.decision_cycle",
            "zidoutrade.market_data",
            "zidoutrade.runner",
        )
        violations = [
            name
            for name in imported
            if any(name == item or name.startswith(item + ".") for item in forbidden)
        ]
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
