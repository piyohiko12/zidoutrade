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

    def test_bool_is_not_an_integer(self):
        changed = dict(self.value)
        changed["maximum_active_symbols"] = True
        with self.assertRaises(ConfigError):
            parse_system_config(changed)

    def test_load_rejects_nan(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(self.value).replace("0.0025", "NaN"))
            with self.assertRaises(ConfigError):
                load_system_config(path)


if __name__ == "__main__":
    unittest.main()
