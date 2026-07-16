import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.config import Config
from orchestrator.ledger import (BudgetExceeded, Ledger, NoChangesError,
                                 StateConflict)
from orchestrator.plan import Plan, Step


def make_repo() -> Path:
    repo = Path(tempfile.mkdtemp())
    (repo / "app.txt").write_text("v1\n")
    return repo


def make_cfg(repo: Path, **kw) -> Config:
    cfg = Config(repo=repo)
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def git_out(repo, *args) -> str:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True).stdout


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.cfg = make_cfg(self.repo)

    def test_init_creates_git_identity_branch_and_exclude(self):
        led = Ledger.load_or_start(self.cfg)
        self.assertTrue((self.repo / ".git").exists())
        self.assertIn(".agentic/", (self.repo / ".git/info/exclude").read_text())
        branch = git_out(self.repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        self.assertTrue(branch.startswith("agentic/run-"))
        self.assertEqual(led.state["branch"], branch)
        # baseline commit exists
        self.assertIn("baseline", git_out(self.repo, "log", "--oneline"))

    def test_state_conflict_and_resume(self):
        led = Ledger.load_or_start(self.cfg)
        led.start("task A")
        led.spend(1.5)
        with self.assertRaises(StateConflict):
            Ledger.load_or_start(self.cfg)
        led2 = Ledger.load_or_start(self.cfg, resume=True)
        self.assertEqual(led2.state["task"], "task A")
        self.assertAlmostEqual(led2.state["total_cost_usd"], 1.5)

    def test_fresh_archives_old_state(self):
        led = Ledger.load_or_start(self.cfg)
        led.start("task A")
        led2 = Ledger.load_or_start(self.cfg, fresh=True)
        self.assertEqual(led2.state["task"], "")
        archived = list(self.cfg.state_dir.glob("state.*.json"))
        self.assertEqual(len(archived), 1)

    def test_resume_without_state_raises(self):
        with self.assertRaises(StateConflict):
            Ledger.load_or_start(self.cfg, resume=True)

    def test_budget_enforced_and_step_cost_attributed(self):
        self.cfg.budget_usd = 1.0
        led = Ledger.load_or_start(self.cfg)
        step = Step(id="1", goal="g", acceptance_criteria=["a"], verify=["true"])
        led.spend(0.6, step)
        self.assertAlmostEqual(step.cost_usd, 0.6)
        with self.assertRaises(BudgetExceeded):
            led.spend(0.6, step)

    def test_commit_step_and_no_changes_refused(self):
        led = Ledger.load_or_start(self.cfg)
        step = Step(id="1", goal="add file", acceptance_criteria=["a"], verify=["true"])
        with self.assertRaises(NoChangesError):
            led.commit_step(step, "no-op")
        (self.repo / "new.txt").write_text("hello\n")
        sha = led.commit_step(step, "step 1: add file")
        self.assertEqual(len(sha), 40)
        self.assertEqual(step.commit_sha, sha)
        self.assertIn("step 1: add file", git_out(self.repo, "log", "--oneline"))

    def test_agentic_dir_never_committed(self):
        led = Ledger.load_or_start(self.cfg)
        (self.repo / "new.txt").write_text("x")
        step = Step(id="1", goal="g", acceptance_criteria=["a"], verify=["true"])
        led.commit_step(step, "msg")
        tracked = git_out(self.repo, "ls-files")
        self.assertNotIn(".agentic", tracked)

    def test_reset_to_head_saves_discarded_diff(self):
        led = Ledger.load_or_start(self.cfg)
        (self.repo / "app.txt").write_text("v2 modified\n")
        led.reset_to_head("test")
        self.assertEqual((self.repo / "app.txt").read_text(), "v1\n")
        dumped = list((self.cfg.state_dir / "discarded").glob("*.diff"))
        self.assertEqual(len(dumped), 1)
        self.assertIn("v2 modified", dumped[0].read_text())

    def test_diff_against_head_includes_untracked_and_caps(self):
        led = Ledger.load_or_start(self.cfg)
        (self.repo / "brand-new.py").write_text("print('hi')\n")
        d = led.diff_against_head(cap=100000)
        self.assertFalse(d["empty"])
        self.assertIn("brand-new.py", d["changed_files"])
        self.assertIn("print('hi')", d["diff"])
        small = led.diff_against_head(cap=10)
        self.assertIn("truncated", small["diff"])

    def test_pristine_worktree(self):
        led = Ledger.load_or_start(self.cfg)
        (self.repo / "dirty.txt").write_text("uncommitted\n")
        with led.pristine_worktree() as wt:
            self.assertTrue((wt / "app.txt").exists())        # committed content present
            self.assertFalse((wt / "dirty.txt").exists())     # uncommitted absent
        self.assertFalse(wt.exists() and any(wt.iterdir()))   # cleaned up

    def test_atomic_save_and_plan_roundtrip(self):
        led = Ledger.load_or_start(self.cfg)
        plan = Plan(summary="s", steps=[Step(id="1", goal="g",
                                             acceptance_criteria=["a"], verify=["true"])])
        led.save_plan(plan)
        raw = json.loads(led.state_path.read_text())
        self.assertEqual(raw["plan"]["summary"], "s")
        led2 = Ledger.load_or_start(self.cfg, resume=True)
        self.assertEqual(led2.load_plan().steps[0].id, "1")
        # no stray tmp files left behind
        self.assertEqual(list(self.cfg.state_dir.glob(".state-*")), [])


if __name__ == "__main__":
    unittest.main()
