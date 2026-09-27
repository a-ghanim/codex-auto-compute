#!/usr/bin/env python3
"""Native Codex checkpoint hooks. No network calls, no inference, no permission grants.

Hook coverage and model compliance are not complete enforcement boundaries. This
script blocks excessive observed spawn calls; it does not impose a billing cap.
"""
from __future__ import annotations
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from accounting import task_category, explicit_feedback, usage_from_rollout, objective_check, sanitize_record, extract_exit_code, checks_from_rollout
from routing import select_role

try:
    import fcntl  # macOS / Linux. Native Windows is intentionally not supported.
except ImportError:
    fcntl = None

VERSION = "0.1.0"

def context(event: str, text: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}

def deny(text: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
        "permissionDecision": "deny", "permissionDecisionReason": text}}

def exit_code(response: Any) -> int | None:
    return extract_exit_code(response)

def last_runtime_context(path: Any) -> dict[str, Any]:
    """Best-effort metadata only. The rollout format is NOT a stable API.

    Never inspect reasoning items or copy transcript messages to the audit file.
    The caller must treat an empty result as unknown, not verified.
    """
    if not isinstance(path, str) or not path:
        return {}
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            length = handle.tell()
            handle.seek(max(0, length - 1_048_576))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(record, dict) or record.get("type") != "turn_context":
                continue
            p = record.get("payload", {})
            if not isinstance(p, dict):
                continue
            model = p.get("model")
            effort = p.get("effort", p.get("reasoning_effort", p.get("model_reasoning_effort")))
            if isinstance(model, str):
                return {"model": model, "effort": effort if isinstance(effort, str) else None}
    except (OSError, ValueError):
        pass
    return {}

def _record(directory: Path | None, row: dict) -> None:
    if directory is None:
        return
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "ledger.jsonl"
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps(sanitize_record({"schema": 1, **row}), separators=(",", ":")) + "\n")
    path.chmod(0o600)


def _history(directory: Path | None) -> list[dict]:
    if directory is None or not (directory / "ledger.jsonl").exists():
        return []
    try:
        lines = (directory / "ledger.jsonl").read_text().splitlines()[-500:]
        return [json.loads(line) for line in lines if line]
    except (OSError, ValueError):
        return []


