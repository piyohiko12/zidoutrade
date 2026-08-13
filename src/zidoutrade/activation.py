"""Local, hash-bound activation verification.

This module deliberately contains no activation-artifact creator.  A public
checkout therefore has neither the local secret nor the reviewed final lock
needed to obtain an :class:`ActivationProof`.  Verification is repeated at
every dispatch boundary; a boolean, callback, or shallow "file exists" check
is never accepted as authority to trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from . import PROGRAM_ID
from .config import ConfigError, parse_system_config
from .exchange_calendar import FrozenExchangeCalendar, FrozenSession
from .selection import SelectionError, SelectionState, SelectionStore
from .storage import canonical_json_bytes, canonical_sha256, read_json


FINAL_LOCK_NAME = "final_lock.json"
ACTIVATION_MARKER_NAME = "activation_marker.json"
ACTIVATION_SECRET_NAME = "activation.secret"
ACTIVE_CONFIG_NAME = "system.active.json"
GLOBAL_STOP_NAME = "GLOBAL_STOP"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SESSION = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SYMBOL = re.compile(r"^US\.[A-Z0-9][A-Z0-9._-]{0,31}$")
_MARKER_DOMAIN = b"zidoutrade/activation-marker/v1\0"
_PROOF_DOMAIN = b"zidoutrade/dispatch-proof/v1\0"
_EXECUTION_DOMAIN = b"zidoutrade/execution-decision/v1\0"


class ActivationError(RuntimeError):
    """The local activation chain cannot be proven exactly."""


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _require_sha256(name: str, value: object) -> str:
    if not _is_sha256(value):
        raise ActivationError("%s must be a lowercase SHA-256" % name)
    return str(value)


def _require_session(value: object) -> str:
    if type(value) is not str or _SESSION.fullmatch(value) is None:
        raise ActivationError("session_id must be exact YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ActivationError("session_id is not a calendar date") from exc
    if parsed.isoformat() != value:
        raise ActivationError("session_id is not canonical")
    return value


def _require_symbol(value: object) -> str:
    if type(value) is not str or _SYMBOL.fullmatch(value) is None:
        raise ActivationError("selected_symbol is not canonical")
    return value


def _parse_utc_timestamp(name: str, value: object) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise ActivationError("%s must be a canonical UTC timestamp" % name)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ActivationError("%s is invalid" % name) from exc
    canonical = parsed.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")
    if canonical != value:
        raise ActivationError("%s is not canonical" % name)
    return parsed


def utc_timestamp(value: datetime) -> str:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ActivationError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _read_regular_file(
    path: Path,
    *,
    maximum: int,
    mode_600: bool = False,
    allow_public_read: bool = False,
) -> bytes:
    """Read one owner-only, non-linked regular file without following symlinks."""

    try:
        before = path.lstat()
    except OSError as exc:
        raise ActivationError("required local activation file is unavailable") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_uid != os.getuid()
        or before.st_size > maximum
    ):
        raise ActivationError("local activation file is unsafe")
    if mode_600 and stat.S_IMODE(before.st_mode) != 0o600:
        raise ActivationError("local activation secret must have mode 0600")
    if not mode_600 and not allow_public_read and stat.S_IMODE(before.st_mode) & 0o077:
        raise ActivationError("local activation artifact must not be group/world accessible")
    if allow_public_read and stat.S_IMODE(before.st_mode) & 0o022:
        raise ActivationError("reviewed source file must not be group/world writable")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(str(path), flags)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ActivationError("local activation file changed during open")
            chunks = []
            remaining = maximum + 1
            while remaining:
                chunk = os.read(descriptor, min(remaining, 65536))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(descriptor)
    except ActivationError:
        raise
    except OSError as exc:
        raise ActivationError("local activation file cannot be read safely") from exc
    payload = b"".join(chunks)
    if len(payload) > maximum:
        raise ActivationError("local activation file is too large")
    return payload


def hash_python_tree(root: Path) -> str:
    """Hash relative names and bytes of every Python file in a fixed tree."""

    directory = Path(root).resolve(strict=True)
    if not directory.is_dir() or directory.is_symlink():
        raise ActivationError("hash root must be a real directory")
    files = sorted(directory.rglob("*.py"), key=lambda item: item.relative_to(directory).as_posix())
    if not files:
        raise ActivationError("hash root contains no Python sources")
    digest = hashlib.sha256()
    digest.update(b"zidoutrade/python-tree/v1\0")
    for path in files:
        relative = path.relative_to(directory).as_posix().encode("utf-8")
        payload = _read_regular_file(
            path, maximum=2_000_000, allow_public_read=True
        )
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def program_binding_sha256(
    *, config_sha256: str, source_sha256: str, tests_sha256: str
) -> str:
    return canonical_sha256(
        {
            "config_sha256": _require_sha256("config_sha256", config_sha256),
            "program_id": PROGRAM_ID,
            "source_sha256": _require_sha256("source_sha256", source_sha256),
            "tests_sha256": _require_sha256("tests_sha256", tests_sha256),
        }
    )


def activation_marker_hmac(unsigned_marker: Mapping[str, Any], secret: bytes) -> str:
    """Return the domain-separated marker authenticator (verification helper)."""

    if not isinstance(secret, bytes) or len(secret) < 32:
        raise ActivationError("local activation secret must contain at least 32 bytes")
    return hmac.new(
        secret,
        _MARKER_DOMAIN + canonical_json_bytes(dict(unsigned_marker)),
        hashlib.sha256,
    ).hexdigest()


@dataclass(frozen=True)
class ActivationProof:
    """Short-lived, immutable proof returned only after full local verification."""

    program_id: str
    runtime_root: str
    session_id: str
    selected_symbol: str
    selection_sha256: str
    account_fingerprint: str
    config_sha256: str
    source_sha256: str
    tests_sha256: str
    program_sha256: str
    final_lock_sha256: str
    activation_marker_sha256: str
    rth_verifier_sha256: str
    session_open_at: str
    session_close_at: str
    verified_at: str
    valid_until: str
    proof_hmac_sha256: str

    def __post_init__(self) -> None:
        if self.program_id != PROGRAM_ID:
            raise ActivationError("activation proof program mismatch")
        _require_session(self.session_id)
        _require_symbol(self.selected_symbol)
        for name in (
            "selection_sha256",
            "account_fingerprint",
            "config_sha256",
            "source_sha256",
            "tests_sha256",
            "program_sha256",
            "final_lock_sha256",
            "activation_marker_sha256",
            "rth_verifier_sha256",
            "proof_hmac_sha256",
        ):
            _require_sha256(name, getattr(self, name))
        opened = _parse_utc_timestamp("session_open_at", self.session_open_at)
        closed = _parse_utc_timestamp("session_close_at", self.session_close_at)
        verified = _parse_utc_timestamp("verified_at", self.verified_at)
        valid_until = _parse_utc_timestamp("valid_until", self.valid_until)
        if not opened <= verified < closed or not verified < valid_until <= closed:
            raise ActivationError("activation proof is outside its RTH validity window")

    def unsigned_payload(self) -> Dict[str, Any]:
        return {
            "account_fingerprint": self.account_fingerprint,
            "activation_marker_sha256": self.activation_marker_sha256,
            "config_sha256": self.config_sha256,
            "final_lock_sha256": self.final_lock_sha256,
            "program_id": self.program_id,
            "program_sha256": self.program_sha256,
            "rth_verifier_sha256": self.rth_verifier_sha256,
            "runtime_root": self.runtime_root,
            "schema": 1,
            "selected_symbol": self.selected_symbol,
            "selection_sha256": self.selection_sha256,
            "session_close_at": self.session_close_at,
            "session_id": self.session_id,
            "session_open_at": self.session_open_at,
            "source_sha256": self.source_sha256,
            "tests_sha256": self.tests_sha256,
            "valid_until": self.valid_until,
            "verified_at": self.verified_at,
        }

    @property
    def binding_sha256(self) -> str:
        stable = dict(self.unsigned_payload())
        for key in ("session_open_at", "session_close_at", "verified_at", "valid_until"):
            stable.pop(key)
        return canonical_sha256(stable)

    @property
    def sha256(self) -> str:
        return canonical_sha256(
            {**self.unsigned_payload(), "proof_hmac_sha256": self.proof_hmac_sha256}
        )


class ActivationVerifier:
    """Verify local reviewed artifacts and issue a five-second dispatch proof."""

    def __init__(
        self,
        *,
        runtime_root: Path,
        repository_root: Path,
        calendar: FrozenExchangeCalendar,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        try:
            root = Path(runtime_root).expanduser().resolve(strict=True)
            repository = Path(repository_root).expanduser().resolve(strict=True)
        except OSError as exc:
            raise ActivationError("runtime and repository roots must already exist") from exc
        if root.is_symlink() or not root.is_dir():
            raise ActivationError("runtime root must be a real directory")
        root_stat = root.stat()
        if root_stat.st_uid != os.getuid() or stat.S_IMODE(root_stat.st_mode) & 0o077:
            raise ActivationError("runtime root must be owner-only")
        try:
            root.relative_to(repository)
        except ValueError:
            pass
        else:
            raise ActivationError("runtime root must remain outside the repository")
        if type(calendar) is not FrozenExchangeCalendar:
            raise ActivationError("a frozen, hash-pinned exchange calendar is required")
        if not callable(clock):
            raise ActivationError("clock must be callable")
        self.runtime_root = root
        self.repository_root = repository
        self.calendar = calendar
        self._clock = clock
        self._assert_loaded_program_origin()

    def _assert_loaded_program_origin(self) -> None:
        """Prove critical loaded modules originate in the reviewed source tree."""

        expected = (self.repository_root / "src" / "zidoutrade").resolve(strict=True)
        critical = (
            "zidoutrade.activation",
            "zidoutrade.broker",
            "zidoutrade.config",
            "zidoutrade.execution",
            "zidoutrade.risk",
            "zidoutrade.runner",
            "zidoutrade.selection",
            "zidoutrade.storage",
            "zidoutrade.strategy",
        )
        for name in critical:
            module = sys.modules.get(name)
            if module is None:
                continue
            raw_file = getattr(module, "__file__", None)
            if not isinstance(raw_file, str):
                raise ActivationError("critical module has no verifiable source origin")
            try:
                loaded = Path(raw_file).resolve(strict=True)
                loaded.relative_to(expected)
            except (OSError, ValueError) as exc:
                raise ActivationError(
                    "critical module is not loaded from the reviewed repository"
                ) from exc

    @property
    def final_lock_path(self) -> Path:
        return self.runtime_root / FINAL_LOCK_NAME

    @property
    def marker_path(self) -> Path:
        return self.runtime_root / ACTIVATION_MARKER_NAME

    @property
    def secret_path(self) -> Path:
        return self.runtime_root / ACTIVATION_SECRET_NAME

    @property
    def config_path(self) -> Path:
        return self.runtime_root / ACTIVE_CONFIG_NAME

    def _secret(self) -> bytes:
        value = _read_regular_file(self.secret_path, maximum=4096, mode_600=True)
        if len(value) < 32:
            raise ActivationError("local activation secret is too short")
        return value

    def _now(self) -> datetime:
        value = self._clock()
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise ActivationError("activation clock must be timezone-aware")
        return value

    def current_time(self) -> datetime:
        """Return the checked clock used by activation and decision expiry."""

        return self._now()

    @staticmethod
    def _canonical_artifact(path: Path) -> Tuple[Mapping[str, Any], bytes]:
        payload = _read_regular_file(path, maximum=1_000_000)
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ActivationError("local activation artifact is invalid JSON") from exc
        if not isinstance(decoded, dict) or canonical_json_bytes(decoded) != payload:
            raise ActivationError("local activation artifact is not canonical JSON")
        return decoded, payload

    def _active_session(self, now: datetime, session_id: str) -> FrozenSession:
        try:
            active = self.calendar.active_session(now)
        except Exception as exc:
            raise ActivationError("authoritative RTH verification failed") from exc
        if active is None or active.session_date.isoformat() != session_id:
            raise ActivationError("current time is not inside the locked US RTH session")
        return active

    def verify(
        self,
        *,
        expected_session_id: Optional[str] = None,
        expected_symbol: Optional[str] = None,
        expected_account_fingerprint: Optional[str] = None,
    ) -> ActivationProof:
        """Re-read and verify the complete chain for one dispatch boundary."""

        if os.path.lexists(self.runtime_root / GLOBAL_STOP_NAME):
            raise ActivationError("global stop marker is present")
        now = self._now()
        secret = self._secret()

        config_payload = _read_regular_file(self.config_path, maximum=1_000_000)
        try:
            config_json = json.loads(config_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ActivationError("active configuration is invalid JSON") from exc
        if not isinstance(config_json, dict) or canonical_json_bytes(config_json) != config_payload:
            raise ActivationError("active configuration must be canonical JSON")
        try:
            config = parse_system_config(config_json)
        except ConfigError as exc:
            raise ActivationError("active configuration is invalid") from exc
        if (
            config.mode != "PAPER_SIMULATE"
            or config.broker_environment != "SIMULATE"
            or config.opend_host != "127.0.0.1"
            or config.opend_port != 11111
            or config.session != "RTH"
        ):
            raise ActivationError("active configuration is not the frozen paper/RTH policy")

        try:
            selection = SelectionStore(
                self.runtime_root, repository_root=self.repository_root
            ).load_latest()
        except SelectionError as exc:
            raise ActivationError("locked selection cannot be verified") from exc
        assert selection is not None
        if (
            selection.state is not SelectionState.SESSION_LOCKED
            or selection.trade_permitted is not True
            or selection.selected_symbol is None
            or selection.locked_at is None
        ):
            raise ActivationError("selection is not a trade-permitted SESSION_LOCKED record")
        session_id = _require_session(selection.target_session)
        selected_symbol = _require_symbol(selection.selected_symbol)
        active_session = self._active_session(now, session_id)

        if expected_session_id is not None and _require_session(expected_session_id) != session_id:
            raise ActivationError("runtime session differs from activation selection")
        if expected_symbol is not None and _require_symbol(expected_symbol) != selected_symbol:
            raise ActivationError("runtime symbol differs from activation selection")
        if expected_account_fingerprint is not None:
            _require_sha256("expected_account_fingerprint", expected_account_fingerprint)

        final_lock, final_lock_bytes = self._canonical_artifact(self.final_lock_path)
        expected_lock_keys = {
            "account_fingerprint",
            "broker_environment",
            "config_sha256",
            "opend_host",
            "opend_port",
            "program_id",
            "program_sha256",
            "rth_verifier_sha256",
            "runtime_root",
            "schema",
            "selected_symbol",
            "selection_sha256",
            "session",
            "session_id",
            "source_sha256",
            "tests_sha256",
        }
        if set(final_lock) != expected_lock_keys:
            raise ActivationError("final lock schema mismatch")
        if final_lock.get("schema") != 1 or final_lock.get("program_id") != PROGRAM_ID:
            raise ActivationError("final lock identity mismatch")
        if (
            final_lock.get("broker_environment") != "SIMULATE"
            or final_lock.get("opend_host") != "127.0.0.1"
            or type(final_lock.get("opend_port")) is not int
            or final_lock.get("opend_port") != 11111
            or final_lock.get("session") != "RTH"
            or final_lock.get("runtime_root") != str(self.runtime_root)
        ):
            raise ActivationError("final lock execution boundary mismatch")

        config_sha = hashlib.sha256(config_payload).hexdigest()
        source_sha = hash_python_tree(self.repository_root / "src" / "zidoutrade")
        tests_sha = hash_python_tree(self.repository_root / "tests")
        program_sha = program_binding_sha256(
            config_sha256=config_sha, source_sha256=source_sha, tests_sha256=tests_sha
        )
        account_fingerprint = _require_sha256(
            "account_fingerprint", final_lock.get("account_fingerprint")
        )
        exact_bindings = {
            "config_sha256": config_sha,
            "source_sha256": source_sha,
            "tests_sha256": tests_sha,
            "program_sha256": program_sha,
            "selection_sha256": selection.sha256,
            "selected_symbol": selected_symbol,
            "session_id": session_id,
            "rth_verifier_sha256": self.calendar.sha256,
        }
        if any(final_lock.get(key) != value for key, value in exact_bindings.items()):
            raise ActivationError("final lock hash/selection/session binding mismatch")
        if (
            expected_account_fingerprint is not None
            and account_fingerprint != expected_account_fingerprint
        ):
            raise ActivationError("runtime account fingerprint differs from final lock")

        marker, marker_bytes = self._canonical_artifact(self.marker_path)
        expected_marker_keys = {
            "activated_at",
            "activation_nonce",
            "final_lock_sha256",
            "marker_hmac_sha256",
            "program_id",
            "schema",
        }
        if set(marker) != expected_marker_keys:
            raise ActivationError("activation marker schema mismatch")
        if marker.get("schema") != 1 or marker.get("program_id") != PROGRAM_ID:
            raise ActivationError("activation marker identity mismatch")
        _require_sha256("activation_nonce", marker.get("activation_nonce"))
        activated_at = _parse_utc_timestamp("activated_at", marker.get("activated_at"))
        if activated_at > now.astimezone(timezone.utc):
            raise ActivationError("activation marker timestamp is in the future")
        final_lock_sha = hashlib.sha256(final_lock_bytes).hexdigest()
        if marker.get("final_lock_sha256") != final_lock_sha:
            raise ActivationError("activation marker does not bind the final lock")
        marker_mac = _require_sha256("marker_hmac_sha256", marker.get("marker_hmac_sha256"))
        unsigned_marker = dict(marker)
        unsigned_marker.pop("marker_hmac_sha256")
        expected_mac = activation_marker_hmac(unsigned_marker, secret)
        if not hmac.compare_digest(marker_mac, expected_mac):
            raise ActivationError("activation marker authenticator mismatch")

        verified_at = now.astimezone(timezone.utc)
        close_at = active_session.close_at.astimezone(timezone.utc)
        valid_until = min(verified_at + timedelta(seconds=5), close_at)
        unsigned_proof = {
            "account_fingerprint": account_fingerprint,
            "activation_marker_sha256": hashlib.sha256(marker_bytes).hexdigest(),
            "config_sha256": config_sha,
            "final_lock_sha256": final_lock_sha,
            "program_id": PROGRAM_ID,
            "program_sha256": program_sha,
            "rth_verifier_sha256": self.calendar.sha256,
            "runtime_root": str(self.runtime_root),
            "schema": 1,
            "selected_symbol": selected_symbol,
            "selection_sha256": selection.sha256,
            "session_close_at": utc_timestamp(active_session.close_at),
            "session_id": session_id,
            "session_open_at": utc_timestamp(active_session.open_at),
            "source_sha256": source_sha,
            "tests_sha256": tests_sha,
            "valid_until": utc_timestamp(valid_until),
            "verified_at": utc_timestamp(verified_at),
        }
        proof_mac = hmac.new(
            secret,
            _PROOF_DOMAIN + canonical_json_bytes(unsigned_proof),
            hashlib.sha256,
        ).hexdigest()
        return ActivationProof(
            proof_hmac_sha256=proof_mac,
            **{key: value for key, value in unsigned_proof.items() if key != "schema"},
        )

    def verify_proof(self, proof: ActivationProof) -> None:
        if type(proof) is not ActivationProof:
            raise ActivationError("dispatch requires an exact ActivationProof")
        now = self._now().astimezone(timezone.utc)
        verified_at = _parse_utc_timestamp("verified_at", proof.verified_at)
        valid_until = _parse_utc_timestamp("valid_until", proof.valid_until)
        if now < verified_at or now >= valid_until:
            raise ActivationError("activation proof expired")
        expected = hmac.new(
            self._secret(),
            _PROOF_DOMAIN + canonical_json_bytes(proof.unsigned_payload()),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(proof.proof_hmac_sha256, expected):
            raise ActivationError("activation proof authenticator mismatch")
        # A proof is a short-lived value object, not a capability that bypasses
        # the filesystem.  Re-verify the full chain (especially GLOBAL_STOP)
        # and require its stable identity to remain unchanged.
        fresh = self.verify(
            expected_session_id=proof.session_id,
            expected_symbol=proof.selected_symbol,
            expected_account_fingerprint=proof.account_fingerprint,
        )
        stable_fields = (
            "runtime_root",
            "selection_sha256",
            "account_fingerprint",
            "config_sha256",
            "source_sha256",
            "tests_sha256",
            "program_sha256",
            "final_lock_sha256",
            "activation_marker_sha256",
            "rth_verifier_sha256",
            "session_id",
            "selected_symbol",
        )
        if any(getattr(fresh, name) != getattr(proof, name) for name in stable_fields):
            raise ActivationError("activation chain changed after proof issuance")

    def verify_execution_payload(
        self, proof: ActivationProof, payload: Mapping[str, Any], authenticator: str
    ) -> None:
        self.verify_proof(proof)
        _require_sha256("execution authenticator", authenticator)
        if payload.get("activation_binding_sha256") != proof.binding_sha256:
            raise ActivationError("execution decision activation binding mismatch")
        expected = hmac.new(
            self._secret(),
            _EXECUTION_DOMAIN + canonical_json_bytes(dict(payload)),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(authenticator, expected):
            raise ActivationError("execution decision authenticator mismatch")

    def _seal_execution_evidence(
        self, proof: ActivationProof, payload: Mapping[str, Any], *, builder_token: object
    ) -> str:
        """Internal capability used only by the typed execution builders."""

        from .execution import _BUILDER_TOKEN, _validate_unsigned_schema

        if builder_token is not _BUILDER_TOKEN:
            raise ActivationError("only the typed execution builder may seal evidence")
        _validate_unsigned_schema(payload, proof=proof)
        self.verify_proof(proof)
        return hmac.new(
            self._secret(),
            _EXECUTION_DOMAIN + canonical_json_bytes(dict(payload)),
            hashlib.sha256,
        ).hexdigest()

    def verify_broker_account(self, proof: ActivationProof, broker: Any) -> None:
        """Bind a dispatch to the adapter's configured account without exposing it."""

        self.verify_proof(proof)
        method = getattr(broker, "configured_account_fingerprint", None)
        if not callable(method):
            raise ActivationError("broker cannot prove its configured account binding")
        try:
            actual = method(self._secret())
        except Exception as exc:
            raise ActivationError("broker account fingerprint could not be computed") from exc
        _require_sha256("broker account fingerprint", actual)
        if not hmac.compare_digest(actual, proof.account_fingerprint):
            raise ActivationError("broker account differs from activation proof")


__all__ = [
    "ACTIVE_CONFIG_NAME",
    "ACTIVATION_MARKER_NAME",
    "ACTIVATION_SECRET_NAME",
    "FINAL_LOCK_NAME",
    "GLOBAL_STOP_NAME",
    "ActivationError",
    "ActivationProof",
    "ActivationVerifier",
    "activation_marker_hmac",
    "hash_python_tree",
    "program_binding_sha256",
    "utc_timestamp",
]
