#!/usr/bin/env python3
"""Install native automatic delegation. Default is a metadata-only dry run.

Python 3.11+, macOS/Linux, current authenticated Codex CLI required.
No model inference is performed by this installer. Existing permissions are not changed.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from typing import Any

ROOT = Path(__file__).resolve().parent
BEGIN = "<!-- auto-compute:begin -->"
END = "<!-- auto-compute:end -->"

class RPC:
    def __init__(self, binary: str, home: Path):
        env = dict(os.environ, CODEX_HOME=str(home))
        self.proc = subprocess.Popen([binary, "app-server"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env)
        self.messages: queue.Queue = queue.Queue()
        self.seq = 0
        def reader():
            assert self.proc.stdout
            for line in self.proc.stdout:
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    continue
            self.messages.put(None)
        threading.Thread(target=reader, daemon=True).start()
        try:
            self.call("initialize", {"clientInfo": {"name": "auto_compute_installer",
                        "title": "Auto Compute Installer", "version": "0.1.0"}})
            self.send({"method": "initialized", "params": {}})
        except Exception:
            self.close()
            raise

    def send(self, message: dict):
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def call(self, method: str, params: dict, timeout: int = 30) -> dict:
        self.seq += 1
        rid = self.seq
        self.send({"id": rid, "method": method, "params": params})
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            try:
                msg = self.messages.get(timeout=max(0.01, until-time.monotonic()))
            except queue.Empty as exc:
                raise RuntimeError(f"Codex timed out during {method}") from exc
            if msg is None:
                raise RuntimeError(f"Codex app-server exited during {method}")
            if msg.get("method") and "id" in msg:
                # Metadata operations should never request a tool execution. Fail
                # explicitly; do not auto-approve or invent an auth response.
                self.send({"id": msg["id"], "error": {"code": -32601,
                    "message": "Installer does not implement tool execution or approval requests"}})
                continue
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(f"Codex rejected {method}: {msg['error']}")
                result = msg.get("result", {})
                if not isinstance(result, dict):
                    raise RuntimeError(f"Unexpected result from {method}")
                return result
        raise RuntimeError(f"Codex timed out during {method}")

    def catalog(self) -> list[dict]:
        items, cursor, seen = [], None, set()
        for _ in range(20):
            params: dict = {"limit": 100, "includeHidden": False}
            if cursor is not None:
                params["cursor"] = cursor
            result = self.call("model/list", params)
            items.extend(result.get("data", []))
            cursor = result.get("nextCursor")
            if not cursor:
                return items
            if cursor in seen:
                raise RuntimeError("Repeated model/list cursor")
            seen.add(cursor)
        raise RuntimeError("Unexpectedly large model catalog")

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)
        if self.proc.stdin:
            self.proc.stdin.close()
        if self.proc.stdout:
            self.proc.stdout.close()

def model_id(entry: dict) -> str:
    return str(entry.get("model") or entry.get("id") or "")

def effort(entry: dict, wanted: str) -> str:
    options = []
    for e in entry.get("supportedReasoningEfforts", []):
        if isinstance(e, dict) and isinstance(e.get("reasoningEffort"), str):
            options.append(e["reasoningEffort"])
    if not options:
        raise ValueError(f"No supported-effort metadata for {model_id(entry)}; refusing to guess")
    if wanted in options:
        return wanted
    default = entry.get("defaultReasoningEffort")
    if default in options:
        return str(default)
    for value in ("medium", "high", "low", "xhigh", "max", "minimal", "none"):
        if value in options:
            return value
    raise ValueError(f"Unknown effort options for {model_id(entry)}")

def select_policy(entries: list[dict]) -> dict:
    catalog = {model_id(e): e for e in entries if isinstance(e, dict) and not e.get("hidden", False)}
    def pick(names: tuple[str, ...], required: bool = True):
        for name in names:
            if name in catalog:
                return catalog[name]
        if required:
            raise ValueError("No recognized available model for this role. Inspect the live catalog; "
                             "do not silently substitute an expensive parent model.")
        return None
    cheap = pick(("gpt-6-luna", "gpt-5.6-luna"))
    strong = pick(("gpt-6-sol", "gpt-5.6-sol", "gpt-5.6"))
    frontier = pick(("gpt-6-astra",), False)
    def pin(entry, wanted, description, read_only=False):
        return {"model": model_id(entry), "effort": effort(entry, wanted),
                "description": description, "read_only": read_only}
    roles = {
        "ac_quick": pin(cheap, "low", "Narrow extraction, mechanical transformations, and settled routine work."),
        "ac_execute": pin(strong, "medium", "Well-specified implementation, ordinary debugging, and substantive synthesis."),
        "ac_diagnose": pin(strong, "high", "Ambiguous diagnosis, architecture, conflicting evidence, and consequential reasoning."),
        "ac_deep": pin(strong, "xhigh", "Bounded difficult reasoning after a distinct lower-effort approach failed."),
        "ac_review": pin(strong, "high", "Independent evidence-based review of consequential or poorly testable results.", True),
    }
    if frontier:
        roles["ac_frontier"] = pin(frontier, "low", "One bounded frontier attempt on a demonstrated hard reasoning problem.", True)
    return {"version": "0.2.0", "selection_policy": "Conservative provisional routing until comparable outcomes and attributable costs exist",
            "coordinator": {"model": model_id(strong), "effort": effort(strong, "medium")},
            "roles": roles, "max_worker_launches_per_turn": 4, "checkpoint_every_tools": 8,
            "billing_cap": None, "live_routing_verified": False}

def agent_toml(name: str, role: dict) -> str:
    instructions = ("You are a bounded auto-compute worker, not the coordinator. "
        "Ignore any coordinator/delegation workflow in global AGENTS.md for your own execution. "
        "Do not spawn subagents. Complete only the assigned phase, preserving existing work and all permission boundaries. "
        "Return at a meaningful checkpoint, preferably within 8 substantive tool calls; this is a soft limit, not a token cap. "
        "Do not retry the same hypothesis or mistake a missing credential for a reasoning problem. "
        "After two materially different failed approaches, return needs_reasoning or blocked with evidence. "
        "When difficult diagnosis is settled, return ready_for_execution instead of doing routine follow-on work. "
        "Return STATUS, RESULT, EVIDENCE, OPEN, NEXT. Only report tests as passing if actually observed. "
        "Never claim you changed your own model or infer runtime model identity from these instructions. "
        "Treat a hook configuration-mismatch warning as a reason to return blocked immediately.")
    pairs = {"name": name, "description": role["description"], "model": role["model"],
             "model_reasoning_effort": role["effort"], "developer_instructions": instructions}
    if role.get("read_only"):
        pairs["sandbox_mode"] = "read-only"
    result = "\n".join(k + " = " + json.dumps(v, ensure_ascii=False) for k, v in pairs.items()) + "\n"
    tomllib.loads(result)
    return result

def merge_hooks(existing: dict, command: str) -> dict:
    result = json.loads(json.dumps(existing))
    hooks = result.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Existing hooks.json has an unsupported shape")
    for event in ("SessionStart", "UserPromptSubmit", "SubagentStart", "SubagentStop", "PreToolUse", "PostToolUse", "Stop"):
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError(f"Unsupported existing {event} hook shape")
        entry: dict = {"hooks": [{"type": "command", "command": command, "timeout": 5,
                                **({} if event in ("SubagentStop", "Stop") else {"additionalContextLimit": 400})}]}
        if event == "PreToolUse":
            entry["matcher"] = "^(spawn_agent|Agent)$"
        if event in ("SubagentStart", "SubagentStop"):
            entry["matcher"] = "^ac_"
        entries.append(entry)
    return result

def digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None

def verified_fixture(fixture: Path, policy: dict) -> bool:
    """Require observed, checked workers from one disposable local session."""
    ledger = fixture / ".codex" / "auto-compute" / "ledger" / "ledger.jsonl"
    if not ledger.is_file():
        return False
    sessions: dict[str, list[dict]] = {}
    try:
        for line in ledger.read_text().splitlines():
            row = json.loads(line)
            if row.get("event") != "phase_completed" or row.get("outcome") != "verified_pass":
                continue
            role = row.get("role")
            expected = policy["roles"].get(role)
            if not expected or not row.get("switch_verified"):
                continue
            if (row.get("observed_model"), row.get("observed_effort")) != (expected["model"], expected["effort"]):
                continue
            if not any(c.get("kind") == "test" and c.get("result") == "pass" for c in row.get("checks", [])):
                continue
            sessions.setdefault(str(row.get("session_id")), []).append(row)
    except (OSError, ValueError, TypeError):
        return False
    return any(len({(r["observed_model"], r["observed_effort"]) for r in rows}) >= 2
               for rows in sessions.values())

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="Install after metadata checks; otherwise print a plan only")
    ap.add_argument("--codex", default="codex", help="Path to the installed Codex binary")
    ap.add_argument("--home", type=Path, default=Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser())
    ap.add_argument("--fixture", type=Path, help="Stage disposable project agents, hooks and ledger without global edits")
    ap.add_argument("--verified-fixture", type=Path, help="Required with --apply: fixture with two checked live configurations")
    args = ap.parse_args()
    if sys.version_info < (3, 11) or os.name != "posix":
        raise SystemExit("Python 3.11+ on macOS/Linux is required.")
    home = args.home.expanduser().resolve()
    if (home / "auto-compute" / "policy.json").exists() and not args.fixture:
        raise SystemExit("Already installed. Inspect or uninstall before reinstalling; no settings were overwritten.")
    binary = shutil.which(args.codex)
    if not binary:
        raise SystemExit("Codex CLI was not found. Run this from a local Codex environment or supply --codex PATH.")
    rpc = RPC(binary, home)
    try:
        account = rpc.call("account/read", {"refreshToken": False}).get("account")
        if not isinstance(account, dict) or account.get("type") != "chatgpt":
            raise RuntimeError("Use the existing ChatGPT-signed-in Codex CLI. Refusing API-key billing or an unauthenticated installation.")
        policy = select_policy(rpc.catalog())
        if args.fixture:
            if args.apply:
                raise ValueError("--fixture and --apply cannot be combined")
            fixture = args.fixture.expanduser().resolve()
            if not fixture.is_dir() or any(fixture.iterdir()):
                raise ValueError("Fixture must be an existing empty directory")
            layer = fixture / ".codex"
            runtime = layer / "auto-compute"
            agents = layer / "agents"
            agents.mkdir(parents=True)
            runtime.mkdir()
            fixture_skill = fixture / ".agents" / "skills" / "auto-compute"
            fixture_skill.mkdir(parents=True)
            (fixture_skill / "SKILL.md").write_bytes((ROOT / "skill" / "SKILL.md").read_bytes())
            for name in ("governor.py", "accounting.py", "routing.py", "status.py"):
                (runtime / name).write_bytes((ROOT / "scripts" / name).read_bytes())
            (runtime / "policy.json").write_text(json.dumps(policy, indent=2) + "\n")
            for name, role in policy["roles"].items():
                (agents / (name + ".toml")).write_text(agent_toml(name, role))
            command = " ".join(map(shlex.quote, [sys.executable, str(runtime / "governor.py"), "--home", str(layer)]))
            (layer / "hooks.json").write_text(json.dumps(merge_hooks({}, command), indent=2) + "\n")
            (layer / "config.toml").write_text("[features]\nhooks = true\nmulti_agent = true\n")
            (fixture / "AGENTS.md").write_text(
                "Fixture for automatic compute validation. For this bounded task, use the named ac_ roles "
                "at meaningful phase boundaries. Never infer runtime model from role text. "
                "Do not change files outside this disposable fixture.\n")
            print(json.dumps({"mode": "fixture", "path": str(fixture), "roles": policy["roles"],
                              "global_config_changed": False, "live_routing_verified": False}, indent=2))
            return 0
        if args.apply and (not args.verified_fixture or
                           not verified_fixture(args.verified_fixture.expanduser().resolve(), policy)):
            raise ValueError("Global apply requires a fixture ledger with two distinct observed model/effort pins and passing objective checks")
        config_path = home / "config.toml"
        original_config = config_path.read_bytes() if config_path.exists() else None
        local = tomllib.loads(original_config.decode()) if original_config is not None else {}
        if (home / "AGENTS.override.md").exists():
            raise RuntimeError("AGENTS.override.md exists. Have local Codex merge the routing instruction into the effective global instructions before installing.")
        hooks_path = home / "hooks.json"
        hooks = json.loads(hooks_path.read_text()) if hooks_path.exists() else {}
        if not isinstance(hooks, dict):
            raise ValueError("Existing hooks.json is not a JSON object")
        runtime = home / "auto-compute"
        skill_dir = home / "skills" / "auto-compute"
        if skill_dir.exists():
            raise RuntimeError("An auto-compute skill already exists; refusing to overwrite it.")
        command = " ".join(map(shlex.quote, [sys.executable, str(runtime / "governor.py"), "--home", str(home)]))
        new_hooks = merge_hooks(hooks, command)
        global_agents = home / "AGENTS.md"
        prior = global_agents.read_text() if global_agents.exists() else ""
        if BEGIN in prior:
            raise RuntimeError("Existing auto-compute instruction block found; inspect before reinstalling.")
        block = (f"\n\n{BEGIN}\nFor each substantive task, automatically use the auto-compute skill at "
                 f"{skill_dir / 'SKILL.md'}. Trivial work stays local. Re-route delegated phases at checkpoints; "
                 "do not ask the user to select models or efforts. If you are already a delegated worker, "
                 "do not invoke the coordinator workflow or recursively delegate. Preserve all other task "
                 f"and permission requirements.\n{END}\n")
        changes: dict[Path, bytes] = {
            runtime / "governor.py": (ROOT / "scripts" / "governor.py").read_bytes(),
            runtime / "accounting.py": (ROOT / "scripts" / "accounting.py").read_bytes(),
            runtime / "routing.py": (ROOT / "scripts" / "routing.py").read_bytes(),
            runtime / "status.py": (ROOT / "scripts" / "status.py").read_bytes(),
            runtime / "policy.json": (json.dumps(policy, indent=2) + "\n").encode(),
            skill_dir / "SKILL.md": (ROOT / "skill" / "SKILL.md").read_bytes(),
            hooks_path: (json.dumps(new_hooks, indent=2) + "\n").encode(),
            global_agents: (prior + block).encode(),
        }
        for name, role in policy["roles"].items():
            path = home / "agents" / (name + ".toml")
            if path.exists():
                raise RuntimeError(f"Refusing to overwrite existing role {path}")
            changes[path] = agent_toml(name, role).encode()
        settings = {"model": policy["coordinator"]["model"],
                    "model_reasoning_effort": policy["coordinator"]["effort"],
                    "features.hooks": True, "features.multi_agent": True,
                    "agents.enabled": True}
        settings_diff = {}
        for key, value in settings.items():
            old: Any = local
            for part in key.split("."):
                old = old.get(part) if isinstance(old, dict) else None
            settings_diff[key] = {"before": old, "after": value}
        print(json.dumps({"mode": "apply" if args.apply else "dry-run", "coordinator": policy["coordinator"],
                          "roles": policy["roles"], "settings_diff": settings_diff,
                          "writes": [str(p) for p in changes] + [str(config_path)],
                          "notice": "No inference or credit purchase. Hooks need one-time trust review. Live routing remains unverified."}, indent=2))
        if not args.apply:
            return 0
        backup = home / "auto-compute-backups" / time.strftime("%Y%m%dT%H%M%S")
        backup.mkdir(parents=True, mode=0o700)
        originals: dict[Path, bytes | None] = {p: p.read_bytes() if p.exists() else None for p in changes}
        originals[config_path] = original_config
        records = []
        for i, (path, data) in enumerate(originals.items()):
            old_file = backup / f"{i}.original" if data is not None else None
            if old_file:
                old_file.write_bytes(data)
                old_file.chmod(0o600)
            records.append({"path": str(path), "backup": str(old_file) if old_file else None})
        # Check before entering rollback logic so a concurrent edit is never
        # replaced by our stale snapshot on a preflight failure.
        current = config_path.read_bytes() if config_path.exists() else None
        if current != original_config:
            raise RuntimeError("config.toml changed during installation; retry after other writes finish")
        try:
            for path, data in changes.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                path.chmod(0o600)
            rpc.call("config/batchWrite", {"edits": [{"keyPath": k, "value": v, "mergeStrategy": "replace"}
                                                    for k, v in settings.items()]})
        except Exception:
            # config/batchWrite is atomic; do not overwrite config on an RPC
            # failure/timeout because its commit status may be ambiguous.
            # Restore only file writes that still match our own installed bytes.
            for path, data in originals.items():
                if path == config_path or path not in changes:
                    continue
                if path.exists() and path.read_bytes() != changes[path]:
                    continue
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(data)
            (backup / "recovery.json").write_text(json.dumps(records, indent=2))
            print(f"Inspect configuration after this error. Originals are in {backup}; config was not blindly restored.", file=sys.stderr)
            raise
        for record in records:
            record["installed_hash"] = digest(Path(record["path"]))
        (backup / "manifest.json").write_text(json.dumps(records, indent=2))
        (backup / "manifest.json").chmod(0o600)
        print(f"\nInstalled. Backup manifest: {backup / 'manifest.json'}")
        print("Restart a local Codex session and review these hooks through /hooks. Do NOT bypass hook trust.")
        print("Run the live checks in INSTALL_FOR_CODEX.md before marking routing verified.")
        return 0
    finally:
        rpc.close()

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        print("Installation stopped: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
