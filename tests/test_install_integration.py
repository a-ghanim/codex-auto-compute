"""End-to-end installer checks against a FAKE metadata server, not live Codex."""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1] / "src" / "codex_auto_compute"
FAKE = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
home=pathlib.Path(os.environ["CODEX_HOME"])
for line in sys.stdin:
    m=json.loads(line)
    if "id" not in m: continue
    method=m.get("method")
    if method == "initialize": result={"userAgent":"test-only"}
    elif method == "account/read": result={"account":{"type":"chatgpt","planType":"pro"}}
    elif method == "model/list":
        result={"data":[{"model":x,"defaultReasoningEffort":"medium","supportedReasoningEfforts":[{"reasoningEffort":e} for e in ("low","medium","high","xhigh")]} for x in ("gpt-5.6-luna","gpt-6-sol","gpt-6-astra")],"nextCursor":None}
    elif method == "config/batchWrite":
        edits=m["params"]["edits"]
        assert all(e["mergeStrategy"]=="replace" for e in edits)
        assert not any("permission" in e["keyPath"] or "approval" in e["keyPath"] or "sandbox" in e["keyPath"] for e in edits)
        (home/"observed_edits.json").write_text(json.dumps(edits))
        p=home/"config.toml"
        p.write_text((p.read_text() if p.exists() else "")+"\n# fake-server write committed\n")
        result={}
    else:
        print(json.dumps({"id":m["id"],"error":{"message":"unsupported fake method"}}),flush=True)
        continue
    print(json.dumps({"id":m["id"],"result":result}),flush=True)
'''
class InstallerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.home = self.base / ".codex"
        self.home.mkdir()
        self.original = b'# existing settings\napproval_policy = "on-request"\n'
        (self.home / "config.toml").write_bytes(self.original)
        self.binary = self.base / "fake-codex"
        self.binary.write_text(FAKE)
        self.binary.chmod(0o700)
        self.env = dict(os.environ, HOME=str(self.base), CODEX_HOME=str(self.home))
    def tearDown(self):
        self.temp.cleanup()
    def install(self, apply=False):
        cmd=[sys.executable,str(ROOT/"install.py"),"--codex",str(self.binary),"--home",str(self.home)]
        if apply:
            fixture=self.base/"verified-fixture"/".codex"/"auto-compute"/"ledger"
            fixture.mkdir(parents=True,exist_ok=True)
            rows=[]
            for role,model,effort in (("ac_quick","gpt-5.6-luna","low"),("ac_execute","gpt-6-sol","medium")):
                rows.append({"event":"phase_completed","session_id":"same-session","role":role,
                             "outcome":"verified_pass","switch_verified":True,
                             "observed_model":model,"observed_effort":effort,
                             "checks":[{"kind":"test","result":"pass"}]})
            (fixture/"ledger.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
            cmd.extend(("--apply","--verified-fixture",str(self.base/"verified-fixture")))
        return subprocess.run(cmd,env=self.env,text=True,capture_output=True,timeout=15)
    def test_apply_without_fixture_is_refused(self):
        result=subprocess.run([sys.executable,str(ROOT/"install.py"),"--codex",str(self.binary),
                               "--home",str(self.home),"--apply"],env=self.env,text=True,capture_output=True,timeout=15)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual((self.home/"config.toml").read_bytes(),self.original)
    def manifest(self):
        return next((self.home/"auto-compute-backups").glob("*/manifest.json"))
    def test_dry_run_does_not_write(self):
        result=self.install()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((self.home/"config.toml").read_bytes(),self.original)
        self.assertFalse((self.home/"auto-compute").exists())
    def test_fixture_stages_without_global_changes(self):
        fixture = self.base / "fixture"
        fixture.mkdir()
        result = subprocess.run([sys.executable, str(ROOT/"install.py"), "--codex", str(self.binary),
                                 "--home", str(self.home), "--fixture", str(fixture)],
                                env=self.env, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((fixture/".codex"/"agents"/"ac_execute.toml").exists())
        self.assertTrue((fixture/".codex"/"auto-compute"/"accounting.py").exists())
        self.assertTrue((fixture/".codex"/"auto-compute"/"status.py").exists())
        self.assertEqual((self.home/"config.toml").read_bytes(), self.original)
    def test_apply_and_restore(self):
        result=self.install(True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue((self.home/"agents"/"ac_deep.toml").exists())
        self.assertTrue((self.home/"skills"/"auto-compute"/"SKILL.md").exists())
        self.assertTrue((self.home/"auto-compute"/"status.py").exists())
        result=subprocess.run([sys.executable,str(ROOT/"uninstall.py"),str(self.manifest())],env=self.env,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((self.home/"config.toml").read_bytes(),self.original)
        self.assertFalse((self.home/"agents"/"ac_deep.toml").exists())
        self.assertFalse((self.home/"skills"/"auto-compute").exists())
        self.assertFalse((self.home/"auto-compute"/"status.py").exists())
    def test_restore_does_not_discard_subsequent_user_changes(self):
        result=self.install(True)
        self.assertEqual(result.returncode,0,result.stderr)
        path=self.home/"config.toml"
        altered=path.read_text()+"\n# later user change\n"
        path.write_text(altered)
        result=subprocess.run([sys.executable,str(ROOT/"uninstall.py"),str(self.manifest())],env=self.env,text=True,capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(path.read_text(),altered)
        self.assertTrue((self.home/"agents"/"ac_deep.toml").exists())
    def test_uninstall_preserves_later_unrelated_config_and_hook_trust(self):
        config=self.home/"config.toml"
        prior=b'model = "gpt-6-sol"\nmodel_reasoning_effort = "medium"\n[features]\nmemories = true\n'
        installed=prior+b'hooks = true\nmulti_agent = true\n[agents]\nenabled = true\n'
        current=installed+b'[desktop]\nconversationDetailMode = "expanded"\n[hooks.state.example]\ntrusted_hash = "abc"\n'
        config.write_bytes(current)
        policy=self.home/"auto-compute"/"policy.json"
        policy.parent.mkdir()
        policy.write_text(json.dumps({"coordinator":{"model":"gpt-6-sol","effort":"medium"}}))
        backup=self.base/"config.original"
        backup.write_bytes(prior)
        manifest=self.base/"manifest.json"
        sha=lambda b: hashlib.sha256(b).hexdigest()
        manifest.write_text(json.dumps([
            {"path":str(config),"backup":str(backup),"installed_hash":sha(installed)},
            {"path":str(policy),"backup":None,"installed_hash":sha(policy.read_bytes())}]))
        result=subprocess.run([sys.executable,str(ROOT/"uninstall.py"),str(manifest)],text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        final=config.read_text()
        self.assertIn('trusted_hash = "abc"',final)
        self.assertIn('conversationDetailMode = "expanded"',final)
        self.assertNotIn('hooks = true',final)
        self.assertNotIn('multi_agent = true',final)
        self.assertNotIn('enabled = true',final)
