import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_auto_compute import install, maintenance


def entry(name, description, efforts=("low", "medium", "high", "xhigh")):
    return {"model": name, "description": description, "hidden": False,
            "defaultReasoningEffort": "medium",
            "supportedReasoningEfforts": [{"reasoningEffort": e} for e in efforts]}


OLD = [entry("old-quick", "Fast and affordable model for easier tasks."),
       entry("old-strong", "Workhorse model for coding and everyday work.")]


class MaintenanceTests(unittest.TestCase):
    def test_new_catalog_names_are_selected_by_metadata(self):
        policy = install.select_policy([entry("next-tiny", "Fast and affordable model for easier tasks."),
                                        entry("next-main", "Workhorse model for coding and everyday work.")])
        self.assertEqual(policy["roles"]["ac_quick"]["model"], "next-tiny")
        self.assertEqual(policy["roles"]["ac_execute"]["model"], "next-main")

    def test_existing_valid_pins_survive_new_release(self):
        old = install.select_policy(OLD)
        newer = [entry("new-quick", "Fast and affordable model for easier tasks."),
                 entry("new-strong", "Workhorse model for coding and everyday work."), *OLD]
        proposed, changes = maintenance.proposed_refresh(old, newer)
        self.assertEqual(changes, [])
        self.assertEqual(proposed, old)

    def test_retired_model_gets_successor_and_resets_verification(self):
        old = install.select_policy(OLD)
        updated = [entry("new-quick", "Fast and affordable model for easier tasks."), OLD[1]]
        proposed, changes = maintenance.proposed_refresh(old, updated)
        self.assertEqual([c["role"] for c in changes], ["ac_quick"])
        self.assertEqual(proposed["roles"]["ac_quick"]["model"], "new-quick")
        self.assertFalse(proposed["live_routing_verified"])
        self.assertEqual(proposed["unverified_roles"], ["ac_quick"])

    def test_ambiguous_replacement_blocks(self):
        old = install.select_policy(OLD)
        with self.assertRaisesRegex(ValueError, "unambiguous available quick"):
            maintenance.proposed_refresh(old, [entry("mystery", "A model."), OLD[1]])

    def test_refresh_preserves_other_settings_and_backs_up_pins(self):
        old = install.select_policy(OLD)
        proposed, _ = maintenance.proposed_refresh(old, [entry("new-quick", "Fast and affordable model for easier tasks."), OLD[1]])
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "auto-compute").mkdir()
            (home / "agents").mkdir()
            (home / "auto-compute" / "policy.json").write_text(json.dumps(old))
            for role, pin in old["roles"].items():
                (home / "agents" / f"{role}.toml").write_text(install.agent_toml(role, pin))
            (home / "config.toml").write_text('approval_policy = "on-request"\n')
            backup = maintenance.apply_refresh(home, old, proposed)
            self.assertTrue(backup.is_file())
            self.assertIn('model = "new-quick"', (home / "agents" / "ac_quick.toml").read_text())
            self.assertIn("on-request", (home / "config.toml").read_text())

    def test_user_modified_agent_blocks_refresh_without_writes(self):
        old = install.select_policy(OLD)
        proposed, _ = maintenance.proposed_refresh(old, [entry("new-quick", "Fast and affordable model for easier tasks."), OLD[1]])
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "auto-compute").mkdir()
            (home / "agents").mkdir()
            path = home / "auto-compute" / "policy.json"
            path.write_text(json.dumps(old))
            for role, pin in old["roles"].items():
                (home / "agents" / f"{role}.toml").write_text(install.agent_toml(role, pin))
            (home / "agents" / "ac_quick.toml").write_text(install.agent_toml("ac_quick", {**old["roles"]["ac_quick"], "model": "my-choice"}))
            with self.assertRaisesRegex(RuntimeError, "edited outside"):
                maintenance.apply_refresh(home, old, proposed)
            self.assertEqual(json.loads(path.read_text()), old)

    def test_refresh_keeps_custom_agent_instructions(self):
        old = install.select_policy(OLD)
        proposed, _ = maintenance.proposed_refresh(old, [entry("new-quick", "Fast and affordable model for easier tasks."), OLD[1]])
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "auto-compute").mkdir()
            (home / "agents").mkdir()
            (home / "auto-compute" / "policy.json").write_text(json.dumps(old))
            for role, pin in old["roles"].items():
                text = install.agent_toml(role, pin)
                if role == "ac_quick":
                    text += "# local comment retained\n"
                (home / "agents" / f"{role}.toml").write_text(text)
            maintenance.apply_refresh(home, old, proposed)
            updated = (home / "agents" / "ac_quick.toml").read_text()
            self.assertIn('model = "new-quick"', updated)
            self.assertIn("# local comment retained", updated)

    def test_retired_coordinator_updates_only_its_existing_pin(self):
        old = install.select_policy(OLD)
        proposed, _ = maintenance.proposed_refresh(old, [OLD[0], entry("new-strong", "Workhorse model for coding and everyday work.")])
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "auto-compute").mkdir()
            (home / "agents").mkdir()
            (home / "auto-compute" / "policy.json").write_text(json.dumps(old))
            for role, pin in old["roles"].items():
                (home / "agents" / f"{role}.toml").write_text(install.agent_toml(role, pin))
            config = home / "config.toml"
            config.write_text('model = "old-strong"\nmodel_reasoning_effort = "medium"\napproval_policy = "on-request"\n')
            class FakeRPC:
                def call(self, method, params):
                    self.assert_called = method
                    self.edits = params["edits"]
                    config.write_text('model = "new-strong"\nmodel_reasoning_effort = "medium"\napproval_policy = "on-request"\n')
            rpc = FakeRPC()
            maintenance.apply_refresh(home, old, proposed, rpc)
            self.assertEqual(rpc.assert_called, "config/batchWrite")
            self.assertEqual({e["keyPath"] for e in rpc.edits}, {"model", "model_reasoning_effort"})
            self.assertIn('approval_policy = "on-request"', config.read_text())

    def test_user_main_model_override_blocks_coordinator_replacement(self):
        old = install.select_policy(OLD)
        proposed, _ = maintenance.proposed_refresh(old, [OLD[0], entry("new-strong", "Workhorse model for coding and everyday work.")])
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "auto-compute").mkdir()
            (home / "auto-compute" / "policy.json").write_text(json.dumps(old))
            (home / "config.toml").write_text('model = "my-choice"\nmodel_reasoning_effort = "medium"\n')
            with self.assertRaisesRegex(RuntimeError, "changed separately"):
                maintenance.apply_refresh(home, old, proposed, object())
            self.assertEqual(json.loads((home / "auto-compute" / "policy.json").read_text()), old)

    def test_ledger_age_labels_historical_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            path.write_text("{}\n")
            os.utime(path, (1000, 1000))
            self.assertEqual(maintenance.ledger_age(path, now=4700)["ledger"], "historical")
            self.assertEqual(maintenance.ledger_age(path, now=1100)["ledger"], "recent")

    def test_unavailable_worker_telemetry_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            path.write_text(json.dumps({"event": "phase_completed", "session_id": "s", "switch_verified": False}) + "\n")
            self.assertEqual(maintenance.latest_telemetry(path), "unverified")
            with path.open("a") as out:
                out.write(json.dumps({"event": "phase_completed", "session_id": "s2", "switch_verified": True}) + "\n")
            self.assertEqual(maintenance.latest_telemetry(path), "verified")


if __name__ == "__main__":
    unittest.main()