def handle(payload: dict[str, Any], state: dict[str, Any], policy: dict[str, Any],
           ledger_dir: Path | None = None) -> dict[str, Any]:
    """Pure policy function except optional metadata read at SubagentStop."""
    event = payload.get("hook_event_name", "")
    roles = policy.get("roles", {})
    if event in ("SessionStart", "UserPromptSubmit"):
        if event == "UserPromptSubmit":
            old_turn = state.get("last_completed_turn")
            feedback = explicit_feedback(payload.get("prompt"))
            if old_turn and feedback != "unknown" and old_turn != payload.get("turn_id"):
                _record(ledger_dir, {"event": "feedback", "session_id": payload.get("session_id"),
                                     "turn_id": old_turn, "feedback": feedback})
            state["category"] = task_category(payload.get("prompt"))
            state["next_phase"] = "diagnosis" if state["category"] == "debug" else (
                "execution" if state["category"] == "implementation" else "routine")
            # A new user turn gets a new launch allowance. Never use Stop loops
            # to manufacture new turns and reset the allowance.
            turn = payload.get("turn_id")
            if turn and turn != state.get("turn_id"):
                state.update(turn_id=turn, launches=0, spawn_ids=[], tools=0, failures={})
        suggestion = select_role(state.get("category", "unknown"), state.get("next_phase", "routine"),
                                 roles, _history(ledger_dir))
        out = context(event, "Apply auto-compute automatically for this task: trivial work stays local; "
                      "delegate substantive phases to the configured ac_* role; re-route at checkpoints. "
                      f"Current provisional phase suggestion: {suggestion['role']} ({suggestion['state']}). "
                      "Read the auto-compute skill if it is not already in context. "
                      "If you are a delegated worker, do not recurse: complete one phase and return its checkpoint. "
                      "Do not ask the user to select models or efforts.")
        expected = policy.get("coordinator", {}).get("model")
        actual = payload.get("model")
        if event == "UserPromptSubmit" and expected and actual and actual != expected:
            out["systemMessage"] = ("Auto-compute: this chat's coordinator is " + str(actual)
                + ", not the installed default " + str(expected)
                + ". Worker routing still applies, but parent-model savings are unverified. "
                "The hook cannot change the current chat's model.")
        return out

    if event == "SubagentStart":
        role = payload.get("agent_type")
        if role not in roles:
            return {}
        phase_id = str(payload.get("agent_id") or f"{payload.get('turn_id','unknown')}:{role}:{time.time_ns()}")
        state.setdefault("active_phases", {})[phase_id] = {
            "role": role, "started": time.time(), "checks": [], "retries": 0}
        state["active_phase"] = phase_id
        _record(ledger_dir, {"event": "phase_started", "session_id": payload.get("session_id"),
                             "turn_id": payload.get("turn_id"), "phase_id": phase_id,
                             "agent_id": payload.get("agent_id"), "role": role,
                             "category": state.get("category", "unknown"),
                             "requested_model": roles[role]["model"],
                             "requested_effort": roles[role]["effort"],
                             "credit_debit": None, "credit_source": "unavailable"})
        return context(event, "You are a bounded auto-compute worker. Do not spawn workers. "
                       "Return STATUS, RESULT, EVIDENCE, OPEN, NEXT at the next meaningful checkpoint. "
                       "If the phase needs more reasoning or is now routine, hand it back instead of continuing "
                       "indefinitely on this configuration. Never change approval or sandbox permissions.")

    if event == "PreToolUse":
        tool = str(payload.get("tool_name", "")).split(".")[-1]
        if tool not in ("spawn_agent", "Agent"):
            return {}
        args = payload.get("tool_input", {})
        args = args if isinstance(args, dict) else {}
        role = args.get("agent_type", args.get("subagent_type"))
        if isinstance(role, str) and role.startswith("ac_") and role not in roles:
            return deny("Requested auto-compute role is unavailable. Use an installed role or report the limitation; "
                        "do not silently inherit the parent configuration.")
        call_id = payload.get("tool_use_id")
        seen = state.setdefault("spawn_ids", [])
        if call_id and call_id in seen:
            return {}
        limit = int(policy.get("max_worker_launches_per_turn", 12))
        if int(state.get("launches", 0)) >= limit:
            return deny("Auto-compute worker-launch cap reached. Preserve progress and report the remaining blocker. "
                        "Do not bypass through subprocesses or new turns. This is not a billing cap.")
        state["launches"] = int(state.get("launches", 0)) + 1
        if call_id:
            seen.append(call_id)
        return {}

    if event == "PostToolUse":
        tool = str(payload.get("tool_name", "")).split(".")[-1]
        if tool in ("wait_agent", "wait"):
            suggestion = select_role(state.get("category", "unknown"), state.get("next_phase", "execution"),
                                     roles, _history(ledger_dir), blocker=state.get("next_phase") == "blocked")
            return context(event, "Delegation checkpoint: inspect completed worker results and evidence. "
                f"Suggested next role: {suggestion['role']} ({suggestion['state']}). "
                "Automatically select the right configured role for the NEXT phase, escalating reasoning "
                "only when needed and returning settled routine work to a cheaper role. "
                "A still-running worker is not a completed phase; do not duplicate its work. "
                "Keep one writer at a time and close completed workers.")
        state["tools"] = int(state.get("tools", 0)) + 1
        args = payload.get("tool_input", {})
        args = args if isinstance(args, dict) else {}
        check = objective_check(tool, args, payload.get("tool_response"))
        active = state.get("active_phases", {}).get(state.get("active_phase"))
        if active is not None and check is not None:
            active["checks"].append(check)
        elif check is not None:
            state.setdefault("root_checks", []).append(check)
        command = args.get("command", args.get("cmd", ""))
        code = exit_code(payload.get("tool_response"))
        # Search commands can return nonzero simply because there were no matches.
        # Treat the observed exit status as a signal, not a judgment of correctness.
        if code is not None and code != 0 and isinstance(command, str) and command:
            key = hashlib.sha256(command.encode()).hexdigest()[:24]
            failures = state.setdefault("failures", {})
            failures[key] = int(failures.get(key, 0)) + 1
            if active is not None:
                active["retries"] += 1
            if failures[key] == 2:
                return context(event, "Checkpoint: the same command has returned nonzero twice. "
                    "First distinguish an expected search miss or environment/authentication problem from a "
                    "reasoning failure. Do not repeat the same hypothesis. A worker should return evidence "
                    "and the blocker; the coordinator should re-route only if more reasoning can help.")
        every = int(policy.get("checkpoint_every_tools", 8))
        if every > 0 and state["tools"] % every == 0:
            return context(event, "Compute checkpoint: reassess the NEXT phase, not the whole task. "
                "If diagnosis is settled, hand routine execution to a cheaper configured role. "
                "If evidence defeats the current approach, return a concise handoff for stronger reasoning. "
                "Do not interrupt an unfinished side-effecting operation or blindly replay it.")
        return {}

    if event == "SubagentStop":
        role = payload.get("agent_type")
        if role not in roles:
            return {}
        expected = roles[role]
        telemetry = usage_from_rollout(payload.get("agent_transcript_path"))
        observed = {"model": telemetry["observed_model"], "effort": telemetry["observed_effort"]}
        if observed["model"] is None:
            observed = last_runtime_context(payload.get("agent_transcript_path"))
        record = {"role": role, "expected": {"model": expected["model"], "effort": expected["effort"]},
                  "observed": observed, "verified": bool(observed and observed.get("model") == expected["model"]
                                                         and observed.get("effort") == expected["effort"])}
        state.setdefault("observations", []).append(record)
        state["observations"] = state["observations"][-100:]
        phase_id = str(payload.get("agent_id") or state.get("active_phase") or "unknown")
        phase = state.get("active_phases", {}).pop(phase_id, {})
        if state.get("active_phase") == phase_id:
            state.pop("active_phase", None)
        checks = phase.get("checks", [])
        transcript_checks = checks_from_rollout(payload.get("agent_transcript_path"))
        if transcript_checks and (not checks or all(c["result"] == "unknown" for c in checks)):
            checks = transcript_checks
        checkpoint = payload.get("last_assistant_message")
        if isinstance(checkpoint, str):
            status = re.search(r"(?im)^STATUS:\s*(ready_for_execution|needs_reasoning|blocked)\b", checkpoint)
            if status:
                state["next_phase"] = {"ready_for_execution": "execution",
                                       "needs_reasoning": "hard", "blocked": "blocked"}[status.group(1).lower()]
        outcome = "verified_pass" if checks and all(c["result"] == "pass" for c in checks) else (
            "verified_fail" if any(c["result"] == "fail" for c in checks) else "unknown")
        _record(ledger_dir, {"event": "phase_completed", "session_id": payload.get("session_id"),
                             "turn_id": payload.get("turn_id"), "phase_id": phase_id,
                             "agent_id": payload.get("agent_id"), "role": role,
                             "category": state.get("category", "unknown"),
                             "requested_model": expected["model"], "requested_effort": expected["effort"],
                             "observed_model": telemetry["observed_model"],
                             "observed_effort": telemetry["observed_effort"],
                             "switch_verified": record["verified"],
                             "started_at": telemetry["started_at"], "ended_at": telemetry["ended_at"],
                             "duration_sec": round(time.time() - phase["started"], 3) if "started" in phase else None,
                             "tokens": telemetry["tokens"], "usage_source": telemetry["usage_source"],
                             "usage_scope": "worker_only",
                             "credit_debit": None, "credit_source": "unavailable", "checks": checks,
                             "retries": phase.get("retries", 0), "outcome": outcome,
                             "decision_state": "provisional"})
        # SubagentStop's documented continuation controls are deliberately not
        # used: the worker should STOP so the coordinator can select another role.
        # Re-routing context is delivered on the parent's wait-tool completion.
        if observed and not record["verified"]:
            return {"systemMessage": "Auto-compute: observed worker configuration does not match its role pin. Stop delegating until inspected."}
        if not record["verified"]:
            return {"systemMessage": "Auto-compute: worker model/effort is unverified because runtime metadata was unavailable."}
        return {}
    if event == "Stop":
        telemetry = usage_from_rollout(payload.get("transcript_path"), payload.get("turn_id"))
        checks = state.pop("root_checks", [])
        transcript_checks = checks_from_rollout(payload.get("transcript_path"))
        if transcript_checks and (not checks or all(c["result"] == "unknown" for c in checks)):
            checks = transcript_checks
        outcome = "verified_pass" if checks and all(c["result"] == "pass" for c in checks) else (
            "verified_fail" if any(c["result"] == "fail" for c in checks) else "unknown")
        _record(ledger_dir, {"event": "turn_completed", "session_id": payload.get("session_id"),
                             "turn_id": payload.get("turn_id"), "category": state.get("category", "unknown"),
                             "observed_model": telemetry["observed_model"],
                             "observed_effort": telemetry["observed_effort"],
                             "tokens": telemetry["tokens"], "usage_source": telemetry["usage_source"],
                             "usage_scope": "aggregate_including_workers",
                             "credit_debit": None, "credit_source": "unavailable", "checks": checks,
                             "outcome": outcome})
        state["last_completed_turn"] = payload.get("turn_id")
        return {}
    return {}

