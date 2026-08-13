"""Strict, quote-only inputs and headless output for structural backtests.

The moomoo quote helper deliberately emits a small JSON envelope which does
not record the requested adjustment mode.  This module therefore accepts data
only through a separate, canonical manifest that pins every source byte and
attests the role, adjustment and session used during acquisition.  It contains
no SDK, network, account or order code.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from .storage import canonical_json_bytes


NEW_YORK = ZoneInfo("America/New_York")

INPUT_SCHEMA = "ZIDOUTRADE_BACKTEST_INPUT_BUNDLE_V1"
REPORT_SCHEMA = "ZIDOUTRADE_BACKTEST_REPORT_V1"
REPORT_MODEL = "HISTORICAL_CANDLE_PROXY_V1"
KLINE_FORMAT = "MOOMOO_KLINE_V1"
CALENDAR_FORMAT = "MOOMOO_TRADING_DAYS_V1"
STRUCTURAL_CLASSIFICATION = "EXPLORATORY_ONLY"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SYMBOL_RE = re.compile(r"^US\.[A-Z][A-Z0-9.-]{0,14}$")
_KLINE_ROOT_KEYS = frozenset(("code", "data", "ktype", "source"))
_KLINE_BAR_KEYS = frozenset(
    ("time", "open", "high", "low", "close", "volume", "turnover")
)
_CALENDAR_ROOT_KEYS = frozenset(("market", "data"))
_CALENDAR_ROW_KEYS = frozenset(("time", "trade_date_type"))
_ROOT_KEYS = frozenset(
    (
        "schema_version",
        "strategy_version",
        "classification",
        "provider",
        "moomoo_sdk",
        "opend_gui",
        "acquired_at_utc",
        "symbol",
        "benchmark_symbol",
        "input_period",
        "quote_only",
        "orders_queried",
        "accounts_queried",
        "timestamp_semantics",
        "provenance",
        "files",
    )
)
_TIMESTAMP_SEMANTICS = {
    "intraday": "BAR_END_ET",
    "daily": "DATE_ET",
    "calendar": "SESSION_TYPE",
}
_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}$")
_UTC_TEXT_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)


class BacktestInputError(ValueError):
    """Input bytes, provenance, timestamps or market data are ambiguous."""


class BacktestOutputError(RuntimeError):
    """A headless report cannot be published without replacing an artifact."""


class InputRole(str, Enum):
    SYMBOL_INTRADAY_QFQ = "SYMBOL_INTRADAY_QFQ"
    SYMBOL_INTRADAY_RAW = "SYMBOL_INTRADAY_RAW"
    SYMBOL_DAILY_QFQ = "SYMBOL_DAILY_QFQ"
    SYMBOL_DAILY_RAW = "SYMBOL_DAILY_RAW"
    BENCHMARK_DAILY_QFQ = "BENCHMARK_DAILY_QFQ"
    US_TRADING_CALENDAR = "US_TRADING_CALENDAR"


_KLINE_ROLES = frozenset(
    (
        InputRole.SYMBOL_INTRADAY_QFQ,
        InputRole.SYMBOL_INTRADAY_RAW,
        InputRole.SYMBOL_DAILY_QFQ,
        InputRole.SYMBOL_DAILY_RAW,
        InputRole.BENCHMARK_DAILY_QFQ,
    )
)


@dataclass(frozen=True)
class FileAttestation:
    role: InputRole
    name: str
    format: str
    code: str
    ktype: str
    adjustment: str
    session: str
    source: str
    byte_length: int
    sha256: str
    rows: int


@dataclass(frozen=True)
class HistoricalBar:
    """One exact moomoo history row with an Eastern timestamp.

    For 15-minute data, ``time`` is the bar end.  For daily data it is the
    session date at Eastern midnight and is never interpreted as an instant at
    which the day's close was already knowable.
    """

    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    turnover: float

    @property
    def session_date(self) -> date:
        return self.time.date()

    @property
    def start(self) -> datetime:
        return self.time - timedelta(minutes=15)


@dataclass(frozen=True)
class HistoricalSession:
    session_date: date
    open_at: datetime
    close_at: datetime
    kind: str


@dataclass(frozen=True)
class BacktestInputBundle:
    """Immutable, fully-attested inputs consumed by the pure backtest engine."""

    strategy_version: str
    classification: str
    provider: str
    moomoo_sdk: str
    opend_gui: str
    symbol: str
    benchmark_symbol: str
    acquired_at_utc: datetime
    input_start: date
    input_end: date
    daily_start: date
    daily_end: date
    intraday_qfq: Tuple[HistoricalBar, ...]
    intraday_raw: Tuple[HistoricalBar, ...]
    symbol_daily_qfq: Tuple[HistoricalBar, ...]
    symbol_daily_raw: Tuple[HistoricalBar, ...]
    benchmark_daily_qfq: Tuple[HistoricalBar, ...]
    sessions: Tuple[HistoricalSession, ...]
    attestations: Tuple[FileAttestation, ...]
    manifest_sha256: str
    manifest_byte_length: int

    def prior_symbol_daily_qfq(self, session_date: date) -> Tuple[HistoricalBar, ...]:
        """Return only symbol daily bars completed before ``session_date``."""

        _require_exact_date("session_date", session_date)
        return tuple(bar for bar in self.symbol_daily_qfq if bar.session_date < session_date)

    def prior_symbol_daily_raw(self, session_date: date) -> Tuple[HistoricalBar, ...]:
        _require_exact_date("session_date", session_date)
        return tuple(bar for bar in self.symbol_daily_raw if bar.session_date < session_date)

    def prior_benchmark_daily_qfq(
        self, session_date: date
    ) -> Tuple[HistoricalBar, ...]:
        _require_exact_date("session_date", session_date)
        return tuple(
            bar for bar in self.benchmark_daily_qfq if bar.session_date < session_date
        )


def _require_exact_date(name: str, value: object) -> date:
    if type(value) is not date:
        raise TypeError("%s must be an exact date" % name)
    return value


def _require_exact_keys(value: object, expected: frozenset, label: str) -> Mapping[str, Any]:
    if type(value) is not dict:
        raise BacktestInputError("%s must be an exact JSON object" % label)
    keys = frozenset(value.keys())
    if keys != expected or any(type(key) is not str for key in value):
        raise BacktestInputError("%s has missing or unexpected keys" % label)
    return value


def _require_str(name: str, value: object, *, expected: Optional[str] = None) -> str:
    if type(value) is not str:
        raise BacktestInputError("%s must be an exact string" % name)
    if expected is not None and value != expected:
        raise BacktestInputError("%s must equal %s" % (name, expected))
    return value


def _require_bool(name: str, value: object, *, expected: bool) -> bool:
    if type(value) is not bool or value is not expected:
        raise BacktestInputError("%s must be %s" % (name, str(expected).lower()))
    return value


def _require_nonnegative_int(name: str, value: object) -> int:
    if type(value) is not int or value < 0:
        raise BacktestInputError("%s must be a nonnegative exact integer" % name)
    return value


def _require_number(name: str, value: object, *, positive: bool) -> float:
    if type(value) not in (int, float):
        raise BacktestInputError("%s must be an exact JSON number" % name)
    result = float(value)
    if not math.isfinite(result):
        raise BacktestInputError("%s must be finite" % name)
    if positive and result <= 0.0:
        raise BacktestInputError("%s must be greater than zero" % name)
    if not positive and result < 0.0:
        raise BacktestInputError("%s must be nonnegative" % name)
    return result


def _decode_json(raw: bytes, label: str) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BacktestInputError("%s must be UTF-8" % label) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise BacktestInputError("%s is not valid JSON" % label) from exc


def _safe_directory(path: Path, label: str, *, private: bool = False) -> Path:
    requested = Path(path).absolute()
    try:
        info = requested.lstat()
    except OSError as exc:
        raise BacktestInputError("%s directory is unavailable" % label) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise BacktestInputError("%s must be a real non-symlink directory" % label)
    resolved = requested.resolve(strict=True)
    resolved_info = resolved.stat()
    if resolved_info.st_uid != os.geteuid():
        raise BacktestInputError("%s must be owned by the current user" % label)
    if private and stat.S_IMODE(resolved_info.st_mode) & 0o077:
        raise BacktestInputError("%s must not grant group/other permissions" % label)
    return resolved


def _read_regular_file(root: Path, name: str, label: str) -> bytes:
    """Read one immutable snapshot through the validated descriptor itself."""

    if not _SAFE_NAME_RE.fullmatch(name):
        raise BacktestInputError("%s file name is unsafe" % label)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(root / name), flags)
    except OSError as exc:
        raise BacktestInputError("%s is unavailable" % label) from exc
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
        ):
            raise BacktestInputError(
                "%s must be an owner-owned regular file with exactly one hard link"
                % label
            )
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _read_attested(root: Path, attestation: FileAttestation) -> bytes:
    raw = _read_regular_file(root, attestation.name, "attested input")
    if len(raw) != attestation.byte_length:
        raise BacktestInputError("byte length mismatch: %s" % attestation.name)
    if hashlib.sha256(raw).hexdigest() != attestation.sha256:
        raise BacktestInputError("SHA-256 mismatch: %s" % attestation.name)
    return raw


def _parse_manifest_bytes(raw: bytes) -> Mapping[str, Any]:
    parsed = _decode_json(raw, "input manifest")
    document = _require_exact_keys(parsed, _ROOT_KEYS, "input manifest")
    try:
        canonical = canonical_json_bytes(document)
    except Exception as exc:
        raise BacktestInputError("input manifest is not canonical JSON") from exc
    if raw != canonical:
        raise BacktestInputError("input manifest must be canonical JSON")
    return document


def _parse_provenance(root: Path, value: object) -> None:
    document = _require_exact_keys(
        value, frozenset(("acquisition_manifest", "calendar_manifest")), "provenance"
    )
    expected_names = {
        "acquisition_manifest": "input_manifest.json",
        "calendar_manifest": "calendar_manifest.json",
    }
    for key, expected_name in expected_names.items():
        entry = _require_exact_keys(
            document[key], frozenset(("name", "bytes", "sha256")), key
        )
        name = _require_str("%s.name" % key, entry["name"], expected=expected_name)
        length = _require_nonnegative_int("%s.bytes" % key, entry["bytes"])
        digest = _require_str("%s.sha256" % key, entry["sha256"])
        if not _SHA256_RE.fullmatch(digest):
            raise BacktestInputError("%s.sha256 is invalid" % key)
        raw = _read_regular_file(root, name, "provenance")
        if len(raw) != length or hashlib.sha256(raw).hexdigest() != digest:
            raise BacktestInputError("provenance mismatch: %s" % name)


def _parse_file_attestations(
    value: object, *, symbol: str, benchmark_symbol: str
) -> Tuple[FileAttestation, ...]:
    if type(value) is not list or len(value) != len(InputRole):
        raise BacktestInputError("files must contain each of the six exact roles")
    result = []
    seen = set()
    expected = {
        InputRole.SYMBOL_INTRADAY_QFQ: (
            "symbol_15m_qfq.json", KLINE_FORMAT, symbol, "15m", "QFQ", "RTH", "history"
        ),
        InputRole.SYMBOL_INTRADAY_RAW: (
            "symbol_15m_raw.json", KLINE_FORMAT, symbol, "15m", "RAW", "RTH", "history"
        ),
        InputRole.SYMBOL_DAILY_QFQ: (
            "symbol_daily_qfq.json", KLINE_FORMAT, symbol, "1d", "QFQ", "RTH", "history"
        ),
        InputRole.SYMBOL_DAILY_RAW: (
            "symbol_daily_raw.json", KLINE_FORMAT, symbol, "1d", "RAW", "RTH", "history"
        ),
        InputRole.BENCHMARK_DAILY_QFQ: (
            "spy_daily_qfq.json", KLINE_FORMAT, benchmark_symbol, "1d", "QFQ", "RTH", "history"
        ),
        InputRole.US_TRADING_CALENDAR: (
            "us_trading_calendar.json", CALENDAR_FORMAT, "US", "calendar", "NONE", "RTH",
            "history",
        ),
    }
    entry_keys = frozenset(
        (
            "role", "name", "format", "code", "ktype", "adjustment", "session",
            "source", "bytes", "sha256", "rows",
        )
    )
    for index, raw_entry in enumerate(value):
        entry = _require_exact_keys(raw_entry, entry_keys, "files[%d]" % index)
        try:
            role = InputRole(_require_str("role", entry["role"]))
        except ValueError as exc:
            raise BacktestInputError("unknown input role") from exc
        if role in seen:
            raise BacktestInputError("duplicate input role: %s" % role.value)
        seen.add(role)
        actual_metadata = tuple(
            _require_str(key, entry[key])
            for key in ("name", "format", "code", "ktype", "adjustment", "session", "source")
        )
        if actual_metadata != expected[role]:
            raise BacktestInputError("unexpected provenance metadata for %s" % role.value)
        length = _require_nonnegative_int("bytes", entry["bytes"])
        rows = _require_nonnegative_int("rows", entry["rows"])
        digest = _require_str("sha256", entry["sha256"])
        if not _SHA256_RE.fullmatch(digest):
            raise BacktestInputError("sha256 must be lowercase hexadecimal")
        result.append(
            FileAttestation(
                role=role,
                name=actual_metadata[0],
                format=actual_metadata[1],
                code=actual_metadata[2],
                ktype=actual_metadata[3],
                adjustment=actual_metadata[4],
                session=actual_metadata[5],
                source=actual_metadata[6],
                byte_length=length,
                sha256=digest,
                rows=rows,
            )
        )
    if seen != set(InputRole):
        raise BacktestInputError("one or more input roles are missing")
    return tuple(sorted(result, key=lambda item: item.role.value))


def _parse_bar_time(text: object, *, ktype: str) -> datetime:
    value = _require_str("bar.time", text)
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise BacktestInputError("bar.time must use YYYY-MM-DD HH:MM:SS") from exc
    if ktype == "1d" and parsed.time() != time(0, 0):
        raise BacktestInputError("daily timestamps must be Eastern midnight dates")
    return parsed.replace(tzinfo=NEW_YORK)


def _parse_iso_date(name: str, value: object) -> date:
    text = _require_str(name, value)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise BacktestInputError("%s must use YYYY-MM-DD" % name) from exc
    if text != parsed.isoformat():
        raise BacktestInputError("%s must be a canonical ISO date" % name)
    return parsed


def _parse_acquired_at(value: object) -> datetime:
    text = _require_str("acquired_at_utc", value)
    if not _UTC_TEXT_RE.fullmatch(text):
        raise BacktestInputError("acquired_at_utc must be an aware UTC Z timestamp")
    try:
        return datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise BacktestInputError("acquired_at_utc is invalid") from exc


def _parse_kline(raw: bytes, attestation: FileAttestation) -> Tuple[HistoricalBar, ...]:
    root = _require_exact_keys(
        _decode_json(raw, attestation.name), _KLINE_ROOT_KEYS, attestation.name
    )
    _require_str("code", root["code"], expected=attestation.code)
    _require_str("ktype", root["ktype"], expected=attestation.ktype)
    _require_str("source", root["source"], expected="history")
    rows = root["data"]
    if type(rows) is not list or len(rows) != attestation.rows or not rows:
        raise BacktestInputError("row count mismatch or empty data: %s" % attestation.name)
    bars = []
    previous = None  # type: Optional[datetime]
    for index, raw_row in enumerate(rows):
        row = _require_exact_keys(
            raw_row, _KLINE_BAR_KEYS, "%s.data[%d]" % (attestation.name, index)
        )
        when = _parse_bar_time(row["time"], ktype=attestation.ktype)
        if previous is not None and when <= previous:
            raise BacktestInputError("bar timestamps must be unique and strictly ordered")
        previous = when
        opened = _require_number("open", row["open"], positive=True)
        high = _require_number("high", row["high"], positive=True)
        low = _require_number("low", row["low"], positive=True)
        close = _require_number("close", row["close"], positive=True)
        if high < max(opened, close) or low > min(opened, close) or high < low:
            raise BacktestInputError("OHLC range is invalid")
        volume = _require_nonnegative_int("volume", row["volume"])
        turnover = _require_number("turnover", row["turnover"], positive=False)
        bars.append(HistoricalBar(when, opened, high, low, close, volume, turnover))
    return tuple(bars)


def _parse_calendar(raw: bytes, attestation: FileAttestation) -> Tuple[HistoricalSession, ...]:
    root = _require_exact_keys(
        _decode_json(raw, attestation.name), _CALENDAR_ROOT_KEYS, attestation.name
    )
    _require_str("calendar.market", root["market"], expected="US")
    rows = root["data"]
    if type(rows) is not list or len(rows) != attestation.rows or not rows:
        raise BacktestInputError("calendar row count mismatch or empty calendar")
    sessions = []
    previous = None  # type: Optional[date]
    for index, encoded in enumerate(rows):
        if type(encoded) is not str:
            raise BacktestInputError("calendar rows must be encoded exact strings")
        try:
            decoded = ast.literal_eval(encoded)
        except (ValueError, SyntaxError) as exc:
            raise BacktestInputError("calendar row is not a safe Python literal") from exc
        row = _require_exact_keys(
            decoded, _CALENDAR_ROW_KEYS, "calendar.data[%d]" % index
        )
        value = _require_str("calendar.time", row["time"])
        try:
            session_date = date.fromisoformat(value)
        except ValueError as exc:
            raise BacktestInputError("calendar date must use YYYY-MM-DD") from exc
        if value != session_date.isoformat():
            raise BacktestInputError("calendar date must be canonical ISO format")
        if previous is not None and session_date <= previous:
            raise BacktestInputError("calendar dates must be unique and strictly ordered")
        previous = session_date
        kind = _require_str("trade_date_type", row["trade_date_type"])
        if kind not in ("WHOLE", "MORNING"):
            raise BacktestInputError("unknown trade_date_type")
        opened = datetime.combine(session_date, time(9, 30), tzinfo=NEW_YORK)
        closed = datetime.combine(
            session_date, time(16, 0) if kind == "WHOLE" else time(13, 0),
            tzinfo=NEW_YORK,
        )
        sessions.append(HistoricalSession(session_date, opened, closed, kind))
    return tuple(sessions)


def _validate_alignment(
    *,
    intraday_qfq: Tuple[HistoricalBar, ...],
    intraday_raw: Tuple[HistoricalBar, ...],
    daily_series: Iterable[Tuple[HistoricalBar, ...]],
    sessions: Tuple[HistoricalSession, ...],
) -> None:
    qfq_times = tuple(bar.time for bar in intraday_qfq)
    raw_times = tuple(bar.time for bar in intraday_raw)
    if qfq_times != raw_times:
        raise BacktestInputError("QFQ and RAW intraday timestamps are not one-to-one")
    session_scale = {}  # type: Dict[date, float]
    for index, (qfq_bar, raw_bar) in enumerate(zip(intraday_qfq, intraday_raw)):
        if qfq_bar.volume != raw_bar.volume:
            raise BacktestInputError(
                "QFQ and RAW intraday volume differs at index %d" % index
            )
        ratios = tuple(
            getattr(raw_bar, field) / getattr(qfq_bar, field)
            for field in ("open", "high", "low", "close")
        )
        reference = ratios[0]
        if any(
            not math.isclose(value, reference, rel_tol=1e-8, abs_tol=1e-10)
            for value in ratios[1:]
        ):
            raise BacktestInputError(
                "QFQ and RAW OHLC do not share one adjustment scale at index %d"
                % index
            )
        prior_scale = session_scale.setdefault(qfq_bar.session_date, reference)
        if not math.isclose(reference, prior_scale, rel_tol=1e-8, abs_tol=1e-10):
            raise BacktestInputError("QFQ adjustment scale changes within a session")
    session_by_date = {item.session_date: item for item in sessions}
    for bar in intraday_qfq:
        session = session_by_date.get(bar.session_date)
        if session is None:
            raise BacktestInputError("intraday bar date is absent from the calendar")
        if not session.open_at < bar.time <= session.close_at:
            raise BacktestInputError("intraday bar end lies outside RTH")
        if bar.time.second or bar.time.microsecond or bar.time.minute % 15:
            raise BacktestInputError("intraday bar end is not a 15-minute boundary")

    first_date = intraday_qfq[0].session_date
    last_date = intraday_qfq[-1].session_date
    relevant_sessions = tuple(
        item for item in sessions if first_date <= item.session_date <= last_date
    )
    expected_times = tuple(
        item.open_at + timedelta(minutes=15 * index)
        for item in relevant_sessions
        for index in range(1, int((item.close_at - item.open_at) / timedelta(minutes=15)) + 1)
    )
    if qfq_times != expected_times:
        raise BacktestInputError("15-minute RTH history is incomplete or calendar-misaligned")

    calendar_dates = tuple(item.session_date for item in sessions)
    for series in daily_series:
        dates = tuple(bar.session_date for bar in series)
        if dates != calendar_dates:
            raise BacktestInputError("daily history must align one-to-one with the calendar")


def load_backtest_input(
    manifest_path: Path, *, expected_manifest_sha256: Optional[str] = None
) -> BacktestInputBundle:
    """Load a strict, immutable moomoo quote-history bundle.

    The caller may additionally pin ``expected_manifest_sha256`` from an
    independently recorded value.  In all cases every data and provenance
    source is pinned by byte length and SHA-256 inside the canonical manifest.
    """

    requested = Path(manifest_path).absolute()
    root = _safe_directory(requested.parent, "manifest parent", private=True)
    raw_manifest = _read_regular_file(root, requested.name, "input manifest")
    manifest_sha256 = hashlib.sha256(raw_manifest).hexdigest()
    if expected_manifest_sha256 is not None:
        if not _SHA256_RE.fullmatch(expected_manifest_sha256):
            raise BacktestInputError("expected manifest SHA-256 is invalid")
        if manifest_sha256 != expected_manifest_sha256:
            raise BacktestInputError("input manifest SHA-256 mismatch")
    document = _parse_manifest_bytes(raw_manifest)
    _require_str("schema_version", document["schema_version"], expected=INPUT_SCHEMA)
    strategy_version = _require_str(
        "strategy_version", document["strategy_version"], expected="RSI_AUTOPILOT_V1"
    )
    classification = _require_str(
        "classification", document["classification"], expected="EXPLORATORY_ONLY"
    )
    provider = _require_str("provider", document["provider"], expected="MOOMOO_OPEND")
    runtime_versions = {}  # type: Dict[str, str]
    for version_name in ("moomoo_sdk", "opend_gui"):
        version = _require_str(version_name, document[version_name])
        if not _VERSION_RE.fullmatch(version):
            raise BacktestInputError("%s is not a dotted numeric version" % version_name)
        runtime_versions[version_name] = version
    acquired_at = _parse_acquired_at(document["acquired_at_utc"])
    symbol = _require_str("symbol", document["symbol"])
    benchmark = _require_str("benchmark_symbol", document["benchmark_symbol"])
    if not _SYMBOL_RE.fullmatch(symbol) or not _SYMBOL_RE.fullmatch(benchmark):
        raise BacktestInputError("symbols must use canonical US.<TICKER> form")
    if symbol == benchmark:
        raise BacktestInputError("symbol and benchmark_symbol must differ")
    period = _require_exact_keys(
        document["input_period"],
        frozenset(("start", "end", "daily_start", "daily_end")),
        "input_period",
    )
    input_start = _parse_iso_date("input_period.start", period["start"])
    input_end = _parse_iso_date("input_period.end", period["end"])
    daily_start = _parse_iso_date("input_period.daily_start", period["daily_start"])
    daily_end = _parse_iso_date("input_period.daily_end", period["daily_end"])
    if input_end < input_start or daily_end < daily_start:
        raise BacktestInputError("input period is inverted")
    if daily_start > input_start or daily_end != input_end:
        raise BacktestInputError("daily period must provide warm-up through the input end")
    _require_bool("quote_only", document["quote_only"], expected=True)
    _require_bool("orders_queried", document["orders_queried"], expected=False)
    _require_bool("accounts_queried", document["accounts_queried"], expected=False)
    semantics = _require_exact_keys(
        document["timestamp_semantics"], frozenset(_TIMESTAMP_SEMANTICS),
        "timestamp_semantics",
    )
    if dict(semantics) != _TIMESTAMP_SEMANTICS:
        raise BacktestInputError("timestamp semantics are not the frozen contract")
    _parse_provenance(root, document["provenance"])
    attestations = _parse_file_attestations(
        document["files"], symbol=symbol, benchmark_symbol=benchmark
    )
    attested = {item.role: item for item in attestations}
    raw_by_role = {
        role: _read_attested(root, attestation)
        for role, attestation in attested.items()
    }
    intraday_qfq = _parse_kline(
        raw_by_role[InputRole.SYMBOL_INTRADAY_QFQ],
        attested[InputRole.SYMBOL_INTRADAY_QFQ],
    )
    intraday_raw = _parse_kline(
        raw_by_role[InputRole.SYMBOL_INTRADAY_RAW],
        attested[InputRole.SYMBOL_INTRADAY_RAW],
    )
    symbol_daily_qfq = _parse_kline(
        raw_by_role[InputRole.SYMBOL_DAILY_QFQ],
        attested[InputRole.SYMBOL_DAILY_QFQ],
    )
    symbol_daily_raw = _parse_kline(
        raw_by_role[InputRole.SYMBOL_DAILY_RAW],
        attested[InputRole.SYMBOL_DAILY_RAW],
    )
    benchmark_daily_qfq = _parse_kline(
        raw_by_role[InputRole.BENCHMARK_DAILY_QFQ],
        attested[InputRole.BENCHMARK_DAILY_QFQ],
    )
    sessions = _parse_calendar(
        raw_by_role[InputRole.US_TRADING_CALENDAR],
        attested[InputRole.US_TRADING_CALENDAR],
    )
    _validate_alignment(
        intraday_qfq=intraday_qfq,
        intraday_raw=intraday_raw,
        daily_series=(symbol_daily_qfq, symbol_daily_raw, benchmark_daily_qfq),
        sessions=sessions,
    )
    if (
        intraday_qfq[0].session_date != input_start
        or intraday_qfq[-1].session_date != input_end
        or symbol_daily_qfq[0].session_date != daily_start
        or symbol_daily_qfq[-1].session_date != daily_end
        or sessions[0].session_date != daily_start
        or sessions[-1].session_date != daily_end
    ):
        raise BacktestInputError("manifest input period does not match attested data")
    return BacktestInputBundle(
        strategy_version=strategy_version,
        classification=classification,
        provider=provider,
        moomoo_sdk=runtime_versions["moomoo_sdk"],
        opend_gui=runtime_versions["opend_gui"],
        symbol=symbol,
        benchmark_symbol=benchmark,
        acquired_at_utc=acquired_at,
        input_start=input_start,
        input_end=input_end,
        daily_start=daily_start,
        daily_end=daily_end,
        intraday_qfq=intraday_qfq,
        intraday_raw=intraday_raw,
        symbol_daily_qfq=symbol_daily_qfq,
        symbol_daily_raw=symbol_daily_raw,
        benchmark_daily_qfq=benchmark_daily_qfq,
        sessions=sessions,
        attestations=attestations,
        manifest_sha256=manifest_sha256,
        manifest_byte_length=len(raw_manifest),
    )


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def write_backtest_report(
    path: Path,
    result: Mapping[str, Any],
    *,
    input_manifest_sha256: str,
    assumptions: Sequence[str],
    classification: str = STRUCTURAL_CLASSIFICATION,
    model_id: str = REPORT_MODEL,
) -> str:
    """Publish one canonical headless report without replacing an artifact.

    Publication uses a fully-written and fsynced temporary inode followed by
    an atomic hard-link creation at the final name.  An existing result always
    fails closed.  Mutable/performance artifacts are rejected inside the
    repository tree.
    """

    if not isinstance(result, Mapping):
        raise TypeError("result must be a mapping")
    if not _SHA256_RE.fullmatch(input_manifest_sha256):
        raise ValueError("input_manifest_sha256 must be lowercase SHA-256")
    if classification != STRUCTURAL_CLASSIFICATION:
        raise ValueError("classification must remain EXPLORATORY_ONLY")
    if model_id != REPORT_MODEL:
        raise ValueError("model_id must remain HISTORICAL_CANDLE_PROXY_V1")
    if isinstance(assumptions, (str, bytes)) or not assumptions:
        raise ValueError("assumptions must be a nonempty sequence")
    normalized_assumptions = []
    for item in assumptions:
        if type(item) is not str or not item.strip() or item != item.strip():
            raise ValueError("each assumption must be a nonempty normalized string")
        normalized_assumptions.append(item)
    if len(set(normalized_assumptions)) != len(normalized_assumptions):
        raise ValueError("assumptions must not contain duplicates")
    normalized_result = dict(result)
    if "model_id" in normalized_result and normalized_result["model_id"] != model_id:
        raise ValueError("result model_id conflicts with the report envelope")
    if (
        "status" in normalized_result
        and normalized_result["status"] != classification
    ):
        raise ValueError("result status conflicts with the report classification")
    if "assumptions" in normalized_result and normalized_result["assumptions"] != list(
        normalized_assumptions
    ):
        raise ValueError("result assumptions conflict with the report envelope")

    raw_path = Path(path)
    if not raw_path.is_absolute():
        raise BacktestOutputError("report path must be absolute")
    requested = raw_path.absolute()
    if not _SAFE_NAME_RE.fullmatch(requested.name):
        raise BacktestOutputError("report file name is unsafe")
    repository = _repository_root()
    candidate_parent = requested.parent.absolute()
    if _is_within(candidate_parent, repository):
        raise BacktestOutputError("backtest reports must remain outside the repository")
    try:
        parent = _safe_directory(candidate_parent, "report", private=True)
    except BacktestInputError as exc:
        raise BacktestOutputError("cannot resolve report directory") from exc
    target = parent / requested.name
    if _is_within(target, repository):
        raise BacktestOutputError("backtest reports must remain outside the repository")
    try:
        target.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise BacktestOutputError("cannot inspect report target") from exc
    else:
        raise BacktestOutputError("report already exists; refusing to replace it")

    document = {
        "assumptions": normalized_assumptions,
        "classification": classification,
        "input_manifest_sha256": input_manifest_sha256,
        "model_id": model_id,
        "result": normalized_result,
        "schema_version": REPORT_SCHEMA,
    }
    try:
        payload = canonical_json_bytes(document)
    except Exception as exc:
        raise BacktestOutputError("report is not canonical-JSON serializable") from exc
    digest = hashlib.sha256(payload).hexdigest()
    fd = None  # type: Optional[int]
    temporary = None  # type: Optional[Path]
    try:
        fd, raw_name = tempfile.mkstemp(prefix=".%s." % target.name, suffix=".tmp", dir=str(parent))
        temporary = Path(raw_name)
        os.fchmod(fd, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise BacktestOutputError("short write while publishing report")
            offset += written
        os.fsync(fd)
        os.close(fd)
        fd = None
        try:
            os.link(str(temporary), str(target), follow_symlinks=False)
        except FileExistsError as exc:
            raise BacktestOutputError("report already exists; refusing to replace it") from exc
        temporary.unlink()
        temporary = None
        directory_fd = os.open(str(parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd is not None:
            os.close(fd)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return digest


__all__ = [
    "BacktestInputBundle",
    "BacktestInputError",
    "BacktestOutputError",
    "FileAttestation",
    "HistoricalBar",
    "HistoricalSession",
    "InputRole",
    "INPUT_SCHEMA",
    "REPORT_SCHEMA",
    "REPORT_MODEL",
    "STRUCTURAL_CLASSIFICATION",
    "load_backtest_input",
    "write_backtest_report",
]
