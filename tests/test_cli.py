from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from zidoutrade import cli
from zidoutrade.risk_settings import RiskSettingsStore, parse_risk_settings_update


ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def test_validate_example(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(["validate-config", str(ROOT / "config/system.example.json")])
        self.assertEqual(code, 0)
        self.assertIn('"valid": true', output.getvalue())
        self.assertNotIn("account", output.getvalue().lower())

    def test_cli_has_no_order_or_activation_command(self):
        help_text = io.StringIO()
        with self.assertRaises(SystemExit):
            with redirect_stdout(help_text):
                cli.main(["--help"])
        text = help_text.getvalue()
        self.assertIn("dashboard", text)
        self.assertNotIn("place-order", text)
        self.assertNotIn("activate", text)

    def test_nonloopback_dashboard_fails_before_binding(self):
        error = io.StringIO()
        with redirect_stderr(error):
            code = cli.main(["dashboard", "--host", "0.0.0.0"])
        self.assertEqual(code, 2)
        self.assertIn("loopback", error.getvalue().lower())

    def test_dashboard_risk_settings_are_read_only_by_default(self):
        captured = []

        class FakeServer:
            def __init__(self, application, *, host, port):
                captured.append(application)
                self.address = (host, port)

            def serve_forever(self):
                return None

            def shutdown(self):
                return None

        with patch.object(cli, "DashboardServer", FakeServer), redirect_stdout(io.StringIO()):
            code = cli.main(["dashboard", "--port", "0"])
        self.assertEqual(code, 0)
        self.assertEqual(len(captured), 1)
        self.assertFalse(captured[0].state()["risk_settings"]["editable"])
        self.assertIsNone(captured[0].risk_settings_callback)

    def test_explicit_safe_external_root_wires_a_real_next_session_store(self):
        captured = []

        class FakeServer:
            def __init__(self, application, *, host, port):
                captured.append(application)
                self.address = (host, port)

            def serve_forever(self):
                return None

            def shutdown(self):
                return None

        with tempfile.TemporaryDirectory() as raw_root:
            runtime_root = Path(raw_root)
            os.chmod(runtime_root, 0o700)
            with patch.object(cli, "DashboardServer", FakeServer), redirect_stdout(io.StringIO()):
                code = cli.main(
                    [
                        "dashboard",
                        "--port",
                        "0",
                        "--risk-settings-runtime-root",
                        str(runtime_root),
                    ]
                )
            self.assertEqual(code, 0)
            app = captured[0]
            self.assertTrue(app.state()["risk_settings"]["editable"])
            update = parse_risk_settings_update(
                {
                    "confirmation": "SAVE_NEXT_SESSION_RISK",
                    "daily_loss_limit_basis_points": 75,
                    "expected_sha256": None,
                    "maximum_investment_cents": 50_000,
                    "planned_risk_basis_points": 25,
                    "risk_policy_version": "RSI_RISK_POLICY_V2",
                    "target_session": "2099-01-05",
                    "weekly_loss_limit_basis_points": 200,
                }
            )
            result = app.risk_settings_callback(update)
            self.assertTrue(result["saved"])
            stored = RiskSettingsStore(
                runtime_root,
                repository_root=ROOT,
            ).load_latest(required=True)
            self.assertEqual(stored.target_session, "2099-01-05")
            self.assertEqual(stored.policy.maximum_investment_cents, 50_000)

    def test_unsafe_risk_settings_root_fails_before_server_creation(self):
        error = io.StringIO()
        with redirect_stderr(error):
            code = cli.main(
                [
                    "dashboard",
                    "--risk-settings-runtime-root",
                    "relative/runtime",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("absolute", error.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
