import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


MODULE = Path(__file__).resolve().parents[1] / "src" / "codex_auto_compute" / "scripts" / "status.py"
spec = importlib.util.spec_from_file_location("auto_compute_status", MODULE)
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)


class StatusTests(unittest.TestCase):
    def test_missing_ledger_is_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn("No routing records", status.render(status.read_rows(Path(tmp) / "none")))

    def test_shows_observed_worker_without_claiming_credit_debit(self):
        rows = [
            {"event": "phase_started", "session_id": "s", "turn_id": "a", "phase_id": "p", "role": "ac_quick", "requested_model": "luna", "requested_effort": "low"},
            {"event": "phase_completed", "session_id": "s", "turn_id": "a", "phase_id": "p", "role": "ac_quick", "observed_model": "luna", "observed_effort": "low", "switch_verified": True, "outcome": "unknown", "tokens": {"total_tokens": 42}},
            {"event": "turn_completed", "session_id": "s", "turn_id": "b", "observed_model": "sol", "observed_effort": "medium"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows))
            report = status.render(status.read_rows(path))
        self.assertIn("ac_quick: luna/low (runtime verified)", report)
        self.assertIn("check unknown; 42 tokens", report)
        self.assertIn("Main chat: sol/medium", report)
        self.assertIn("credit debit: unavailable", report)

    def test_pending_and_missing_runtime_remain_unverified(self):
        rows = [{"event": "phase_started", "session_id": "s", "turn_id": "a", "phase_id": "p", "role": "ac_execute", "requested_model": "sol", "requested_effort": "medium"},
                {"event": "phase_completed", "session_id": "s", "turn_id": "a", "phase_id": "q", "role": "ac_review", "observed_model": None, "observed_effort": None, "switch_verified": False}]
        report = status.render(rows)
        self.assertIn("runtime pending", report)
        self.assertIn("unknown/unknown (runtime unverified)", report)
