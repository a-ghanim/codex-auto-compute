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

The views show requested or observed model and effort, objective check labels, available token totals, and clear unknowns. The main chat's token total includes worker usage; do not add it to worker totals. The ledger is local at `~/.codex/auto-compute/ledger/ledger.jsonl` and excludes raw task text.

Codex rollout record structure is not a stable public API. Verify after Codex updates. Hook coverage is best effort, and this package has not been tested on another user's machine. The source is available under the MIT license.
