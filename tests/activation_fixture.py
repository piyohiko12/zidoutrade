"""Synthetic local activation artifacts shared by safety tests."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict
from zoneinfo import ZoneInfo

from zidoutrade import PROGRAM_ID
from zidoutrade.activation import (
    ACTIVE_CONFIG_NAME,
    ACTIVATION_MARKER_NAME,
    ACTIVATION_SECRET_NAME,
    FINAL_LOCK_NAME,
    ActivationVerifier,
    activation_marker_hmac,
    hash_python_tree,
    program_binding_sha256,
    utc_timestamp,
)
from zidoutrade.exchange_calendar import (
    FrozenExchangeCalendar,
    FrozenSession,
    calendar_payload_sha256,
)
from zidoutrade.execution import (
    DecisionKind,
    QuoteSnapshot,
    build_entry_decision,
    build_exit_decision,
)
from zidoutrade.models import (
    CompletedBar15m,
    MarketGates,
    PositionSnapshot,
    StrategyContext,
    TrendEligibility,
)
from zidoutrade.risk import RiskState, SizingRequest
from zidoutrade.selection import (
    PresentedCandidate,
    SelectionStore,
    SelectionWorkflow,
)
from zidoutrade.storage import (
    account_fingerprint,
    canonical_json_bytes,
)


ROOT = Path(__file__).resolve().parents[1]
NY = ZoneInfo("America/New_York")
SESSION_ID = "2026-08-13"
SYMBOL = "US.TEST"
RAW_SYNTHETIC_ACCOUNT = "111111"
SECRET = b"synthetic-test-key-32-bytes-long!!"


class FixedClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _write_private(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    path.chmod(0o600)


def system_config() -> Dict[str, Any]:
    value = json.loads((ROOT / "config" / "system.example.json").read_text("utf-8"))
    value["mode"] = "PAPER_SIMULATE"
    return value


def create_fixture(runtime_root: Path) -> Dict[str, Any]:
    root = Path(runtime_root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    clock = FixedClock(datetime(2026, 8, 13, 10, 30, 1, tzinfo=NY))

    session_date = date.fromisoformat(SESSION_ID)
    session = FrozenSession(
        session_date=session_date,
        open_at=datetime.combine(session_date, time(9, 30), NY),
        close_at=datetime.combine(session_date, time(16, 0), NY),
    )
    calendar_hash = calendar_payload_sha256(
        source_revision="synthetic-reviewed-calendar-v1",
        covered_from=session_date,
        covered_through=session_date,
        sessions=(session,),
    )
    calendar = FrozenExchangeCalendar(
        source_revision="synthetic-reviewed-calendar-v1",
        covered_from=session_date,
        covered_through=session_date,
        sessions=(session,),
        expected_sha256=calendar_hash,
    )

    store = SelectionStore(root, repository_root=ROOT)
    candidates = (PresentedCandidate(SYMBOL, 0, True),)
    timestamps = [
        datetime(2026, 8, 12, 10, minute, tzinfo=timezone.utc)
        for minute in range(4)
    ]
    selection = SelectionWorkflow.new_draft(
        target_session=SESSION_ID,
        presented_candidates=candidates,
        selected_symbol=SYMBOL,
        now=timestamps[0],
    )
    store.save(selection)
    selection = SelectionWorkflow.validate(selection, now=timestamps[1])
    store.save(selection)
    selection = SelectionWorkflow.arm(selection, now=timestamps[2])
    store.save(selection)
    selection = SelectionWorkflow.lock_session(
        selection, session_date=SESSION_ID, now=timestamps[3]
    )
    store.save(selection)

    config_bytes = canonical_json_bytes(system_config())
    _write_private(root / ACTIVE_CONFIG_NAME, config_bytes)
    _write_private(root / ACTIVATION_SECRET_NAME, SECRET)

    config_hash = hashlib.sha256(config_bytes).hexdigest()
    source_hash = hash_python_tree(ROOT / "src" / "zidoutrade")
    tests_hash = hash_python_tree(ROOT / "tests")
    program_hash = program_binding_sha256(
        config_sha256=config_hash,
        source_sha256=source_hash,
        tests_sha256=tests_hash,
    )
    fingerprint = account_fingerprint(RAW_SYNTHETIC_ACCOUNT, SECRET)
    final_lock = {
        "account_fingerprint": fingerprint,
        "broker_environment": "SIMULATE",
        "config_sha256": config_hash,
        "opend_host": "127.0.0.1",
        "opend_port": 11111,
        "program_id": PROGRAM_ID,
        "program_sha256": program_hash,
        "rth_verifier_sha256": calendar_hash,
        "runtime_root": str(root.resolve()),
        "schema": 1,
        "selected_symbol": SYMBOL,
        "selection_sha256": selection.sha256,
        "session": "RTH",
        "session_id": SESSION_ID,
        "source_sha256": source_hash,
        "tests_sha256": tests_hash,
    }
    final_lock_bytes = canonical_json_bytes(final_lock)
    _write_private(root / FINAL_LOCK_NAME, final_lock_bytes)
    unsigned_marker = {
        "activated_at": utc_timestamp(
            datetime(2026, 8, 12, 11, 0, tzinfo=timezone.utc)
        ),
        "activation_nonce": "a" * 64,
        "final_lock_sha256": hashlib.sha256(final_lock_bytes).hexdigest(),
        "program_id": PROGRAM_ID,
        "schema": 1,
    }
    marker = {
        **unsigned_marker,
        "marker_hmac_sha256": activation_marker_hmac(unsigned_marker, SECRET),
    }
    _write_private(root / ACTIVATION_MARKER_NAME, canonical_json_bytes(marker))
    verifier = ActivationVerifier(
        runtime_root=root,
        repository_root=ROOT,
        calendar=calendar,
        clock=clock,
    )
    proof = verifier.verify(
        expected_session_id=SESSION_ID,
        expected_symbol=SYMBOL,
        expected_account_fingerprint=fingerprint,
    )
    return {
        "calendar": calendar,
        "clock": clock,
        "fingerprint": fingerprint,
        "proof": proof,
        "root": root,
        "selection": selection,
        "verifier": verifier,
    }


def _bars():
    values = []
    raw = (
        (9, 45, 9.90, 10.00, 9.80, 9.90),
        (10, 0, 9.90, 9.95, 9.80, 9.90),
        (10, 15, 9.90, 10.10, 9.85, 10.00),
    )
    for hour, minute, opening, high, low, close in raw:
        start = datetime(2026, 8, 13, hour, minute, tzinfo=NY)
        values.append(
            CompletedBar15m(
                symbol=SYMBOL,
                start=start,
                end=start + timedelta(minutes=15),
                open=opening,
                high=high,
                low=low,
                close=close,
                volume=100_000,
            )
        )
    return tuple(values)


def signed_decision(
    fixture: Dict[str, Any],
    *,
    kind: DecisionKind,
    quantity: int,
    limit_price: str,
    completed_roundtrips_today: int = 0,
    entry_dispatches_today: int = 0,
    exit_dispatches_today: int = 0,
):
    """Build authentic synthetic evidence through the public typed builders."""

    now = fixture["clock"].value
    price = float(limit_price)
    quote = QuoteSnapshot(
        symbol=SYMBOL,
        bid=limit_price,
        ask=limit_price,
        observed_at=now - timedelta(seconds=1),
    )
    state = RiskState(
        day_start_equity=100.0 * quantity,
        week_start_equity=100.0 * quantity,
        daily_pnl=0.0,
        weekly_pnl=0.0,
        completed_roundtrips_today=completed_roundtrips_today,
    )
    if kind is DecisionKind.ENTRY:
        context = StrategyContext(
            active_symbol=SYMBOL,
            selected_symbol=SYMBOL,
            bars=_bars(),
            rsi_values=(29.0, 34.0, 36.0),
            trend=TrendEligibility(True, True, True),
            gates=MarketGates(True, True, True),
            now=now,
            traded_roundtrips_today=completed_roundtrips_today,
        )
        return build_entry_decision(
            verifier=fixture["verifier"],
            proof=fixture["proof"],
            calendar=fixture["calendar"],
            context=context,
            sizing_request=SizingRequest(
                state=state,
                entry_limit=price,
                stop_trigger=price - 0.10,
            ),
            atr_raw=0.10 / 1.5,
            quote=quote,
            entry_dispatches_today=entry_dispatches_today,
            exit_dispatches_today=exit_dispatches_today,
        )
    context = StrategyContext(
        active_symbol=SYMBOL,
        selected_symbol=SYMBOL,
        bars=_bars(),
        rsi_values=(29.0, 34.0, 60.0),
        trend=TrendEligibility(True, True, True),
        gates=MarketGates(True, True, True),
        now=now,
        position=PositionSnapshot(
            entry_raw=max(price - 0.10, 0.20),
            atr_raw=0.05,
            current_raw_price=price,
            bars_held=1,
            entry_time=datetime(2026, 8, 13, 9, 45, tzinfo=NY),
            exchange_close=datetime(2026, 8, 13, 16, 0, tzinfo=NY),
        ),
        traded_roundtrips_today=completed_roundtrips_today,
    )
    return build_exit_decision(
        verifier=fixture["verifier"],
        proof=fixture["proof"],
        calendar=fixture["calendar"],
        context=context,
        risk_state=state,
        quote=quote,
        known_position_quantity=quantity,
        entry_dispatches_today=entry_dispatches_today,
        exit_dispatches_today=exit_dispatches_today,
    )


__all__ = [
    "FixedClock",
    "RAW_SYNTHETIC_ACCOUNT",
    "ROOT",
    "SECRET",
    "SESSION_ID",
    "SYMBOL",
    "create_fixture",
    "signed_decision",
]
