from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import unittest
from unittest.mock import patch

from zidoutrade import cli


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


if __name__ == "__main__":
    unittest.main()
