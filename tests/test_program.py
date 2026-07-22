import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import program as P
from orchestrator.claude_cli import ClaudeResult
from orchestrator.config import Config


def cfg_for():
    return Config(repo=Path(tempfile.mkdtemp()))


class FakeEpicRunner:
    """Stand-in for loop.run: records each call and returns queued exit codes."""
    def __init__(self, codes):
        self.codes = list(codes)
        self.calls = []

    def __call__(self, brief, cfg, fresh=False, resume=False):
        self.calls.append({"brief": brief, "fresh": fresh, "resume": resume})
        return self.codes.pop(0) if self.codes else 0


def decompose3(cfg, prd, ledger=None):
    return [{"id": "a", "title": "Data model", "objective": "obj a"},
            {"id": "b", "title": "API", "objective": "obj b"},
            {"id": "c", "title": "UI", "objective": "obj c"}]


class TestDecompose(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg_for()

    def _fake(self, structured, ok=True):
        return lambda *a, **k: ClaudeResult(ok=ok, text="", session_id=None, cost_usd=0.0,
                                            structured=structured, raw={})

    def test_maps_epics(self):
        with mock.patch.object(P, "run_claude", self._fake({"epics": [
                {"id": "x", "title": "X", "objective": "do x"}]})):
            epics = P.decompose(self.cfg, "prd")
        self.assertEqual([e["id"] for e in epics], ["x"])
        self.assertEqual(epics[0]["objective"], "do x")

    def test_skips_empty_objective_and_dedups_ids(self):
        with mock.patch.object(P, "run_claude", self._fake({"epics": [
                {"id": "x", "title": "X", "objective": "a"},
                {"id": "x", "title": "Y", "objective": ""},      # dropped (no objective)
                {"id": "x", "title": "Z", "objective": "c"}]})):  # id collides → deduped
            epics = P.decompose(self.cfg, "prd")
        self.assertEqual(len(epics), 2)
        self.assertNotEqual(epics[0]["id"], epics[1]["id"])

    def test_failure_returns_empty(self):
        with mock.patch.object(P, "run_claude", self._fake(None, ok=False)):
            self.assertEqual(P.decompose(self.cfg, "prd"), [])


class TestRunProgram(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg_for()

    def test_runs_all_epics_in_order_fresh(self):
        r = FakeEpicRunner([0, 0, 0])
        rc = P.run_program("build the app", self.cfg, run_epic=r, decompose_fn=decompose3)
        self.assertEqual(rc, 0)
        self.assertEqual(len(r.calls), 3)
        self.assertTrue(all(c["fresh"] and not c["resume"] for c in r.calls))
        prog = P.load(self.cfg.repo)
        self.assertEqual([e["status"] for e in prog["epics"]], ["done", "done", "done"])
        self.assertIn("obj a", r.calls[0]["brief"])   # epic objective reaches the run

    def test_failure_stops_and_resumes(self):
        r = FakeEpicRunner([0, 1, 0])
        rc = P.run_program("x", self.cfg, run_epic=r, decompose_fn=decompose3)
        self.assertEqual(rc, 1)
        self.assertEqual([e["status"] for e in P.load(self.cfg.repo)["epics"]],
                         ["done", "failed", "pending"])
        self.assertEqual(len(r.calls), 2)

        # resume: skip the done epic, resume the failed one, then run the pending one fresh
        r2 = FakeEpicRunner([0, 0])
        rc2 = P.run_program(None, self.cfg, resume=True, run_epic=r2, decompose_fn=decompose3)
        self.assertEqual(rc2, 0)
        self.assertEqual(len(r2.calls), 2)
        self.assertTrue(r2.calls[0]["resume"] and not r2.calls[0]["fresh"])   # failed → resumed
        self.assertTrue(r2.calls[1]["fresh"] and not r2.calls[1]["resume"])   # pending → fresh
        self.assertTrue(all(e["status"] == "done" for e in P.load(self.cfg.repo)["epics"]))

    def test_single_epic_degrades_to_one_run(self):
        r = FakeEpicRunner([0])
        rc = P.run_program("small task", self.cfg, run_epic=r,
                           decompose_fn=lambda c, p, ledger=None: [{"id": "o", "title": "T", "objective": "o"}])
        self.assertEqual(rc, 0)
        self.assertEqual(len(r.calls), 1)
        self.assertEqual(r.calls[0]["brief"], "small task")   # ran the task, not an epic brief
        self.assertFalse(P.exists(self.cfg.repo))             # no program.json for one unit

    def test_no_prd_returns_setup_error(self):
        r = FakeEpicRunner([0])
        rc = P.run_program(None, self.cfg, run_epic=r, decompose_fn=decompose3)
        self.assertEqual(rc, 2)
        self.assertEqual(len(r.calls), 0)

    def test_per_epic_forecast_is_disabled(self):
        self.cfg.forecast_enabled = True
        P.run_program("x", self.cfg, run_epic=FakeEpicRunner([0, 0, 0]), decompose_fn=decompose3)
        self.assertFalse(self.cfg.forecast_enabled)


class TestCheckpointHelpers(unittest.TestCase):
    def test_noninteractive_auto_continues_and_approves(self):
        cfg = cfg_for()  # tests run with no TTY → auto-proceed
        self.assertEqual(P._epic_checkpoint(cfg, {"title": "x"}), "continue")
        self.assertTrue(P._approve_plan(cfg))

    def test_state_roundtrip(self):
        cfg = cfg_for()
        prog = {"epics": [{"id": "a", "title": "A", "objective": "o", "status": "pending"}]}
        P.save(cfg.repo, prog)
        self.assertTrue(P.exists(cfg.repo))
        self.assertEqual(P.load(cfg.repo)["epics"][0]["id"], "a")


if __name__ == "__main__":
    unittest.main()
