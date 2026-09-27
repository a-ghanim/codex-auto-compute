# Live verification before global installation

1. Run the package tests in a source checkout: `python3 -m unittest discover -s tests -q`.
2. Run `codex-auto-compute plan` and inspect the exact proposed config changes.
3. Create an empty disposable project and run `codex-auto-compute fixture /absolute/path/to/project`.
4. In a Codex CLI session inside that project, review and trust the fixture hooks through `/hooks`. Do not bypass trust.
5. Run one bounded, read-only task through two distinct `ac_*` workers with an objective test for each. Inspect `.codex/auto-compute/ledger/ledger.jsonl`. Both `phase_completed` records must show matching observed model and effort, `switch_verified: true`, and `verified_pass` for the checks. Missing token fields remain unknown.
6. Run `codex-auto-compute install --verified-fixture /absolute/path/to/project`. The installer enforces the two-pin gate and writes a backup manifest.
7. Start a fresh Codex session. Review and trust global hooks through `/hooks`. Confirm a new worker phase in `codex-auto-compute status` before treating routing as live on this machine.

The installer preserves existing authentication, project overrides, approval settings, and sandbox settings. It never creates an API key or changes permissions. Actual per-phase credit debits are unavailable from observed telemetry.
