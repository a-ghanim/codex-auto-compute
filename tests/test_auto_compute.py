import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import tomllib
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1] / "src" / "codex_auto_compute"
sys.path.insert(0, str(ROOT / "scripts"))
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
G = load("governor", ROOT / "scripts" / "governor.py")
I = load("installer", ROOT / "install.py")
import accounting as A
import routing as R

def catalog():
    return [{"model": name, "hidden": False, "defaultReasoningEffort": "medium",
             "supportedReasoningEfforts": [{"reasoningEffort": x} for x in ("low", "medium", "high", "xhigh")]}
            for name in ("gpt-5.6-luna", "gpt-6-sol", "gpt-6-astra")]

def payload(event, **kw):
    return dict(hook_event_name=event, session_id="session", turn_id="turn", **kw)

class ModelTests(unittest.TestCase):
    def test_selects_available_models(self):
        p = I.select_policy(catalog())
        self.assertEqual(p["roles"]["ac_quick"]["model"], "gpt-5.6-luna")
        self.assertEqual(p["coordinator"], {"model": "gpt-6-sol", "effort": "medium"})
        self.assertEqual(p["roles"]["ac_deep"]["effort"], "xhigh")
    def test_missing_frontier_is_not_invented(self):
        self.assertNotIn("ac_frontier", I.select_policy(catalog()[:2])["roles"])
    def test_unknown_catalog_fails(self):
        with self.assertRaises(ValueError):
            I.select_policy([])
    def test_missing_effort_metadata_fails(self):
        c = catalog(); c[0]["supportedReasoningEfforts"] = []
        with self.assertRaises(ValueError):
            I.select_policy(c)
    def test_unsupported_effort_falls_back_to_actual_supported_default(self):
        c = catalog()[0]
        c["supportedReasoningEfforts"] = [{"reasoningEffort": "medium"}]
        self.assertEqual(I.effort(c, "xhigh"), "medium")
    def test_hidden_models_not_chosen(self):
        c = catalog(); c[0]["hidden"] = True
        with self.assertRaises(ValueError):
            I.select_policy(c)
    def test_unverified_by_default(self):
        self.assertFalse(I.select_policy(catalog())["live_routing_verified"])
    def test_no_billing_cap_claim(self):
        self.assertIsNone(I.select_policy(catalog())["billing_cap"])
    def test_toml_pins_model_and_effort(self):
        p = I.select_policy(catalog())
        for name, role in p["roles"].items():
            parsed = tomllib.loads(I.agent_toml(name, role))
            self.assertEqual(parsed["model"], role["model"])
            self.assertEqual(parsed["model_reasoning_effort"], role["effort"])
            self.assertIn("Do not spawn subagents", parsed["developer_instructions"])
    def test_reviewer_is_read_only_requested(self):
        r = I.select_policy(catalog())["roles"]["ac_review"]
        self.assertEqual(tomllib.loads(I.agent_toml("ac_review", r))["sandbox_mode"], "read-only")
    def test_execute_does_not_raise_permissions(self):
        r = I.select_policy(catalog())["roles"]["ac_execute"]
        d = tomllib.loads(I.agent_toml("ac_execute", r))
        self.assertNotIn("sandbox_mode", d)
        self.assertNotIn("approval_policy", d)

