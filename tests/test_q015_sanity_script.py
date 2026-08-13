import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_q015_sanity", ROOT / "scripts" / "run_q015_sanity.py"
)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import contract
    raise RuntimeError("Q015 sanity runner could not be loaded")
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class Q015SanityRunnerTests(unittest.TestCase):
    def test_cli_exposes_no_strategy_threshold_risk_or_output_knobs(self):
        args = RUNNER._parse_args(
            [
                "--manifest",
                "/private/input/backtest_input_manifest.json",
                "--baseline-report",
                "/private/input/aapl_fixed_baseline_report_v2.json",
                "--expected-commit",
                "a" * 40,
            ]
        )
        self.assertEqual(args.expected_commit, "a" * 40)
        with self.assertRaises(SystemExit):
            RUNNER._parse_args(
                [
                    "--manifest",
                    "/private/input/backtest_input_manifest.json",
                    "--baseline-report",
                    "/private/input/aapl_fixed_baseline_report_v2.json",
                    "--expected-commit",
                    "a" * 40,
                    "--threshold",
                    "1.2",
                ]
            )

    def test_pinned_reader_rejects_wrong_hash_symlink_and_hardlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            target = root / "pinned.json"
            target.write_bytes(b"{}\n")
            target.chmod(0o600)
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            self.assertEqual(
                RUNNER._read_pinned_file(
                    target.absolute(), expected_name="pinned.json", expected_sha256=digest
                ),
                b"{}\n",
            )
            with self.assertRaisesRegex(RUNNER.SanityRunError, "SHA-256 mismatch"):
                RUNNER._read_pinned_file(
                    target.absolute(),
                    expected_name="pinned.json",
                    expected_sha256="0" * 64,
                )
            link = root / "linked.json"
            link.symlink_to(target)
            with self.assertRaises(RUNNER.SanityRunError):
                RUNNER._read_pinned_file(
                    link.absolute(), expected_name="linked.json", expected_sha256=digest
                )
            hardlink = root / "second.json"
            os.link(target, hardlink)
            with self.assertRaisesRegex(RUNNER.SanityRunError, "singly linked"):
                RUNNER._read_pinned_file(
                    target.absolute(), expected_name="pinned.json", expected_sha256=digest
                )

    def test_input_manifest_requires_quote_only_and_frozen_period(self):
        document = {
            "schema_version": RUNNER.EXPECTED_INPUT_SCHEMA,
            "strategy_version": "RSI_AUTOPILOT_V1",
            "classification": "EXPLORATORY_ONLY",
            "provider": "MOOMOO_OPEND",
            "symbol": RUNNER.EXPECTED_SYMBOL,
            "benchmark_symbol": RUNNER.EXPECTED_BENCHMARK,
            "quote_only": True,
            "orders_queried": False,
            "accounts_queried": False,
            "input_period": {
                "daily_end": RUNNER.EXPECTED_LAST_SESSION,
                "daily_start": "2022-01-03",
                "end": RUNNER.EXPECTED_LAST_SESSION,
                "start": RUNNER.EXPECTED_FIRST_SESSION,
            },
            "files": [
                {
                    "role": f"ROLE_{index}",
                    "bytes": 1,
                    "rows": 1,
                    "sha256": f"{index:x}" * 64,
                }
                for index in range(6)
            ],
        }
        RUNNER._validate_input_manifest(document)
        document["orders_queried"] = True
        with self.assertRaisesRegex(RUNNER.SanityRunError, "orders_queried"):
            RUNNER._validate_input_manifest(document)

    def test_baseline_policy_and_accounting_are_exact(self):
        result = {
            "status": "EXPLORATORY_ONLY",
            "model_id": RUNNER.EXPECTED_MODEL,
            "symbol": RUNNER.EXPECTED_SYMBOL,
            "first_session": RUNNER.EXPECTED_FIRST_SESSION,
            "last_session": RUNNER.EXPECTED_LAST_SESSION,
            "config": RUNNER.EXPECTED_BASELINE_CONFIG,
            "total_gross_pnl": 10.0,
            "total_fees": 2.0,
            "total_net_pnl": 8.0,
            "initial_equity": 100_000.0,
            "final_equity": 100_008.0,
            "terminal_return": 0.00008,
        }
        document = {
            "assumptions": ["Synthetic."],
            "classification": "EXPLORATORY_ONLY",
            "input_manifest_sha256": RUNNER.EXPECTED_INPUT_MANIFEST_SHA256,
            "model_id": RUNNER.EXPECTED_MODEL,
            "result": result,
            "schema_version": RUNNER.EXPECTED_REPORT_SCHEMA,
        }
        self.assertIs(RUNNER._validate_baseline(document), result)
        altered = dict(result)
        altered["config"] = dict(RUNNER.EXPECTED_BASELINE_CONFIG)
        altered["config"]["risk_policy"] = dict(RUNNER.EXPECTED_POLICY)
        altered["config"]["risk_policy"]["planned_risk_basis_points"] = 26
        document["result"] = altered
        with self.assertRaisesRegex(RUNNER.SanityRunError, "policy"):
            RUNNER._validate_baseline(document)

    def test_output_directory_creation_is_the_nonretryable_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            parent.chmod(0o755)
            input_dir = parent / RUNNER.INPUT_DIRECTORY_NAME
            input_dir.mkdir(mode=0o700)
            repository = parent / "repository"
            repository.mkdir()
            output, digest = RUNNER._reserve_output(
                input_dir.absolute(), repository.absolute(), {"synthetic": True}
            )
            self.assertEqual(output.name, RUNNER.OUTPUT_DIRECTORY_NAME)
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)
            reservation = output / RUNNER.RESERVATION_NAME
            self.assertEqual(reservation.stat().st_mode & 0o777, 0o600)
            self.assertEqual(digest, hashlib.sha256(reservation.read_bytes()).hexdigest())
            self.assertEqual(json.loads(reservation.read_text()), {"synthetic": True})
            with self.assertRaisesRegex(RUNNER.SanityRunError, "do not retry"):
                RUNNER._reserve_output(
                    input_dir.absolute(), repository.absolute(), {"synthetic": True}
                )


if __name__ == "__main__":
    unittest.main()
