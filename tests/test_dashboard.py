import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from http.server import ThreadingHTTPServer
import threading

from orchestrator import dashboard
from orchestrator.config import Config
from orchestrator.ledger import Ledger
from orchestrator.plan import Plan, Step


def repo_with_run():
    repo = Path(tempfile.mkdtemp())
    (repo / "app.txt").write_text("x")
    cfg = Config(repo=repo)
    cfg.budget_usd = 20
    led = Ledger.load_or_start(cfg)
    led.start("build a widget")
    led.spend(3.25)
    step = Step(id="1", goal="add widget", acceptance_criteria=["a"], verify=["true"],
                status="done", commit_sha="abcdef1234", cost_usd=3.25, attempts=2)
    pending = Step(id="2", goal="wire it up", acceptance_criteria=["b"], verify=["true"])
    led.save_plan(Plan(summary="do it", steps=[step, pending]))
    led.log({"event": "step_committed", "step": "1", "sha": "abcdef1234"})
    (cfg.state_dir / "brief.md").write_text("# Task\nbuild a widget properly")
    return repo


class TestSnapshot(unittest.TestCase):
    def test_snapshot_of_a_run(self):
        repo = repo_with_run()
        s = dashboard.snapshot(repo, running=True)
        self.assertTrue(s["exists"])
        self.assertTrue(s["running"])
        self.assertEqual(s["budget_usd"], 20)
        self.assertAlmostEqual(s["total_cost_usd"], 3.25)
        self.assertEqual(s["counts"], {"done": 1, "skipped": 0, "total": 2})
        self.assertEqual(s["current_step"]["id"], "2")     # first non-done
        self.assertEqual(s["steps"][0]["commit"], "abcdef123")  # trimmed to 9
        self.assertIn("build a widget properly", s["brief"])
        self.assertTrue(any(e["event"] == "step_committed" for e in s["events"]))

    def test_snapshot_no_run(self):
        empty = Path(tempfile.mkdtemp())
        s = dashboard.snapshot(empty)
        self.assertFalse(s["exists"])
        self.assertEqual(s["events"], [])


class TestBuildCommand(unittest.TestCase):
    def test_new_uses_fresh(self):
        cmd = dashboard.build_run_command("/tmp/x", 8, "new")
        self.assertIn("--fresh", cmd)
        self.assertNotIn("--resume-run", cmd)
        self.assertEqual(cmd[cmd.index("--budget") + 1], "8")
        self.assertIn("--repo", cmd)

    def test_resume_uses_resume_run(self):
        cmd = dashboard.build_run_command("/tmp/x", 8, "resume")
        self.assertIn("--resume-run", cmd)
        self.assertNotIn("--fresh", cmd)


class TestLaunchValidation(unittest.TestCase):
    def test_new_without_task_or_brief_refused(self):
        mgr = dashboard.RunManager()
        repo = Path(tempfile.mkdtemp())
        r = mgr.launch(repo=repo, task="", budget=5, pm_model="", dev_model="", mode="new")
        self.assertFalse(r["ok"])
        self.assertIn("provide a task", r["error"])

    def test_bad_budget_refused(self):
        mgr = dashboard.RunManager()
        repo = Path(tempfile.mkdtemp())
        r = mgr.launch(repo=repo, task="do a thing", budget="lots",
                       pm_model="", dev_model="", mode="new")
        self.assertFalse(r["ok"])
        self.assertIn("budget", r["error"])
        # the task WAS written before the budget check — brief exists
        self.assertTrue((repo / ".agentic" / "brief.md").is_file())


class TestHTTP(unittest.TestCase):
    def setUp(self):
        self.repo = repo_with_run()
        handler = dashboard._make_handler(dashboard.RunManager(), str(self.repo), 20.0)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return r.status, r.read().decode()

    def test_index_served(self):
        code, body = self._get("/")
        self.assertEqual(code, 200)
        self.assertIn("loopd", body)
        self.assertIn("Start a run", body)

    def test_config_endpoint(self):
        code, body = self._get("/api/config")
        d = json.loads(body)
        self.assertEqual(d["default_repo"], str(self.repo))
        self.assertEqual(d["default_budget"], 20.0)

    def test_state_endpoint(self):
        code, body = self._get("/api/state?repo=" + urllib.parse.quote(str(self.repo)))
        d = json.loads(body)
        self.assertTrue(d["exists"])
        self.assertEqual(d["counts"]["total"], 2)

    def test_unknown_route_404(self):
        try:
            self._get("/api/nope")
            self.fail("expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)


if __name__ == "__main__":
    unittest.main()