class HookTests(unittest.TestCase):
    def setUp(self):
        self.p = I.select_policy(catalog())
        self.s = {}
    def runhook(self, event, **kw):
        return G.handle(payload(event, **kw), self.s, self.p)
    def test_prompt_injects_automatic_workflow(self):
        r = self.runhook("UserPromptSubmit")
        self.assertIn("automatically", r["hookSpecificOutput"]["additionalContext"])
    def test_parent_model_mismatch_not_silently_ignored(self):
        r = self.runhook("UserPromptSubmit", model="gpt-6-astra")
        self.assertIn("cannot change", r["systemMessage"])
    def test_parent_matching_no_warning(self):
        r = self.runhook("UserPromptSubmit", model=self.p["coordinator"]["model"])
        self.assertNotIn("systemMessage", r)
    def test_launch_cap_blocks(self):
        for i in range(self.p["max_worker_launches_per_turn"]):
            r = self.runhook("PreToolUse", tool_name="spawn_agent", tool_use_id=str(i), tool_input={"agent_type": "ac_quick"})
            self.assertEqual(r, {})
        r = self.runhook("PreToolUse", tool_name="spawn_agent", tool_use_id="13", tool_input={"agent_type": "ac_quick"})
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "deny")
    def test_duplicate_call_does_not_double_count(self):
        for _ in range(3):
            self.runhook("PreToolUse", tool_name="spawn_agent", tool_use_id="same", tool_input={})
        self.assertEqual(self.s["launches"], 1)
    def test_unknown_ac_role_denied(self):
        r = self.runhook("PreToolUse", tool_name="Agent", tool_input={"agent_type": "ac_fake"})
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "deny")
    def test_other_tools_never_approved_or_rewritten(self):
        r = self.runhook("PreToolUse", tool_name="Bash", tool_input={"command": "echo test"})
        self.assertEqual(r, {})
    def test_checkpoints_every_eight_tools(self):
        for _ in range(7):
            self.assertEqual(self.runhook("PostToolUse", tool_name="Bash", tool_response={"exitCode": 0}), {})
        r = self.runhook("PostToolUse", tool_name="Bash", tool_response={"exitCode": 0})
        self.assertIn("checkpoint", r["hookSpecificOutput"]["additionalContext"])
    def test_repeated_nonzero_not_blind_escalation(self):
        kw = dict(tool_name="Bash", tool_input={"command": "pytest -q"}, tool_response={"exitCode": 1})
        self.assertEqual(self.runhook("PostToolUse", **kw), {})
        r = self.runhook("PostToolUse", **kw)
        self.assertIn("environment/authentication", r["hookSpecificOutput"]["additionalContext"])
    def test_normal_error_word_not_treated_as_exit_failure(self):
        self.runhook("PostToolUse", tool_name="Bash", tool_input={"command": "echo error"}, tool_response="error")
        self.assertNotIn("failures", self.s)
    def test_only_command_hash_stored(self):
        cmd = "tool --secret VERY_PRIVATE_VALUE"
        self.runhook("PostToolUse", tool_name="Bash", tool_input={"command": cmd}, tool_response={"exitCode": 1})
        self.assertNotIn("VERY_PRIVATE_VALUE", json.dumps(self.s))
    def test_wait_returns_checkpoint_not_new_worker(self):
        r = self.runhook("PostToolUse", tool_name="wait_agent", tool_response={})
        self.assertIn("still-running", r["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("launches", self.s)
    def test_worker_no_recursion(self):
        r = self.runhook("SubagentStart", agent_type="ac_execute")
        self.assertIn("Do not spawn", r["hookSpecificOutput"]["additionalContext"])
    def test_stop_no_continuation_loop(self):
        r = self.runhook("SubagentStop", agent_type="ac_execute")
        self.assertNotIn("decision", r)
        self.assertNotIn("continue", r)
        self.assertNotIn("hookSpecificOutput", r)
    def test_missing_runtime_is_unknown(self):
        self.runhook("SubagentStop", agent_type="ac_execute")
        self.assertFalse(self.s["observations"][-1]["verified"])
    def test_observed_configuration_matched(self):
        role = self.p["roles"]["ac_execute"]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rollout.jsonl"
            path.write_text(json.dumps({"type": "turn_context", "payload": {"model": role["model"], "effort": role["effort"]}}) + "\n")
            r = self.runhook("SubagentStop", agent_type="ac_execute", agent_transcript_path=str(path))
        self.assertTrue(self.s["observations"][-1]["verified"])
        self.assertEqual(r, {})
    def test_wrong_observed_configuration_warns(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rollout.jsonl"
            path.write_text(json.dumps({"type": "turn_context", "payload": {"model": "wrong", "effort": "low"}}))
            r = self.runhook("SubagentStop", agent_type="ac_execute", agent_transcript_path=str(path))
        self.assertIn("does not match", r["systemMessage"])
    def test_new_turn_resets_launch_allowance(self):
        self.runhook("UserPromptSubmit")
        self.s["launches"] = 12
        G.handle(payload("UserPromptSubmit", **{}) | {"turn_id": "next"}, self.s, self.p)
        self.assertEqual(self.s["launches"], 0)
    def test_same_turn_does_not_reset_cap(self):
        self.runhook("UserPromptSubmit")
        self.s["launches"] = 12
        self.runhook("UserPromptSubmit")
        self.assertEqual(self.s["launches"], 12)
    def test_unknown_event_no_op(self):
        self.assertEqual(self.runhook("Nothing"), {})
    def test_exit_code_parsing(self):
        self.assertEqual(G.exit_code("Process exited with code 127"), 127)
        self.assertIsNone(G.exit_code({"exitCode": True}))
        self.assertEqual(G.exit_code({"content": [{"text": "exit_code=0"}]}), 0)

class PersistenceTests(unittest.TestCase):
    def test_existing_hooks_preserved(self):
        old = {"description": "mine", "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "existing"}]}]}}
        original = copy.deepcopy(old)
        result = I.merge_hooks(old, "python governor.py")
        self.assertEqual(old, original)
        self.assertEqual(result["hooks"]["SessionStart"][0], old["hooks"]["SessionStart"][0])
    def test_hooks_no_permission_request_handler(self):
        result = I.merge_hooks({}, "python governor.py")
        self.assertNotIn("PermissionRequest", result["hooks"])
        self.assertIn("Stop", result["hooks"])
    def test_locked_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            with G.locked_state(Path(td), "session") as s:
                s["launches"] = 3
            with G.locked_state(Path(td), "session") as s:
                self.assertEqual(s["launches"], 3)
    def test_transcript_other_items_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "rollout"
            p.write_text(json.dumps({"type": "reasoning", "payload": {"model": "not-runtime-metadata"}}))
            self.assertEqual(G.last_runtime_context(str(p)), {})

if __name__ == "__main__":
    unittest.main()


class AccountingTests(unittest.TestCase):
    def test_missing_telemetry_and_unknown_feedback(self):
        self.assertIsNone(A.usage_from_rollout(None)["tokens"])
        self.assertEqual(A.explicit_feedback("Can you also explain this?"), "unknown")
    def test_explicit_correction_and_acceptance(self):
        self.assertEqual(A.explicit_feedback("Please fix the broken result"), "correction_explicit")
        self.assertEqual(A.explicit_feedback("Looks good, thank you"), "accepted_explicitly")
    def test_usage_deduplicates_and_filters_turn(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "rollout.jsonl"
            rows = [{"type": "turn_context", "payload": {"model": "gpt-6-luna", "effort": "low"}},
                    {"type": "token_usage_record", "payload": {"turn_id": "a", "response_id": "r1", "usage": {"input_tokens": 7}}},
                    {"type": "token_usage_record", "payload": {"turn_id": "a", "response_id": "r1", "usage": {"input_tokens": 7}}},
                    {"type": "token_usage_record", "payload": {"turn_id": "b", "response_id": "r2", "usage": {"input_tokens": 99}}}]
            p.write_text("\n".join(json.dumps(r) for r in rows))
            data = A.usage_from_rollout(str(p), "a")
            self.assertEqual(data["tokens"]["input_tokens"], 7)
            self.assertEqual(data["observed_effort"], "low")
    def test_ledger_allowlist_excludes_task_content(self):
        row = A.sanitize_record({"event": "phase_completed", "prompt": "secret", "tool_output": "secret"})
        self.assertNotIn("secret", json.dumps(row))
    def test_fixture_is_not_a_fix_request(self):
        self.assertEqual(A.task_category("Read the fixture values"), "unknown")
    def test_nested_tool_response_check(self):
        response = {"content": [{"text": '{"exit_code":0,"output":"Ran 1 test"}'}]}
        check = A.objective_check("Bash", {"cmd": "python3 -m unittest test_fixture"}, response)
        self.assertEqual(check, {"kind": "test", "result": "pass"})
    def test_rollout_recovers_objective_check(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"rollout.jsonl"
            rows=[{"type":"response_item","payload":{"type":"custom_tool_call","call_id":"c1","input":"python3 -m unittest test_fixture"}},
                  {"type":"response_item","payload":{"type":"custom_tool_call_output","call_id":"c1","output":[{"text":"exit_code=0"}]}}]
            p.write_text("\n".join(json.dumps(r) for r in rows))
            self.assertEqual(A.checks_from_rollout(str(p)),[{"kind":"test","result":"pass"}])


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.roles = I.select_policy(catalog())["roles"]
    def test_deescalates_after_diagnosis(self):
        self.assertEqual(R.select_role("debug", "diagnosis", self.roles)["role"], "ac_diagnose")
        self.assertEqual(R.select_role("debug", "execution", self.roles)["role"], "ac_execute")
    def test_blocker_does_not_escalate(self):
        self.assertIsNone(R.select_role("debug", "hard", self.roles, blocker=True)["role"])
    def test_sparse_comparisons_stay_provisional(self):
        rows = [{"category": "debug", "role": "ac_execute", "outcome": "verified_pass", "credit_debit": 1}]
        self.assertEqual(R.select_role("debug", "execution", self.roles, rows)["state"], "provisional")
