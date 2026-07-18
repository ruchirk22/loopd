import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import workspace


def git_repo() -> Path:
    repo = Path(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q"], cwd=repo)
    (repo / "a.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=repo)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
                   cwd=repo)
    return repo


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.p = mock.patch.dict("os.environ", {"LOOPD_HOME": str(self.home)})
        self.p.start()
        self.addCleanup(self.p.stop)
        self.repo = git_repo()

    def test_register_and_recent(self):
        workspace.register(self.repo)
        r = workspace.recent()
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["name"], self.repo.name)

    def test_record_run_accumulates(self):
        workspace.register(self.repo)
        workspace.record_run(self.repo, 0, 1.5)
        workspace.record_run(self.repo, 0, 2.0)
        e = workspace.recent()[0]
        self.assertEqual(e["runs"], 2)
        self.assertAlmostEqual(e["lifetime_cost_usd"], 3.5)
        self.assertEqual(e["last_code"], 0)

    def test_recent_skips_deleted_paths(self):
        workspace.register(self.repo)
        workspace.register(Path(tempfile.mkdtemp()))  # a dir we then remove
        # remove the second dir's path from disk
        data = json.loads((self.home / "projects.json").read_text())
        ghost = [e for e in data["projects"] if e["path"] != str(self.repo.resolve())][0]
        Path(ghost["path"]).rmdir()
        names = [e["path"] for e in workspace.recent()]
        self.assertIn(str(self.repo.resolve()), names)
        self.assertNotIn(ghost["path"], names)

    def test_record_run_creates_entry_if_missing(self):
        workspace.record_run(self.repo, 3, 0.4)  # never registered first
        e = workspace.recent()[0]
        self.assertEqual(e["runs"], 1)
        self.assertEqual(e["last_code"], 3)


class TestHealthAndState(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.p = mock.patch.dict("os.environ", {"LOOPD_HOME": str(self.home)})
        self.p.start()
        self.addCleanup(self.p.stop)
        self.repo = git_repo()

    def test_health_clean(self):
        h = workspace.health(self.repo)
        self.assertTrue(h["is_repo"])
        self.assertFalse(h["dirty"])

    def test_health_dirty(self):
        (self.repo / "b.txt").write_text("new\n")
        h = workspace.health(self.repo)
        self.assertTrue(h["dirty"])
        self.assertGreaterEqual(h["dirty_count"], 1)

    def test_health_non_repo(self):
        self.assertFalse(workspace.health(Path(tempfile.mkdtemp()))["is_repo"])

    def test_run_state_none(self):
        self.assertFalse(workspace.run_state(self.repo)["exists"])

    def test_run_state_paused_and_finished(self):
        ad = self.repo / ".agentic"
        ad.mkdir()
        state = {"task": "add auth", "finished": False, "total_cost_usd": 3.0, "budget_usd": 25,
                 "plan": {"steps": [{"status": "done"}, {"status": "pending"}]}}
        (ad / "state.json").write_text(json.dumps(state))
        rs = workspace.run_state(self.repo)
        self.assertTrue(rs["paused"])
        self.assertEqual(rs["steps_done"], 1)
        self.assertEqual(rs["steps_total"], 2)
        self.assertEqual(rs["task"], "add auth")
        state["finished"] = True
        (ad / "state.json").write_text(json.dumps(state))
        self.assertFalse(workspace.run_state(self.repo)["paused"])

    def test_summary_shape(self):
        workspace.register(self.repo)
        s = workspace.summary(self.repo)
        for k in ("name", "runs", "forecast_accuracy", "memory_count", "health", "run_state"):
            self.assertIn(k, s)
        self.assertIsNone(s["forecast_accuracy"])  # no graded runs yet


if __name__ == "__main__":
    unittest.main()
