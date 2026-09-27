#!/usr/bin/env python3
"""Show the latest auto-compute routing evidence without reading task content."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def read_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("event") in ("phase_started", "phase_completed", "turn_completed"):
                rows.append(row)
    return rows


def default_ledger_path() -> Path:
    return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "auto-compute" / "ledger" / "ledger.jsonl"


def latest_session(rows: list[dict]) -> list[dict]:
    if not rows:
        return []
    last = rows[-1]
    session = last.get("session_id")
    return [row for row in rows if row.get("session_id") == session]


def render(rows: list[dict]) -> str:
    selected = latest_session(rows)
    if not selected:
        return "No routing records yet. Start a fresh Codex task with trusted hooks."
    lines = [f"Session: {selected[0].get('session_id', 'unknown')}"]
    recent_phases = [r for r in selected if r.get("event") in ("phase_started", "phase_completed")][-10:]
    recent_parent = [r for r in selected if r.get("event") == "turn_completed"][-1:]
    for row in recent_phases + recent_parent:
        event = row.get("event")
        if event == "phase_started":
            if not any(r.get("event") == "phase_completed" and r.get("phase_id") == row.get("phase_id") for r in selected):
                lines.append(f"Worker {row.get('role', 'unknown')}: requested {row.get('requested_model') or 'unknown'}/{row.get('requested_effort') or 'unknown'}; runtime pending")
        elif event == "phase_completed":
            verified = row.get("switch_verified") is True
            model = row.get("observed_model") or "unknown"
            effort = row.get("observed_effort") or "unknown"
            label = "runtime verified" if verified else "runtime unverified"
            outcome = row.get("outcome") or "unknown"
            tokens = row.get("tokens") or {}
            count = tokens.get("total_tokens") if isinstance(tokens, dict) else None
            token_text = f"; {count} tokens" if isinstance(count, int) else "; tokens unknown"
            lines.append(f"Worker {row.get('role', 'unknown')}: {model}/{effort} ({label}); check {outcome}{token_text}")
        elif event == "turn_completed":
            model = row.get("observed_model") or "unknown"
            effort = row.get("observed_effort") or "unknown"
            lines.append(f"Main chat: {model}/{effort} (runtime record; turn tokens include workers)")
    lines.append("Actual per-phase credit debit: unavailable")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, default=default_ledger_path())
    args = parser.parse_args()
    print(render(read_rows(args.ledger)))


if __name__ == "__main__":
    main()
