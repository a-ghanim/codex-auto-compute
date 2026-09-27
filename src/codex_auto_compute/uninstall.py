#!/usr/bin/env python3
"""Restore an installation only when its files have not since been edited."""
import hashlib
import json
from pathlib import Path
import re
import sys
import tomllib

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None

if len(sys.argv) != 2:
    raise SystemExit("Usage: python3 uninstall.py /absolute/path/to/backup/manifest.json")
records = json.loads(Path(sys.argv[1]).read_text())
def value_at(data, parts):
    for part in parts:
        data = data.get(part) if isinstance(data, dict) else None
    return data

def safe_config_merge(record):
    """Undo only owned config keys, preserving later hook trust and user edits."""
    path = Path(record["path"])
    old = tomllib.loads(Path(record["backup"]).read_text()) if record["backup"] else {}
    now_text = path.read_text()
    now = tomllib.loads(now_text)
    policy = json.loads((path.parent / "auto-compute" / "policy.json").read_text())
    expected = {("model",): policy["coordinator"]["model"],
                ("model_reasoning_effort",): policy["coordinator"]["effort"],
                ("features", "hooks"): True, ("features", "multi_agent"): True,
                ("agents", "enabled"): True}
    for parts, installed in expected.items():
        if value_at(now, parts) != installed:
            raise ValueError("An installed setting was changed later: " + ".".join(parts))
    lines = now_text.splitlines(keepends=True)
    table = ""
    found = set()
    revised = []
    for line in lines:
        header = re.match(r"^\s*\[(.+?)\]\s*$", line)
        if header:
            table = header.group(1)
            revised.append(line)
            continue
        assignment = re.match(r"^\s*([A-Za-z0-9_-]+)\s*=", line)
        parts = (table, assignment.group(1)) if table and assignment else ((assignment.group(1),) if assignment else ())
        if parts in expected:
            found.add(parts)
            prior = value_at(old, parts)
            if prior is None:
                continue
            if prior != expected[parts]:
                literal = json.dumps(prior) if isinstance(prior, str) else str(prior).lower()
                line = f"{parts[-1]} = {literal}\n"
        revised.append(line)
    if found != set(expected):
        raise ValueError("Installed config keys were not found in expected tables")
    merged = "".join(revised)
    parsed = tomllib.loads(merged)
    for parts in expected:
        if value_at(parsed, parts) != value_at(old, parts):
            raise ValueError("Config merge did not restore " + ".".join(parts))
    return merged.encode()

config_merges = {}
conflicts = []
for record in records:
    path = Path(record["path"])
    if digest(path) == record["installed_hash"]:
        continue
    if path.name == "config.toml" and record["backup"]:
        try:
            config_merges[record["path"]] = safe_config_merge(record)
            continue
        except (OSError, ValueError, KeyError, TypeError):
            pass
    conflicts.append(record["path"])
if conflicts:
    raise SystemExit("Nothing restored: these files changed after installation. Merge manually to preserve those edits:\n" + "\n".join(conflicts))
for r in records:
    target = Path(r["path"])
    if r["path"] in config_merges:
        target.write_bytes(config_merges[r["path"]])
        target.chmod(0o600)
    elif r["backup"]:
        target.write_bytes(Path(r["backup"]).read_bytes())
        target.chmod(0o600)
    else:
        target.unlink(missing_ok=True)
for directory in {Path(r["path"]).parent for r in records}:
    try:
        directory.rmdir()  # Empty directories only; no recursive deletion.
    except OSError:
        pass
print("Restored the previous files. Restart Codex. Audit state and backups remain local.")
