"""Safe local command-line entrypoint.

The CLI deliberately exposes configuration validation, a redacted dashboard,
and tests.  It does not expose an order command.  Paper activation remains a
separate, future, locally audited workflow rather than something a public clone
can trigger.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import secrets
import sys
from typing import Any, Dict, Optional, Sequence
from zoneinfo import ZoneInfo

from . import PROGRAM_ID, __version__
from .config import ConfigError, load_system_config
from .dashboard import DashboardApplication, DashboardServer
from .risk_settings import RiskSettingsError, RiskSettingsStore, RiskSettingsUpdate


_NEW_YORK = ZoneInfo("America/New_York")


def _safe_dashboard_state() -> Dict[str, Any]:
    return {
        "overview": {
            "program_id": PROGRAM_ID,
            "mode": "SHADOW",
            "operating_mode": "SUPERVISED_ONLY",
            "control": "DISARMED",
            "exposure": "FLAT",
            "message": "No local runtime snapshot is attached.",
        },
        "candidates": [],
        "selection": None,
        "decision": {
            "action": "WAIT",
            "reasons": ["NO_VALIDATED_CANDIDATE_SNAPSHOT"],
        },
        "risk": {
            "new_entries_permitted": False,
            "reason": "DISARMED",
        },
        "journal": [],
        "system": {
            "broker_environment": "SIMULATE",
            "opend_endpoint": "127.0.0.1:11111",
            "activation_present": False,
            "sensitive_data_exposed": False,
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zidoutrade",
        description="RSI_AUTOPILOT_V1 (SHADOW / supervised SIMULATE only)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate-config", help="validate an inert policy file")
    validate.add_argument("path", type=Path)

    dashboard = commands.add_parser("dashboard", help="serve the redacted local dashboard")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument(
        "--risk-settings-runtime-root",
        type=Path,
        help=(
            "explicit absolute owner-only directory outside the repository; "
            "without it risk settings are read-only"
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "validate-config":
        try:
            config = load_system_config(args.path)
        except ConfigError as exc:
            print(json.dumps({"valid": False, "error": str(exc)}, sort_keys=True))
            return 2
        print(
            json.dumps(
                {
                    "valid": True,
                    "program_id": config.program_id,
                    "mode": config.mode,
                    "broker_environment": config.broker_environment,
                },
                sort_keys=True,
            )
        )
        return 0

    if args.command == "dashboard":
        # This fallback UI is deliberately read-only and disarmed. Runtime
        # integration must inject a separately reviewed state provider.
        try:
            risk_store = None
            if args.risk_settings_runtime_root is not None:
                risk_store = RiskSettingsStore(
                    args.risk_settings_runtime_root,
                    repository_root=Path(__file__).resolve().parents[2],
                )

            def provide_risk_settings() -> Dict[str, Any]:
                if risk_store is None:  # pragma: no cover - callback not wired
                    raise RiskSettingsError("risk settings store is unavailable")
                return risk_store.public_view(editable=True)

            def save_risk_settings(update: RiskSettingsUpdate) -> Dict[str, Any]:
                if risk_store is None:  # pragma: no cover - callback not wired
                    raise RiskSettingsError("risk settings store is unavailable")
                current = datetime.now(_NEW_YORK)
                record = risk_store.save_next_session(
                    update,
                    current_session_date=current.date().isoformat(),
                    now=current,
                )
                return {
                    "revision": record.revision,
                    "saved": True,
                    "target_session": record.target_session,
                }

            app = DashboardApplication(
                _safe_dashboard_state,
                selection_callback=None,
                risk_settings_provider=(
                    provide_risk_settings if risk_store is not None else None
                ),
                risk_settings_callback=(
                    save_risk_settings if risk_store is not None else None
                ),
                csrf_token=secrets.token_urlsafe(32),
            )
            server = DashboardServer(app, host=args.host, port=args.port)
        except (OSError, RiskSettingsError, ValueError) as exc:
            print(f"dashboard startup failed: {exc}", file=sys.stderr)
            return 2
        print(f"Dashboard: http://{server.address[0]}:{server.address[1]}/")
        settings_mode = "NEXT_SESSION_RISK_EDITABLE" if risk_store else "READ_ONLY"
        print(f"Mode: SHADOW / DISARMED / {settings_mode}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
        return 0

    return 2


__all__ = ["main"]
