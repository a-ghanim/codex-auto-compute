"""Command-line entry point for the auto-compute pilot."""
from __future__ import annotations

import argparse
from pathlib import Path
import runpy
import sys

from . import install
from .scripts import status


def main() -> None:
    parser = argparse.ArgumentParser(prog="codex-auto-compute")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", help="Read account metadata and show proposed changes")
    fixture = sub.add_parser("fixture", help="Stage a disposable project")
    fixture.add_argument("path", type=Path)
    apply = sub.add_parser("install", help="Apply after a checked live fixture")
    apply.add_argument("--verified-fixture", required=True, type=Path)
    sub.add_parser("status", help="Show the latest session's routing evidence")
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
    if rest:
        parser.error("unrecognized arguments: " + " ".join(rest))
    sys.argv = ["codex-auto-compute uninstall", str(args.manifest)]
    runpy.run_path(str(Path(__file__).with_name("uninstall.py")), run_name="__main__")
