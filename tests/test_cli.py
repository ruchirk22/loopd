import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import cli


def git_repo() -> Path:
    repo = Path(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q"], cwd=repo)
    (repo / "a.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=repo)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
                   cwd=repo)
    return repo


class FakeRun:
    """Stand-in for loop.run: records the call and (optionally) fires on_start."""
    def __init__(self, code=0):
        self.code = code
        self.calls = []

    def __call__(self, task, cfg, resume=False, fresh=False, on_start=None, resume_choice=None):
        self.calls.append({"task": task, "cfg": cfg, "resume": resume, "fresh": fresh,
                           "resume_choice": resume_choice})
        if on_start:
            on_start()
        return self.code


class CliTestBase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.repo = git_repo()
        env = mock.patch.dict("os.environ", {"LOOPD_HOME": str(self.home)})
        env.start(); self.addCleanup(env.stop)
        cwd = mock.patch.object(cli.Path, "cwd", staticmethod(lambda: self.repo))
        # Path.cwd patch is fiddly; use chdir instead:
        self._old = Path.cwd()
        import os
        os.chdir(self.repo)
        self.addCleanup(lambda: os.chdir(self._old))

    def run_cli(self, argv, code=0, fresh_fake=True):
        fake = FakeRun(code)
        p = mock.patch.object(cli.loop, "run", fake)
        p.start(); self.addCleanup(p.stop)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(argv)
        return rc, buf.getvalue(), fake


# --------------------------------------------------------------- source detection

class TestDetection(unittest.TestCase):
    def test_issue(self):
        self.assertTrue(cli._is_issue("#142"))
        self.assertTrue(cli._is_issue("https://github.com/o/r/issues/142"))
        self.assertFalse(cli._is_issue("add a thing"))

    def test_repo_url(self):
        self.assertTrue(cli._is_repo_url("https://github.com/o/r"))
        self.assertTrue(cli._is_repo_url("github.com/o/r"))
        self.assertTrue(cli._is_repo_url("git@github.com:o/r.git"))
        self.assertFalse(cli._is_repo_url("https://github.com/o/r/issues/1"))
        self.assertFalse(cli._is_repo_url("just a task"))

    def test_clone_name(self):
        self.assertEqual(cli._clone_name("https://github.com/o/repo.git"), "repo")
        self.assertEqual(cli._clone_name("github.com/o/repo"), "repo")


# --------------------------------------------------------------- routing / help

class TestRouting(CliTestBase):
    def test_help_and_version(self):
        _, out, _ = self.run_cli(["help"])
        self.assertIn("loopd", out)
        _, out, _ = self.run_cli(["version"])
        self.assertIn("loopd 0.", out)

    def test_task_routes_to_run(self):
        rc, out, fake = self.run_cli(["add a /health endpoint"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["task"], "add a /health endpoint")

    def test_multiword_unquoted_task_is_joined(self):
        _, _, fake = self.run_cli(["add", "health", "endpoint"])
        self.assertEqual(fake.calls[0]["task"], "add health endpoint")

    def test_reassurance_shown_on_start(self):
        _, out, _ = self.run_cli(["build a thing"])
        self.assertIn("I've got it from here", out)

    def test_quiet_suppresses_reassurance(self):
        _, out, _ = self.run_cli(["build a thing", "--quiet"])
        self.assertNotIn("I've got it from here", out)

    def test_issue_is_deferred_gracefully(self):
        rc, out, fake = self.run_cli(["#142"])
        self.assertEqual(rc, 2)
        self.assertIn("GitHub", out)
        self.assertEqual(fake.calls, [])  # nothing built

    def test_spec_file_becomes_brief(self):
        spec = self.repo / "spec.md"
        spec.write_text("# build the thing\n")
        _, _, fake = self.run_cli([str(spec)])
        self.assertIsNone(fake.calls[0]["task"])           # brief drives it
        self.assertEqual(str(fake.calls[0]["cfg"].brief_path), str(spec))

    def test_budget_flag_flows_to_cfg(self):
        _, _, fake = self.run_cli(["do it", "--budget", "40"])
        self.assertEqual(fake.calls[0]["cfg"].budget_usd, 40.0)
        self.assertTrue(fake.calls[0]["cfg"].budget_explicit)

    def test_constrained_flag_flows(self):
        _, _, fake = self.run_cli(["do it", "--constrained"])
        self.assertTrue(fake.calls[0]["cfg"].constrained)

    def test_run_records_project(self):
        self.run_cli(["do it"])
        from orchestrator import workspace
        self.assertTrue(any(e["path"] == str(self.repo.resolve()) for e in workspace.recent()))


# --------------------------------------------------------------- ambient verbs

class TestAmbient(CliTestBase):
    def _seed_state(self, finished=True):
        ad = self.repo / ".agentic"
        ad.mkdir(exist_ok=True)
        (ad / "state.json").write_text(json.dumps({
            "task": "add auth", "finished": finished, "total_cost_usd": 2.5, "budget_usd": 25,
            "plan": {"summary": "the plan", "steps": [
                {"id": "1", "goal": "step one", "status": "done"},
                {"id": "2", "goal": "step two", "status": "pending"}]}}))
        return ad

    def test_status_no_runs(self):
        rc, out, _ = self.run_cli(["status"])
        self.assertEqual(rc, 0)
        self.assertIn("No runs yet", out)

    def test_status_paused(self):
        self._seed_state(finished=False)
        _, out, _ = self.run_cli(["status"])
        self.assertIn("progress", out.lower() + "")
        self.assertIn("add auth", out)

    def test_plan_checklist(self):
        self._seed_state()
        _, out, _ = self.run_cli(["plan"])
        self.assertIn("step one", out)
        self.assertIn("step two", out)

    def test_report_missing_and_present(self):
        _, out, _ = self.run_cli(["report"])
        self.assertIn("No report", out)
        ad = self.repo / ".agentic"; ad.mkdir(exist_ok=True)
        (ad / "report.md").write_text("# run report\nall good\n")
        _, out, _ = self.run_cli(["report"])
        self.assertIn("all good", out)

    def test_memory_missing(self):
        _, out, _ = self.run_cli(["memory"])
        self.assertIn("haven't learned", out)

    def test_resume_calls_engine_with_resume(self):
        _, _, fake = self.run_cli(["resume"])
        self.assertTrue(fake.calls[0]["resume"])

    def test_resume_yes_applies_recommended_analysis_option(self):
        ad = self.repo / ".agentic"; ad.mkdir(exist_ok=True)
        (ad / "analysis.json").write_text(json.dumps({
            "summary": "stuck", "root_cause": "needs redis", "category": "environment",
            "step": "6", "reason": "pm_abort",
            "options": [{"id": "fix", "label": "Add fixture", "kind": "loopd_fix", "recommended": True},
                        {"id": "stop", "label": "Stop", "kind": "abort"}]}))
        _, out, fake = self.run_cli(["resume", "--yes"])
        self.assertIn("What happened", out)              # the diagnosis is shown
        rc = fake.calls[0]["resume_choice"]
        self.assertIsNotNone(rc)
        self.assertEqual(rc["kind"], "loopd_fix")
        self.assertEqual(rc["step"], "6")

    def test_ui_launches_dashboard(self):
        served = {}
        def fake_serve(host, port, repo, budget):
            served.update(host=host, port=port, repo=repo, budget=budget)
        with mock.patch("orchestrator.dashboard.serve", fake_serve):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["ui", "--port", "9111"])
        self.assertEqual(rc, 0)
        self.assertEqual(served["port"], 9111)
        self.assertEqual(served["host"], "127.0.0.1")

    def test_projects_lists_recent(self):
        from orchestrator import workspace
        workspace.register(self.repo)
        workspace.record_run(self.repo, 0, 1.0)
        _, out, _ = self.run_cli(["projects"])
        self.assertIn(self.repo.name, out)


class TestHome(CliTestBase):
    def test_home_non_tty_git_repo_shows_header(self):
        # non-interactive: prints the workspace header, doesn't hang on input
        _, out, _ = self.run_cli([])
        self.assertIn(self.repo.name, out)

    def test_home_paused_run_non_tty_hints_resume(self):
        ad = self.repo / ".agentic"; ad.mkdir(exist_ok=True)
        (ad / "state.json").write_text(json.dumps({
            "task": "add auth", "finished": False, "total_cost_usd": 1.0,
            "plan": {"steps": [{"status": "done"}, {"status": "pending"}]}}))
        _, out, _ = self.run_cli([])
        self.assertIn("loopd resume", out)


if __name__ == "__main__":
    unittest.main()
