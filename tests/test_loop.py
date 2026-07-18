"""End-to-end tests of the whole control plane with scripted fake PM/dev agents.
No network, no Claude CLI: run_claude is patched at both call sites."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import developer as dev_module
from orchestrator import loop
from orchestrator import pm as pm_module
from orchestrator.claude_cli import ClaudeResult
from orchestrator.config import Config


def ok_result(structured, sid, cost=0.02):
    return ClaudeResult(ok=True, text=json.dumps(structured), session_id=sid,
                        cost_usd=cost, structured=structured, raw={})


class FakeAgent:
    """Scripted stand-in for run_claude. Each entry: (must_contain | None, responder).
    A responder is a dict (directive/summary -> ok_result) or a callable
    (prompt, cwd, kwargs) -> ClaudeResult."""

    def __init__(self, test, name, sid, script):
        self.test, self.name, self.sid = test, name, sid
        self.script = list(script)
        self.calls = []

    def __call__(self, prompt, *, cwd, **kw):
        self.calls.append({"prompt": prompt, "cwd": Path(cwd), **kw})
        if not self.script:
            self.test.fail(f"{self.name}: unexpected extra call:\n{prompt[:300]}")
        expect, responder = self.script.pop(0)
        if expect:
            self.test.assertIn(expect, prompt,
                               f"{self.name}: expected {expect!r} in prompt:\n{prompt[:500]}")
        if callable(responder):
            return responder(prompt, Path(cwd), kw)
        return ok_result(responder, self.sid)

    def assert_exhausted(self):
        self.test.assertEqual(self.script, [], f"{self.name}: unused script entries")


def plan_directive(verify, criteria=None):
    return {"verdict": "plan", "reasoning": "one step is enough",
            "plan_mutations": [{"op": "add", "step": {
                "id": "1", "goal": "produce hello.txt",
                "acceptance_criteria": criteria or ["hello.txt exists"],
                "verify": verify}}]}


DISPATCH = {"verdict": "dispatch", "reasoning": "go",
            "next_prompt": "Create hello.txt containing hello", "dev_session": "fresh"}

DISPATCH_WORLD = {"verdict": "dispatch", "reasoning": "go",
                  "next_prompt": "Create world.txt containing world", "dev_session": "fresh"}

# Distinctive file content so accept-evidence can quote a real DIFF line (the dev's own
# summary is excluded from the proof corpus).
HELLO_CONTENT = "health endpoint returns status ok\n"

# verify commands reference hello.txt, which the dev creates -> GATE_TARGETS_TOUCHED
# fires (high_risk), so a valid accept must also carry integrity_ack.
ACCEPT = {"verdict": "accept", "reasoning": "diff shows the file; gates green",
          "commit_message": "step 1: hello",
          "integrity_ack": "GATE_TARGETS_TOUCHED: hello.txt is the deliverable itself; the diff "
                           "shows its real content, the check was not gamed.",
          "criteria_evidence": [{"criterion": "hello.txt exists", "satisfied": True,
                                 "evidence": "health endpoint returns status ok"}]}


def dev_writes_hello(content=HELLO_CONTENT):
    def responder(prompt, cwd, kw):
        (cwd / "hello.txt").write_text(content)
        return ok_result({"summary": "created hello.txt with the required content",
                          "files_changed": ["hello.txt"],
                          "commands_run": ["test -f hello.txt"], "concerns": []}, "dev-1")
    return responder


DEV_NOOP = {"summary": "could not do it", "files_changed": [], "commands_run": [],
            "concerns": ["file cannot be created"]}


class LoopTestBase(unittest.TestCase):
    def setUp(self):
        repo = Path(tempfile.mkdtemp())
        (repo / "app.txt").write_text("v1\n")
        self.cfg = Config(repo=repo)
        (self.cfg.state_dir / "brief.md").write_text("# Task brief\n\n## Objective\nmake hello.txt\n")

    def patch_agents(self, pm_script, dev_script):
        self.fake_pm = FakeAgent(self, "PM", "pm-1", pm_script)
        self.fake_dev = FakeAgent(self, "DEV", "dev-1", dev_script)
        p1 = mock.patch.object(pm_module, "run_claude", self.fake_pm)
        p2 = mock.patch.object(dev_module, "run_claude", self.fake_dev)
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)

    def git_log(self):
        return subprocess.run(["git", "log", "--oneline"], cwd=str(self.cfg.repo),
                              capture_output=True, text=True).stdout

    def state(self):
        return json.loads((self.cfg.state_dir / "state.json").read_text())


class TestHappyPath(LoopTestBase):
    def test_plan_dispatch_accept_finalize(self):
        self.patch_agents(
            pm_script=[
                ("Create the plan", plan_directive(["test -f hello.txt"])),
                ("Author the developer's instructions", DISPATCH),
                ("Review the developer's handover", ACCEPT),
                ("All planned steps are accepted",
                 {"verdict": "task_complete", "reasoning": "brief satisfied",
                  "final_verify": ["test -f hello.txt", "test -f app.txt"],
                  "memory": {"decisions": ["hello.txt is the deliverable"],
                             "todos": ["add a greeting param"]}}),
            ],
            dev_script=[("Create hello.txt", dev_writes_hello())],
        )
        rc = loop.run(None, self.cfg)
        self.assertEqual(rc, 0)
        self.fake_pm.assert_exhausted()
        self.fake_dev.assert_exhausted()
        self.assertIn("step 1: hello", self.git_log())
        st = self.state()
        self.assertTrue(st["finished"])
        self.assertEqual(st["plan"]["steps"][0]["status"], "done")
        self.assertEqual(len(st["plan"]["steps"][0]["commit_sha"]), 40)
        # first dev call was a fresh session
        self.assertIsNone(self.fake_dev.calls[0].get("resume_session"))
        # handover packet persisted
        self.assertTrue(list((self.cfg.state_dir / "handovers").glob("step-1-*.md")))
        # end-of-run report written on success
        report = (self.cfg.state_dir / "report.md").read_text()
        self.assertIn("complete", report)
        self.assertIn("Changes committed", report)
        self.assertIn("produce hello.txt", report)
        # engineering memory updated from the PM's task_complete directive
        mem = (self.cfg.state_dir / "memory.md").read_text()
        self.assertIn("hello.txt is the deliverable", mem)
        self.assertIn("add a greeting param", mem)


class TestRedGatesDescope(LoopTestBase):
    def test_gates_never_pass_pm_must_descope(self):
        def assert_no_accept(prompt, cwd, kw):
            # dynamic schema: accept must not even be in the verdict enum on red gates
            enum = kw["json_schema"]["properties"]["verdict"]["enum"]
            self.assertEqual(enum, ["replan", "descope", "abort"])
            self.assertIn("GATES FAILED", prompt)
            return ok_result({"verdict": "descope",
                              "reasoning": "impossible check; skipping — impact: none"}, "pm-1")

        self.patch_agents(
            pm_script=[
                (None, plan_directive(["test -f never.txt"])),
                (None, DISPATCH),
                ("Review the developer's handover", assert_no_accept),
                (None, {"verdict": "task_complete", "reasoning": "nothing left",
                        "final_verify": ["test -f app.txt"]}),
            ],
            dev_script=[(None, DEV_NOOP), (None, DEV_NOOP), (None, DEV_NOOP)],
        )
        rc = loop.run(None, self.cfg)
        self.assertEqual(rc, 0)
        self.fake_dev.assert_exhausted()  # exactly MAX_ATTEMPTS_PER_STEP dev calls
        st = self.state()
        self.assertEqual(st["plan"]["steps"][0]["status"], "skipped")
        self.assertEqual(st["plan"]["steps"][0]["attempts"], 3)


class TestRejectThenAcceptWithCheckpoint(LoopTestBase):
    def test_reject_resumes_dev_session_then_checkpoint_reincarnates_pm(self):
        self.cfg.checkpoint_every_reviews = 1

        def finalize_expect_seed(prompt, cwd, kw):
            # after reincarnation the finalize turn must be seeded from scratch
            self.assertIn("Checkpoint from your predecessor", prompt)
            self.assertIn("make hello.txt", prompt)  # brief present
            return ok_result({"verdict": "task_complete", "reasoning": "done",
                              "final_verify": ["test -f hello.txt"]}, "pm-2")

        self.patch_agents(
            pm_script=[
                (None, plan_directive(["test -f hello.txt"])),
                (None, DISPATCH),
                ("Review", {"verdict": "reject", "reasoning": "content is wrong",
                            "next_prompt": "Put the word hello inside hello.txt"}),
                ("Review", ACCEPT),
                ("checkpoint", {"mission_summary": "make hello", "key_decisions": ["plain file"],
                                "open_risks": [], "remaining_plan_note": "none",
                                "advice_to_successor": "n/a"}),
                ("All planned steps are accepted", finalize_expect_seed),
            ],
            dev_script=[
                (None, dev_writes_hello("junk placeholder\n")),
                ("Put the word hello inside", dev_writes_hello()),
            ],
        )
        rc = loop.run(None, self.cfg)
        self.assertEqual(rc, 0)
        self.fake_pm.assert_exhausted()
        # rejection resumed the SAME dev session
        self.assertEqual(self.fake_dev.calls[1].get("resume_session"), "dev-1")
        st = self.state()
        self.assertEqual(st["plan"]["steps"][0]["rejections"], 1)
        self.assertIsNotNone(st["checkpoint"])
        self.assertEqual(st["pm_session_id"], "pm-2")


class TestCorrectiveAndAbort(LoopTestBase):
    def test_invalid_plan_gets_one_corrective_then_abort_escalates(self):
        self.patch_agents(
            pm_script=[
                (None, {"verdict": "plan", "reasoning": "oops, no mutations"}),
                ("REFUSED", plan_directive(["test -f hello.txt"])),
                (None, {"verdict": "abort", "reasoning": "owner input needed"}),
            ],
            dev_script=[],
        )
        rc = loop.run(None, self.cfg)
        self.assertEqual(rc, 1)
        esc = json.loads((self.cfg.state_dir / "escalation.json").read_text())
        self.assertEqual(esc["reason"], "pm_abort")
        self.assertIn("owner input needed", esc["pm_reasoning"])
        # a report is written on failure too, with the stop reason
        report = (self.cfg.state_dir / "report.md").read_text()
        self.assertIn("stopped", report)
        self.assertIn("Why it stopped", report)
        # the failure is recorded to engineering memory for future runs
        mem = (self.cfg.state_dir / "memory.md").read_text()
        self.assertIn("Past failures", mem)


class TestBudgetStopAndResume(LoopTestBase):
    def test_budget_exceeded_is_resumable(self):
        self.cfg.budget_usd = 0.03  # plan (0.02) fits; dispatch (0.04 total) blows
        self.patch_agents(
            pm_script=[
                (None, plan_directive(["test -f hello.txt"])),
                (None, DISPATCH),  # cost of this turn crosses the cap
            ],
            dev_script=[],
        )
        rc = loop.run(None, self.cfg)
        self.assertEqual(rc, 3)
        esc = json.loads((self.cfg.state_dir / "escalation.json").read_text())
        self.assertEqual(esc["reason"], "budget_exceeded")
        st = self.state()
        self.assertIsNotNone(st["plan"])  # plan survived for the resume

        # --- resume with a raised budget: continues from the saved plan, no re-plan ---
        cfg2 = Config(repo=self.cfg.repo)
        self.patch_agents(
            pm_script=[
                ("Author the developer's instructions", DISPATCH),
                ("Review", ACCEPT),
                (None, {"verdict": "task_complete", "reasoning": "done",
                        "final_verify": ["test -f hello.txt"]}),
            ],
            dev_script=[(None, dev_writes_hello())],
        )
        rc = loop.run(None, cfg2, resume=True)
        self.assertEqual(rc, 0)
        self.fake_pm.assert_exhausted()
        st = self.state()
        self.assertTrue(st["finished"])
        # spend carried over across the resume
        self.assertGreater(st["total_cost_usd"], 0.04)


class TestFinalizeNoOpReplan(LoopTestBase):
    def test_finalize_replan_without_pending_step_is_refused(self):
        # PM tries to "replan" at finalize with a mutation that adds no pending work,
        # then corrects to a real added step.
        noop_replan = {"verdict": "replan", "reasoning": "tidy summary",
                       "plan_mutations": [{"op": "set_summary", "summary": "cleaner"}]}
        real_replan = {"verdict": "replan", "reasoning": "actually one more step",
                       "plan_mutations": [{"op": "add", "step": {
                           "id": "2", "goal": "make world.txt",
                           "acceptance_criteria": ["world.txt exists"],
                           "verify": ["test -f world.txt"]}}]}
        world_accept = {"verdict": "accept", "reasoning": "created",
                        "commit_message": "step 2",
                        "integrity_ack": "GATE_TARGETS_TOUCHED: world.txt is the deliverable; diff is real.",
                        "criteria_evidence": [{"criterion": "world.txt exists", "satisfied": True,
                                               "evidence": "world data payload written here"}]}

        def dev_world(prompt, cwd, kw):
            (cwd / "world.txt").write_text("world data payload written here\n")
            return ok_result({"summary": "created world.txt as the deliverable",
                              "files_changed": ["world.txt"], "commands_run": ["test -f world.txt"],
                              "concerns": []}, "dev-1")

        self.patch_agents(
            pm_script=[
                (None, plan_directive(["test -f hello.txt"])),
                (None, DISPATCH),
                ("Review", ACCEPT),
                ("All planned steps are accepted", noop_replan),   # finalize -> bad replan
                ("REFUSED", real_replan),                          # corrective -> real step
                ("Author the developer's instructions", DISPATCH_WORLD),
                ("Review", world_accept),
                (None, {"verdict": "task_complete", "reasoning": "both done",
                        "final_verify": ["test -f hello.txt", "test -f world.txt"]}),
            ],
            dev_script=[(None, dev_writes_hello()), (None, dev_world)],
        )
        rc = loop.run(None, self.cfg)
        self.assertEqual(rc, 0)
        self.fake_pm.assert_exhausted()
        st = self.state()
        self.assertEqual(st["replans_used"], 1)
        self.assertEqual([s["status"] for s in st["plan"]["steps"]], ["done", "done"])


class TestReplanPath(LoopTestBase):
    def test_pm_replans_at_dispatch_and_new_step_runs(self):
        replan = {"verdict": "replan", "reasoning": "verify was wrong",
                  "plan_mutations": [{"op": "update", "step": {
                      "id": "1", "verify": ["test -s hello.txt"]}}]}
        self.patch_agents(
            pm_script=[
                (None, plan_directive(["test -f wrong-check.txt"])),
                (None, replan),          # PM fixes the plan at dispatch time
                (None, DISPATCH),        # dispatch of the updated step
                ("Review", ACCEPT),
                (None, {"verdict": "task_complete", "reasoning": "done",
                        "final_verify": ["test -s hello.txt"]}),
            ],
            dev_script=[(None, dev_writes_hello())],
        )
        rc = loop.run(None, self.cfg)
        self.assertEqual(rc, 0)
        st = self.state()
        self.assertEqual(st["replans_used"], 1)
        self.assertEqual(st["plan"]["steps"][0]["verify"], ["test -s hello.txt"])
        self.assertEqual(st["plan"]["steps"][0]["status"], "done")


if __name__ == "__main__":
    unittest.main()
