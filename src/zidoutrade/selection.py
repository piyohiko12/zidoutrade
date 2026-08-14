"""Append-only, hash-bound user selection workflow.

The workflow intentionally separates *presentation*, *validation*, *arming*,
and *session locking*.  A selected instrument is never silently substituted.
Every change is a new SHA-256 chained revision.  Persistence has no default
path: callers must provide an external runtime directory explicitly so that
state and account-adjacent artifacts do not land in the source repository.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from enum import Enum
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from .candidates import CandidateEvaluation, EligibilityStatus, normalize_us_symbol


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SelectionError(ValueError):
    """A fail-closed selection workflow error."""


class SelectionState(str, Enum):
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    ARMED_NEXT_SESSION = "ARMED_NEXT_SESSION"
    SESSION_LOCKED = "SESSION_LOCKED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class PresentedCandidate:
    symbol: str
    priority: int
    eligible: bool
    reason_codes: Tuple[str, ...] = ()

    @classmethod
    def from_evaluation(cls, item: CandidateEvaluation) -> "PresentedCandidate":
        return cls(
            symbol=normalize_us_symbol(item.symbol),
            priority=item.priority,
            eligible=item.status is EligibilityStatus.PASS,
            reason_codes=tuple(reason.value for reason in item.reason_codes),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "eligible": self.eligible,
            "priority": self.priority,
            "reason_codes": list(self.reason_codes),
            "symbol": self.symbol,
        }


@dataclass(frozen=True)
class SelectionRecord:
    revision: int
    state: SelectionState
    target_session: str
    presented_candidates: Tuple[PresentedCandidate, ...]
    selected_symbol: Optional[str]
    trade_permitted: bool
    no_trade_reason: Optional[str]
    created_at: str
    updated_at: str
    parent_sha256: Optional[str] = None
    locked_at: Optional[str] = None

    def payload(self) -> Dict[str, Any]:
        return {
            "created_at": self.created_at,
            "locked_at": self.locked_at,
            "no_trade_reason": self.no_trade_reason,
            "parent_sha256": self.parent_sha256,
            "presented_candidates": [candidate.to_dict() for candidate in self.presented_candidates],
            "revision": self.revision,
            "selected_symbol": self.selected_symbol,
            "state": self.state.value,
            "target_session": self.target_session,
            "trade_permitted": self.trade_permitted,
            "updated_at": self.updated_at,
        }

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.payload())

    def envelope(self) -> Dict[str, Any]:
        return {"record": self.payload(), "sha256": self.sha256}


def canonical_json_bytes(value: Any) -> bytes:
    """Canonical JSON: UTF-8, sorted keys, minified, and exactly one LF."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SelectionError("NON_CANONICAL_JSON_VALUE") from exc
    return encoded + b"\n"


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _timestamp(now: Optional[datetime] = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise SelectionError("TIMESTAMP_MUST_BE_TIMEZONE_AWARE")
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _session(value: str) -> str:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise SelectionError("INVALID_TARGET_SESSION")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise SelectionError("INVALID_TARGET_SESSION") from exc
    return value


def _validate_presented(candidates: Sequence[PresentedCandidate]) -> Tuple[PresentedCandidate, ...]:
    if len(candidates) > 20:
        raise SelectionError("WATCHLIST_LIMIT_EXCEEDED")
    normalized = []
    seen = set()
    for candidate in candidates:
        symbol = normalize_us_symbol(candidate.symbol)
        if symbol in seen:
            raise SelectionError("DUPLICATE_PRESENTED_CANDIDATE")
        if isinstance(candidate.priority, bool) or not isinstance(candidate.priority, int):
            raise SelectionError("INVALID_USER_PRIORITY")
        if candidate.priority < 0:
            raise SelectionError("INVALID_USER_PRIORITY")
        seen.add(symbol)
        normalized.append(replace(candidate, symbol=symbol, reason_codes=tuple(candidate.reason_codes)))
    # The presentation snapshot itself is deterministic and must not imply an
    # expected-return order.
    return tuple(sorted(normalized, key=lambda item: (item.priority, item.symbol)))


def _selected_symbol(
    raw_symbol: Optional[str], candidates: Sequence[PresentedCandidate]
) -> Optional[str]:
    if raw_symbol is None:
        return None
    symbol = normalize_us_symbol(raw_symbol)
    matches = [candidate for candidate in candidates if candidate.symbol == symbol]
    if len(matches) != 1:
        raise SelectionError("SELECTION_NOT_PRESENTED")
    if not matches[0].eligible:
        raise SelectionError("SELECTION_NOT_ELIGIBLE")
    return symbol


def _next_record(
    previous: SelectionRecord,
    *,
    state: SelectionState,
    now: Optional[datetime] = None,
    selected_symbol: object = Ellipsis,
    trade_permitted: Optional[bool] = None,
    no_trade_reason: object = Ellipsis,
    locked_at: object = Ellipsis,
) -> SelectionRecord:
    updated = _timestamp(now)
    return SelectionRecord(
        revision=previous.revision + 1,
        state=state,
        target_session=previous.target_session,
        presented_candidates=previous.presented_candidates,
        selected_symbol=(
            previous.selected_symbol if selected_symbol is Ellipsis else selected_symbol  # type: ignore[arg-type]
        ),
        trade_permitted=(previous.trade_permitted if trade_permitted is None else trade_permitted),
        no_trade_reason=(
            previous.no_trade_reason if no_trade_reason is Ellipsis else no_trade_reason  # type: ignore[arg-type]
        ),
        created_at=previous.created_at,
        updated_at=updated,
        parent_sha256=previous.sha256,
        locked_at=(previous.locked_at if locked_at is Ellipsis else locked_at),  # type: ignore[arg-type]
    )


class SelectionWorkflow:
    """Pure state transitions for one next-session user choice."""

    @staticmethod
    def new_draft(
        *,
        target_session: str,
        presented_candidates: Sequence[PresentedCandidate],
        selected_symbol: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> SelectionRecord:
        candidates = _validate_presented(presented_candidates)
        selected = _selected_symbol(selected_symbol, candidates)
        timestamp = _timestamp(now)
        return SelectionRecord(
            revision=1,
            state=SelectionState.DRAFT,
            target_session=_session(target_session),
            presented_candidates=candidates,
            selected_symbol=selected,
            trade_permitted=False,
            no_trade_reason="NO_USER_SELECTION" if selected is None else "NOT_ARMED",
            created_at=timestamp,
            updated_at=timestamp,
        )

    @staticmethod
    def revise(
        record: SelectionRecord,
        *,
        selected_symbol: Optional[str],
        current_session_date: str,
        target_session: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> SelectionRecord:
        """Create a new DRAFT revision before the target session begins."""

        if record.state is SelectionState.SESSION_LOCKED:
            raise SelectionError("SESSION_ALREADY_LOCKED")
        if record.state is SelectionState.EXPIRED:
            raise SelectionError("SELECTION_ALREADY_EXPIRED")
        current = _session(current_session_date)
        if current >= record.target_session:
            raise SelectionError("MID_SESSION_REPLACEMENT_FORBIDDEN")
        if target_session is not None and _session(target_session) != record.target_session:
            raise SelectionError("TARGET_SESSION_IS_FIXED")
        selected = _selected_symbol(selected_symbol, record.presented_candidates)
        return _next_record(
            record,
            state=SelectionState.DRAFT,
            now=now,
            selected_symbol=selected,
            trade_permitted=False,
            no_trade_reason="NO_USER_SELECTION" if selected is None else "NOT_ARMED",
            locked_at=None,
        )

    @staticmethod
    def validate(record: SelectionRecord, *, now: Optional[datetime] = None) -> SelectionRecord:
        if record.state is not SelectionState.DRAFT:
            raise SelectionError("VALIDATE_REQUIRES_DRAFT")
        selected = _selected_symbol(record.selected_symbol, record.presented_candidates)
        return _next_record(
            record,
            state=SelectionState.VALIDATED,
            now=now,
            trade_permitted=False,
            no_trade_reason="NO_USER_SELECTION" if selected is None else "NOT_ARMED",
        )

    @staticmethod
    def arm(record: SelectionRecord, *, now: Optional[datetime] = None) -> SelectionRecord:
        if record.state is not SelectionState.VALIDATED:
            raise SelectionError("ARM_REQUIRES_VALIDATED")
        selected = _selected_symbol(record.selected_symbol, record.presented_candidates)
        return _next_record(
            record,
            state=SelectionState.ARMED_NEXT_SESSION,
            now=now,
            trade_permitted=selected is not None,
            no_trade_reason=None if selected is not None else "NO_USER_SELECTION",
        )

    @staticmethod
    def lock_session(
        record: SelectionRecord,
        *,
        session_date: str,
        now: Optional[datetime] = None,
    ) -> SelectionRecord:
        if record.state is not SelectionState.ARMED_NEXT_SESSION:
            raise SelectionError("LOCK_REQUIRES_ARMED_NEXT_SESSION")
        if _session(session_date) != record.target_session:
            raise SelectionError("SESSION_DATE_MISMATCH")
        timestamp = _timestamp(now)
        return _next_record(
            record,
            state=SelectionState.SESSION_LOCKED,
            now=now,
            trade_permitted=record.selected_symbol is not None and record.trade_permitted,
            no_trade_reason=None if record.selected_symbol is not None else "NO_USER_SELECTION",
            locked_at=timestamp,
        )

    @staticmethod
    def expire(
        record: SelectionRecord,
        *,
        current_session_date: str,
        now: Optional[datetime] = None,
    ) -> SelectionRecord:
        """Close a selection only after its target session date has passed."""

        if record.state is SelectionState.EXPIRED:
            raise SelectionError("SELECTION_ALREADY_EXPIRED")
        if _session(current_session_date) <= record.target_session:
            raise SelectionError("TARGET_SESSION_HAS_NOT_EXPIRED")
        return _next_record(
            record,
            state=SelectionState.EXPIRED,
            now=now,
            trade_permitted=False,
            no_trade_reason="SELECTION_EXPIRED",
        )


def _candidate_from_mapping(value: Mapping[str, Any]) -> PresentedCandidate:
    expected = {"eligible", "priority", "reason_codes", "symbol"}
    if set(value) != expected:
        raise SelectionError("INVALID_PRESENTED_CANDIDATE_SCHEMA")
    reasons = value["reason_codes"]
    if not isinstance(reasons, list) or not all(isinstance(item, str) for item in reasons):
        raise SelectionError("INVALID_REASON_CODES")
    if not isinstance(value["eligible"], bool):
        raise SelectionError("INVALID_ELIGIBILITY_VALUE")
    return PresentedCandidate(
        symbol=str(value["symbol"]),
        priority=value["priority"],
        eligible=value["eligible"],
        reason_codes=tuple(reasons),
    )


def record_from_payload(payload: Mapping[str, Any]) -> SelectionRecord:
    expected = {
        "created_at",
        "locked_at",
        "no_trade_reason",
        "parent_sha256",
        "presented_candidates",
        "revision",
        "selected_symbol",
        "state",
        "target_session",
        "trade_permitted",
        "updated_at",
    }
    if set(payload) != expected:
        raise SelectionError("INVALID_SELECTION_RECORD_SCHEMA")
    presented = payload["presented_candidates"]
    if not isinstance(presented, list):
        raise SelectionError("INVALID_PRESENTED_CANDIDATES")
    try:
        state = SelectionState(payload["state"])
    except (TypeError, ValueError) as exc:
        raise SelectionError("INVALID_SELECTION_STATE") from exc
    normalized_presented = _validate_presented([_candidate_from_mapping(item) for item in presented])
    normalized_selected = _selected_symbol(payload["selected_symbol"], normalized_presented)
    record = SelectionRecord(
        revision=payload["revision"],
        state=state,
        target_session=_session(payload["target_session"]),
        presented_candidates=normalized_presented,
        selected_symbol=normalized_selected,
        trade_permitted=payload["trade_permitted"],
        no_trade_reason=payload["no_trade_reason"],
        created_at=payload["created_at"],
        updated_at=payload["updated_at"],
        parent_sha256=payload["parent_sha256"],
        locked_at=payload["locked_at"],
    )
    if isinstance(record.revision, bool) or not isinstance(record.revision, int) or record.revision < 1:
        raise SelectionError("INVALID_REVISION")
    if not isinstance(record.trade_permitted, bool):
        raise SelectionError("INVALID_TRADE_PERMISSION")
    if record.trade_permitted and (
        record.state not in (SelectionState.ARMED_NEXT_SESSION, SelectionState.SESSION_LOCKED)
        or record.selected_symbol is None
    ):
        raise SelectionError("INVALID_TRADE_PERMISSION")
    return record


class SelectionStore:
    """Fail-closed append-only runtime persistence.

    ``runtime_root`` is mandatory and should point outside the Git checkout,
    for example ``~/Library/Application Support/zidoutrade``.  Pass
    ``repository_root`` to enforce that boundary programmatically.
    """

    def __init__(self, runtime_root: Path, *, repository_root: Optional[Path] = None) -> None:
        if runtime_root is None:
            raise SelectionError("RUNTIME_PATH_REQUIRED")
        requested_root = Path(runtime_root).expanduser()
        if requested_root.is_symlink():
            raise SelectionError("UNSAFE_RUNTIME_ROOT")
        self.root = requested_root.resolve()
        for ancestor in (self.root, *self.root.parents):
            if (ancestor / ".git").exists():
                raise SelectionError("RUNTIME_PATH_INSIDE_REPOSITORY")
        if repository_root is not None:
            repo = Path(repository_root).expanduser().resolve()
            try:
                self.root.relative_to(repo)
            except ValueError:
                pass
            else:
                raise SelectionError("RUNTIME_PATH_INSIDE_REPOSITORY")
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        root_stat = self.root.stat()
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or root_stat.st_uid != os.getuid()
            or stat.S_IMODE(root_stat.st_mode) & 0o077
        ):
            raise SelectionError("UNSAFE_RUNTIME_ROOT")
        self.revisions = self.root / "selection-revisions"
        self.latest_path = self.root / "selection-latest.json"
        self.lock_path = self.root / ".selection-write.lock"

    def save(self, record: SelectionRecord) -> Path:
        self.revisions.mkdir(parents=True, exist_ok=True)
        lock_flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            lock_flags |= os.O_NOFOLLOW
        try:
            lock_fd = os.open(str(self.lock_path), lock_flags, 0o600)
        except FileExistsError as exc:
            raise SelectionError("SELECTION_STORE_LOCKED") from exc
        try:
            os.close(lock_fd)
            latest = self.load_latest(required=False)
            if latest is None:
                if record.revision != 1 or record.parent_sha256 is not None:
                    raise SelectionError("INVALID_INITIAL_REVISION")
            else:
                if record.revision != latest.revision + 1:
                    raise SelectionError("NON_SEQUENTIAL_REVISION")
                if record.parent_sha256 != latest.sha256:
                    raise SelectionError("PARENT_HASH_MISMATCH")

            body = canonical_json_bytes(record.envelope())
            revision_path = self.revisions / ("%06d-%s.json" % (record.revision, record.sha256))
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = os.open(str(revision_path), flags, 0o600)
            except FileExistsError as exc:
                raise SelectionError("REVISION_ALREADY_EXISTS") from exc
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                # The immutable file may be incomplete; loading it will fail
                # closed.  Never overwrite it with a retry.
                raise

            temp_latest = self.root / (".selection-latest.%s.tmp" % record.sha256)
            temp_flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                temp_flags |= os.O_NOFOLLOW
            temp_fd = os.open(str(temp_latest), temp_flags, 0o600)
            try:
                with os.fdopen(temp_fd, "wb") as handle:
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(str(temp_latest), str(self.latest_path))
            finally:
                if temp_latest.exists():
                    temp_latest.unlink()
            return revision_path
        finally:
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass

    def load_latest(self, *, required: bool = True) -> Optional[SelectionRecord]:
        if not self.latest_path.exists():
            if required:
                raise SelectionError("NO_SAVED_SELECTION")
            return None
        return self._load_envelope(self.latest_path)

    @staticmethod
    def _load_envelope(path: Path) -> SelectionRecord:
        try:
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 1_000_000:
                raise SelectionError("UNSAFE_SELECTION_FILE")
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(str(path), flags)
            try:
                opened = os.fstat(fd)
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise SelectionError("SELECTION_FILE_CHANGED_DURING_OPEN")
                with os.fdopen(fd, "rb") as handle:
                    fd = -1
                    raw = handle.read(1_000_001)
            finally:
                if fd >= 0:
                    os.close(fd)
            if len(raw) > 1_000_000:
                raise SelectionError("SELECTION_FILE_TOO_LARGE")
            if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
                raise SelectionError("NON_CANONICAL_SELECTION_FILE")
            envelope = json.loads(raw.decode("utf-8"))
        except SelectionError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SelectionError("UNREADABLE_SELECTION_FILE") from exc
        if not isinstance(envelope, dict) or set(envelope) != {"record", "sha256"}:
            raise SelectionError("INVALID_SELECTION_ENVELOPE")
        if not isinstance(envelope["record"], dict) or not isinstance(envelope["sha256"], str):
            raise SelectionError("INVALID_SELECTION_ENVELOPE")
        expected = canonical_sha256(envelope["record"])
        if not hmac.compare_digest(expected, envelope["sha256"]):
            raise SelectionError("SELECTION_HASH_MISMATCH")
        if raw != canonical_json_bytes(envelope):
            raise SelectionError("NON_CANONICAL_SELECTION_FILE")
        record = record_from_payload(envelope["record"])
        # Normalization must not change persisted bytes.  This catches, among
        # other things, reordered presentation rows and noncanonical symbols.
        if record.payload() != envelope["record"] or record.sha256 != envelope["sha256"]:
            raise SelectionError("NON_CANONICAL_SELECTION_RECORD")
        return record


__all__ = [
    "PresentedCandidate",
    "SelectionError",
    "SelectionRecord",
    "SelectionState",
    "SelectionStore",
    "SelectionWorkflow",
    "canonical_json_bytes",
    "canonical_sha256",
    "record_from_payload",
]
