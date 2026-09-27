"""Command-line entry point for the auto-compute pilot."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import runpy
import sys

from . import install, maintenance
from .scripts import status
from .scripts import tui


def main() -> None:
    parser = argparse.ArgumentParser(prog="codex-auto-compute")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", help="Read account metadata and show proposed changes")
    fixture = sub.add_parser("fixture", help="Stage a disposable project")
    fixture.add_argument("path", type=Path)
    apply = sub.add_parser("install", help="Apply after a checked live fixture")
    apply.add_argument("--verified-fixture", required=True, type=Path)
    sub.add_parser("status", help="Show the latest session's routing evidence")
    dashboard = sub.add_parser("tui", help="Browse live routing evidence in the terminal")
    dashboard.add_argument("--ledger", type=Path, default=status.default_ledger_path())
    doctor = sub.add_parser("doctor", help="Check installed pins against Codex's live catalog")
    doctor.add_argument("--home", type=Path, default=Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser())
    refresh = sub.add_parser("refresh", help="Repair retired pins after bounded live smoke checks")
    refresh.add_argument("--home", type=Path, default=Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser())
    uninstall = sub.add_parser("uninstall", help="Restore using the backup manifest")
    uninstall.add_argument("manifest", type=Path)
    args, rest = parser.parse_known_args()
    if args.command in ("plan", "fixture", "install"):
        extra = [] if args.command == "plan" else (["--fixture", str(args.path)] if args.command == "fixture" else ["--apply", "--verified-fixture", str(args.verified_fixture)])
        sys.argv = ["codex-auto-compute", *extra, *rest]
        raise SystemExit(install.main())
    if args.command == "status":
        sys.argv = ["codex-auto-compute status", *rest]
        status.main()
        return
    if args.command == "tui":
        if rest:
            parser.error("unrecognized arguments: " + " ".join(rest))
        tui.main(args.ledger)
        return
    if args.command == "doctor":
        if rest:
            parser.error("unrecognized arguments: " + " ".join(rest))
        report = maintenance.health(args.home.expanduser().resolve())
        print(json.dumps(report, indent=2))
        if report.get("catalog") != "healthy":
            raise SystemExit(2)
        return
    if args.command == "refresh":
        if rest:
            parser.error("unrecognized arguments: " + " ".join(rest))
        home = args.home.expanduser().resolve()
        old = json.loads((home / "auto-compute" / "policy.json").read_text())
        binary = install.shutil.which("codex")
        if not binary:
            raise SystemExit("Codex CLI not found; no pins were changed")
        rpc = install.RPC(binary, home)
        try:
            account = rpc.call("account/read", {"refreshToken": False}).get("account")
            if not isinstance(account, dict) or account.get("type") != "chatgpt":
                raise RuntimeError("Refresh requires a ChatGPT-signed-in Codex CLI")
            replacement, changes = maintenance.proposed_refresh(old, rpc.catalog())
            if not changes:
                print("Pins match the live catalog. No changes needed.")
                return
            pins = {(replacement["roles"][c["role"]]["model"], replacement["roles"][c["role"]]["effort"])
                    for c in changes if c["role"] in replacement["roles"]}
            if old.get("coordinator") != replacement.get("coordinator"):
                pins.add((replacement["coordinator"]["model"], replacement["coordinator"]["effort"]))
            for model, reasoning_effort in sorted(pins):
                maintenance.smoke_pin(binary, home, model, reasoning_effort)
            backup = maintenance.apply_refresh(home, old, replacement, rpc)
            print(json.dumps({"changed_pins": changes, "backup_manifest": str(backup),
                              "custom_agent_runtime": "unverified until a fresh worker phase runs",
                              "restart_required": True}, indent=2))
        except (OSError, ValueError, RuntimeError) as exc:
            raise SystemExit("Refresh stopped: " + str(exc)) from exc
        finally:
            rpc.close()
        return
    if rest:
        parser.error("unrecognized arguments: " + " ".join(rest))
    sys.argv = ["codex-auto-compute uninstall", str(args.manifest)]
    runpy.run_path(str(Path(__file__).with_name("uninstall.py")), run_name="__main__")
