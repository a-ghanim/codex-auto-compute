---
name: auto-compute
description: Automatically allocate work among model-and-effort-pinned workers before tasks and at checkpoints. Use for substantive tasks without asking the user to select models. Trivial tasks stay with the coordinator.
---

# Automatic compute allocation

## Scope and operating rule

This is native Codex delegation, not a mechanism to change the parent chat's model.
The coordinator uses the conservative installed default. Named workers have model and effort
pinned in their custom agent files. Select a worker yourself; do not ask the user to
choose a model, effort, tier, or whether to escalate. Never claim a switch happened
without a successful delegated call. If the tools or configured agents are missing,
report routing unavailable once; do not simulate a model switch in prose.

If you are already a delegated worker: execute only the assigned phase and return
its result. Do not delegate recursively or load this skill again.

## Before work

For trivial, self-contained transformations or conversational replies, answer
locally. Do not add a routing call, a plan, or a reviewer to an obvious one-step task.
For substantive work, identify the next meaningful phase, its acceptance criteria,
and its error consequences. Read the active auto-compute policy.json once per session
for the installed roles (global: ~/.codex/auto-compute/policy.json; disposable fixture:
.codex/auto-compute/policy.json). Missing roles are unavailable, not invitations to
invent model names. Routing is provisional until comparable outcomes and costs exist.

In a fresh coordinator session, run `codex-auto-compute doctor` once before the first
worker launch. This checks the installed pins against Codex's live model catalog.
If it reports `needs_refresh`, run `codex-auto-compute refresh`: it chooses a clear
successor, runs bounded live pin checks, and backs up managed files. Do not ask the
user to choose models or run checks. A changed pin is unverified as a custom agent
until a fresh worker phase confirms runtime metadata. Do not use changed pins in
the current session; continue locally where possible and report that a fresh
session is needed for routing. If the catalog cannot be checked or no clear
successor exists, report routing unavailable instead of guessing.

Use these roles, not a mandatory ladder:
- ac_quick: narrow extraction, formatting, mechanical work, simple inspection.
- ac_execute: well-specified implementation, ordinary debugging, synthesis.
- ac_diagnose: ambiguity, architecture, conflicting evidence, difficult diagnosis.
- ac_deep: a genuinely reasoning-limited phase that ac_diagnose did not resolve.
- ac_review: independent review of consequential or hard-to-test results.
- ac_frontier, only when present: one bounded hard-problem attempt after deep
  reasoning failed, or when the initial problem has comparable difficulty.

The installation may map several roles to the same available model at different
efforts. Do not interpret role labels as measured price or capability scores.

Do not send an obviously difficult task through every cheap tier first. Conversely,
do not make the coordinator solve the hard task and then pay a worker to repeat it.

## Delegation contract

Delegate ONE bounded phase at a time. Pass the objective, acceptance criteria,
relevant facts, exact file/source references, previous failed hypotheses, and any
permission boundaries. Keep the handoff short, but preserve evidence required for
correctness. Do not dump the entire chat into every worker. Workers may retrieve
underlying files when summaries omit important details.

Keep one writer at a time. Use parallel agents only for genuinely independent work
where a concrete latency benefit justifies the extra inference. Close completed
workers. Reuse a worker only for another phase that needs the SAME configured role;
a role change requires a new worker. Do not claim changing a role label changes an
already-running worker's model.

Every worker returns a brief checkpoint:
STATUS: done | continue | needs_reasoning | ready_for_execution | blocked
RESULT: conclusion or changed artifacts, with exact paths/references
EVIDENCE: observed tests/checks and outcomes; distinguish unrun checks
OPEN: unresolved issues and failed hypotheses
NEXT: next bounded phase, or none

This is an external work summary, not private chain-of-thought. The worker must not
assert which model/effort actually ran; only runtime telemetry can establish that.

## Re-route during the task

At a returned checkpoint, repeated test failure, or material change in uncertainty:
1. Diagnose the kind of obstacle. Missing credentials, permissions, data, broken
   dependencies, rate limits, and unavailable tools are not evidence that a more
   expensive model would help. Resolve the prerequisite or report the blocker.
2. Escalate a reasoning-limited phase when evidence contradicts the current approach
   or a materially distinct attempt fails. Avoid retrying the same hypothesis.
3. Once diagnosis/design is settled and the next phase is routine, move that next
   phase to ac_quick or ac_execute. Do not keep an expensive worker alive for cleanup.
4. Maintain continuity with RESULT, EVIDENCE, OPEN, NEXT. Preserve existing edits;
   do not replay external actions simply because the model changed.
5. Apply hysteresis: do not switch for every command. Switch at a meaningful phase
   boundary, not on arbitrary self-reported confidence or elapsed time alone.

You may escalate automatically within the installed policy. Do not ask the user
which model to use. Explicit user model restrictions override the heuristic.

## Verification and safety

Use existing tests, compilation, schema checks, source checks, or reproducible
examples where applicable. A passing test suite is evidence for the properties it
covers, not proof of correctness. The author's confidence is not verification.
For consequential or poorly testable output, get an independent ac_review pass
against the original requirements and evidence. Do not pay for a reviewer on every
trivial task. If a review finds missing evidence, gather it before increasing effort.

A larger model does not authorize payments, sends, deployment, deletion, or access
changes. Preserve all sandbox, connector, and human-approval requirements. Before
continuing after interruption, check actual side effects; never assume rollback.

Avoid runaway work: at most two materially distinct failed approaches at one role;
then escalate, revise scope, or stop with the blocker. Stay within the worker-launch
cap in policy.json. The hook enforces only supported spawn-tool paths; this is NOT
a dollar cap, a token cap, or an inference-time limit. Do not evade it through shell
subprocesses, unregistered workers, a new turn, or a renamed tool.

## Honest accounting

A model/effort in a TOML file is a request, not proof of execution. Runtime records
must match before calling the routing installation verified. Unavailable model or
unsupported effort: fail visibly rather than silently using an expensive parent.

Same-model reuse may reduce handoff overhead. Never promise cross-model cache hits.
The local ledger records observed settings, available token categories, duration,
retries, objective checks, and explicit later feedback without raw task content.
An unknown check or absent feedback is not a failure or success label. Root turn
usage includes workers; do not add root and worker token totals together. Actual
credit debit is unavailable unless the account exposes an attributable value.
Do not calculate credit savings from token counts or quota-window changes. Until
credit and quality evidence are sufficient, use the conservative provisional
policy and do not claim optimality or a hard spending cap.

Make model selection visible in the chat. Before launching a worker, give a short
status update with its role and requested model/effort; label this as requested,
not running or verified. At each completed phase, use the ledger's phase_completed
record for that worker to check observed_model, observed_effort, and
switch_verified. If the record is absent or ambiguous, say the observed setting
is unknown. Do not infer it from the role name, agent file, or worker report.

For a substantive routed task, include a compact line in the final answer such
as "Models used: main chat gpt-6-sol/medium; worker ac_quick gpt-6-luna/low
(runtime verified)." Read the main chat's own runtime metadata before naming its
model and effort; if unavailable, say "main chat: not verified." Report only
workers used in that task. If there was no worker, say "No worker switch" when
the user asks about routing. The app's main model picker describes the parent
chat, not every worker phase. Keep the rest of the answer focused on the work,
blockers, and verification limits.
