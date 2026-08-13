"""Crash-conscious local persistence primitives.

Runtime files must live outside the source tree.  The helpers in this module
write canonical JSON, fsync before publishing, and fail closed when ownership
or a hash chain cannot be proven.  They deliberately do not contain broker or
market-data code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
from typing import Any, Dict, Iterable, List, Mapping, Optional


class StorageError(RuntimeError):
    """Base class for durable-storage failures."""


class LockAlreadyHeld(StorageError):
    """Raised when another writer already owns an O_EXCL reservation."""


class IntegrityError(StorageError):
    """Raised when persisted bytes do not match the expected structure/hash."""


class SensitiveDataError(StorageError):
    """Raised before a raw account identifier can be persisted."""


_INTENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_RAW_ACCOUNT_KEYS = {
    "accountid",
    "accid",
    "paperaccountid",
    "rawaccountid",
}


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _reject_raw_account_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in _RAW_ACCOUNT_KEYS:
                raise SensitiveDataError(
                    "raw account identifiers must not be written; persist only an "
                    "account fingerprint"
                )
            _reject_raw_account_keys(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_raw_account_keys(nested)


def canonical_json_bytes(value: Any, *, check_sensitive: bool = True) -> bytes:
    """Return compact, sorted, UTF-8 JSON terminated by exactly one newline."""

    if check_sensitive:
        _reject_raw_account_keys(value)
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise StorageError("value is not canonical-JSON serializable") from exc
    return (text + "\n").encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def account_fingerprint(raw_account_id: str, secret_key: bytes) -> str:
    """Create a keyed, non-reversible label for low-entropy account IDs.

    ``secret_key`` must be generated locally, kept outside the repository, and
    protected as a mode-0600 secret.  A plain hash is intentionally forbidden
    because numeric broker account identifiers are cheap to brute force.
    """

    value = str(raw_account_id).strip()
    if not value:
        raise ValueError("paper account environment variable is empty")
    if not isinstance(secret_key, bytes) or len(secret_key) < 32:
        raise ValueError("fingerprint secret_key must contain at least 32 bytes")
    return hmac.new(
        secret_key,
        ("zidoutrade/account/v1\0" + value).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _safe_canonical_target(
    path: Path, *, permit_missing_target: bool = True
) -> Path:
    """Resolve the selected runtime directory, then reject target substitution.

    Resolving the parent once deliberately permits platform aliases such as
    macOS ``/var`` -> ``/private/var``.  Callers should pass a dedicated,
    mode-restricted runtime directory; children are always addressed beneath
    its canonical path.  Portable Python still cannot defend against every
    same-UID rename race, so the OS account boundary remains part of the model.
    """

    requested = Path(path).absolute()
    if requested.parent.is_symlink():
        raise IntegrityError("runtime file parent must not be a symlink")
    try:
        candidate = requested.parent.resolve(strict=True) / requested.name
    except OSError as exc:
        raise StorageError("cannot resolve runtime directory") from exc
    try:
        target_stat = candidate.lstat()
    except FileNotFoundError:
        if permit_missing_target:
            return candidate
        raise IntegrityError("runtime path does not exist")
    except OSError as exc:
        raise StorageError("cannot validate runtime path") from exc
    if candidate.is_symlink():
        raise IntegrityError("runtime file must not be a symlink")
    if not candidate.is_file():
        raise IntegrityError("runtime target is not a regular file")
    if target_stat.st_nlink != 1:
        raise IntegrityError("runtime file must not have multiple hard links")
    return candidate


def _write_all(fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(fd, payload[offset:])
        if written <= 0:
            raise StorageError("short write while persisting state")
        offset += written


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(str(directory), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    """Atomically replace *path* after the file and containing dir are fsynced."""

    requested = Path(path)
    requested.parent.mkdir(parents=True, exist_ok=True)
    target = _safe_canonical_target(requested)
    payload = canonical_json_bytes(value)
    fd: Optional[int] = None
    temporary: Optional[Path] = None
    try:
        fd, raw_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
        )
        temporary = Path(raw_name)
        os.fchmod(fd, mode)
        _write_all(fd, payload)
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(str(temporary), str(target))
        temporary = None
        _fsync_directory(target.parent)
    finally:
        if fd is not None:
            os.close(fd)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def read_json(path: Path) -> Any:
    path = _safe_canonical_target(Path(path), permit_missing_target=False)
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise StorageError(f"cannot read {Path(path).name}") from exc
    if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
        raise IntegrityError("canonical JSON file must end with one newline")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityError("invalid UTF-8 JSON") from exc
    if canonical_json_bytes(parsed) != raw:
        raise IntegrityError("JSON is not in canonical form")
    return parsed


class ExclusiveFileLock:
    """A non-stealing, process-level one-writer lock based on ``O_EXCL``."""

    def __init__(self, path: Path, *, purpose: str = "runtime-writer") -> None:
        requested = Path(path)
        requested.parent.mkdir(parents=True, exist_ok=True)
        self.path = _safe_canonical_target(requested)
        self.purpose = purpose
        self.token: Optional[str] = None

    def acquire(self) -> "ExclusiveFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path = _safe_canonical_target(self.path)
        token = secrets.token_hex(32)
        record = {
            "created_at": utc_now_text(),
            "purpose": self.purpose,
            "schema": 1,
            "token": token,
        }
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(str(self.path), flags, 0o600)
        except FileExistsError as exc:
            raise LockAlreadyHeld(f"writer reservation exists: {self.path.name}") from exc
        try:
            _write_all(fd, canonical_json_bytes(record))
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            raise
        else:
            os.close(fd)
        _fsync_directory(self.path.parent)
        self.token = token
        return self

    def release(self) -> None:
        if self.token is None:
            return
        record = read_json(self.path)
        if not isinstance(record, dict) or not secrets.compare_digest(
            str(record.get("token", "")), self.token
        ):
            raise IntegrityError("writer lock ownership changed; refusing to remove it")
        self.path.unlink()
        _fsync_directory(self.path.parent)
        self.token = None

    def __enter__(self) -> "ExclusiveFileLock":
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()


@dataclass(frozen=True)
class IntentReservation:
    intent_id: str
    path: Path
    document: Mapping[str, Any]


class IntentStore:
    """Immutable intent reservations created before any SDK import/dispatch."""

    def __init__(self, directory: Path) -> None:
        requested = Path(directory)
        requested.mkdir(parents=True, exist_ok=True)
        if requested.is_symlink():
            raise IntegrityError("intent runtime root must not be a symlink")
        self.directory = requested.resolve(strict=True)
        if not self.directory.is_dir() or self.directory.is_symlink():
            raise IntegrityError("intent runtime root must be a real directory")

    def _path(self, intent_id: str) -> Path:
        if not _INTENT_ID.fullmatch(intent_id):
            raise ValueError("invalid intent id")
        return self.directory / f"{intent_id}.json"

    def reserve(self, intent_id: str, document: Mapping[str, Any]) -> IntentReservation:
        """Persist exactly once using O_EXCL; an existing intent is never replaced."""

        target = self._path(intent_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target = _safe_canonical_target(target)
        envelope: Dict[str, Any] = {
            "document": dict(document),
            "intent_id": intent_id,
            "schema": 1,
        }
        payload = canonical_json_bytes(envelope)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(str(target), flags, 0o600)
        except FileExistsError as exc:
            raise LockAlreadyHeld(f"intent reservation exists: {intent_id}") from exc
        try:
            _write_all(fd, payload)
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            # An incomplete reservation is evidence requiring operator recovery;
            # do not silently unlink it and make a second dispatch possible.
            raise
        else:
            os.close(fd)
        _fsync_directory(target.parent)
        return IntentReservation(intent_id, target, envelope["document"])

    def get(self, intent_id: str) -> Optional[IntentReservation]:
        path = self._path(intent_id)
        if not path.exists():
            return None
        value = read_json(path)
        if not isinstance(value, dict) or value.get("schema") != 1:
            raise IntegrityError("invalid intent envelope")
        if value.get("intent_id") != intent_id or not isinstance(
            value.get("document"), dict
        ):
            raise IntegrityError("intent identity mismatch")
        return IntentReservation(intent_id, path, value["document"])

    def all(self) -> List[IntentReservation]:
        if not self.directory.exists():
            return []
        reservations: List[IntentReservation] = []
        for path in sorted(self.directory.glob("*.json")):
            found = self.get(path.stem)
            if found is not None:
                reservations.append(found)
        return reservations


class HashChainJournal:
    """Append-only newline-delimited JSON with a SHA-256 predecessor chain."""

    GENESIS = "0" * 64

    def __init__(self, path: Path) -> None:
        requested = Path(path)
        requested.parent.mkdir(parents=True, exist_ok=True)
        self.path = _safe_canonical_target(requested)
        self._append_lock = ExclusiveFileLock(
            self.path.with_name(self.path.name + ".append.lock"),
            purpose="journal-append",
        )

    @staticmethod
    def _hash_body(body: Mapping[str, Any]) -> str:
        return hashlib.sha256(canonical_json_bytes(body)).hexdigest()

    def read_all(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        self.path = _safe_canonical_target(self.path, permit_missing_target=False)
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            raise StorageError("cannot read journal") from exc
        if raw and not raw.endswith(b"\n"):
            raise IntegrityError("journal has a partial trailing record")
        records: List[Dict[str, Any]] = []
        predecessor = self.GENESIS
        for expected_sequence, line in enumerate(raw.splitlines(), start=1):
            try:
                record = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise IntegrityError("invalid journal record") from exc
            if not isinstance(record, dict):
                raise IntegrityError("journal record is not an object")
            record_hash = record.get("hash")
            body = {key: value for key, value in record.items() if key != "hash"}
            if canonical_json_bytes(record)[:-1] != line:
                raise IntegrityError("journal record is not canonical")
            if body.get("sequence") != expected_sequence:
                raise IntegrityError("journal sequence discontinuity")
            if body.get("previous_hash") != predecessor:
                raise IntegrityError("journal predecessor mismatch")
            if not isinstance(record_hash, str) or not secrets.compare_digest(
                record_hash, self._hash_body(body)
            ):
                raise IntegrityError("journal hash mismatch")
            predecessor = record_hash
            records.append(record)
        return records

    def append(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        occurred_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not event_type or not isinstance(event_type, str):
            raise ValueError("event_type is required")
        with self._append_lock:
            records = self.read_all()
            body: Dict[str, Any] = {
                "event_type": event_type,
                "occurred_at": occurred_at or utc_now_text(),
                "payload": dict(payload),
                "previous_hash": records[-1]["hash"] if records else self.GENESIS,
                "sequence": len(records) + 1,
            }
            record = dict(body)
            record["hash"] = self._hash_body(body)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path = _safe_canonical_target(self.path)
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(str(self.path), flags, 0o600)
            try:
                _write_all(fd, canonical_json_bytes(record))
                os.fsync(fd)
            finally:
                os.close(fd)
            _fsync_directory(self.path.parent)
            return record


__all__ = [
    "ExclusiveFileLock",
    "HashChainJournal",
    "IntegrityError",
    "IntentReservation",
    "IntentStore",
    "LockAlreadyHeld",
    "SensitiveDataError",
    "StorageError",
    "account_fingerprint",
    "atomic_write_json",
    "canonical_json_bytes",
    "canonical_sha256",
    "read_json",
]
