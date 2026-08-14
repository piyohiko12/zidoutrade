#!/usr/bin/env python3
"""Run the one frozen Q015 in-sample sanity comparison exactly once.

This is deliberately a study-specific runner, not a configurable backtest CLI.
It accepts no strategy, threshold, symbol, period, risk, fee, or output options.
The selected quote-only input and pre-existing baseline report are pinned below.
No broker, OpenD, account, position, or order module is imported.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any, Mapping, Sequence


STUDY_ID = "Q015_AAPL_2023_2025_ONE_SHOT_SANITY_V1"
CANDIDATE_ID = "Q015_PRIOR_CLOSE_NET_REWARD_RISK_GATE_V1"
INPUT_DIRECTORY_NAME = "aapl-2023-2025-moomoo-20260813"
INPUT_MANIFEST_NAME = "backtest_input_manifest.json"
BASELINE_REPORT_NAME = "aapl_fixed_baseline_report_v2.json"
OUTPUT_DIRECTORY_NAME = "q015-aapl-2023-2025-one-shot-20260814"
RESERVATION_NAME = "q015-one-shot-reservation.json"
REPORT_NAME = "q015-in-sample-post-hoc-report.json"

EXPECTED_INPUT_MANIFEST_SHA256 = (
    "866495a4804d822cdb4a33248952138834d395e29a42f23f32a46cf354b81df0"
)
EXPECTED_BASELINE_REPORT_SHA256 = (
    "f6fa1b7e73fbc4725f40a090c4214925dfc72dfd60e73f97dafaf92057e1f15b"
)
EXPECTED_SYMBOL = "US.AAPL"
EXPECTED_BENCHMARK = "US.SPY"
EXPECTED_FIRST_SESSION = "2023-01-03"
EXPECTED_LAST_SESSION = "2025-12-31"
EXPECTED_MODEL = "HISTORICAL_CANDLE_PROXY_V1"
EXPECTED_REPORT_SCHEMA = "ZIDOUTRADE_BACKTEST_REPORT_V1"
EXPECTED_INPUT_SCHEMA = "ZIDOUTRADE_BACKTEST_INPUT_BUNDLE_V1"
EXPECTED_POLICY = {
    "daily_loss_limit_basis_points": 75,
    "max_notional_fraction": 0.1,
    "max_roundtrips_per_day": 1,
    "maximum_investment_cents": 1_000_000,
    "planned_risk_basis_points": 25,
    "risk_policy_version": "RSI_RISK_POLICY_V2",
    "weekly_loss_limit_basis_points": 200,
}
EXPECTED_BASELINE_CONFIG = {
    "assumed_spread_bps": 10.0,
    "entry_cushion_bps": 10.0,
    "initial_equity": 100_000.0,
    "model_id": EXPECTED_MODEL,
    "normal_exit_cushion_bps": 15.0,
    "risk_policy": EXPECTED_POLICY,
    "stressed_exit_cushion_bps": 50.0,
    "symbol": EXPECTED_SYMBOL,
}
CLASSIFICATIONS = (
    "IN_SAMPLE_POST_HOC",
    "EXPLORATORY_ONLY",
    "RESEARCH_ONLY",
    "NOT_ADOPTED",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")


class SanityRunError(RuntimeError):
    """The frozen study boundary cannot be proven."""


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SanityRunError("value is not canonical-JSON serializable") from exc
    return (encoded + "\n").encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _safe_directory(path: Path, *, private: bool) -> Path:
    if not path.is_absolute():
        raise SanityRunError("all study paths must be absolute")
    requested = path.absolute()
    try:
        info = requested.lstat()
    except OSError as exc:
        raise SanityRunError("study directory is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SanityRunError("study directory must be a real directory")
    resolved = requested.resolve(strict=True)
    actual = resolved.stat()
    if actual.st_uid != os.geteuid():
        raise SanityRunError("study directory must be owned by the current user")
    mode = stat.S_IMODE(actual.st_mode)
    if private and mode & 0o077:
        raise SanityRunError("study directory must be owner-only")
    if not private and mode & 0o022:
        raise SanityRunError("study parent must not be group/other writable")
    return resolved


def _read_pinned_file(path: Path, *, expected_name: str, expected_sha256: str) -> bytes:
    if not path.is_absolute() or path.name != expected_name:
        raise SanityRunError("pinned input path or name is not exact")
    if not _SHA256.fullmatch(expected_sha256):
        raise SanityRunError("pinned SHA-256 is invalid")
    root = _safe_directory(path.parent, private=True)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(root / expected_name), flags)
    except OSError as exc:
        raise SanityRunError("pinned file is unavailable") from exc
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise SanityRunError("pinned file must be owner-only, regular, and singly linked")
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(fd)
    if _sha256(raw) != expected_sha256:
        raise SanityRunError("pinned file SHA-256 mismatch")
    return raw


def _decode_canonical(raw: bytes, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SanityRunError(f"{label} is not canonical JSON") from exc
    if type(value) is not dict or raw != _canonical_json_bytes(value):
        raise SanityRunError(f"{label} is not canonical JSON")
    return value


def _validate_input_manifest(document: Mapping[str, Any]) -> None:
    exact = {
        "schema_version": EXPECTED_INPUT_SCHEMA,
        "strategy_version": "RSI_AUTOPILOT_V1",
        "classification": "EXPLORATORY_ONLY",
        "provider": "MOOMOO_OPEND",
        "symbol": EXPECTED_SYMBOL,
        "benchmark_symbol": EXPECTED_BENCHMARK,
        "quote_only": True,
        "orders_queried": False,
        "accounts_queried": False,
    }
    for key, expected in exact.items():
        if document.get(key) != expected or type(document.get(key)) is not type(expected):
            raise SanityRunError(f"input manifest {key} is not frozen")
    expected_period = {
        "daily_end": EXPECTED_LAST_SESSION,
        "daily_start": "2022-01-03",
        "end": EXPECTED_LAST_SESSION,
        "start": EXPECTED_FIRST_SESSION,
    }
    if document.get("input_period") != expected_period:
        raise SanityRunError("input period is not frozen")
    files = document.get("files")
    if type(files) is not list or len(files) != 6:
        raise SanityRunError("input file attestations are incomplete")
    roles = set()
    for item in files:
        if type(item) is not dict:
            raise SanityRunError("input file attestation is malformed")
        digest = item.get("sha256")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise SanityRunError("input file attestation hash is malformed")
        if type(item.get("bytes")) is not int or item["bytes"] <= 0:
            raise SanityRunError("input file byte length is malformed")
        if type(item.get("rows")) is not int or item["rows"] <= 0:
            raise SanityRunError("input file row count is malformed")
        roles.add(item.get("role"))
    if len(roles) != 6:
        raise SanityRunError("input roles are not unique")


def _finite_number(value: Any, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise SanityRunError(f"baseline {label} must be finite")
    return float(value)


def _validate_baseline(document: Mapping[str, Any]) -> Mapping[str, Any]:
    if set(document) != {
        "assumptions",
        "classification",
        "input_manifest_sha256",
        "model_id",
        "result",
        "schema_version",
    }:
        raise SanityRunError("baseline report envelope is not exact")
    expected = {
        "schema_version": EXPECTED_REPORT_SCHEMA,
        "classification": "EXPLORATORY_ONLY",
        "model_id": EXPECTED_MODEL,
        "input_manifest_sha256": EXPECTED_INPUT_MANIFEST_SHA256,
    }
    for key, value in expected.items():
        if document.get(key) != value:
            raise SanityRunError(f"baseline report {key} is not frozen")
    result = document.get("result")
    if type(result) is not dict:
        raise SanityRunError("baseline result is missing")
    exact_result = {
        "status": "EXPLORATORY_ONLY",
        "model_id": EXPECTED_MODEL,
        "symbol": EXPECTED_SYMBOL,
        "first_session": EXPECTED_FIRST_SESSION,
        "last_session": EXPECTED_LAST_SESSION,
    }
    for key, value in exact_result.items():
        if result.get(key) != value:
            raise SanityRunError(f"baseline result {key} is not frozen")
    if "strategy_variant_id" in result or result.get("config") != EXPECTED_BASELINE_CONFIG:
        raise SanityRunError("baseline identity or policy is not frozen")
    gross = _finite_number(result.get("total_gross_pnl"), "gross PnL")
    fees = _finite_number(result.get("total_fees"), "fees")
    net = _finite_number(result.get("total_net_pnl"), "net PnL")
    initial = _finite_number(result.get("initial_equity"), "initial equity")
    final = _finite_number(result.get("final_equity"), "final equity")
    terminal = _finite_number(result.get("terminal_return"), "terminal return")
    if not math.isclose(net, gross - fees, rel_tol=1e-12, abs_tol=1e-9):
        raise SanityRunError("baseline fee identity failed")
    if not math.isclose(final, initial + net, rel_tol=1e-12, abs_tol=1e-9):
        raise SanityRunError("baseline equity identity failed")
    if not math.isclose(terminal, final / initial - 1.0, rel_tol=1e-12, abs_tol=1e-12):
        raise SanityRunError("baseline return identity failed")
    return result


def _git(repo: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ("git", "-C", str(repo), *args),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise SanityRunError("git attestation failed")
    return completed.stdout


def _validate_clean_commit(repo: Path, expected_commit: str) -> tuple[str, str]:
    if not _GIT_COMMIT.fullmatch(expected_commit):
        raise SanityRunError("expected commit must be 40 lowercase hexadecimal characters")
    top = Path(_git(repo, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if top != repo.resolve():
        raise SanityRunError("runner is not in the expected repository")
    commit = _git(repo, "rev-parse", "HEAD").decode().strip()
    if commit != expected_commit:
        raise SanityRunError("current commit does not match the sealed commit")
    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise SanityRunError("repository must be completely clean")
    tree = _git(repo, "rev-parse", "HEAD^{tree}").decode().strip()
    if not _GIT_COMMIT.fullmatch(tree):
        raise SanityRunError("Git tree attestation is malformed")
    return commit, tree


def _attest_tracked_python(repo: Path) -> Mapping[str, Any]:
    raw_names = _git(repo, "ls-files", "-z", "--", "src", "scripts", "tests")
    names = sorted(
        item.decode("utf-8")
        for item in raw_names.split(b"\0")
        if item and item.endswith(b".py")
    )
    if "scripts/run_q015_sanity.py" not in names or not any(
        name.startswith("tests/") for name in names
    ):
        raise SanityRunError("source/test inventory is incomplete")
    source_files = {}
    test_files = {}
    for name in names:
        path = repo / name
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise SanityRunError("tracked Python input is not a regular file")
        digest = _sha256(path.read_bytes())
        target = test_files if name.startswith("tests/") else source_files
        target[name] = digest
    return {
        "source_files": source_files,
        "source_set_sha256": _sha256(_canonical_json_bytes(source_files)),
        "test_files": test_files,
        "test_set_sha256": _sha256(_canonical_json_bytes(test_files)),
    }


def _run_synthetic_tests(repo: Path) -> Mapping[str, Any]:
    command = (
        sys.executable,
        "-B",
        "-W",
        "error",
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
        "-v",
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(repo / "src")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        command,
        cwd=repo,
        env=environment,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    transcript = completed.stdout + b"\0" + completed.stderr
    if completed.returncode != 0:
        raise SanityRunError("synthetic test suite failed before reservation")
    match = re.search(rb"Ran ([0-9]+) tests? in ", completed.stderr)
    if match is None or int(match.group(1)) <= 0:
        raise SanityRunError("synthetic test count could not be attested")
    return {
        "command": list(command[1:]),
        "exit_code": completed.returncode,
        "test_count": int(match.group(1)),
        "transcript_sha256": _sha256(transcript),
    }


def _write_exclusive(path: Path, payload: bytes) -> str:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(path), flags, 0o600)
    except FileExistsError as exc:
        raise SanityRunError("one-shot reservation already exists; do not retry") from exc
    try:
        os.fchmod(fd, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise SanityRunError("short reservation write")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)
    directory_fd = os.open(
        str(path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    )
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return _sha256(payload)


def _reserve_output(
    input_directory: Path, repository: Path, reservation: Mapping[str, Any]
) -> tuple[Path, str]:
    if input_directory.name != INPUT_DIRECTORY_NAME:
        raise SanityRunError("input directory name is not frozen")
    parent = _safe_directory(input_directory.parent, private=False)
    output = parent / OUTPUT_DIRECTORY_NAME
    if _is_within(output, repository.resolve()):
        raise SanityRunError("performance output must remain outside the repository")
    try:
        os.mkdir(output, 0o700)
    except FileExistsError as exc:
        raise SanityRunError("one-shot output already exists; do not retry") from exc
    except OSError as exc:
        raise SanityRunError("cannot create one-shot output directory") from exc
    # Directory creation is itself the global reservation for this frozen
    # manifest. Any failure after this point intentionally consumes the run.
    output = _safe_directory(output.absolute(), private=True)
    reservation_hash = _write_exclusive(
        output / RESERVATION_NAME, _canonical_json_bytes(dict(reservation))
    )
    return output, reservation_hash


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="One frozen Q015 in-sample sanity comparison; no tuning options."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--baseline-report", type=Path, required=True)
    parser.add_argument("--expected-commit", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    reserved = False
    try:
        args = _parse_args(argv)
        if not args.manifest.is_absolute() or not args.baseline_report.is_absolute():
            raise SanityRunError("manifest and baseline paths must be absolute")
        if args.manifest.parent != args.baseline_report.parent:
            raise SanityRunError("manifest and baseline must share the frozen input directory")
        repository = Path(__file__).resolve().parents[1]
        commit, tree = _validate_clean_commit(repository, args.expected_commit)
        source_attestation = _attest_tracked_python(repository)

        manifest_raw = _read_pinned_file(
            args.manifest,
            expected_name=INPUT_MANIFEST_NAME,
            expected_sha256=EXPECTED_INPUT_MANIFEST_SHA256,
        )
        _validate_input_manifest(_decode_canonical(manifest_raw, "input manifest"))
        baseline_raw = _read_pinned_file(
            args.baseline_report,
            expected_name=BASELINE_REPORT_NAME,
            expected_sha256=EXPECTED_BASELINE_REPORT_SHA256,
        )
        baseline_document = _decode_canonical(baseline_raw, "baseline report")
        baseline_result = _validate_baseline(baseline_document)

        test_attestation = _run_synthetic_tests(repository)
        # Close the test/run TOCTOU window before consuming the one-shot slot.
        _validate_clean_commit(repository, args.expected_commit)
        if _attest_tracked_python(repository) != source_attestation:
            raise SanityRunError("source/test bytes changed during preflight")

        sys.path.insert(0, str(repository / "src"))
        from zidoutrade.backtest import BacktestVariant
        from zidoutrade.backtest_io import load_backtest_input, write_backtest_report
        from zidoutrade.backtest_runner import run_attested_backtest
        from zidoutrade.risk import RiskPolicy

        try:
            variant = BacktestVariant.Q015_PRIOR_CLOSE_NET_REWARD_RISK_GATE_V1
        except AttributeError as exc:
            raise SanityRunError("frozen Q015 implementation is unavailable") from exc
        if variant.value != CANDIDATE_ID:
            raise SanityRunError("frozen Q015 candidate ID mismatch")
        bundle = load_backtest_input(
            args.manifest,
            expected_manifest_sha256=EXPECTED_INPUT_MANIFEST_SHA256,
        )
        if (
            bundle.symbol != EXPECTED_SYMBOL
            or bundle.benchmark_symbol != EXPECTED_BENCHMARK
            or bundle.input_start.isoformat() != EXPECTED_FIRST_SESSION
            or bundle.input_end.isoformat() != EXPECTED_LAST_SESSION
        ):
            raise SanityRunError("strict input bundle identity mismatch")
        policy = RiskPolicy(
            planned_risk_basis_points=25,
            daily_loss_limit_basis_points=75,
            weekly_loss_limit_basis_points=200,
            maximum_investment_cents=1_000_000,
        )
        if policy.evidence_payload() != EXPECTED_POLICY:
            raise SanityRunError("runtime policy does not match frozen baseline policy")

        reservation = {
            "candidate_id": CANDIDATE_ID,
            "classifications": list(CLASSIFICATIONS),
            "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "data_attestation": {
                "baseline_report_sha256": EXPECTED_BASELINE_REPORT_SHA256,
                "input_manifest_sha256": EXPECTED_INPUT_MANIFEST_SHA256,
            },
            "git_commit": commit,
            "git_tree": tree,
            "one_shot": True,
            "production_order_impact": 0,
            "schema_version": "Q015_ONE_SHOT_RESERVATION_V1",
            "source_test_attestation": source_attestation,
            "study_id": STUDY_ID,
            "synthetic_test_attestation": test_attestation,
        }
        output, reservation_sha256 = _reserve_output(
            args.manifest.parent.absolute(), repository, reservation
        )
        reserved = True

        # This is the sole performance evaluation in this process and occurs
        # only after the immutable reservation has been published.
        candidate = run_attested_backtest(
            bundle,
            initial_equity=100_000.0,
            risk_policy=policy,
            strategy_variant=variant,
        )
        candidate_dict = candidate.to_dict()
        if (
            candidate.status != "EXPLORATORY_ONLY"
            or candidate.strategy_variant_id != CANDIDATE_ID
            or candidate.config.to_dict()["risk_policy"] != EXPECTED_POLICY
        ):
            raise SanityRunError("candidate result escaped the frozen study contract")

        comparison = {
            "candidate_minus_baseline_terminal_return": (
                candidate.terminal_return - float(baseline_result["terminal_return"])
            ),
            "candidate_minus_baseline_total_net_pnl": (
                candidate.total_net_pnl - float(baseline_result["total_net_pnl"])
            ),
        }
        result = {
            "attestations": {
                "baseline_report_sha256": EXPECTED_BASELINE_REPORT_SHA256,
                "git_commit": commit,
                "git_tree": tree,
                "input_files": [
                    {
                        "bytes": item.byte_length,
                        "name": item.name,
                        "role": item.role.value,
                        "sha256": item.sha256,
                    }
                    for item in bundle.attestations
                ],
                "input_manifest_sha256": bundle.manifest_sha256,
                "reservation_sha256": reservation_sha256,
                "source_test": source_attestation,
                "synthetic_tests": test_attestation,
            },
            "baseline": baseline_result,
            "candidate": candidate_dict,
            "classifications": list(CLASSIFICATIONS),
            "comparison": comparison,
            "free_threshold_arguments": False,
            "model_id": EXPECTED_MODEL,
            "production_order_impact": 0,
            "schema_version": "Q015_ONE_SHOT_SANITY_REPORT_V1",
            "status": "EXPLORATORY_ONLY",
            "study_id": STUDY_ID,
        }
        report_sha256 = write_backtest_report(
            output / REPORT_NAME,
            result,
            input_manifest_sha256=bundle.manifest_sha256,
            assumptions=candidate.assumptions,
        )
        print(
            json.dumps(
                {
                    "classifications": list(CLASSIFICATIONS),
                    "ok": True,
                    "performance_disclosed": False,
                    "report_path": str(output / REPORT_NAME),
                    "report_sha256": report_sha256,
                    "reservation_sha256": reservation_sha256,
                },
                sort_keys=True,
            )
        )
        return 0
    except (OSError, SanityRunError, TypeError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "classifications": list(CLASSIFICATIONS),
                    "error": str(exc),
                    "ok": False,
                    "reserved_no_retry": reserved,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
