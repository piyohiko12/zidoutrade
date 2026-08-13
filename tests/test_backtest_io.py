from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import unittest

from zidoutrade.backtest_io import (
    BacktestInputError,
    BacktestOutputError,
    INPUT_SCHEMA,
    InputRole,
    STRUCTURAL_CLASSIFICATION,
    load_backtest_input,
    write_backtest_report,
)
from zidoutrade.storage import canonical_json_bytes


def _write(path: Path, value) -> tuple[int, str]:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path.write_bytes(raw)
    return len(raw), hashlib.sha256(raw).hexdigest()


def _bar(time_text: str, *, base: float = 100.0):
    return {
        "time": time_text,
        "open": base,
        "high": base + 2.0,
        "low": base - 1.0,
        "close": base + 1.0,
        "volume": 1000,
        "turnover": 100500.0,
    }


def _fixture(root: Path) -> Path:
    files = []

    def kline(role, name, code, ktype, adjustment, rows):
        length, digest = _write(
            root / name,
            {"code": code, "data": rows, "ktype": ktype, "source": "history"},
        )
        files.append(
            {
                "role": role,
                "name": name,
                "format": "MOOMOO_KLINE_V1",
                "code": code,
                "ktype": ktype,
                "adjustment": adjustment,
                "session": "RTH",
                "source": "history",
                "bytes": length,
                "sha256": digest,
                "rows": len(rows),
            }
        )

    whole = [
        _bar("2025-01-02 %02d:%02d:00" % (9 + (45 + 15 * index) // 60, (45 + 15 * index) % 60))
        for index in range(26)
    ]
    # The compact expression above ends exactly at 16:00 and models bar-end ET.
    self_times = [row["time"] for row in whole]
    if self_times[-1] != "2025-01-02 16:00:00":
        raise AssertionError(self_times[-1])
    kline(
        InputRole.SYMBOL_INTRADAY_QFQ.value,
        "symbol_15m_qfq.json",
        "US.AAPL",
        "15m",
        "QFQ",
        whole,
    )
    kline(
        InputRole.SYMBOL_INTRADAY_RAW.value,
        "symbol_15m_raw.json",
        "US.AAPL",
        "15m",
        "RAW",
        whole,
    )
    daily = [_bar("2025-01-02 00:00:00")]
    kline(
        InputRole.SYMBOL_DAILY_QFQ.value,
        "symbol_daily_qfq.json",
        "US.AAPL",
        "1d",
        "QFQ",
        daily,
    )
    kline(
        InputRole.SYMBOL_DAILY_RAW.value,
        "symbol_daily_raw.json",
        "US.AAPL",
        "1d",
        "RAW",
        daily,
    )
    kline(
        InputRole.BENCHMARK_DAILY_QFQ.value,
        "spy_daily_qfq.json",
        "US.SPY",
        "1d",
        "QFQ",
        daily,
    )
    calendar = {
        "data": ["{'time': '2025-01-02', 'trade_date_type': 'WHOLE'}"],
        "market": "US",
    }
    length, digest = _write(root / "us_trading_calendar.json", calendar)
    files.append(
        {
            "role": InputRole.US_TRADING_CALENDAR.value,
            "name": "us_trading_calendar.json",
            "format": "MOOMOO_TRADING_DAYS_V1",
            "code": "US",
            "ktype": "calendar",
            "adjustment": "NONE",
            "session": "RTH",
            "source": "history",
            "bytes": length,
            "sha256": digest,
            "rows": 1,
        }
    )
    provenance = {}
    for key, name in (
        ("acquisition_manifest", "input_manifest.json"),
        ("calendar_manifest", "calendar_manifest.json"),
    ):
        length, digest = _write(root / name, {"source": key})
        provenance[key] = {"name": name, "bytes": length, "sha256": digest}
    manifest = {
        "schema_version": INPUT_SCHEMA,
        "strategy_version": "RSI_AUTOPILOT_V1",
        "classification": "EXPLORATORY_ONLY",
        "provider": "MOOMOO_OPEND",
        "moomoo_sdk": "10.9.6908",
        "opend_gui": "10.9.6918",
        "acquired_at_utc": "2026-08-13T01:02:03Z",
        "symbol": "US.AAPL",
        "benchmark_symbol": "US.SPY",
        "input_period": {
            "start": "2025-01-02",
            "end": "2025-01-02",
            "daily_start": "2025-01-02",
            "daily_end": "2025-01-02",
        },
        "quote_only": True,
        "orders_queried": False,
        "accounts_queried": False,
        "timestamp_semantics": {
            "intraday": "BAR_END_ET",
            "daily": "DATE_ET",
            "calendar": "SESSION_TYPE",
        },
        "provenance": provenance,
        "files": files,
    }
    path = root / "backtest_input_manifest.json"
    path.write_bytes(canonical_json_bytes(manifest))
    return path


class BacktestInputTests(unittest.TestCase):
    def test_loads_exact_history_and_exposes_prior_daily_slices(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = _fixture(Path(directory))
            digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
            bundle = load_backtest_input(manifest, expected_manifest_sha256=digest)
            self.assertEqual(bundle.symbol, "US.AAPL")
            self.assertEqual(bundle.benchmark_symbol, "US.SPY")
            self.assertEqual(bundle.provider, "MOOMOO_OPEND")
            self.assertEqual(bundle.moomoo_sdk, "10.9.6908")
            self.assertEqual(len(bundle.intraday_qfq), 26)
            self.assertEqual(bundle.intraday_qfq[0].time.hour, 9)
            self.assertEqual(bundle.intraday_qfq[0].time.minute, 45)
            self.assertEqual(bundle.intraday_qfq[0].start.hour, 9)
            self.assertEqual(bundle.intraday_qfq[0].start.minute, 30)
            self.assertEqual(bundle.sessions[0].close_at.hour, 16)
            self.assertEqual(bundle.manifest_sha256, digest)
            self.assertEqual(
                bundle.prior_symbol_daily_qfq(bundle.input_start), ()
            )

    def test_manifest_must_be_canonical_and_external_pin_must_match(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = _fixture(Path(directory))
            with self.assertRaisesRegex(BacktestInputError, "manifest SHA-256 mismatch"):
                load_backtest_input(manifest, expected_manifest_sha256="0" * 64)
            document = json.loads(manifest.read_text(encoding="utf-8"))
            manifest.write_text(json.dumps(document, indent=2), encoding="utf-8")
            with self.assertRaisesRegex(BacktestInputError, "canonical JSON"):
                load_backtest_input(manifest)

    def test_rejects_wrong_hash_metadata_and_quote_safety_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = _fixture(root)
            document = json.loads(manifest.read_text(encoding="utf-8"))
            document["accounts_queried"] = True
            manifest.write_bytes(canonical_json_bytes(document))
            with self.assertRaisesRegex(BacktestInputError, "accounts_queried"):
                load_backtest_input(manifest)

            manifest = _fixture(root)
            document = json.loads(manifest.read_text(encoding="utf-8"))
            document["files"][0]["sha256"] = "0" * 64
            manifest.write_bytes(canonical_json_bytes(document))
            with self.assertRaisesRegex(BacktestInputError, "SHA-256 mismatch"):
                load_backtest_input(manifest)

    def test_rejects_nonfinite_or_malformed_ohlcv_and_unknown_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = _fixture(root)
            source = root / "symbol_15m_qfq.json"
            document = json.loads(source.read_text(encoding="utf-8"))
            document["data"][0]["close"] = math.inf
            raw = json.dumps(document, separators=(",", ":")).encode("utf-8")
            source.write_bytes(raw)
            man = json.loads(manifest.read_text(encoding="utf-8"))
            entry = next(item for item in man["files"] if item["name"] == source.name)
            entry["bytes"] = len(raw)
            entry["sha256"] = hashlib.sha256(raw).hexdigest()
            manifest.write_bytes(canonical_json_bytes(man))
            with self.assertRaises(BacktestInputError):
                load_backtest_input(manifest)

    def test_rejects_duplicate_or_nonmatching_qfq_raw_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = _fixture(root)
            source = root / "symbol_15m_raw.json"
            document = json.loads(source.read_text(encoding="utf-8"))
            document["data"][1]["time"] = document["data"][0]["time"]
            raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
            source.write_bytes(raw)
            man = json.loads(manifest.read_text(encoding="utf-8"))
            entry = next(item for item in man["files"] if item["name"] == source.name)
            entry["bytes"] = len(raw)
            entry["sha256"] = hashlib.sha256(raw).hexdigest()
            manifest.write_bytes(canonical_json_bytes(man))
            with self.assertRaisesRegex(BacktestInputError, "strictly ordered"):
                load_backtest_input(manifest)

    def test_rejects_qfq_raw_volume_or_within_session_scale_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = _fixture(root)
            source = root / "symbol_15m_raw.json"
            document = json.loads(source.read_text(encoding="utf-8"))
            document["data"][0]["volume"] += 1
            raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
            source.write_bytes(raw)
            man = json.loads(manifest.read_text(encoding="utf-8"))
            entry = next(item for item in man["files"] if item["name"] == source.name)
            entry["bytes"] = len(raw)
            entry["sha256"] = hashlib.sha256(raw).hexdigest()
            manifest.write_bytes(canonical_json_bytes(man))
            with self.assertRaisesRegex(BacktestInputError, "volume differs"):
                load_backtest_input(manifest)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = _fixture(root)
            source = root / "symbol_15m_raw.json"
            document = json.loads(source.read_text(encoding="utf-8"))
            row = document["data"][0]
            row["open"] *= 1.01
            row["high"] *= 1.01
            row["low"] *= 1.01
            row["close"] *= 1.01
            raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
            source.write_bytes(raw)
            man = json.loads(manifest.read_text(encoding="utf-8"))
            entry = next(item for item in man["files"] if item["name"] == source.name)
            entry["bytes"] = len(raw)
            entry["sha256"] = hashlib.sha256(raw).hexdigest()
            manifest.write_bytes(canonical_json_bytes(man))
            with self.assertRaisesRegex(BacktestInputError, "scale changes"):
                load_backtest_input(manifest)

    def test_calendar_literal_eval_rejects_code_and_unknown_session_type(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = _fixture(root)
            source = root / "us_trading_calendar.json"
            document = json.loads(source.read_text(encoding="utf-8"))
            document["data"] = ["__import__('os').system('false')"]
            raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
            source.write_bytes(raw)
            man = json.loads(manifest.read_text(encoding="utf-8"))
            entry = next(item for item in man["files"] if item["name"] == source.name)
            entry["bytes"] = len(raw)
            entry["sha256"] = hashlib.sha256(raw).hexdigest()
            manifest.write_bytes(canonical_json_bytes(man))
            with self.assertRaisesRegex(BacktestInputError, "safe Python literal"):
                load_backtest_input(manifest)

    def test_descriptor_snapshot_rejects_hard_links_and_symlink_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = _fixture(root)
            extra = root / "second-link.json"
            os.link(root / "symbol_15m_qfq.json", extra)
            with self.assertRaisesRegex(BacktestInputError, "hard link"):
                load_backtest_input(manifest)
            extra.unlink()
            link = root / "manifest-link.json"
            link.symlink_to(manifest)
            with self.assertRaises(BacktestInputError):
                load_backtest_input(link)

    def test_manifest_parent_must_be_private_and_owner_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = _fixture(root)
            root.chmod(0o755)
            try:
                with self.assertRaisesRegex(BacktestInputError, "group/other"):
                    load_backtest_input(manifest)
            finally:
                root.chmod(0o700)

    def test_half_day_requires_fourteen_exact_rth_bars(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = _fixture(root)
            calendar_source = root / "us_trading_calendar.json"
            calendar = {
                "data": ["{'time': '2025-01-02', 'trade_date_type': 'MORNING'}"],
                "market": "US",
            }
            cal_raw = json.dumps(calendar, sort_keys=True, separators=(",", ":")).encode("utf-8")
            calendar_source.write_bytes(cal_raw)
            man = json.loads(manifest.read_text(encoding="utf-8"))
            cal_entry = next(item for item in man["files"] if item["name"] == calendar_source.name)
            cal_entry["bytes"] = len(cal_raw)
            cal_entry["sha256"] = hashlib.sha256(cal_raw).hexdigest()
            manifest.write_bytes(canonical_json_bytes(man))
            with self.assertRaisesRegex(BacktestInputError, "outside RTH|RTH history"):
                load_backtest_input(manifest)


class BacktestOutputTests(unittest.TestCase):
    def test_report_is_canonical_exclusive_and_classified(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "report.json"
            digest = write_backtest_report(
                target,
                {"trade_count": 0},
                input_manifest_sha256="a" * 64,
                assumptions=("Historical bid/ask was unavailable.",),
            )
            raw = target.read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
            document = json.loads(raw.decode("utf-8"))
            self.assertEqual(document["classification"], STRUCTURAL_CLASSIFICATION)
            self.assertEqual(document["model_id"], "HISTORICAL_CANDLE_PROXY_V1")
            self.assertEqual(raw, canonical_json_bytes(document))
            with self.assertRaisesRegex(BacktestOutputError, "already exists"):
                write_backtest_report(
                    target,
                    {"trade_count": 1},
                    input_manifest_sha256="a" * 64,
                    assumptions=("Historical bid/ask was unavailable.",),
                )

    def test_report_rejects_nonfinite_and_unclassified_output(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(BacktestOutputError):
                write_backtest_report(
                    Path(directory) / "report.json",
                    {"return": math.nan},
                    input_manifest_sha256="a" * 64,
                    assumptions=("Synthetic assumption.",),
                )
            with self.assertRaises(ValueError):
                write_backtest_report(
                    Path(directory) / "report.json",
                    {},
                    input_manifest_sha256="a" * 64,
                    assumptions=("Synthetic assumption.",),
                    classification="PRODUCTION_READY",
                )
            with self.assertRaisesRegex(ValueError, "result status"):
                write_backtest_report(
                    Path(directory) / "report.json",
                    {"status": "PRODUCTION_READY"},
                    input_manifest_sha256="a" * 64,
                    assumptions=("Synthetic assumption.",),
                )

    def test_report_requires_existing_absolute_private_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing" / "report.json"
            with self.assertRaises(BacktestOutputError):
                write_backtest_report(
                    missing,
                    {},
                    input_manifest_sha256="a" * 64,
                    assumptions=("Synthetic assumption.",),
                )
            self.assertFalse(missing.parent.exists())
            with self.assertRaisesRegex(BacktestOutputError, "absolute"):
                write_backtest_report(
                    Path("relative-report.json"),
                    {},
                    input_manifest_sha256="a" * 64,
                    assumptions=("Synthetic assumption.",),
                )
            root.chmod(0o755)
            try:
                with self.assertRaises(BacktestOutputError):
                    write_backtest_report(
                        root / "report.json",
                        {},
                        input_manifest_sha256="a" * 64,
                        assumptions=("Synthetic assumption.",),
                    )
            finally:
                root.chmod(0o700)

    def test_report_rejects_repository_target_before_creating_parent(self):
        repository = Path(__file__).resolve().parents[1]
        missing = repository / "__backtest_report_must_not_exist__" / "report.json"
        self.assertFalse(missing.parent.exists())
        with self.assertRaisesRegex(BacktestOutputError, "outside the repository"):
            write_backtest_report(
                missing,
                {},
                input_manifest_sha256="a" * 64,
                assumptions=("Synthetic assumption.",),
            )
        self.assertFalse(missing.parent.exists())


if __name__ == "__main__":
    unittest.main()
