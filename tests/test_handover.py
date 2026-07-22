import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.claude_cli import ClaudeResult
from orchestrator.config import Config
from orchestrator.handover import build_handover
from orchestrator.ledger import Ledger
from orchestrator.plan import Step


def dev_result(summary="did the thing"):
    return ClaudeResult(ok=True, text=summary, session_id="d1", cost_usd=0.1,
                        structured={"summary": summary, "files_changed": ["a.py"],
                                    "commands_run": ["pytest -q"], "concerns": []},
                        raw={})


class TestHandover(unittest.TestCase):
    def setUp(self):
        repo = Path(tempfile.mkdtemp())
        (repo / "app.py").write_text("x = 1\n")
        (repo / "tests").mkdir()
        (repo / "tests" / "test_app.py").write_text("def test_x(): assert True\n")
        self.cfg = Config(repo=repo)
        self.ledger = Ledger.load_or_start(self.cfg)
        self.repo = self.cfg.repo

    def step(self, goal="change app", verify=None):
        return Step(id="1", goal=goal, acceptance_criteria=["works"],
                    verify=verify or ["python3 -c 'import app'"], attempts=1)

    def test_noop_diff_flag(self):
        ho = build_handover(self.step(), dev_result(), True, "ALL OK", self.ledger, self.cfg)
        self.assertTrue(any("NO_OP_DIFF" in f for f in ho.flags))
        self.assertIn("NO_OP_DIFF", ho.text)

    def test_tests_touched_flag(self):
        (self.repo / "tests" / "test_app.py").write_text("def test_x(): assert 1\n")
        ho = build_handover(self.step(), dev_result(), True, "log", self.ledger, self.cfg)
        self.assertTrue(any("TESTS_TOUCHED" in f for f in ho.flags))

    def test_tests_touched_suppressed_when_step_is_about_tests(self):
        (self.repo / "tests" / "test_app.py").write_text("def test_y(): assert 2\n")
        ho = build_handover(self.step(goal="add tests for app"), dev_result(), True,
                            "log", self.ledger, self.cfg)
        self.assertFalse(any("TESTS_TOUCHED" in f for f in ho.flags))

    def test_gate_targets_touched_flag(self):
        (self.repo / "run_checks.sh").write_text("exit 0\n")
        ho = build_handover(self.step(verify=["bash run_checks.sh"]), dev_result(), True,
                            "log", self.ledger, self.cfg)
        self.assertTrue(any("GATE_TARGETS_TOUCHED" in f for f in ho.flags))

    def test_packet_contains_ground_truth_sections(self):
        (self.repo / "app.py").write_text("x = 2\n")
        ho = build_handover(self.step(), dev_result("changed x"), True,
                            "$ pytest\n[ok]", self.ledger, self.cfg)
        self.assertTrue(ho.gates_passed)
        self.assertIn("ALL GATES PASSED", ho.text)
        self.assertIn("x = 2", ho.text)            # the real diff
        self.assertIn("changed x", ho.text)        # dev summary
        self.assertIn("$ pytest", ho.text)         # gate transcript
        self.assertEqual(ho.bytes, len(ho.text.encode()))

    def test_gates_failed_packet(self):
        (self.repo / "app.py").write_text("x = 3\n")
        ho = build_handover(self.step(), dev_result(), False, "[FAILED: exit 1]",
                            self.ledger, self.cfg)
        self.assertIn("GATES FAILED", ho.text)

    def test_dev_error_included(self):
        ho = build_handover(self.step(), None, False, "", self.ledger, self.cfg,
                            dev_error="[claude CLI timed out after 3600s]")
        self.assertIn("timed out", ho.text)
        self.assertIn("(no developer output)", ho.text)

    # --- C3: advisory verification-depth flags (never high_risk) ---
    def test_weak_verification_flag_on_new_route_with_unit_only_gate(self):
        (self.repo / "app.py").write_text("x = 1\n@app.route('/goals')\ndef goals(): return 'ok'\n")
        ho = build_handover(self.step(verify=["pytest -q"]), dev_result(), True, "log",
                            self.ledger, self.cfg)
        self.assertTrue(any("WEAK_VERIFICATION" in f for f in ho.flags))
        self.assertFalse(ho.high_risk)  # advisory only — must not force integrity_ack

    def test_weak_verification_suppressed_with_flow_gate(self):
        (self.repo / "app.py").write_text("x = 1\n@app.route('/goals')\ndef goals(): return 'ok'\n")
        v = ["python3 -m orchestrator.probe flow --file flow.json --base-url http://localhost:8080"]
        ho = build_handover(self.step(verify=v), dev_result(), True, "log", self.ledger, self.cfg)
        self.assertFalse(any("WEAK_VERIFICATION" in f for f in ho.flags))

    def test_weak_isolation_flag_on_tenant_data(self):
        (self.repo / "app.py").write_text("x = 1\nrows = q.filter(Goal.tenant_id == tid)\n")
        ho = build_handover(self.step(verify=["pytest -q"]), dev_result(), True, "log",
                            self.ledger, self.cfg)
        self.assertTrue(any("WEAK_ISOLATION" in f for f in ho.flags))
        self.assertFalse(ho.high_risk)

    def test_weak_isolation_suppressed_with_isolation_gate(self):
        (self.repo / "app.py").write_text("x = 1\nrows = q.filter(Goal.tenant_id == tid)\n")
        v = ["python3 -m orchestrator.probe isolation --file iso.json --base-url http://x"]
        ho = build_handover(self.step(verify=v), dev_result(), True, "log", self.ledger, self.cfg)
        self.assertFalse(any("WEAK_ISOLATION" in f for f in ho.flags))


if __name__ == "__main__":
    unittest.main()
