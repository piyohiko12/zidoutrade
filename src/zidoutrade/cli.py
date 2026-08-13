"""Safe local command-line entrypoint.

The CLI deliberately exposes configuration validation, a redacted dashboard,
and tests.  It does not expose an order command.  Paper activation remains a
separate, future, locally audited workflow rather than something a public clone
can trigger.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import secrets
import sys
from typing import Any, Dict, Optional, Sequence

from . import PROGRAM_ID, __version__
from .config import ConfigError, load_system_config
from .dashboard import DashboardApplication, DashboardServer


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
        app = DashboardApplication(
            _safe_dashboard_state,
            selection_callback=None,
            csrf_token=secrets.token_urlsafe(32),
        )
        try:
            server = DashboardServer(app, host=args.host, port=args.port)
        except (OSError, ValueError) as exc:
            print(f"dashboard startup failed: {exc}", file=sys.stderr)
            return 2
        print(f"Dashboard: http://{server.address[0]}:{server.address[1]}/")
        print("Mode: SHADOW / DISARMED / READ_ONLY")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
        return 0

    return 2


__all__ = ["main"]
