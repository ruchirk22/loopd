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

    def test_reset_to_head_saves_tracked_diff_and_untracked_files(self):
        led = Ledger.load_or_start(self.cfg)
        (self.repo / "app.txt").write_text("v2 modified\n")     # tracked edit
        (self.repo / "new_module.py").write_text("print('new')\n")  # untracked new file
        led.reset_to_head("test")
        self.assertEqual((self.repo / "app.txt").read_text(), "v1\n")
        self.assertFalse((self.repo / "new_module.py").exists())  # clean -fd removed it
        dumped = list((self.cfg.state_dir / "discarded").glob("**/tracked.diff"))
        self.assertEqual(len(dumped), 1)
        self.assertIn("v2 modified", dumped[0].read_text())
        # the untracked file's content was preserved forensically
        saved = list((self.cfg.state_dir / "discarded").glob("**/new_module.py"))
        self.assertEqual(len(saved), 1)
        self.assertIn("print('new')", saved[0].read_text())

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

    def test_dirty_tree_snapshotted_at_start(self):
        # a repo with pre-existing uncommitted work
        r = make_repo()
        subprocess.run(["git", "init", "-q"], cwd=str(r))
        subprocess.run(["git", "add", "-A"], cwd=str(r))
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-qm", "base"], cwd=str(r))
        (r / "app.txt").write_text("user's uncommitted edit\n")
        (r / "wip.txt").write_text("user's work in progress\n")
        led = Ledger.load_or_start(make_cfg(r))
        # HEAD is now clean; the user's work is preserved in a snapshot commit
        self.assertEqual(subprocess.run(["git", "status", "--porcelain"], cwd=str(r),
                                        capture_output=True, text=True).stdout.strip(), "")
        self.assertIn("pre-run snapshot", git_out(r, "log", "--oneline"))
        self.assertIn("uncommitted edit", (r / "app.txt").read_text())

    def test_adopt_head_if_matches_crash_window(self):
        led = Ledger.load_or_start(self.cfg)
        step = Step(id="1", goal="g", acceptance_criteria=["a"], verify=["true"])
        step.base_sha = led.head_sha()
        led.save_plan(Plan(steps=[step]))
        # crash window: commit_step ran (marker set, HEAD advanced) but clear_pending_commit
        # never did, and the plan still shows the step in_progress with no commit_sha.
        (self.repo / "shipped.txt").write_text("done\n")
        committed = led.commit_step(step, "step 1: shipped")
        self.assertIsNotNone(led.state["pending_commit"])
        step.commit_sha = ""  # as if save_plan never ran
        led.save_plan(Plan(steps=[step]))
        # on the no-op re-accept, adoption recovers exactly this step's commit
        adopted = led.adopt_head_if_matches(step)
        self.assertEqual(adopted, committed)
        self.assertIsNone(led.state["pending_commit"])  # cleared on adoption

    def test_adopt_refuses_without_marker(self):
        # a developer self-commit (no orchestrator marker) must NOT be adopted
        led = Ledger.load_or_start(self.cfg)
        step = Step(id="1", goal="g", acceptance_criteria=["a"], verify=["true"])
        step.base_sha = led.head_sha()
        (self.repo / "sneaky.txt").write_text("dev committed this itself\n")
        _git = __import__("subprocess").run
        _git(["git", "add", "-A"], cwd=str(self.repo))
        _git(["git", "commit", "-qm", "dev self-commit"], cwd=str(self.repo))
        self.assertIsNone(led.adopt_head_if_matches(step))

    def test_revert_unclaimed_commits(self):
        led = Ledger.load_or_start(self.cfg)
        step = Step(id="1", goal="g", acceptance_criteria=["a"], verify=["true"])
        step.base_sha = led.head_sha()
        (self.repo / "sneaky.txt").write_text("out-of-band\n")
        import subprocess as _sp
        _sp.run(["git", "add", "-A"], cwd=str(self.repo))
        _sp.run(["git", "commit", "-qm", "dev self-commit"], cwd=str(self.repo))
        self.assertNotEqual(led.head_sha(), step.base_sha)
        led.revert_unclaimed_commits(step, Plan(steps=[step]), "descope")
        self.assertEqual(led.head_sha(), step.base_sha)   # branch rolled back
        self.assertFalse((self.repo / "sneaky.txt").exists())
        self.assertTrue(list((self.cfg.state_dir / "discarded").glob("**/reverted.diff")))

    def test_dirty_tree_refused_without_run_branch(self):
        r = make_repo()
        import subprocess as _sp
        _sp.run(["git", "init", "-q"], cwd=str(r))
        _sp.run(["git", "add", "-A"], cwd=str(r))
        _sp.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "b"], cwd=str(r))
        (r / "app.txt").write_text("uncommitted\n")
        with self.assertRaises(StateConflict):
            Ledger.load_or_start(make_cfg(r, use_run_branch=False))

    def test_detached_head_records_sha(self):
        r = make_repo()
        subprocess.run(["git", "init", "-q"], cwd=str(r))
        subprocess.run(["git", "add", "-A"], cwd=str(r))
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-qm", "base"], cwd=str(r))
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(r),
                             capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "checkout", "-q", "--detach", sha], cwd=str(r))
        led = Ledger.load_or_start(make_cfg(r, use_run_branch=False))
        self.assertEqual(led.state["branch"], sha)  # SHA, not the literal "HEAD"

    def test_resume_refuses_incompatible_and_finished_state(self):
        led = Ledger.load_or_start(self.cfg)
        led.start("t")
        # wrong schema version
        bad = json.loads(led.state_path.read_text())
        bad["schema_version"] = 999
        led.state_path.write_text(json.dumps(bad))
        with self.assertRaises(StateConflict):
            Ledger.load_or_start(self.cfg, resume=True)
        # finished run is not resumable
        good = json.loads(json.dumps(led.state)); good["finished"] = True
        led.state_path.write_text(json.dumps(good))
        with self.assertRaises(StateConflict):
            Ledger.load_or_start(self.cfg, resume=True)

    def test_diff_skips_binary_and_huge_untracked(self):
        led = Ledger.load_or_start(self.cfg)
        (self.repo / "blob.bin").write_bytes(b"\x00\x01\x02" * 100)
        d = led.diff_against_head(cap=100000)
        self.assertIn("blob.bin", d["changed_files"])
        self.assertIn("not shown", d["diff"])  # binary flagged, not embedded

    def test_git_hooks_disabled(self):
        led = Ledger.load_or_start(self.cfg)
        hooks = self.repo / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        marker = self.repo / "HOOK_FIRED"
        hook = hooks / "post-checkout"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
        with led.pristine_worktree() as wt:
            self.assertTrue((wt / "app.txt").exists())
        self.assertFalse(marker.exists())  # dev-planted hook never fired

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
