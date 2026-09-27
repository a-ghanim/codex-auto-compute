"""Read-only catalog health checks and conservative pin refresh."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import tomllib

from .install import RPC, agent_toml, model_id, select_policy
from .scripts.accounting import usage_from_rollout
from .scripts import status


def inspect_policy(policy: dict, catalog: list[dict]) -> dict:
    available = {model_id(e): e for e in catalog if isinstance(e, dict) and model_id(e) and not e.get("hidden", False)}
    invalid = []
    for role, pin in policy.get("roles", {}).items():
        entry = available.get(pin.get("model"))
        if entry is None:
            invalid.append({"role": role, "reason": "model_missing", "model": pin.get("model")})
        elif pin.get("effort") not in {e.get("reasoningEffort") for e in entry.get("supportedReasoningEfforts", []) if isinstance(e, dict)}:
            invalid.append({"role": role, "reason": "effort_missing", "model": pin.get("model"), "effort": pin.get("effort")})
    coordinator = policy.get("coordinator", {})
    parent_entry = available.get(coordinator.get("model"))
    if parent_entry is None:
        invalid.append({"role": "coordinator", "reason": "model_missing", "model": coordinator.get("model")})
    elif coordinator.get("effort") not in {e.get("reasoningEffort") for e in parent_entry.get("supportedReasoningEfforts", []) if isinstance(e, dict)}:
        invalid.append({"role": "coordinator", "reason": "effort_missing", "model": coordinator.get("model"), "effort": coordinator.get("effort")})
    return {"catalog": "healthy" if not invalid else "needs_refresh", "invalid_pins": invalid}


def ledger_age(path: Path, now: float | None = None) -> dict:
    if not path.is_file():
        return {"ledger": "missing", "age_seconds": None}
    age = max(0, int((time.time() if now is None else now) - path.stat().st_mtime))
    return {"ledger": "historical" if age >= 3600 else "recent", "age_seconds": age}


def latest_telemetry(path: Path) -> str:
    completed = [r for r in status.read_rows(path) if r.get("event") == "phase_completed"]
    if not completed:
        return "no_completed_worker"
    return "verified" if completed[-1].get("switch_verified") is True else "unverified"


def health(home: Path, binary: str = "codex") -> dict:
    policy_path = home / "auto-compute" / "policy.json"
    ledger = home / "auto-compute" / "ledger" / "ledger.jsonl"
    if not policy_path.is_file():
        return {"catalog": "not_installed", "telemetry": latest_telemetry(ledger), **ledger_age(ledger)}
    policy = json.loads(policy_path.read_text())
    exe = shutil.which(binary)
    if not exe:
        return {"catalog": "unavailable", "reason": "Codex CLI not found", "telemetry": latest_telemetry(ledger), **ledger_age(ledger)}
    try:
        rpc = RPC(exe, home)
        try:
            report = inspect_policy(policy, rpc.catalog())
        finally:
            rpc.close()
    except (OSError, RuntimeError, ValueError) as exc:
        report = {"catalog": "unavailable", "reason": str(exc)}
    return {**report, "telemetry": latest_telemetry(ledger), **ledger_age(ledger)}


def proposed_refresh(policy: dict, catalog: list[dict]) -> tuple[dict, list[dict]]:
    report = inspect_policy(policy, catalog)
    if not report["invalid_pins"]:
        return policy, []
    replacement = select_policy(catalog, previous=policy)
    changes = []
    for role in sorted(set(policy.get("roles", {})) | set(replacement["roles"])):
        old = policy.get("roles", {}).get(role)
        new = replacement["roles"].get(role)
        if old != new:
            changes.append({"role": role, "before": None if old is None else f"{old['model']}/{old['effort']}",
                            "after": None if new is None else f"{new['model']}/{new['effort']}"})
    if policy.get("coordinator") != replacement["coordinator"]:
        changes.append({"role": "coordinator", "before": policy.get("coordinator"), "after": replacement["coordinator"]})
    replacement["live_routing_verified"] = False
    replacement["verification_scope"] = "pins refreshed from live catalog; new custom-agent routing awaits observation"
    replacement["unverified_roles"] = [change["role"] for change in changes if change["role"] != "coordinator"]
    return replacement, changes


def smoke_pin(binary: str, home: Path, model: str, reasoning_effort: str) -> None:
    """Check a new pin with a bounded live CLI call before updating files.

    This verifies direct CLI runtime metadata, not a custom-agent outcome.
    """
    with tempfile.TemporaryDirectory(prefix="auto-compute-pin-") as directory:
        command = [binary, "exec", "--json", "--ignore-user-config", "--ignore-rules",
                   "--skip-git-repo-check", "--sandbox", "read-only", "-C", directory,
                   "--model", model, "--config", f'model_reasoning_effort="{reasoning_effort}"',
                   "Reply exactly OK. Do not use tools."]
        try:
            result = subprocess.run(command, env=dict(os.environ, CODEX_HOME=str(home)),
                                    text=True, capture_output=True, timeout=120)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Live pin smoke timed out for {model}/{reasoning_effort}") from exc
        if result.returncode != 0:
            raise RuntimeError(f"Live pin smoke failed for {model}/{reasoning_effort}; no pins were changed")
        events = []
        for line in result.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        thread_id = next((e.get("thread_id") for e in events if e.get("type") == "thread.started"), None)
        if not isinstance(thread_id, str) or not any(e.get("type") == "turn.completed" for e in events):
            raise RuntimeError(f"Codex did not report a completed smoke turn for {model}/{reasoning_effort}")
        matches = list((home / "sessions").rglob(f"*{thread_id}*.jsonl"))
        if len(matches) != 1:
            raise RuntimeError(f"Runtime record unavailable for {model}/{reasoning_effort}; no pins were changed")
        observed = usage_from_rollout(str(matches[0]))
        if (observed["observed_model"], observed["observed_effort"]) != (model, reasoning_effort):
            raise RuntimeError(f"Runtime did not match requested {model}/{reasoning_effort}; no pins were changed")


def _atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
        out.write(data)
        name = out.name
    os.chmod(name, 0o600)
    os.replace(name, path)


def replace_agent_pin(content: str, old: dict, new: dict) -> str:
    """Change only the two managed pin lines; retain local agent instructions."""
    parsed = tomllib.loads(content)
    if (parsed.get("model"), parsed.get("model_reasoning_effort")) != (old["model"], old["effort"]):
        raise RuntimeError("Agent model or effort was edited outside auto-compute")
    result = content
    for key, value in (("model", new["model"]), ("model_reasoning_effort", new["effort"])):
        pattern = rf"(?m)^{key}\s*=\s*[^\n]+$"
        result, count = re.subn(pattern, lambda _: f"{key} = {json.dumps(value)}", result)
        if count != 1:
            raise RuntimeError(f"Expected one top-level {key} pin in agent file")
    tomllib.loads(result)
    return result


def apply_refresh(home: Path, old: dict, new: dict, rpc: RPC | None = None) -> Path:
    """Replace only unchanged managed pin files; preserve unrelated Codex settings."""
    policy_path = home / "auto-compute" / "policy.json"
    old_bytes = policy_path.read_bytes()
    if json.loads(old_bytes) != old:
        raise RuntimeError("Installed policy changed during refresh; no files were written")
    writes: dict[Path, bytes | None] = {policy_path: (json.dumps(new, indent=2) + "\n").encode()}
    coordinator_changed = old.get("coordinator") != new.get("coordinator")
    config_path = home / "config.toml"
    if coordinator_changed:
        if rpc is None:
            raise RuntimeError("Coordinator pin changed but no Codex config connection is available")
        config = tomllib.loads(config_path.read_text())
        expected = old["coordinator"]
        if (config.get("model"), config.get("model_reasoning_effort")) != (expected["model"], expected["effort"]):
            raise RuntimeError("Main model setting was changed separately; refusing to overwrite it")
    for role in set(old.get("roles", {})) | set(new.get("roles", {})):
        path = home / "agents" / f"{role}.toml"
        before = old.get("roles", {}).get(role)
        after = new.get("roles", {}).get(role)
        if before == after:
            continue
        if before:
            if not path.is_file():
                raise RuntimeError(f"Managed agent file is missing: {path}; no files were written")
            content = path.read_text()
            if after:
                updated = replace_agent_pin(content, before, after)
            elif content == agent_toml(role, before):
                updated = None
            else:
                raise RuntimeError(f"Retired agent file has local edits: {path}; no files were written")
        elif path.exists():
            raise RuntimeError(f"Agent file already exists: {path}; no files were written")
        else:
            updated = agent_toml(role, after) if after else None
        writes[path] = updated.encode() if updated is not None else None
    backup = home / "auto-compute-backups" / ("refresh-" + time.strftime("%Y%m%dT%H%M%S"))
    backup.mkdir(parents=True, exist_ok=False, mode=0o700)
    originals = {path: path.read_bytes() if path.exists() else None for path in writes}
    if coordinator_changed:
        originals[config_path] = config_path.read_bytes()
    manifest = []
    for n, (path, data) in enumerate(originals.items()):
        saved = backup / f"{n}.original" if data is not None else None
        if saved:
            saved.write_bytes(data)
            saved.chmod(0o600)
        manifest.append({"path": str(path), "backup": str(saved) if saved else None,
                         "before_sha256": hashlib.sha256(data).hexdigest() if data is not None else None})
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    try:
        for path, data in writes.items():
            current = path.read_bytes() if path.exists() else None
            if current != originals[path]:
                raise RuntimeError(f"{path} changed during refresh")
            if data is None:
                path.unlink(missing_ok=True)
            else:
                _atomic(path, data)
        if coordinator_changed:
            if config_path.read_bytes() != originals[config_path]:
                raise RuntimeError("config.toml changed during refresh; no config write attempted")
            rpc.call("config/batchWrite", {"edits": [
                {"keyPath": "model", "value": new["coordinator"]["model"], "mergeStrategy": "replace"},
                {"keyPath": "model_reasoning_effort", "value": new["coordinator"]["effort"], "mergeStrategy": "replace"},
            ]})
    except Exception:
        for path, original in originals.items():
            if path == config_path:
                continue  # RPC commit state may be ambiguous; never overwrite it blindly.
            current = path.read_bytes() if path.exists() else None
            intended = writes[path]
            if current == intended:
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic(path, original)
        raise
    return backup / "manifest.json"
