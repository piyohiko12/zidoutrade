import json
from pathlib import Path
import tempfile
import unittest

from zidoutrade.config import ConfigError, load_system_config, parse_system_config


ROOT = Path(__file__).resolve().parents[1]


class SystemConfigTests(unittest.TestCase):
    def setUp(self):
        self.value = json.loads((ROOT / "config/system.example.json").read_text())

    def test_example_is_valid_and_inert(self):
        config = parse_system_config(self.value)
        self.assertEqual(config.mode, "SHADOW")
        self.assertEqual(config.broker_environment, "SIMULATE")
        self.assertEqual(config.planned_risk_basis_points, 25)
        self.assertEqual(config.daily_loss_limit_basis_points, 75)
        self.assertEqual(config.weekly_loss_limit_basis_points, 200)
        self.assertIsNone(config.maximum_investment_cents)
        self.assertIsNone(config.to_risk_policy().maximum_investment_cents)

    def test_real_and_nonloopback_fail_closed(self):
        for key, value in (
            ("broker_environment", "REAL"),
            ("opend_host", "localhost"),
            ("opend_port", 22222),
            ("session", "ALL"),
            ("operating_mode", "UNATTENDED"),
        ):
            with self.subTest(key=key):
                changed = dict(self.value)
                changed[key] = value
                with self.assertRaises(ConfigError):
                    parse_system_config(changed)

    def test_unknown_or_missing_fields_fail(self):
        changed = dict(self.value)
        changed["enable_live"] = False
        with self.assertRaises(ConfigError):
            parse_system_config(changed)
        changed = dict(self.value)
        del changed["rsi_exit"]
        with self.assertRaises(ConfigError):
            parse_system_config(changed)

    def test_frozen_strategy_parameter_change_requires_new_version(self):
        changed = dict(self.value)
        changed["rsi_recovery"] = 36.0
        with self.assertRaises(ConfigError):
            parse_system_config(changed)

    def test_risk_limits_accept_hard_boundaries_and_reject_unsafe_values(self):
        changed = dict(self.value)
        changed.update(
            planned_risk_basis_points=100,
            daily_loss_limit_basis_points=200,
            weekly_loss_limit_basis_points=500,
            maximum_investment_cents=1,
        )
        config = parse_system_config(changed)
        self.assertEqual(config.to_risk_policy().planned_risk_basis_points, 100)
        for key, value in (
            ("planned_risk_basis_points", 101),
            ("daily_loss_limit_basis_points", 201),
            ("weekly_loss_limit_basis_points", 501),
            ("maximum_investment_cents", 0),
            ("maximum_investment_cents", 10.5),
        ):
            with self.subTest(key=key):
                invalid = dict(self.value)
                invalid[key] = value
                with self.assertRaises(ConfigError):
                    parse_system_config(invalid)

    def test_paper_simulate_requires_explicit_positive_investment_limit(self):
        changed = dict(self.value)
        changed["mode"] = "PAPER_SIMULATE"
        with self.assertRaisesRegex(ConfigError, "MAXIMUM_INVESTMENT_REQUIRED"):
            parse_system_config(changed)
        changed["maximum_investment_cents"] = 100_000
        config = parse_system_config(changed)
        self.assertEqual(config.maximum_investment_cents, 100_000)

    def test_bool_is_not_an_integer(self):
        changed = dict(self.value)
        changed["maximum_active_symbols"] = True
        with self.assertRaises(ConfigError):
            parse_system_config(changed)

    def test_load_rejects_nan(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            invalid = dict(self.value)
            invalid["rsi_exit"] = float("nan")
            path.write_text(json.dumps(invalid))
            with self.assertRaises(ConfigError):
                load_system_config(path)


if __name__ == "__main__":
    unittest.main()
