import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
    step = Step(id="1", goal="add widget", acceptance_criteria=["renders", "has a test"],
                verify=["pytest -q"], status="done", commit_sha="abcdef1234", cost_usd=3.25,
                attempts=2, dev_summary="added widget.py and a test")
    pending = Step(id="2", goal="wire it up", acceptance_criteria=["b"], verify=["true"])
    led.save_plan(Plan(summary="do it", steps=[step, pending]))
    led.log({"event": "gates", "step": "1", "passed": True})
    led.log({"event": "step_committed", "step": "1", "sha": "abcdef1234"})
    (cfg.state_dir / "handovers").mkdir(exist_ok=True)
    (cfg.state_dir / "handovers" / "step-1-attempt-2.md").write_text("## Handover for step 1\nALL GATES PASSED")
    return repo


class TestSnapshot(unittest.TestCase):
    def test_snapshot_of_a_run(self):
        s = dashboard.snapshot(repo_with_run(), running=True)
        self.assertTrue(s["exists"] and s["running"])
        self.assertEqual(s["budget_usd"], 20)
        self.assertEqual(s["pm_model"], "claude-opus-4-8")
        self.assertEqual(s["counts"], {"done": 1, "skipped": 0, "total": 2})
        self.assertEqual(s["current_step"]["id"], "2")
        self.assertEqual(s["step_index"], 2)
        self.assertEqual(s["steps"][0]["commit"], "abcdef123")
        self.assertIsNotNone(s["elapsed_s"])
        # derived metrics + timeline + active node
        self.assertEqual(s["metrics"]["accepted"], 1)
        self.assertEqual(s["metrics"]["gate_pass"], 1)
        self.assertEqual(s["metrics"]["gate_total"], 1)
        self.assertIn(s["active_node"], ("planner", "developer", "verification", "review", "decision", "done"))
        self.assertTrue(any("accepted" in ev["text"] for ev in s["timeline"]))

    def test_snapshot_freezes_elapsed_when_finished(self):
        # A finished run must report a frozen elapsed (end - start), not an ever-growing clock,
        # so the "Delivered" card is stable and reopening an old run shows the true duration.
        repo = repo_with_run()
        sp = repo / ".agentic" / "state.json"
        st = json.loads(sp.read_text())
        started = st["started"]
        st["finished"] = True
        sp.write_text(json.dumps(st))
        with (repo / ".agentic" / "log.jsonl").open("a") as f:
            f.write(json.dumps({"event": "run_finished", "ts": started + 5}) + "\n")
        s = dashboard.snapshot(repo, running=False)
        self.assertTrue(s["finished"])
        self.assertAlmostEqual(s["elapsed_s"], 5, delta=0.5)
        # frozen: a later call returns the SAME value (does not track wall-clock)
        self.assertEqual(dashboard.snapshot(repo, running=False)["elapsed_s"], s["elapsed_s"])

    def test_snapshot_no_run(self):
        s = dashboard.snapshot(Path(tempfile.mkdtemp()))
        self.assertFalse(s["exists"])
        self.assertEqual(s["timeline"], [])

    def test_snapshot_carries_workspace_framing(self):
        # even before a run, the Project screen's empty state gets the project's identity
        s = dashboard.snapshot(Path(tempfile.mkdtemp()))
        for k in ("name", "health", "memory_count", "forecast_accuracy", "runs"):
            self.assertIn(k, s)
        self.assertIn("is_repo", s["health"])

    def test_snapshot_of_a_run_has_forecast_and_health_keys(self):
        s = dashboard.snapshot(repo_with_run(), running=True)
        for k in ("name", "health", "memory_count", "forecast_accuracy", "runs", "escalation",
                  "analysis"):
            self.assertIn(k, s)

    def test_snapshot_exposes_failure_analysis(self):
        repo = repo_with_run()
        (repo / ".agentic" / "analysis.json").write_text(json.dumps({
            "summary": "stuck", "root_cause": "needs redis", "category": "environment",
            "options": [{"id": "fix", "label": "Add fixture", "kind": "loopd_fix", "recommended": True}]}))
        s = dashboard.snapshot(repo, running=False)
        self.assertIsNotNone(s["analysis"])
        self.assertEqual(s["analysis"]["category"], "environment")


