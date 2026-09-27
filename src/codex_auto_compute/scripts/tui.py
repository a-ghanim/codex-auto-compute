"""Read-only terminal browser for the local auto-compute ledger."""
from __future__ import annotations

import curses
import os
from pathlib import Path
import sys
import threading
import time

from . import status
from .. import maintenance


def sessions(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        session_id = row.get("session_id")
        if isinstance(session_id, str) and session_id:
            grouped.setdefault(session_id, []).append(row)
    return list(reversed(list(grouped.items())))


def phases(rows: list[dict]) -> list[dict]:
    """One entry per phase, with completion replacing its start record."""
    ordered: dict[str, dict] = {}
    for index, row in enumerate(rows):
        if row.get("event") not in ("phase_started", "phase_completed"):
            continue
        key = str(row.get("phase_id") or f"unidentified-{index}")
        if key not in ordered or row.get("event") == "phase_completed":
            ordered[key] = row
    return list(ordered.values())


def phase_label(row: dict) -> str:
    role = row.get("role") or "unknown worker"
    if row.get("event") == "phase_started":
        return f"{role}  pending"
    if row.get("switch_verified") is True:
        return f"{role}  verified"
    return f"{role}  unverified"


def next_phase_index(current: int, count: int, delta: int) -> int:
    return min(max(0, count - 1), max(0, current + delta))


def detail_lines(rows: list[dict], phase_index: int) -> list[str]:
    if not rows:
        return ["No ledger records yet.", "Start a Codex task with trusted hooks."]
    parent = next((r for r in reversed(rows) if r.get("event") == "turn_completed"), None)
    selected = phases(rows)
    lines = ["MAIN CHAT"]
    if parent:
        lines.append(f"  Model:  {parent.get('observed_model') or 'unknown'}")
        lines.append(f"  Effort: {parent.get('observed_effort') or 'unknown'}")
        lines.append("  Token total includes worker usage")
    else:
        lines.append("  Runtime record: unknown")
    lines.extend(["", "WORKER PHASE"])
    if not selected:
        lines.append("  No worker recorded for this session")
    else:
        row = selected[min(phase_index, len(selected)-1)]
        pending = row.get("event") == "phase_started"
        lines.append(f"  {phase_label(row)}  ({min(phase_index, len(selected)-1)+1}/{len(selected)})")
        lines.append(f"  Requested: {row.get('requested_model') or 'unknown'} / {row.get('requested_effort') or 'unknown'}")
        lines.append(f"  Observed:  {row.get('observed_model') or 'unknown'} / {row.get('observed_effort') or 'unknown'}")
        lines.append(f"  Check:     {'pending' if pending else row.get('outcome') or 'unknown'}")
        tokens = row.get("tokens") if isinstance(row.get("tokens"), dict) else {}
        lines.append(f"  Tokens:    {tokens.get('total_tokens') if isinstance(tokens.get('total_tokens'), int) else 'unknown'}")
        duration = row.get("duration_sec")
        lines.append(f"  Duration:  {duration:.1f}s" if isinstance(duration, (int, float)) else "  Duration:  unknown")
        checks = row.get("checks") if isinstance(row.get("checks"), list) else []
        if checks:
            lines.append("  Objective: " + ", ".join(f"{c.get('kind','check')}={c.get('result','unknown')}" for c in checks if isinstance(c, dict)))
    lines.extend(["", "Actual per-phase credit debit: unavailable"])
    return lines


def _put(win, y: int, x: int, value: str, width: int, style: int = 0) -> None:
    if width <= 0:
        return
    height, screen_width = win.getmaxyx()
    if 0 <= y < height and 0 <= x < screen_width:
        try:
            win.addnstr(y, x, value, min(width, screen_width-x), style)
        except curses.error:
            pass


def _draw(win, ledger: Path, selected_session: int, selected_phase: int, rows: list[dict], health_report: dict) -> None:
    win.erase()
    height, width = win.getmaxyx()
    if height < 16 or width < 64:
        _put(win, 1, 2, "Terminal too small. Resize to at least 64 x 16.", width-4)
        win.refresh()
        return
    groups = sessions(rows)
    left = min(34, max(25, width // 3))
    title = "AUTO COMPUTE  /  ROUTING LEDGER"
    _put(win, 0, 2, title, width-4, curses.A_BOLD)
    age = health_report.get("age_seconds")
    age_text = "no ledger records" if age is None else f"last record {age//60}m ago"
    catalog = health_report.get("catalog", "unavailable")
    warning = " · run codex-auto-compute refresh" if catalog == "needs_refresh" else ""
    telemetry = health_report.get("telemetry", "unknown")
    _put(win, 1, 2, f"Catalog: {catalog} · telemetry: {telemetry} · {age_text}{warning}", width-4)
    win.hline(2, 0, curses.ACS_HLINE, width)
    _put(win, 3, 2, "SESSIONS (newest first)", left-3, curses.A_BOLD)
    win.vline(3, left, curses.ACS_VLINE, height-5)
    if not groups:
        _put(win, 5, 2, "No records yet", left-3)
    else:
        visible = height-8
        start = max(0, selected_session-visible+1)
        for n in range(start, min(len(groups), start+visible)):
            sid, session_rows = groups[n]
            marker = ">" if n == selected_session else " "
            count = len(phases(session_rows))
            _put(win, 5+n-start, 2, f"{marker} {sid[:13]}  {count} worker(s)", left-3,
                 curses.A_REVERSE if n == selected_session else 0)
    x = left+3
    if groups:
        sid, session_rows = groups[min(selected_session, len(groups)-1)]
        _put(win, 3, x, f"SESSION {sid}", width-x-2, curses.A_BOLD)
        for n, value in enumerate(detail_lines(session_rows, selected_phase)):
            _put(win, 5+n, x, value, width-x-2, curses.A_BOLD if value in ("MAIN CHAT", "WORKER PHASE") else 0)
    else:
        _put(win, 5, x, "Run a Codex task with trusted hooks to populate this view.", width-x-2)
    win.hline(height-3, 0, curses.ACS_HLINE, width)
    _put(win, height-2, 2, "↑/↓ or j/k session   ←/→ or h/l worker   r refresh   q quit", width-4)
    win.refresh()


def _run(win, ledger: Path) -> None:
    curses.curs_set(0)
    win.keypad(True)
    win.timeout(200)
    selected_session = 0
    selected_phase = 0
    rows: list[dict] = []
    last_read = 0.0
    last_health = 0.0
    health_report: dict = {"catalog": "checking", "age_seconds": None}
    health_pending = False
    def check_catalog() -> None:
        nonlocal health_pending
        try:
            home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
            checked = maintenance.health(home)
            health_report["catalog"] = checked.get("catalog", "unavailable")
        except Exception:
            health_report["catalog"] = "unavailable"
        finally:
            health_pending = False
    while True:
        now = time.monotonic()
        if now-last_read >= 2:
            rows = status.read_rows(ledger)
            last_read = now
            health_report.update(maintenance.ledger_age(ledger))
            health_report["telemetry"] = maintenance.latest_telemetry(ledger)
        if now-last_health >= 300 and not health_pending:
            health_pending = True
            last_health = time.monotonic()
            threading.Thread(target=check_catalog, daemon=True).start()
        groups = sessions(rows)
        selected_session = min(selected_session, max(0, len(groups)-1))
        _draw(win, ledger, selected_session, selected_phase, rows, health_report)
        key = win.getch()
        if key in (ord("q"), 27):
            return
        if key in (curses.KEY_UP, ord("k")):
            selected_session = max(0, selected_session-1)
            selected_phase = 0
        elif key in (curses.KEY_DOWN, ord("j")):
            selected_session = min(max(0, len(groups)-1), selected_session+1)
            selected_phase = 0
        elif key in (curses.KEY_LEFT, ord("h")):
            selected_phase = next_phase_index(selected_phase, len(phases(groups[selected_session][1])) if groups else 0, -1)
        elif key in (curses.KEY_RIGHT, ord("l")) and groups:
            selected_phase = next_phase_index(selected_phase, len(phases(groups[selected_session][1])), 1)
        elif key in (ord("r"), curses.KEY_RESIZE):
            last_read = 0
            last_health = 0


def main(ledger: Path) -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit("TUI requires an interactive terminal. Use `codex-auto-compute status` for plain output.")
    curses.wrapper(_run, ledger)
