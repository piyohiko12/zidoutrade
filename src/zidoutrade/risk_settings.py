"""Versioned, next-session-only persistence for user risk settings.

The store is deliberately external to the repository and has no implicit
default location.  A missing absolute investment cap remains a valid saved
research setting, but it blocks every new entry in the risk engine.  These
settings never authorize an order and never block management of an existing
position.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hmac
import os
from pathlib import Path
import re
import stat
from typing import Any, Dict, Mapping, Optional

from .risk import (
    DEFAULT_DAILY_LOSS_LIMIT_BASIS_POINTS,
    DEFAULT_PLANNED_RISK_BASIS_POINTS,
    DEFAULT_WEEKLY_LOSS_LIMIT_BASIS_POINTS,
    RISK_POLICY_VERSION,
    RiskPolicy,
)
from .storage import (
    ExclusiveFileLock,
    IntegrityError,
    LockAlreadyHeld,
    StorageError,
    atomic_write_json,
    canonical_json_bytes,
    canonical_sha256,
    read_json,
)


APPLICATION_SCOPE = "NEXT_SESSION_ONLY"
SAVE_CONFIRMATION = "SAVE_NEXT_SESSION_RISK"
MAX_SAFE_CENTS = 9_007_199_254_740_991
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RiskSettingsError(ValueError):
    """Invalid, unsafe, stale, or conflicting risk-settings operation."""


class RiskSettingsConflictError(RiskSettingsError):
    """The caller's expected revision does not match the durable latest one."""


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(str(path), flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise RiskSettingsError("risk settings directory sync failed") from exc


def _canonical_date(name: str, value: object) -> str:
    if type(value) is not str:
        raise RiskSettingsError("%s must be an exact YYYY-MM-DD string" % name)
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise RiskSettingsError("%s must be an exact YYYY-MM-DD string" % name) from exc
    if parsed.isoformat() != value:
        raise RiskSettingsError("%s must be canonical" % name)
    return value


def _timestamp(now: Optional[datetime] = None) -> str:
    current = now or datetime.now(timezone.utc)
    if type(current) is not datetime or current.tzinfo is None or current.utcoffset() is None:
        raise RiskSettingsError("timestamp must be timezone-aware")
    return current.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _validate_timestamp(value: object) -> str:
    if type(value) is not str or not value.endswith("Z"):
        raise RiskSettingsError("invalid UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise RiskSettingsError("invalid UTC timestamp") from exc
    if _timestamp(parsed) != value:
        raise RiskSettingsError("noncanonical UTC timestamp")
    return value


@dataclass(frozen=True)
class RiskSettingsUpdate:
    """Strict dashboard request after JSON schema and policy validation."""

    target_session: str
    policy: RiskPolicy
    expected_sha256: Optional[str]

    def __post_init__(self) -> None:
        _canonical_date("target_session", self.target_session)
        if type(self.policy) is not RiskPolicy:
            raise RiskSettingsError("policy must be an exact RiskPolicy")
        if self.expected_sha256 is not None and (
            type(self.expected_sha256) is not str
            or _SHA256.fullmatch(self.expected_sha256) is None
        ):
            raise RiskSettingsError("expected_sha256 must be null or lowercase SHA-256")


def parse_risk_settings_update(value: Mapping[str, Any]) -> RiskSettingsUpdate:
    """Parse the exact public POST contract; booleans are never integers."""

    if type(value) is not dict:
        raise RiskSettingsError("risk settings payload must be an exact object")
    expected = {
        "confirmation",
        "daily_loss_limit_basis_points",
        "expected_sha256",
        "maximum_investment_cents",
        "planned_risk_basis_points",
        "risk_policy_version",
        "target_session",
        "weekly_loss_limit_basis_points",
    }
    if set(value) != expected:
        raise RiskSettingsError("risk settings payload schema mismatch")
    if value["confirmation"] != SAVE_CONFIRMATION:
        raise RiskSettingsError("risk settings confirmation mismatch")
    if value["risk_policy_version"] != RISK_POLICY_VERSION:
        raise RiskSettingsError("risk policy version mismatch")
    for name in (
        "planned_risk_basis_points",
        "daily_loss_limit_basis_points",
        "weekly_loss_limit_basis_points",
    ):
        if type(value[name]) is not int:
            raise RiskSettingsError("%s must be an exact integer" % name)
    maximum = value["maximum_investment_cents"]
    if maximum is not None and (
        type(maximum) is not int or maximum <= 0 or maximum > MAX_SAFE_CENTS
    ):
        raise RiskSettingsError(
            "maximum_investment_cents must be null or a positive safe integer"
        )
    try:
        policy = RiskPolicy(
            risk_policy_version=value["risk_policy_version"],
            planned_risk_basis_points=value["planned_risk_basis_points"],
            daily_loss_limit_basis_points=value["daily_loss_limit_basis_points"],
            weekly_loss_limit_basis_points=value["weekly_loss_limit_basis_points"],
            maximum_investment_cents=maximum,
        )
    except (TypeError, ValueError) as exc:
        raise RiskSettingsError("risk settings exceed limits or are out of order") from exc
    expected_sha = value["expected_sha256"]
    if expected_sha is not None and (
        type(expected_sha) is not str or _SHA256.fullmatch(expected_sha) is None
    ):
        raise RiskSettingsError("expected_sha256 must be null or lowercase SHA-256")
    return RiskSettingsUpdate(
        target_session=_canonical_date("target_session", value["target_session"]),
        policy=policy,
        expected_sha256=expected_sha,
    )


@dataclass(frozen=True)
class RiskSettingsRecord:
    revision: int
    target_session: str
    policy: RiskPolicy
    created_at: str
    updated_at: str
    parent_sha256: Optional[str] = None
    application_scope: str = APPLICATION_SCOPE

    def __post_init__(self) -> None:
        if type(self.revision) is not int or self.revision < 1:
            raise RiskSettingsError("revision must be a positive exact integer")
        _canonical_date("target_session", self.target_session)
        if type(self.policy) is not RiskPolicy:
            raise RiskSettingsError("policy must be an exact RiskPolicy")
        _validate_timestamp(self.created_at)
        _validate_timestamp(self.updated_at)
        if self.parent_sha256 is not None and (
            type(self.parent_sha256) is not str
            or _SHA256.fullmatch(self.parent_sha256) is None
        ):
            raise RiskSettingsError("parent_sha256 must be null or lowercase SHA-256")
        if (self.revision == 1 and self.parent_sha256 is not None) or (
            self.revision > 1 and self.parent_sha256 is None
        ):
            raise RiskSettingsError("risk settings revision chain is malformed")
        if self.application_scope != APPLICATION_SCOPE:
            raise RiskSettingsError("risk settings may apply only to a next session")

    def payload(self) -> Dict[str, Any]:
        return {
            "application_scope": self.application_scope,
            "created_at": self.created_at,
            "daily_loss_limit_basis_points": self.policy.daily_loss_limit_basis_points,
            "maximum_investment_cents": self.policy.maximum_investment_cents,
            "parent_sha256": self.parent_sha256,
            "planned_risk_basis_points": self.policy.planned_risk_basis_points,
            "revision": self.revision,
            "risk_policy_version": self.policy.risk_policy_version,
            "target_session": self.target_session,
            "updated_at": self.updated_at,
            "weekly_loss_limit_basis_points": self.policy.weekly_loss_limit_basis_points,
        }

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.payload())

    def envelope(self) -> Dict[str, Any]:
        return {"record": self.payload(), "sha256": self.sha256}

    def public_view(self, *, editable: bool) -> Dict[str, Any]:
        return {
            "application_scope": APPLICATION_SCOPE,
            "daily_loss_limit_basis_points": self.policy.daily_loss_limit_basis_points,
            "editable": bool(editable),
            "entry_blocked": self.policy.maximum_investment_cents is None,
            "maximum_investment_cents": self.policy.maximum_investment_cents,
            "planned_risk_basis_points": self.policy.planned_risk_basis_points,
            "revision": self.revision,
            "risk_policy_version": self.policy.risk_policy_version,
            "saved": True,
            "sha256": self.sha256,
            "target_session": self.target_session,
            "weekly_loss_limit_basis_points": self.policy.weekly_loss_limit_basis_points,
        }


def default_public_risk_settings(*, editable: bool = False) -> Dict[str, Any]:
    policy = RiskPolicy()
    return {
        "application_scope": APPLICATION_SCOPE,
        "daily_loss_limit_basis_points": policy.daily_loss_limit_basis_points,
        "editable": bool(editable),
        "entry_blocked": True,
        "maximum_investment_cents": None,
        "planned_risk_basis_points": policy.planned_risk_basis_points,
        "revision": 0,
        "risk_policy_version": policy.risk_policy_version,
        "saved": False,
        "sha256": None,
        "target_session": None,
        "weekly_loss_limit_basis_points": policy.weekly_loss_limit_basis_points,
    }


def _record_from_payload(value: Mapping[str, Any]) -> RiskSettingsRecord:
    expected = {
        "application_scope",
        "created_at",
        "daily_loss_limit_basis_points",
        "maximum_investment_cents",
        "parent_sha256",
        "planned_risk_basis_points",
        "revision",
        "risk_policy_version",
        "target_session",
        "updated_at",
        "weekly_loss_limit_basis_points",
    }
    if type(value) is not dict or set(value) != expected:
        raise RiskSettingsError("stored risk settings schema mismatch")
    try:
        policy = RiskPolicy(
            risk_policy_version=value["risk_policy_version"],
            planned_risk_basis_points=value["planned_risk_basis_points"],
            daily_loss_limit_basis_points=value["daily_loss_limit_basis_points"],
            weekly_loss_limit_basis_points=value["weekly_loss_limit_basis_points"],
            maximum_investment_cents=value["maximum_investment_cents"],
        )
    except (TypeError, ValueError) as exc:
        raise RiskSettingsError("stored risk policy is invalid") from exc
    return RiskSettingsRecord(
        revision=value["revision"],
        target_session=value["target_session"],
        policy=policy,
        created_at=value["created_at"],
        updated_at=value["updated_at"],
        parent_sha256=value["parent_sha256"],
        application_scope=value["application_scope"],
    )


class RiskSettingsStore:
    """Append-only external settings store with optimistic concurrency."""

    def __init__(self, runtime_root: Path, *, repository_root: Path) -> None:
        requested = Path(runtime_root)
        if not requested.is_absolute():
            raise RiskSettingsError("risk settings runtime root must be absolute")
        if requested.is_symlink():
            raise RiskSettingsError("risk settings runtime root must not be a symlink")
        try:
            root = requested.resolve(strict=True)
            repository = Path(repository_root).resolve(strict=True)
        except OSError as exc:
            raise RiskSettingsError("risk settings runtime root must already exist") from exc
        try:
            root.relative_to(repository)
        except ValueError:
            pass
        else:
            raise RiskSettingsError("risk settings runtime root must be outside repository")
        metadata = root.stat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise RiskSettingsError(
                "risk settings runtime root must be owned by this user and owner-only"
            )
        self.root = root
        self.revisions = root / "revisions"
        if self.revisions.exists() and self.revisions.is_symlink():
            raise RiskSettingsError("risk settings revisions directory is unsafe")
        self.revisions.mkdir(mode=0o700, exist_ok=True)
        revision_stat = self.revisions.stat()
        if (
            not stat.S_ISDIR(revision_stat.st_mode)
            or revision_stat.st_uid != os.getuid()
            or stat.S_IMODE(revision_stat.st_mode) & 0o077
        ):
            raise RiskSettingsError("risk settings revisions directory is not owner-only")
        self.latest_path = root / "latest.json"
        self.lock_path = root / ".writer.lock"

    def _revision_path(self, record: RiskSettingsRecord) -> Path:
        return self.revisions / ("%06d-%s.json" % (record.revision, record.sha256))

    @staticmethod
    def _load_envelope(path: Path) -> RiskSettingsRecord:
        try:
            envelope = read_json(path)
        except (StorageError, OSError, ValueError) as exc:
            raise RiskSettingsError("risk settings file failed integrity checks") from exc
        if type(envelope) is not dict or set(envelope) != {"record", "sha256"}:
            raise RiskSettingsError("risk settings envelope schema mismatch")
        if type(envelope["record"]) is not dict or type(envelope["sha256"]) is not str:
            raise RiskSettingsError("risk settings envelope is invalid")
        expected = canonical_sha256(envelope["record"])
        if not hmac.compare_digest(expected, envelope["sha256"]):
            raise RiskSettingsError("risk settings hash mismatch")
        record = _record_from_payload(envelope["record"])
        if record.payload() != envelope["record"] or record.sha256 != envelope["sha256"]:
            raise RiskSettingsError("stored risk settings are noncanonical")
        return record

    def load_latest(self, *, required: bool = False) -> Optional[RiskSettingsRecord]:
        if not self.latest_path.exists():
            if required:
                raise RiskSettingsError("no saved risk settings")
            return None
        record = self._load_envelope(self.latest_path)
        revision_path = self._revision_path(record)
        if not revision_path.exists():
            raise RiskSettingsError("latest risk settings lack immutable revision")
        durable = self._load_envelope(revision_path)
        if durable != record:
            raise RiskSettingsError("latest risk settings differ from immutable revision")
        child = durable
        while child.revision > 1:
            parent_digest = child.parent_sha256 or ""
            parent_path = self.revisions / (
                "%06d-%s.json" % (child.revision - 1, parent_digest)
            )
            if not parent_path.exists():
                raise RiskSettingsError("risk settings parent revision is missing")
            parent = self._load_envelope(parent_path)
            if (
                parent.revision != child.revision - 1
                or not hmac.compare_digest(parent.sha256, parent_digest)
            ):
                raise RiskSettingsError("risk settings parent revision is invalid")
            child = parent
        if child.revision != 1 or child.parent_sha256 is not None:
            raise RiskSettingsError("risk settings revision chain has no valid root")
        return record

    def policy_for_session(self, session_date: str) -> Optional[RiskPolicy]:
        """Return a policy only for an exact target-date match.

        This does not decide whether the date is an exchange session.  A
        caller must first validate it with its reviewed exchange calendar;
        non-matches fail closed and never fall back to the latest record.
        """

        session = _canonical_date("session_date", session_date)
        record = self.load_latest()
        if record is None or record.target_session != session:
            return None
        return record.policy

    def public_view(self, *, editable: bool) -> Dict[str, Any]:
        record = self.load_latest()
        return (
            default_public_risk_settings(editable=editable)
            if record is None
            else record.public_view(editable=editable)
        )

    def save_next_session(
        self,
        update: RiskSettingsUpdate,
        *,
        current_session_date: str,
        now: Optional[datetime] = None,
    ) -> RiskSettingsRecord:
        if type(update) is not RiskSettingsUpdate:
            raise RiskSettingsError("update must be an exact RiskSettingsUpdate")
        current = _canonical_date("current_session_date", current_session_date)
        if update.target_session <= current:
            raise RiskSettingsError(
                "risk settings can be saved only for a future session"
            )
        lock = ExclusiveFileLock(self.lock_path, purpose="risk-settings-writer")
        try:
            lock.acquire()
        except LockAlreadyHeld as exc:
            raise RiskSettingsConflictError("risk settings store is busy") from exc
        try:
            latest = self.load_latest()
            if latest is None:
                if update.expected_sha256 is not None:
                    raise RiskSettingsConflictError("no revision matches expected_sha256")
                revision = 1
                created_at = _timestamp(now)
                parent = None
            else:
                if update.expected_sha256 is None or not hmac.compare_digest(
                    update.expected_sha256, latest.sha256
                ):
                    raise RiskSettingsConflictError("risk settings revision changed")
                # A target session at or before today is immutable.  Saving a
                # future target creates a new pending policy and cannot mutate
                # the settings already in force for the current session.
                if latest.target_session <= current and update.target_session == latest.target_session:
                    raise RiskSettingsError("mid-session risk changes are forbidden")
                revision = latest.revision + 1
                created_at = latest.created_at
                parent = latest.sha256
            timestamp = _timestamp(now)
            record = RiskSettingsRecord(
                revision=revision,
                target_session=update.target_session,
                policy=update.policy,
                created_at=created_at,
                updated_at=timestamp,
                parent_sha256=parent,
            )
            target = self._revision_path(record)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            payload = canonical_json_bytes(record.envelope())
            try:
                descriptor = os.open(str(target), flags, 0o600)
            except FileExistsError as exc:
                raise RiskSettingsConflictError("risk settings revision already exists") from exc
            try:
                offset = 0
                while offset < len(payload):
                    written = os.write(descriptor, payload[offset:])
                    if written <= 0:
                        raise RiskSettingsError("risk settings revision write failed")
                    offset += written
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            # Make the immutable revision's directory entry durable before
            # publishing latest.json.  After a crash, latest can therefore
            # never name a revision that was only present in a volatile cache.
            _fsync_directory(self.revisions)
            atomic_write_json(self.latest_path, record.envelope(), mode=0o600)
            return record
        finally:
            lock.release()


__all__ = [
    "APPLICATION_SCOPE",
    "MAX_SAFE_CENTS",
    "SAVE_CONFIRMATION",
    "RiskSettingsConflictError",
    "RiskSettingsError",
    "RiskSettingsRecord",
    "RiskSettingsStore",
    "RiskSettingsUpdate",
    "default_public_risk_settings",
    "parse_risk_settings_update",
]
