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
from orchestrator.ledger import Ledger


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
        self.cfg.forecast_enabled = False  # control-plane tests; forecast covered in test_forecast.py
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
        # Mirrors the documented recovery: `--resume-run --budget <higher>`.
        cfg2 = Config(repo=self.cfg.repo, budget_explicit=True)
        cfg2.budget_usd = 1.0
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

    def test_resume_without_explicit_budget_carries_prior_budget(self):
        """A resume that does NOT pass --budget must keep the run's stored budget, not snap
        it back to the env default (which could sit below what's already been spent and brick
        the resume)."""
        self.cfg.budget_usd = 0.03
        self.patch_agents(
            pm_script=[
                (None, plan_directive(["test -f hello.txt"])),
                (None, DISPATCH),
            ],
            dev_script=[],
        )
        rc = loop.run(None, self.cfg)
        self.assertEqual(rc, 3)

        # Resume with a DEFAULT config (no explicit --budget). budget_explicit is False,
        # so the stored $0.03 is carried forward — the run stays budget-stopped rather than
        # jumping to the $25 default.
        cfg2 = Config(repo=self.cfg.repo)
        self.assertFalse(cfg2.budget_explicit)
        self.patch_agents(
            pm_script=[("Author", DISPATCH)],
            dev_script=[(None, dev_writes_hello())],
        )
        rc = loop.run(None, cfg2, resume=True)
        self.assertEqual(rc, 3)  # still budget-stopped; the default budget did NOT take over
        self.assertEqual(self.state()["budget_usd"], 0.03)


class TestWallClock(LoopTestBase):
    def test_wall_clock_stop_is_operational_not_a_memory_failure(self):
        from orchestrator import loop as loopmod
        self.cfg.max_wall_clock_min = 1
        calls = [0]

        def fake_time():
            calls[0] += 1
            return 1000.0 if calls[0] == 1 else 1000.0 + 100000  # jump past the cap after t0

        self.patch_agents(pm_script=[(None, plan_directive(["test -f hello.txt"]))], dev_script=[])
        with mock.patch.object(loopmod.time, "time", fake_time):
            rc = loop.run(None, self.cfg)
        self.assertEqual(rc, 1)
        memf = self.cfg.state_dir / "memory.md"
        if memf.exists():  # wall-clock is operational — must not pollute Past failures
            self.assertNotIn("run stopped", memf.read_text())


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


FORECAST_ANALYSIS = {
    "estimated_steps": 8, "complexity": 60, "risk": "medium", "research_required": False,
    "likely_replans": 1, "likely_retries": 3, "verification_types": ["unit"], "confidence": 75,
}


class TestForecastInLoop(LoopTestBase):
    """The forecast runs once before planning, is persisted, graded against actuals, and — when
    the user proceeds constrained — reaches the planner's seed."""

    def _enable_forecast(self, force=False, budget=5.0, analysis=None):
        self.cfg.forecast_enabled = True
        self.cfg.force = force          # non-interactive: proceed at current budget if short
        self.cfg.budget_usd = budget
        fake = lambda *a, **k: ok_result(analysis or FORECAST_ANALYSIS, None, cost=0.01)
        p = mock.patch.object(loop.forecast, "run_claude", fake)
        p.start(); self.addCleanup(p.stop)

    def _happy_script(self):
        self.patch_agents(
            pm_script=[
                ("Create the plan", plan_directive(["test -f hello.txt"])),
                ("Author the developer's instructions", DISPATCH),
                ("Review the developer's handover", ACCEPT),
                ("All planned steps are accepted",
                 {"verdict": "task_complete", "reasoning": "brief satisfied",
                  "final_verify": ["test -f hello.txt"]}),
            ],
            dev_script=[("Create hello.txt", dev_writes_hello())],
        )

    def test_forecast_persisted_and_graded(self):
        self._enable_forecast(force=True, budget=25.0)
        self._happy_script()
        rc = loop.run(None, self.cfg)
        self.assertEqual(rc, 0)
        st = self.state()
        self.assertIsNotNone(st["forecast"])
        self.assertEqual(st["forecast"]["estimated_steps"], 8)
        self.assertIn("actual", st["forecast"])                 # graded after the run
        self.assertEqual(st["forecast"]["actual"]["steps_done"], 1)
        from orchestrator.forecast import ForecastHistory
        recs = ForecastHistory(self.cfg.repo).load()            # appended to history
        self.assertEqual(len(recs), 1)
        self.assertTrue(recs[0]["run_success"])

    def test_constrained_reaches_pm_seed(self):
        # Budget $5 is far below the estimate → --force proceeds in constrained mode.
        self._enable_forecast(force=True, budget=5.0)
        self._happy_script()
        loop.run(None, self.cfg)
        self.assertIn("BUDGET-CONSTRAINED EXECUTION", self.fake_pm.calls[0]["prompt"])
        self.assertTrue(self.state()["forecast"]["constrained"])

    def test_not_constrained_when_budget_covers(self):
        self._enable_forecast(force=True, budget=1000.0)
        self._happy_script()
        loop.run(None, self.cfg)
        self.assertNotIn("BUDGET-CONSTRAINED EXECUTION", self.fake_pm.calls[0]["prompt"])
        self.assertFalse(self.state()["forecast"]["constrained"])

    def test_explicit_constrained_survives_non_constrained_forecast(self):
        # User passed --constrained but the budget covers the estimate: the flag must NOT be
        # silently downgraded by the forecast decision.
        self._enable_forecast(force=True, budget=1000.0)
        self.cfg.constrained = True
        self._happy_script()
        loop.run(None, self.cfg)
        self.assertIn("BUDGET-CONSTRAINED EXECUTION", self.fake_pm.calls[0]["prompt"])
        self.assertTrue(self.state()["forecast"]["constrained"])

    def test_forecast_decline_is_operational_not_a_memory_failure(self):
        # Declining at the forecast is a cost decision, not an engineering failure: it must
        # not be laundered into project memory, and it should leave an escalation.
        self._enable_forecast(force=False, budget=5.0)
        p = mock.patch.object(loop, "_forecast_choice", lambda cfg, fc: ("abort", None))
        p.start(); self.addCleanup(p.stop)
        # no agents should be called — the run aborts before planning
        self.patch_agents(pm_script=[], dev_script=[])
        rc = loop.run(None, self.cfg)
        self.assertEqual(rc, 1)
        self.assertTrue((self.cfg.state_dir / "escalation.json").is_file())
        mem_file = self.cfg.state_dir / "memory.md"
        if mem_file.is_file():
            self.assertNotIn("aborted at the execution forecast", mem_file.read_text())
        self.assertTrue(self.state().get("_operational_stop"))

    def test_forecast_call_is_charged_to_budget(self):
        self._enable_forecast(force=True, budget=25.0)
        self._happy_script()
        loop.run(None, self.cfg)
        # the $0.01 forecast call is billed like every other call (budget rail stays honest)
        self.assertGreaterEqual(self.state()["total_cost_usd"], 0.01)

    def test_restore_constrained_on_resume(self):
        # On resume we skip re-forecasting; the constrained choice must be honored from state.
        led = Ledger.load_or_start(self.cfg)
        led.save_forecast({"constrained": True})
        cfg = Config(repo=self.cfg.repo)
        cfg.constrained = False
        loop._restore_constrained(cfg, led)
        self.assertTrue(cfg.constrained)


if __name__ == "__main__":
    unittest.main()