class TestProjectsList(unittest.TestCase):
    def setUp(self):
        import os
        self.home = Path(tempfile.mkdtemp())
        self._p = mock.patch.dict(os.environ, {"LOOPD_HOME": str(self.home)})
        self._p.start(); self.addCleanup(self._p.stop)

    def test_projects_list_from_registry(self):
        from orchestrator import workspace
        repo = repo_with_run()
        workspace.register(repo)
        workspace.record_run(repo, 0, 3.25)
        rows = dashboard._projects_list(dashboard.RunManager())
        self.assertTrue(any(r["path"] == str(Path(repo).resolve()) for r in rows))
        row = next(r for r in rows if r["path"] == str(Path(repo).resolve()))
        self.assertIn(row["status"], ("idle", "paused", "done", "working"))
        self.assertIn("health", row)


class TestStepDetail(unittest.TestCase):
    def test_step_detail(self):
        d = dashboard.step_detail(repo_with_run(), "1")
        self.assertTrue(d["found"])
        self.assertEqual(d["step"]["acceptance_criteria"], ["renders", "has a test"])
        self.assertEqual(d["step"]["verify"], ["pytest -q"])
        self.assertIn("ALL GATES PASSED", d["handover"])
        self.assertEqual(d["handover_count"], 1)

    def test_step_detail_missing(self):
        d = dashboard.step_detail(repo_with_run(), "99")
        self.assertFalse(d["found"])


class TestBuildCommand(unittest.TestCase):
    def test_github_info_and_pr_helpers(self):
        from orchestrator import github
        with mock.patch.object(github, "available", lambda: {"ok": True}), \
             mock.patch.object(github, "current_branch", lambda repo: "b"), \
             mock.patch.object(github, "repo_meta", lambda repo: {"slug": "o/r", "default_branch": "main"}), \
             mock.patch.object(github, "pr_status", lambda repo, br: None):
            info = dashboard._github_info("/tmp/x")
        self.assertTrue(info["available"])
        self.assertEqual(info["repo"]["slug"], "o/r")

    def test_github_info_degrades(self):
        from orchestrator import github
        with mock.patch.object(github, "available", lambda: {"ok": False, "hint": "gh auth login"}):
            info = dashboard._github_info("/tmp/x")
        self.assertFalse(info["available"])
        self.assertIn("gh auth login", info["hint"])

    def test_open_pr_api_needs_github(self):
        from orchestrator import github
        with mock.patch.object(github, "available", lambda: {"ok": False, "hint": "install gh"}):
            r = dashboard._open_pr_api("/tmp/x")
        self.assertFalse(r["ok"])

    def test_resume_carries_failure_analysis_option(self):
        cmd = dashboard.build_run_command("/tmp/x", 8, "resume", option="add-redis-fixture")
        self.assertIn("--option", cmd)
        self.assertIn("add-redis-fixture", cmd)
        # a fresh run never carries a resume option
        self.assertNotIn("--option", dashboard.build_run_command("/tmp/x", 8, "new", option="x"))

    def test_new_vs_resume(self):
        self.assertIn("--fresh", dashboard.build_run_command("/tmp/x", 8, "new"))
        self.assertIn("--resume-run", dashboard.build_run_command("/tmp/x", 8, "resume"))


class TestManager(unittest.TestCase):
    def test_new_without_task_refused(self):
        r = dashboard.RunManager().launch(repo=Path(tempfile.mkdtemp()), task="", budget=5,
                                          pm_model="", dev_model="", mode="new")
        self.assertFalse(r["ok"])
        self.assertIn("provide a task", r["error"])

    def test_stop_without_run(self):
        r = dashboard.RunManager().stop(Path(tempfile.mkdtemp()))
        self.assertFalse(r["ok"])
        self.assertIn("no active run", r["error"])


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
            return r.status, r.read(), r.headers.get("Content-Type", "")

    def test_index(self):
        code, body, _ = self._get("/")
        self.assertEqual(code, 200)
        self.assertIn(b"loopd", body)
        self.assertIn(b"New project", body)

    def test_asset_served(self):
        code, body, ctype = self._get("/assets/loopd.svg")
        self.assertEqual(code, 200)
        self.assertIn("svg", ctype)
        self.assertIn(b"<svg", body)

    def test_asset_traversal_blocked(self):
        try:
            self._get("/assets/../ledger.py")
            self.fail("expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_state_and_step_endpoints(self):
        rp = urllib.parse.quote(str(self.repo))
        code, body, _ = self._get("/api/state?repo=" + rp)
        self.assertEqual(json.loads(body)["counts"]["total"], 2)
        code, body, _ = self._get(f"/api/step?repo={rp}&id=1")
        self.assertTrue(json.loads(body)["found"])


if __name__ == "__main__":
    unittest.main()
