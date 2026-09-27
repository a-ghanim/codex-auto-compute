import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_auto_compute.scripts import tui


class TuiDataTests(unittest.TestCase):
    def test_sessions_newest_first_and_phase_completion_replaces_start(self):
        rows = [
            {"event": "phase_started", "session_id": "old", "phase_id": "p", "role": "ac_quick"},
            {"event": "phase_started", "session_id": "new", "phase_id": "q", "role": "ac_execute"},
            {"event": "phase_completed", "session_id": "new", "phase_id": "q", "role": "ac_execute", "switch_verified": True},
        ]
        grouped = tui.sessions(rows)
        self.assertEqual([id for id, _ in grouped], ["new", "old"])
        self.assertEqual(len(tui.phases(grouped[0][1])), 1)
        self.assertIn("verified", tui.phase_label(tui.phases(grouped[0][1])[0]))

    def test_missing_telemetry_stays_unknown(self):
        lines = tui.detail_lines([
            {"event": "phase_completed", "session_id": "s", "phase_id": "p", "role": "ac_quick", "outcome": "unknown"},
        ], 0)
        self.assertIn("  Observed:  unknown / unknown", lines)
        self.assertIn("  Tokens:    unknown", lines)
        self.assertIn("  Check:     unknown", lines)
        self.assertIn("Actual per-phase credit debit: unavailable", lines)

    def test_objective_failure_is_visible_without_claiming_credit(self):
        lines = tui.detail_lines([
            {"event": "phase_completed", "session_id": "s", "phase_id": "p", "role": "ac_diagnose",
             "observed_model": "gpt-6-sol", "observed_effort": "high", "switch_verified": True,
             "outcome": "verified_fail", "tokens": {"total_tokens": 123},
             "checks": [{"kind": "test", "result": "fail"}]},
        ], 0)
        self.assertIn("  Check:     verified_fail", lines)
        self.assertIn("  Objective: test=fail", lines)
        self.assertIn("  Tokens:    123", lines)


if __name__ == "__main__":
    unittest.main()
