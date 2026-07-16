import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.gates import run_gates


class TestGates(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_empty_list_fails(self):
        ok, log = run_gates([], self.tmp)
        self.assertFalse(ok)
        self.assertIn("no verify commands", log)
        ok, _ = run_gates(["", "   "], self.tmp)
        self.assertFalse(ok)

    def test_pass_and_fail(self):
        ok, log = run_gates(["test -d ."], self.tmp)
        self.assertTrue(ok)
        self.assertIn("[ok]", log)
        ok, log = run_gates(["test -f does-not-exist"], self.tmp)
        self.assertFalse(ok)
        self.assertIn("FAILED: exit 1", log)

    def test_stops_at_first_failure(self):
        ok, log = run_gates(["false", "touch after.txt"], self.tmp)
        self.assertFalse(ok)
        self.assertFalse((self.tmp / "after.txt").exists())

    def test_timeout_prefix(self):
        ok, log = run_gates(["timeout=1;sleep 5"], self.tmp)
        self.assertFalse(ok)
        self.assertIn("TIMEOUT after 1s", log)

    def test_setup_failure_fails_gate_and_teardown_always_runs(self):
        ok, log = run_gates(["touch check.txt"], self.tmp,
                            setup=["false"], teardown=["touch torn.txt"])
        self.assertFalse(ok)
        self.assertFalse((self.tmp / "check.txt").exists())
        self.assertTrue((self.tmp / "torn.txt").exists())

    def test_setup_check_teardown_order(self):
        ok, log = run_gates(["echo check >> order.txt", "test -f order.txt"], self.tmp,
                            setup=["echo setup >> order.txt"],
                            teardown=["echo teardown >> order.txt"])
        self.assertTrue(ok)
        lines = (self.tmp / "order.txt").read_text().split()
        self.assertEqual(lines, ["setup", "check", "teardown"])

    def test_teardown_failure_does_not_flip_pass(self):
        ok, log = run_gates(["true && test -d ."], self.tmp, teardown=["false"])
        self.assertTrue(ok)
        self.assertIn("teardown failure ignored", log)


if __name__ == "__main__":
    unittest.main()
