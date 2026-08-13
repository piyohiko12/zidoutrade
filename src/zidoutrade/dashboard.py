"""Loopback-only dashboard for candidate review and next-session selection.

The dashboard has no broker account or order API.  Its sole mutation is a
user's next-session candidate choice, delegated to a caller-supplied callback.
Security checks are intentionally performed before a request body is parsed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import re
import secrets
import socket
import threading
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from .candidates import CandidateBatch, InstrumentKind, normalize_us_symbol
from .selection import SelectionError, SelectionRecord, SelectionState, record_from_payload


MAX_JSON_BODY_BYTES = 4096
_STATIC_DIR = Path(__file__).with_name("static")

_PUBLIC_STATE_FIELDS = {
    "overview",
    "candidates",
    "selection",
    "decision",
    "risk",
    "journal",
    "system",
    "strategy_explanation",
}
_PUBLIC_STATE_REQUIRED = _PUBLIC_STATE_FIELDS - {"strategy_explanation"}
_FORBIDDEN_PUBLIC_KEY_PARTS = {
    "account",
    "accid",
    "apikey",
    "balance",
    "credential",
    "fingerprint",
    "hash",
    "intent",
    "marketdata",
    "orderid",
    "password",
    "pnl",
    "position",
    "price",
    "remark",
    "secret",
    "token",
}

_PUBLIC_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_PUBLIC_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_LOWER_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EMBEDDED_SHA256 = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])")
_LONG_DIGITS = re.compile(r"(?<![0-9])[0-9]{6,}(?![0-9])")
_UUID = re.compile(
    r"(?i)(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}(?![0-9a-f])"
)
_JWT = re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+")
_SENSITIVE_TEXT_HINT = re.compile(
    r"(?ix)"
    r"(?:\b(?:password|passwd|credentials?|authorization|bearer)\b|"
    r"\b(?:api[\s._:/#=-]*key|access[\s._:/#=-]*token|"
    r"refresh[\s._:/#=-]*token|client[\s._:/#=-]*secret|"
    r"private[\s._:/#=-]*key)\b|"
    r"\b(?:account|acct|acc|order|trade)[\s._:/#=-]*"
    r"(?:id|identifier|number|no)\b|"
    r"\b(?:active[\s._:/#=-]*intent|intent|execution|proof)[\s._:/#=-]*"
    r"(?:id|identifier|hash|sha256)\b|"
    r"\b(?:fingerprint|remark)\b|"
    r"-----BEGIN[ ]+(?:RSA[ ]+|EC[ ]+|OPENSSH[ ]+)?PRIVATE[ ]+KEY-----)"
)

_MAX_PUBLIC_DEPTH = 12
_MAX_PUBLIC_MAPPING_ITEMS = 64
_MAX_PUBLIC_SEQUENCE_ITEMS = 100
_MAX_PUBLIC_STRING_LENGTH = 2_048

_HASH_VALUE_PATHS = {
    ("record_sha256",),
    ("selection", "sha256"),
    ("selection", "record", "parent_sha256"),
}
_SELECTION_STATES = frozenset(item.value for item in SelectionState)
_INSTRUMENT_KINDS = frozenset(item.value for item in InstrumentKind)
_JOURNAL_FIELDS = {
    "action",
    "decision",
    "detail",
    "event",
    "kind",
    "mood",
    "note",
    "reason",
    "reason_codes",
    "schema_version",
    "session_date",
    "symbol",
    "tags",
    "time",
    "timestamp",
}

_SECTION_FIELDS = {
    "overview": {
        "active_symbol",
        "control",
        "exposure",
        "message",
        "mode",
        "operating_mode",
        "program_id",
        "target_session",
    },
    "decision": {"action", "reason", "reasons"},
    "risk": {"new_entries_permitted", "planned_risk", "reason"},
    "system": {
        "activation_present",
        "broker_environment",
        "mode",
        "opend_endpoint",
        "sensitive_data_exposed",
    },
}
_SAFE_OVERVIEW_MESSAGES = {"No local runtime snapshot is attached."}
_STRATEGY_EXPLANATION = {
    "indicator": "Wilder RSI(14)",
    "bar": "completed 15-minute RTH bars only",
    "entry": [
        "RSI <= 30 occurred within the previous 3 completed bars",
        "previous RSI <= 35 and current RSI > 35",
        "current close > previous completed-bar high",
        "decision window 10:00-15:15 America/New_York",
    ],
    "exit": [
        "protective stop at 1.5 ATR from entry",
        "RSI >= 60, or 8 completed bars (2 hours)",
        "forced flat 15 minutes before regular-session close",
    ],
    "important": "Candidate eligibility is not an expected-return score or ranking.",
}


def _is_hash_value_path(path: Tuple[object, ...]) -> bool:
    return tuple(item for item in path if isinstance(item, str)) in _HASH_VALUE_PATHS


def _assert_public_string(value: str, path: Tuple[object, ...]) -> None:
    if len(value) > _MAX_PUBLIC_STRING_LENGTH:
        raise ValueError("PUBLIC_STATE_STRING_TOO_LONG")
    if any(ord(character) < 32 and character not in "\t\n\r" for character in value):
        raise ValueError("PUBLIC_STATE_CONTROL_CHARACTER")

    if _is_hash_value_path(path):
        if not _LOWER_SHA256.fullmatch(value):
            raise ValueError("INVALID_PUBLIC_SELECTION_HASH")
        return

    if (
        _LONG_DIGITS.search(value)
        or _EMBEDDED_SHA256.search(value)
        or _UUID.search(value)
        or _JWT.search(value)
        or _SENSITIVE_TEXT_HINT.search(value)
    ):
        raise ValueError("SENSITIVE_PUBLIC_STATE_VALUE")


def _assert_typed_public_leaf(key: Optional[str], value: Any) -> None:
    """Apply narrow types to security-relevant, machine-readable fields."""

    if key in {"eligible", "saved", "trade_permitted"} and type(value) is not bool:
        raise ValueError("INVALID_PUBLIC_BOOLEAN")
    if key in {"priority", "revision"} and (
        type(value) is not int or value < 0
    ):
        raise ValueError("INVALID_PUBLIC_INTEGER")
    if key in {"symbol", "selected_symbol", "active_symbol"} and value is not None:
        if type(value) is not str or normalize_us_symbol(value) != value:
            raise ValueError("INVALID_PUBLIC_SYMBOL")
    if key in {"target_session", "session_date"} and value is not None:
        if type(value) is not str:
            raise ValueError("INVALID_PUBLIC_DATE")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("INVALID_PUBLIC_DATE") from exc
        if parsed.isoformat() != value:
            raise ValueError("INVALID_PUBLIC_DATE")
    if key == "record_sha256" and (
        type(value) is not str or not _LOWER_SHA256.fullmatch(value)
    ):
        raise ValueError("INVALID_PUBLIC_SELECTION_HASH")
    if key == "state" and (type(value) is not str or value not in _SELECTION_STATES):
        raise ValueError("INVALID_PUBLIC_SELECTION_STATE")
    if key == "status" and value not in {"PASS", "FAIL"}:
        raise ValueError("INVALID_PUBLIC_CANDIDATE_STATUS")
    if key == "instrument_kind" and value not in _INSTRUMENT_KINDS:
        raise ValueError("INVALID_PUBLIC_INSTRUMENT_KIND")


def _assert_candidate_schema(candidate: Any) -> None:
    if type(candidate) is not dict:
        raise ValueError("INVALID_PUBLIC_CANDIDATE")
    required = {"eligible", "priority", "reason_codes", "status", "symbol"}
    allowed = required | {"instrument_kind"}
    if not required.issubset(candidate) or not set(candidate).issubset(allowed):
        raise ValueError("INVALID_PUBLIC_CANDIDATE")
    if type(candidate["eligible"]) is not bool:
        raise ValueError("INVALID_PUBLIC_CANDIDATE")
    if candidate["status"] != ("PASS" if candidate["eligible"] else "FAIL"):
        raise ValueError("INVALID_PUBLIC_CANDIDATE")
    reasons = candidate["reason_codes"]
    if type(reasons) is not list or any(
        type(reason) is not str or not _PUBLIC_CODE.fullmatch(reason) for reason in reasons
    ):
        raise ValueError("INVALID_PUBLIC_CANDIDATE")
    if candidate["eligible"] and reasons:
        raise ValueError("INVALID_PUBLIC_CANDIDATE")


def _assert_selection_schema(selection: Any) -> None:
    if selection is None:
        return
    if type(selection) is not dict or set(selection) != {"record", "sha256"}:
        raise ValueError("INVALID_PUBLIC_SELECTION")
    if type(selection["record"]) is not dict:
        raise ValueError("INVALID_PUBLIC_SELECTION")
    if type(selection["sha256"]) is not str or not _LOWER_SHA256.fullmatch(
        selection["sha256"]
    ):
        raise ValueError("INVALID_PUBLIC_SELECTION")
    try:
        record = record_from_payload(selection["record"])
    except (KeyError, TypeError, ValueError, SelectionError) as exc:
        raise ValueError("INVALID_PUBLIC_SELECTION") from exc
    if not hmac.compare_digest(record.sha256, selection["sha256"]):
        raise ValueError("INVALID_PUBLIC_SELECTION")
    parent = record.parent_sha256
    if (record.revision == 1 and parent is not None) or (
        record.revision > 1
        and (type(parent) is not str or not _LOWER_SHA256.fullmatch(parent))
    ):
        raise ValueError("INVALID_PUBLIC_SELECTION")


def _assert_public_state_shape(value: Mapping[str, Any]) -> None:
    for field in ("overview", "decision", "risk", "system"):
        if type(value[field]) is not dict:
            raise ValueError("DASHBOARD_STATE_SCHEMA_MISMATCH")
        if not set(value[field]).issubset(_SECTION_FIELDS[field]):
            raise ValueError("DASHBOARD_STATE_SCHEMA_MISMATCH")
    if type(value["candidates"]) is not list:
        raise ValueError("DASHBOARD_STATE_SCHEMA_MISMATCH")
    if type(value["journal"]) is not list:
        raise ValueError("DASHBOARD_STATE_SCHEMA_MISMATCH")
    if "strategy_explanation" in value:
        if value["strategy_explanation"] != _STRATEGY_EXPLANATION:
            raise ValueError("DASHBOARD_STATE_SCHEMA_MISMATCH")

    overview = value["overview"]
    if "message" in overview and overview["message"] not in _SAFE_OVERVIEW_MESSAGES:
        raise ValueError("DASHBOARD_STATE_SCHEMA_MISMATCH")
    for field in ("program_id", "mode", "operating_mode", "control", "exposure"):
        if field in overview and (
            type(overview[field]) is not str or not _PUBLIC_CODE.fullmatch(overview[field])
        ):
            raise ValueError("DASHBOARD_STATE_SCHEMA_MISMATCH")

    decision = value["decision"]
    for field in ("action", "reason"):
        if field in decision and (
            type(decision[field]) is not str or not _PUBLIC_CODE.fullmatch(decision[field])
        ):
            raise ValueError("DASHBOARD_STATE_SCHEMA_MISMATCH")
    if "reasons" in decision and (
        type(decision["reasons"]) is not list
        or any(
            type(reason) is not str or not _PUBLIC_CODE.fullmatch(reason)
            for reason in decision["reasons"]
        )
    ):
        raise ValueError("DASHBOARD_STATE_SCHEMA_MISMATCH")

    risk = value["risk"]
    if "new_entries_permitted" in risk and type(risk["new_entries_permitted"]) is not bool:
        raise ValueError("DASHBOARD_STATE_SCHEMA_MISMATCH")
    if "planned_risk" in risk and risk["planned_risk"] != "0.25%":
        raise ValueError("DASHBOARD_STATE_SCHEMA_MISMATCH")
    if "reason" in risk and (
        type(risk["reason"]) is not str or not _PUBLIC_CODE.fullmatch(risk["reason"])
    ):
        raise ValueError("DASHBOARD_STATE_SCHEMA_MISMATCH")

    system = value["system"]
    for field in ("activation_present", "sensitive_data_exposed"):
        if field in system and type(system[field]) is not bool:
            raise ValueError("DASHBOARD_STATE_SCHEMA_MISMATCH")
    if "mode" in system and system["mode"] not in {"SHADOW", "SIMULATE_ONLY"}:
        raise ValueError("DASHBOARD_STATE_SCHEMA_MISMATCH")
    if "broker_environment" in system and system["broker_environment"] != "SIMULATE":
        raise ValueError("DASHBOARD_STATE_SCHEMA_MISMATCH")
    if "opend_endpoint" in system and system["opend_endpoint"] != "127.0.0.1:11111":
        raise ValueError("DASHBOARD_STATE_SCHEMA_MISMATCH")
    for candidate in value["candidates"]:
        _assert_candidate_schema(candidate)
    _assert_selection_schema(value["selection"])
    for event in value["journal"]:
        if type(event) is not dict or not set(event).issubset(_JOURNAL_FIELDS):
            raise ValueError("INVALID_PUBLIC_JOURNAL")


def _assert_public_redacted(
    value: Any,
    *,
    _path: Tuple[object, ...] = (),
    _ancestors: Optional[set] = None,
) -> None:
    """Reject sensitive keys *and values* before anything reaches the UI."""

    if len(_path) > _MAX_PUBLIC_DEPTH:
        raise ValueError("PUBLIC_STATE_TOO_DEEP")
    ancestors = set() if _ancestors is None else _ancestors
    if type(value) is dict:
        if len(value) > _MAX_PUBLIC_MAPPING_ITEMS or id(value) in ancestors:
            raise ValueError("INVALID_PUBLIC_STATE_CONTAINER")
        ancestors.add(id(value))
        try:
            for key, nested in value.items():
                if type(key) is not str or not _PUBLIC_KEY.fullmatch(key):
                    raise ValueError("INVALID_PUBLIC_STATE_KEY")
                normalized = "".join(
                    character for character in key.lower() if character.isalnum()
                )
                if any(part in normalized for part in _FORBIDDEN_PUBLIC_KEY_PARTS):
                    raise ValueError("SENSITIVE_PUBLIC_STATE_FIELD")
                _assert_typed_public_leaf(key, nested)
                _assert_public_redacted(
                    nested,
                    _path=_path + (key,),
                    _ancestors=ancestors,
                )
        finally:
            ancestors.remove(id(value))
        return
    if type(value) in (list, tuple):
        if len(value) > _MAX_PUBLIC_SEQUENCE_ITEMS or id(value) in ancestors:
            raise ValueError("INVALID_PUBLIC_STATE_CONTAINER")
        ancestors.add(id(value))
        try:
            for index, nested in enumerate(value):
                _assert_public_redacted(
                    nested,
                    _path=_path + (index,),
                    _ancestors=ancestors,
                )
        finally:
            ancestors.remove(id(value))
        return
    if type(value) is str:
        _assert_public_string(value, _path)
        return
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if len(str(abs(value))) >= 6:
            raise ValueError("SENSITIVE_PUBLIC_STATE_VALUE")
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("NON_FINITE_PUBLIC_NUMBER")
        if value.is_integer() and len(str(abs(int(value)))) >= 6:
            raise ValueError("SENSITIVE_PUBLIC_STATE_VALUE")
        return
    raise ValueError("UNSUPPORTED_PUBLIC_STATE_TYPE")


def _plain_mapping(value: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    return dict(value or {})


@dataclass(frozen=True)
class DashboardSnapshot:
    """Safe dashboard data; deliberately excludes account/broker order data."""

    candidates: CandidateBatch
    selection: Optional[SelectionRecord] = None
    overview: Mapping[str, Any] = field(default_factory=dict)
    decision: Mapping[str, Any] = field(default_factory=dict)
    risk: Mapping[str, Any] = field(default_factory=dict)
    journal: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    system: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "overview": _plain_mapping(self.overview),
            "candidates": [item.to_dict() for item in self.candidates.evaluations],
            "selection": None if self.selection is None else self.selection.envelope(),
            "decision": _plain_mapping(self.decision),
            "risk": _plain_mapping(self.risk),
            "journal": [dict(item) for item in self.journal],
            "system": _plain_mapping(self.system),
            "strategy_explanation": dict(_STRATEGY_EXPLANATION),
        }


class DashboardApplication:
    """State and the optional, narrow next-session selection callback."""

    def __init__(
        self,
        state_provider: Callable[[], Mapping[str, Any]],
        selection_callback: Optional[Callable[[Optional[str], str], Mapping[str, Any]]] = None,
        validation_callback: Optional[Callable[[str], Mapping[str, Any]]] = None,
        arming_callback: Optional[Callable[[str, str], Mapping[str, Any]]] = None,
        *,
        csrf_token: Optional[str] = None,
    ) -> None:
        self.state_provider = state_provider
        self.selection_callback = selection_callback
        self.validation_callback = validation_callback
        self.arming_callback = arming_callback
        self.csrf_token = csrf_token or secrets.token_urlsafe(32)
        if len(self.csrf_token) < 32:
            raise ValueError("CSRF_TOKEN_TOO_SHORT")

    @classmethod
    def from_snapshot(
        cls,
        snapshot_provider: Callable[[], DashboardSnapshot],
        selection_callback: Optional[Callable[[Optional[str], str], Mapping[str, Any]]] = None,
        *,
        csrf_token: Optional[str] = None,
    ) -> "DashboardApplication":
        return cls(
            lambda: snapshot_provider().to_dict(),
            selection_callback,
            csrf_token=csrf_token,
        )

    def state(self) -> Dict[str, Any]:
        value = self.state_provider()
        if not isinstance(value, Mapping):
            raise TypeError("dashboard state provider must return a mapping")
        result = dict(value)
        if not _PUBLIC_STATE_REQUIRED.issubset(result) or not set(result).issubset(
            _PUBLIC_STATE_FIELDS
        ):
            raise ValueError("DASHBOARD_STATE_SCHEMA_MISMATCH")
        _assert_public_state_shape(result)
        _assert_public_redacted(result)
        return result


def _loopback_host(header_value: Optional[str]) -> Optional[Tuple[str, Optional[int]]]:
    if not header_value or any(character in header_value for character in "\r\n/@\\"):
        return None
    try:
        parsed = urlsplit("//" + header_value)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return None
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    return host, port


def _valid_origin(origin: Optional[str], host_header: Optional[str]) -> bool:
    host = _loopback_host(host_header)
    if host is None or not origin:
        return False
    try:
        parsed = urlsplit(origin)
        origin_host = (parsed.hostname or "").lower()
        origin_port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "http" or parsed.path not in ("", "/"):
        return False
    if parsed.query or parsed.fragment or parsed.username is not None or parsed.password is not None:
        return False
    return (origin_host, origin_port) == host and origin_host in {"127.0.0.1", "localhost", "::1"}


def _strict_json_object(raw: bytes) -> Dict[str, Any]:
    def pairs(items: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    def reject_constant(_: str) -> Any:
        raise ValueError("NON_FINITE_JSON_NUMBER")

    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError("JSON_OBJECT_REQUIRED")
    return value


def _eligible_symbols(state: Mapping[str, Any]) -> Tuple[str, ...]:
    raw_candidates = state.get("candidates", [])
    if not isinstance(raw_candidates, list):
        return ()
    result = []
    for candidate in raw_candidates:
        if not isinstance(candidate, Mapping) or candidate.get("eligible") is not True:
            continue
        try:
            result.append(normalize_us_symbol(candidate.get("symbol")))
        except (TypeError, ValueError):
            continue
    return tuple(result)


def _handler_class(application: DashboardApplication) -> type:
    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "ZidouTradeDashboard/1"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # Avoid leaking selections or paths through default stderr logs.
            return

        def _security_headers(self, content_type: str) -> None:
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
                "form-action 'self'; frame-ancestors 'none'",
            )

        def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self._security_headers(content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, value: Mapping[str, Any]) -> None:
            body = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8") + b"\n"
            self._send_bytes(status, body, "application/json; charset=utf-8")

        def _error(self, status: int, code: str) -> None:
            self._json(status, {"error": code})

        def _single_header(self, name: str) -> Optional[str]:
            values = self.headers.get_all(name, [])
            return values[0] if len(values) == 1 else None

        def _host_allowed(self) -> bool:
            if _loopback_host(self._single_header("Host")) is None:
                self._error(HTTPStatus.FORBIDDEN, "LOOPBACK_HOST_REQUIRED")
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_allowed():
                return
            path = self.path.split("?", 1)[0]
            if path == "/api/state":
                try:
                    state = application.state()
                except Exception:
                    self._error(HTTPStatus.SERVICE_UNAVAILABLE, "STATE_UNAVAILABLE")
                    return
                self._json(HTTPStatus.OK, state)
                return
            if path == "/api/csrf":
                self._json(HTTPStatus.OK, {"csrf_token": application.csrf_token})
                return
            static_files = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/index.html": ("index.html", "text/html; charset=utf-8"),
                "/app.css": ("app.css", "text/css; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            }
            target = static_files.get(path)
            if target is None:
                self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND")
                return
            try:
                body = (_STATIC_DIR / target[0]).read_bytes()
            except OSError:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "STATIC_ASSET_UNAVAILABLE")
                return
            self._send_bytes(HTTPStatus.OK, body, target[1])

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_POST(self) -> None:  # noqa: N802
            if not self._host_allowed():
                return
            request_path = self.path.split("?", 1)[0]
            if request_path not in {
                "/api/selection",
                "/api/selection/validate",
                "/api/selection/arm",
            }:
                self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND")
                return
            if not _valid_origin(self._single_header("Origin"), self._single_header("Host")):
                self._error(HTTPStatus.FORBIDDEN, "LOOPBACK_ORIGIN_REQUIRED")
                return
            supplied_csrf = self._single_header("X-CSRF-Token") or ""
            if not hmac.compare_digest(supplied_csrf, application.csrf_token):
                self._error(HTTPStatus.FORBIDDEN, "INVALID_CSRF_TOKEN")
                return
            if self.headers.get_all("Transfer-Encoding", []):
                self._error(HTTPStatus.BAD_REQUEST, "TRANSFER_ENCODING_FORBIDDEN")
                return
            if self._single_header("Content-Type") != "application/json":
                self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "EXACT_JSON_CONTENT_TYPE_REQUIRED")
                return
            raw_length = self._single_header("Content-Length")
            try:
                content_length = int(raw_length) if raw_length is not None else -1
            except ValueError:
                content_length = -1
            if content_length < 1 or content_length > MAX_JSON_BODY_BYTES:
                self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "INVALID_CONTENT_LENGTH")
                return
            raw = self.rfile.read(content_length)
            if len(raw) != content_length:
                self._error(HTTPStatus.BAD_REQUEST, "INCOMPLETE_JSON_BODY")
                return
            try:
                payload = _strict_json_object(raw)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                self._error(HTTPStatus.BAD_REQUEST, "INVALID_JSON")
                return
            if request_path == "/api/selection/validate":
                if set(payload) != {"expected_sha256"} or self._single_header("X-Confirm-Action") is not None:
                    self._error(HTTPStatus.BAD_REQUEST, "INVALID_VALIDATION_SCHEMA")
                    return
                if application.validation_callback is None:
                    self._error(HTTPStatus.SERVICE_UNAVAILABLE, "SELECTION_IS_READ_ONLY")
                    return
                try:
                    result = application.validation_callback(payload["expected_sha256"])
                except SelectionError:
                    self._error(HTTPStatus.CONFLICT, "SELECTION_CONFLICT")
                    return
                except Exception:
                    self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "SELECTION_UPDATE_FAILED")
                    return
                return self._send_selection_result(result)
            if request_path == "/api/selection/arm":
                if set(payload) != {"confirmation", "expected_sha256"}:
                    self._error(HTTPStatus.BAD_REQUEST, "INVALID_ARM_SCHEMA")
                    return
                if application.arming_callback is None:
                    self._error(HTTPStatus.SERVICE_UNAVAILABLE, "SELECTION_IS_READ_ONLY")
                    return
                try:
                    result = application.arming_callback(
                        payload["expected_sha256"], payload["confirmation"]
                    )
                except SelectionError:
                    self._error(HTTPStatus.CONFLICT, "SELECTION_CONFLICT")
                    return
                except Exception:
                    self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "SELECTION_UPDATE_FAILED")
                    return
                return self._send_selection_result(result)
            if set(payload) != {"selected_symbol", "target_session"}:
                self._error(HTTPStatus.BAD_REQUEST, "INVALID_SELECTION_SCHEMA")
                return
            target_session = payload["target_session"]
            if not isinstance(target_session, str):
                self._error(HTTPStatus.BAD_REQUEST, "INVALID_TARGET_SESSION")
                return
            try:
                parsed_session = date.fromisoformat(target_session)
            except ValueError:
                self._error(HTTPStatus.BAD_REQUEST, "INVALID_TARGET_SESSION")
                return
            if parsed_session.isoformat() != target_session:
                self._error(HTTPStatus.BAD_REQUEST, "INVALID_TARGET_SESSION")
                return
            selected = payload["selected_symbol"]
            if selected is not None:
                if not isinstance(selected, str):
                    self._error(HTTPStatus.BAD_REQUEST, "INVALID_SELECTED_SYMBOL")
                    return
                try:
                    selected = normalize_us_symbol(selected)
                except ValueError:
                    self._error(HTTPStatus.BAD_REQUEST, "INVALID_SELECTED_SYMBOL")
                    return
                try:
                    eligible = _eligible_symbols(application.state())
                except Exception:
                    self._error(HTTPStatus.SERVICE_UNAVAILABLE, "STATE_UNAVAILABLE")
                    return
                if selected not in eligible:
                    self._error(HTTPStatus.UNPROCESSABLE_ENTITY, "SELECTION_NOT_ELIGIBLE")
                    return
            if application.selection_callback is None:
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, "SELECTION_IS_READ_ONLY")
                return
            try:
                result = application.selection_callback(selected, target_session)
                if not isinstance(result, Mapping):
                    raise TypeError("selection callback must return a mapping")
            except SelectionError:
                self._error(HTTPStatus.CONFLICT, "SELECTION_CONFLICT")
                return
            except Exception:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "SELECTION_UPDATE_FAILED")
                return
            self._send_selection_result(result)

        def _send_selection_result(self, result: Mapping[str, Any]) -> None:
            result = dict(result)
            allowed = {
                "message",
                "record_sha256",
                "saved",
                "selected_symbol",
                "state",
                "target_session",
            }
            if (
                not {"saved", "selected_symbol", "target_session"}.issubset(result)
                or not set(result).issubset(allowed)
            ):
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "INVALID_CALLBACK_RESPONSE")
                return
            if "message" in result and result["message"] != "Selection revision saved.":
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "INVALID_CALLBACK_RESPONSE")
                return
            try:
                _assert_public_redacted(result)
            except ValueError:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "INVALID_CALLBACK_RESPONSE")
                return
            self._json(HTTPStatus.OK, result)

        def do_OPTIONS(self) -> None:  # noqa: N802
            if not self._host_allowed():
                return
            self._error(HTTPStatus.METHOD_NOT_ALLOWED, "CORS_PREFLIGHT_NOT_SUPPORTED")

    return DashboardHandler


class DashboardServer:
    """A loopback-only threaded HTTP server suitable for a local UI."""

    def __init__(
        self,
        application: DashboardApplication,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
    ) -> None:
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("DASHBOARD_MUST_BIND_TO_LOOPBACK")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("INVALID_DASHBOARD_PORT")
        server_class = ThreadingHTTPServer
        if host == "::1":
            class IPv6ThreadingHTTPServer(ThreadingHTTPServer):
                address_family = socket.AF_INET6

            server_class = IPv6ThreadingHTTPServer
        self._server = server_class((host, port), _handler_class(application))
        self._server.daemon_threads = True

    @property
    def address(self) -> Tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def serve_forever(self) -> None:
        self._server.serve_forever(poll_interval=0.2)

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def start_in_thread(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, name="zidoutrade-dashboard", daemon=True)
        thread.start()
        return thread


__all__ = [
    "DashboardApplication",
    "DashboardServer",
    "DashboardSnapshot",
    "MAX_JSON_BODY_BYTES",
]
