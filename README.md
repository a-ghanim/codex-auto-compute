# Codex Auto Compute

Conservative, local routing for Codex worker phases, with an evidence-labeled usage and outcome ledger. It uses a ChatGPT-signed-in Codex CLI and its native custom agents and hooks. The main chat keeps its chosen model; only delegated workers use different pins.

**Status: alpha.** Verified on one Mac with two distinct live worker pins in a disposable fixture and a fresh global session. The routing policy is provisional. It does not measure attributable credit debits, prove savings, enforce a spending cap, or guarantee every task delegates.

## Install and verify

Requirements: Python 3.11+, macOS or Linux, a current Codex CLI signed into ChatGPT, custom-agent and hook support, and two suitable models in the live account catalog. API-key billing is refused.

```sh
python3 -m pip install codex-auto-compute
codex-auto-compute plan
mkdir /tmp/auto-compute-fixture
codex-auto-compute fixture /tmp/auto-compute-fixture
```

Review the fixture's hooks through Codex `/hooks`. Run one bounded live task using two distinct worker pins with objective checks, then inspect the fixture ledger. The installer will refuse global application unless that ledger records both observed settings and passing checks. See [the full verification procedure](https://github.com/a-ghanim/codex-auto-compute/blob/main/INSTALL_FOR_CODEX.md).

```sh
codex-auto-compute install --verified-fixture /tmp/auto-compute-fixture
```

Review global hooks through `/hooks` and verify a fresh session. The installer saves a backup manifest. To restore it later:

```sh
codex-auto-compute uninstall /absolute/path/to/manifest.json
```

## See what ran

```sh
codex-auto-compute status
codex-auto-compute tui
```

`tui` opens a read-only, automatically refreshing terminal screen. Use up/down to choose a session, left/right to inspect worker phases, `r` to refresh, and `q` to quit. Run it in a terminal beside Codex CLI; Codex's own chat screen cannot embed another terminal application. `status` prints the latest recorded session when you need plain output. Both commands support `--ledger /path/to/ledger.jsonl` for a disposable fixture.

## Model and telemetry changes

```sh
codex-auto-compute doctor
codex-auto-compute refresh
```

The TUI checks the live Codex model catalog on launch and every five minutes, and labels the age of its ledger. `doctor` reports missing models or effort settings. `refresh` preserves valid pins, selects a successor only when the catalog clearly identifies the same role, runs a small direct CLI smoke check for each new pin, backs up managed files, then updates only those pins. It refuses edited agent files and separate main-model overrides. New custom-agent pins remain explicitly **unverified** until a fresh worker phase confirms runtime metadata; restart Codex after a refresh. A catalog with no clear successor blocks routing instead of guessing. New model releases do not automatically replace working pins.

The coordinator skill instructs Codex to run the catalog check before its first worker launch in a fresh session and invoke `refresh` when a pin has disappeared. This is instruction-driven, so `doctor` is also available as a deterministic CLI check. A changed pin is used only in a new session. The refresh probe verifies the **observed** direct CLI model and effort from Codex's runtime record; the subsequent custom-agent invocation still needs its own runtime match.

This handles routine name changes without a hardcoded model-version list. It cannot guarantee compatibility with an undocumented future catalog or hook schema. Missing runtime telemetry is reported as unknown, and old ledger entries are labeled historical rather than live.

The views show requested or observed model and effort, objective check labels, available token totals, and clear unknowns. The main chat's token total includes worker usage; do not add it to worker totals. The ledger is local at `~/.codex/auto-compute/ledger/ledger.jsonl` and excludes raw task text.

Codex rollout record structure is not a stable public API. Verify after Codex updates. Hook coverage is best effort, and this package has not been tested on another user's machine. The source is available under the MIT license.
