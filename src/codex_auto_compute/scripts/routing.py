"""Conservative routing until comparable quality and attributable cost exist."""
from __future__ import annotations

from math import sqrt
from statistics import mean
from typing import Any

QUALITY_FLOOR = 0.8
MARGIN = 0.05
MIN_COMPARABLE = 8


def _lower_success_bound(successes: int, n: int) -> float:
    if n == 0:
        return 0.0
    z = 1.64
    p = successes / n
    return max(0.0, (p + z*z/(2*n) - z*sqrt((p*(1-p) + z*z/(4*n))/n)) / (1 + z*z/n))


def select_role(category: str, phase: str, roles: dict, history: list[dict] | None = None,
                failed_roles: list[str] | None = None, blocker: bool = False) -> dict[str, Any]:
    """Return a role and evidence state; never equate a role label with a price.

    A measured decision needs sufficient comparable completed phases, explicit
    success evidence and attributable credit debits. Otherwise it remains a
    conservative provisional decision.
    """
    if blocker:
        return {"role": None, "state": "blocked", "reason": "external_prerequisite"}
    history = history or []
    failed = set(failed_roles or [])
    order = {
        "routine": ("ac_quick", "ac_execute", "ac_diagnose", "ac_deep", "ac_frontier"),
        "execution": ("ac_execute", "ac_quick", "ac_diagnose", "ac_deep", "ac_frontier"),
        "diagnosis": ("ac_diagnose", "ac_execute", "ac_deep", "ac_frontier"),
        "hard": ("ac_deep", "ac_diagnose", "ac_frontier"),
        "review": ("ac_review", "ac_diagnose"),
    }.get(phase, ("ac_execute", "ac_diagnose", "ac_deep"))
    available = [r for r in order if r in roles and r not in failed]
    if not available:
        return {"role": None, "state": "blocked", "reason": "no_available_role"}

    candidates = []
    for role in available:
        rows = [r for r in history if r.get("category") == category and r.get("role") == role
                and r.get("outcome") in ("verified_pass", "verified_fail")
                and isinstance(r.get("credit_debit"), (int, float))]
        if len(rows) < MIN_COMPARABLE:
            continue
        successes = sum(r["outcome"] == "verified_pass" for r in rows)
        quality = _lower_success_bound(successes, len(rows))
        if quality + MARGIN < QUALITY_FLOOR:
            continue
        observed = mean(float(r["credit_debit"]) for r in rows)
        verify = mean(float(r.get("verification_credit_debit") or 0) for r in rows)
        retry = mean(float(r.get("retry_credit_debit") or 0) for r in rows)
        # Expected debit per successful completion, including verification/retry.
        candidates.append((observed + verify + retry) / max(quality, 0.01), role)
    if candidates:
        _, selected = min(candidates)
        return {"role": selected, "state": "measured", "reason": "expected_credit_per_verified_success"}
    return {"role": available[0], "state": "provisional", "reason": "insufficient_comparable_outcomes_or_credit_telemetry"}
