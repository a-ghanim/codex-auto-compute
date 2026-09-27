"""Privacy-limited, best-effort accounting for Codex rollout metadata.

Rollout JSONL is not a stable API. Unknown fields stay unknown; in particular,
token counts and account quota changes are never called credit debits.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

TOKEN_KEYS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
              "output_tokens", "reasoning_output_tokens", "total_tokens")


def task_category(prompt: Any) -> str:
    if not isinstance(prompt, str):
        return "unknown"
    p = prompt.lower()
    for category, words in (
        ("debug", ("debug", "fix", "failing", "error", "bug")),
        ("implementation", ("implement", "build", "add feature", "refactor")),
        ("review", ("review", "audit", "inspect")),
        ("research", ("research", "compare", "find sources")),
        ("writing", ("draft", "write", "rewrite", "summarize")),
    ):
        if any(re.search(r"\b" + re.escape(w) + r"\b", p) for w in words):
            return category
    return "unknown"


def explicit_feedback(prompt: Any) -> str:
    """Conservative evidence from a later prompt, never a success oracle."""
    if not isinstance(prompt, str):
        return "unknown"
    p = prompt.strip().lower()
    if re.match(r"^(thanks[,!. ]|thank you[,!. ]|looks good[,!. ]|approved[,!. ]|that works[,!. ])", p):
        return "accepted_explicitly"
    if re.match(r"^(please )?(fix|correct|redo|you missed|that (is|was) wrong|still broken)\b", p):
        return "correction_explicit"
    return "unknown"


def usage_from_rollout(path: Any, turn_id: str | None = None) -> dict[str, Any]:
    """Deduplicate incremental response records; avoid cumulative token events."""
    result: dict[str, Any] = {"observed_model": None, "observed_effort": None,
                              "tokens": None, "started_at": None, "ended_at": None,
                              "usage_source": "missing"}
    if not isinstance(path, str) or not path:
        return result
    try:
        p = Path(path)
        if not p.is_file():
            return result
        usage_by_response: dict[str, dict] = {}
        with p.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                payload = row.get("payload")
                if not isinstance(payload, dict):
                    continue
                if row.get("type") == "turn_context":
                    model = payload.get("model")
                    effort = payload.get("effort", payload.get("model_reasoning_effort"))
                    if isinstance(model, str):
                        result["observed_model"] = model
                    if isinstance(effort, str):
                        result["observed_effort"] = effort
                elif row.get("type") == "token_usage_record":
                    if turn_id and payload.get("turn_id") != turn_id:
                        continue
                    usage = payload.get("usage")
                    rid = payload.get("response_id")
                    if isinstance(rid, str) and isinstance(usage, dict):
                        usage_by_response[rid] = usage
                else:
                    continue
                stamp = row.get("timestamp")
                if isinstance(stamp, str):
                    result["started_at"] = result["started_at"] or stamp
                    result["ended_at"] = stamp
        if usage_by_response:
            result["tokens"] = {key: sum(int(u[key]) for u in usage_by_response.values()
                                              if isinstance(u.get(key), int) and not isinstance(u[key], bool))
                                for key in TOKEN_KEYS}
            result["usage_source"] = "rollout_response_records"
    except OSError:
        pass
    return result


def objective_check(tool: Any, args: Any, response: Any) -> dict | None:
    if not isinstance(args, dict):
        return None
    command = args.get("cmd", args.get("command"))
    if not isinstance(command, str):
        return None
    cmd = command.lower()
    kind = None
    if re.search(r"\b(pytest|unittest|npm test|pnpm test|cargo test|go test)\b", cmd):
        kind = "test"
    elif re.search(r"\b(npm run build|pnpm build|cargo build|go build|tsc)\b", cmd):
        kind = "build"
    if kind is None:
        return None
    code = extract_exit_code(response)
    return {"kind": kind, "result": "pass" if code == 0 else "fail" if code is not None else "unknown"}


def extract_exit_code(value: Any, depth: int = 0) -> int | None:
    if depth > 5:
        return None
    if isinstance(value, dict):
        for key in ("exitCode", "exit_code", "returncode"):
            code = value.get(key)
            if isinstance(code, int) and not isinstance(code, bool):
                return code
        for key in ("content", "output", "text", "result", "structuredContent"):
            if key in value:
                found = extract_exit_code(value[key], depth + 1)
                if found is not None:
                    return found
    elif isinstance(value, list):
        for item in value:
            found = extract_exit_code(item, depth + 1)
            if found is not None:
                return found
    elif isinstance(value, str):
        match = re.search(r"(?:Process exited with code\s+|(?m:^exit_code\s*=\s*))(-?\d+)\b", value)
        if match:
            return int(match.group(1))
        if value.strip().startswith("{"):
            try:
                return extract_exit_code(json.loads(value), depth + 1)
            except ValueError:
                pass
    return None


def checks_from_rollout(path: Any) -> list[dict]:
    """Recover objective command results when a hook's tool output is opaque."""
    if not isinstance(path, str) or not path:
        return []
    calls: dict[str, str] = {}
    checks: list[dict] = []
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("type") != "response_item":
                    continue
                item = row.get("payload", {})
                if not isinstance(item, dict):
                    continue
                kind = item.get("type")
                call_id = item.get("call_id")
                if not isinstance(call_id, str):
                    continue
                if kind in ("custom_tool_call", "function_call"):
                    inp = item.get("input", item.get("arguments"))
                    if isinstance(inp, str):
                        calls[call_id] = inp
                elif kind in ("custom_tool_call_output", "function_call_output") and call_id in calls:
                    check = objective_check(item.get("name"), {"cmd": calls.pop(call_id)}, item.get("output"))
                    if check:
                        checks.append(check)
    except OSError:
        pass
    return checks


def sanitize_record(record: dict) -> dict:
    """Allowlist prevents accidental storage of prompt, paths, or tool output."""
    allowed = ("schema", "event", "session_id", "turn_id", "phase_id", "agent_id",
               "role", "category", "requested_model", "requested_effort", "observed_model",
               "observed_effort", "switch_verified", "started_at", "ended_at", "duration_sec",
               "tokens", "usage_source", "usage_scope", "credit_debit", "credit_source", "checks", "retries",
               "outcome", "feedback", "decision_state")
    return {k: record[k] for k in allowed if k in record}