@contextlib.contextmanager
def locked_state(directory: Path, session: str):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(session.encode()).hexdigest()
    path = directory / (key + ".json")
    with (directory / (key + ".lock")).open("a+") as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = json.loads(path.read_text()) if path.exists() else {}
            if not isinstance(state, dict):
                raise ValueError("Invalid auto-compute state")
            yield state
            fd, temp = tempfile.mkstemp(prefix="state-", dir=directory)
            try:
                with os.fdopen(fd, "w") as out:
                    json.dump(state, out)
                os.replace(temp, path)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
        finally:
            if fcntl is not None:
                fcntl.flock(lock, fcntl.LOCK_UN)

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, default=Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser())
    args = parser.parse_args()
    payload: dict[str, Any] = {}
    try:
        raw = sys.stdin.read(2_097_153)
        if len(raw) > 2_097_152:
            raise ValueError("Hook input exceeds 2 MB safety limit")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Hook input must be an object")
        policy = json.loads((args.home / "auto-compute" / "policy.json").read_text())
        with locked_state(args.home / "auto-compute" / "state", str(payload.get("session_id", "unknown"))) as state:
            result = handle(payload, state, policy, args.home / "auto-compute" / "ledger")
        print(json.dumps(result))
        return 0
    except Exception as exc:
        message = "Auto-compute hook unavailable: " + str(exc)
        if payload.get("hook_event_name") == "PreToolUse":
            tool = str(payload.get("tool_name", "")).split(".")[-1]
            if tool in ("spawn_agent", "Agent"):
                print(json.dumps(deny(message + ". Delegation blocked rather than silently bypassing the guard.")))
                return 0
        print(json.dumps({"systemMessage": message}))
        return 0

if __name__ == "__main__":
    raise SystemExit(main())
