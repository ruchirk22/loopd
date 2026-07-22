import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import confidence as C
from orchestrator.config import Config
from orchestrator.confidence import (ConfidenceConfig, ConfidenceInputs, ConfidenceHistory,
                                      WeightedScorer, assess_delivery, assess_plan)
from orchestrator.ledger import Ledger
from orchestrator.plan import DONE, SKIPPED, PENDING, Plan, Step


def step(sid="s1", crit=("c1",), verify=("pytest -q",), status=DONE,
         evidenced=None, rejections=0):
    """A Step with N criteria; `evidenced` = how many criteria are backed by satisfied evidence."""
    crit = list(crit)
    n_ev = len(crit) if evidenced is None else evidenced
    ev = [{"criterion": crit[i], "satisfied": True, "evidence": "x"} for i in range(n_ev)]
    return Step(id=sid, goal="g", acceptance_criteria=crit, verify=list(verify),
                status=status, criteria_evidence=ev, rejections=rejections)


PERFECT = ConfidenceInputs(
    criteria_evidenced=4, criteria_total=4, steps_done=2, steps_total=2,
    steps_behavior_verified=2, steps_depth_pool=2, final_verify_passed=True,
    rejections=0, replans=0, high_risk_accepts=0,
    tenancy_declared=False, isolation_gate_present=False,
)


# --------------------------------------------------------------- scorer math

class TestScorerFactors(unittest.TestCase):
    def setUp(self):
        self.s = WeightedScorer(ConfidenceConfig())

    def test_perfect_delivery_scores_100(self):
        rep = self.s.score(PERFECT, "delivery")
        self.assertEqual(rep.score, 100)
        self.assertEqual(rep.band, "Very High")
        self.assertTrue(rep.meets_bar)

    def test_empty_delivery_scores_0(self):
        rep = self.s.score(ConfidenceInputs(), "delivery")
        self.assertEqual(rep.score, 0)
        self.assertEqual(rep.band, "Low")
        self.assertFalse(rep.meets_bar)

    def test_failed_final_verify_drops_score(self):
        passed = self.s.score(PERFECT, "delivery").score
        failed = self.s.score(ConfidenceInputs(**{**PERFECT.to_dict(), "final_verify_passed": False}),
                              "delivery").score
        # Losing the final-verify factor drops the score by exactly its normalized weight.
        c = ConfidenceConfig()
        expected = round(100 * c.w_final_verify / (c.w_coverage + c.w_completion + c.w_final_verify
                                                   + c.w_depth + c.w_stability + c.w_integrity))
        self.assertEqual(passed - failed, expected)

    def test_partial_coverage_lowers_score(self):
        half = ConfidenceInputs(**{**PERFECT.to_dict(), "criteria_evidenced": 2})  # 2/4
        rep = self.s.score(half, "delivery")
        self.assertLess(rep.score, 100)
        cov = next(f for f in rep.factors if f.key == "coverage")
        self.assertAlmostEqual(cov.value, 0.5)

    def test_unit_only_depth_hits_floor_not_zero(self):
        unit = ConfidenceInputs(**{**PERFECT.to_dict(), "steps_behavior_verified": 0})
        rep = self.s.score(unit, "delivery")
        depth = next(f for f in rep.factors if f.key == "depth")
        self.assertAlmostEqual(depth.value, ConfidenceConfig().depth_floor)  # 0.5, not 0

    def test_behavior_gate_raises_depth_above_floor(self):
        unit = self.s.score(ConfidenceInputs(**{**PERFECT.to_dict(), "steps_behavior_verified": 0}),
                            "delivery")
        full = self.s.score(PERFECT, "delivery")
        du = next(f for f in unit.factors if f.key == "depth").value
        df = next(f for f in full.factors if f.key == "depth").value
        self.assertGreater(df, du)
        self.assertAlmostEqual(df, 1.0)

    def test_tenancy_declared_without_isolation_penalizes_depth(self):
        base = ConfidenceInputs(**{**PERFECT.to_dict(),
                                   "tenancy_declared": True, "isolation_gate_present": False})
        rep = self.s.score(base, "delivery")
        depth = next(f for f in rep.factors if f.key == "depth")
        self.assertAlmostEqual(depth.value, 1.0 * ConfidenceConfig().tenancy_unproven_mult)
        self.assertIn("tenancy", depth.note.lower())

    def test_tenancy_with_isolation_no_penalty(self):
        base = ConfidenceInputs(**{**PERFECT.to_dict(),
                                   "tenancy_declared": True, "isolation_gate_present": True})
        depth = next(f for f in self.s.score(base, "delivery").factors if f.key == "depth")
        self.assertAlmostEqual(depth.value, 1.0)

    def test_churn_lowers_stability(self):
        churny = ConfidenceInputs(**{**PERFECT.to_dict(), "rejections": 3, "replans": 3})
        rep = self.s.score(churny, "delivery")
        stab = next(f for f in rep.factors if f.key == "stability")
        self.assertLess(stab.value, 1.0)

    def test_high_risk_accepts_lower_integrity(self):
        risky = ConfidenceInputs(**{**PERFECT.to_dict(), "high_risk_accepts": 2})
        integ = next(f for f in self.s.score(risky, "delivery").factors if f.key == "integrity")
        self.assertAlmostEqual(integ.value, 1 - 2 * ConfidenceConfig().high_risk_penalty)

    def test_deterministic(self):
        a = self.s.score(PERFECT, "delivery").score
        b = self.s.score(PERFECT, "delivery").score
        self.assertEqual(a, b)

    def test_score_bounded_0_100(self):
        for inp in (PERFECT, ConfidenceInputs(), ConfidenceInputs(high_risk_accepts=100)):
            self.assertTrue(0 <= self.s.score(inp, "delivery").score <= 100)


# --------------------------------------------------------------- bands

class TestBands(unittest.TestCase):
    def test_band_thresholds(self):
        c = ConfidenceConfig()
        self.assertEqual(c.band(95), "Very High")
        self.assertEqual(c.band(80), "High")
        self.assertEqual(c.band(60), "Moderate")
        self.assertEqual(c.band(20), "Low")

    def test_meets_bar_at_75(self):
        c = ConfidenceConfig()
        self.assertEqual(c.band(75), "High")
        # meets_bar is score >= band_high (75)
        s = WeightedScorer(c)
        rep = s.score(ConfidenceInputs(criteria_evidenced=1, criteria_total=1, steps_done=1,
                                       steps_total=1, steps_behavior_verified=1, steps_depth_pool=1,
                                       final_verify_passed=True), "delivery")
        self.assertTrue(rep.meets_bar)


# --------------------------------------------------------------- config from env

class TestConfigFromEnv(unittest.TestCase):
    def test_defaults_when_unset(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            for k in list(__import__("os").environ):
                if k.startswith("CONFIDENCE_"):
                    del __import__("os").environ[k]
            c = ConfidenceConfig.from_env()
            self.assertEqual(c.w_coverage, ConfidenceConfig().w_coverage)

    def test_env_override(self):
        with mock.patch.dict("os.environ", {"CONFIDENCE_BAND_HIGH": "60",
                                            "CONFIDENCE_W_COVERAGE": "0.9"}):
            c = ConfidenceConfig.from_env()
            self.assertEqual(c.band_high, 60.0)
            self.assertEqual(c.w_coverage, 0.9)
            self.assertEqual(c.band(65), "High")  # threshold moved down


# --------------------------------------------------------------- gather inputs

class TestInputsFromDelivery(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        (self.repo / "app.txt").write_text("v1\n")
        self.cfg = Config(repo=self.repo)
        self.led = Ledger.load_or_start(self.cfg)

    def test_counts_from_plan_and_state(self):
        plan = Plan(steps=[
            step("1", crit=("a", "b"), verify=("pytest -q",)),                 # unit
            step("2", crit=("c",), verify=("python -m orchestrator.probe flow --file f.json",)),
            step("3", crit=("d",), status=SKIPPED),                            # descoped
        ])
        self.led.state["replans_used"] = 1
        self.led.state["high_risk_accepts"] = 1
        inp = C.inputs_from_delivery(self.cfg, plan, self.led, 0)
        self.assertEqual(inp.criteria_total, 3)       # only DONE steps' criteria
        self.assertEqual(inp.criteria_evidenced, 3)
        self.assertEqual(inp.steps_done, 2)
        self.assertEqual(inp.steps_total, 3)
        self.assertEqual(inp.steps_behavior_verified, 1)  # the flow-gate step
        self.assertTrue(inp.final_verify_passed)
        self.assertEqual(inp.replans, 1)
        self.assertEqual(inp.high_risk_accepts, 1)

    def test_final_verify_reflects_code(self):
        plan = Plan(steps=[step("1")])
        self.assertTrue(C.inputs_from_delivery(self.cfg, plan, self.led, 0).final_verify_passed)
        self.assertFalse(C.inputs_from_delivery(self.cfg, plan, self.led, 3).final_verify_passed)

    def test_isolation_gate_detected(self):
        plan = Plan(steps=[step("1", verify=("python -m orchestrator.probe isolation --file i.json",))])
        inp = C.inputs_from_delivery(self.cfg, plan, self.led, 0)
        self.assertTrue(inp.isolation_gate_present)

    def test_tenancy_declared_from_spine(self):
        from orchestrator import architecture
        # No spine → not declared.
        self.assertFalse(C.inputs_from_delivery(self.cfg, Plan(steps=[step()]), self.led, 0)
                         .tenancy_declared)
        # Write a spine declaring RLS.
        with mock.patch.object(architecture, "tenancy_strategy", return_value="rls"):
            self.assertTrue(C.inputs_from_delivery(self.cfg, Plan(steps=[step()]), self.led, 0)
                            .tenancy_declared)


# --------------------------------------------------------------- plan ceiling

class TestPlanCeiling(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.cfg = Config(repo=self.repo)

    def test_unit_only_plan_capped_below_100(self):
        plan = Plan(steps=[step("1", verify=("pytest -q",), status=PENDING),
                           step("2", verify=("npm test",), status=PENDING)])
        rep = assess_plan(self.cfg, plan)
        self.assertEqual(rep.kind, "plan-ceiling")
        self.assertLess(rep.score, 100)   # depth floor caps it

    def test_behavior_plan_reaches_100(self):
        plan = Plan(steps=[
            step("1", verify=("python -m orchestrator.probe flow --file f.json",), status=PENDING),
            step("2", verify=("python -m orchestrator.probe http --url x",), status=PENDING)])
        rep = assess_plan(self.cfg, plan)
        self.assertEqual(rep.score, 100)

    def test_ceiling_ignores_actual_incompletion(self):
        # Even with pending steps, the ceiling assumes full completion.
        plan = Plan(steps=[step("1", verify=("python -m orchestrator.probe flow --file f.json",),
                                 status=PENDING)])
        rep = assess_plan(self.cfg, plan)
        comp = next(f for f in rep.factors if f.key == "completion")
        self.assertAlmostEqual(comp.value, 1.0)


# --------------------------------------------------------------- history

class TestHistory(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())

    def test_append_load_average(self):
        h = ConfidenceHistory(self.repo)
        self.assertIsNone(h.average())
        h.append({"score": 80})
        h.append({"score": 90})
        self.assertEqual(len(h.load()), 2)
        self.assertEqual(h.average(), 85.0)

    def test_survives_in_agentic(self):
        h = ConfidenceHistory(self.repo)
        h.append({"score": 70})
        self.assertTrue((self.repo / ".agentic" / "confidence.jsonl").is_file())

    def test_skips_corrupt_lines(self):
        h = ConfidenceHistory(self.repo)
        h.path.parent.mkdir(parents=True, exist_ok=True)
        h.path.write_text('{"score": 50}\nnot json\n{"score": 90}\n')
        self.assertEqual(len(h.load()), 2)
        self.assertEqual(h.average(), 70.0)


# --------------------------------------------------------------- rendering

class TestRender(unittest.TestCase):
    def test_card_has_score_band_factors(self):
        rep = WeightedScorer(ConfidenceConfig()).score(PERFECT, "delivery")
        card = C.render_card(rep)
        self.assertIn("DELIVERY CONFIDENCE", card)
        self.assertIn("100%", card)
        self.assertIn("Very High", card)
        self.assertIn("Evidence coverage", card)

    def test_ceiling_card_shows_depth_hint_when_below_bar(self):
        low = ConfidenceInputs(criteria_evidenced=1, criteria_total=1, steps_done=1, steps_total=1,
                               steps_behavior_verified=0, steps_depth_pool=1, final_verify_passed=True)
        rep = WeightedScorer(ConfidenceConfig()).score(low, "plan-ceiling")
        card = C.render_card(rep)
        self.assertIn("PLAN CONFIDENCE (CEILING)", card)
        if not rep.meets_bar:
            self.assertIn("behavior gates", card)

    def test_one_line(self):
        rep = WeightedScorer(ConfidenceConfig()).score(PERFECT, "delivery")
        self.assertEqual(C.one_line(rep), "100% (Very High)")


# --------------------------------------------------------------- ledger integration

class TestLedgerRecordConfidence(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        (self.repo / "app.txt").write_text("v1\n")
        self.cfg = Config(repo=self.repo)
        self.led = Ledger.load_or_start(self.cfg)

    def test_persists_json_and_history(self):
        plan = Plan(steps=[step("1", verify=("python -m orchestrator.probe flow --file f.json",))])
        rep = self.led.record_confidence(plan, 0)
        self.assertIsNotNone(rep)
        cj = self.cfg.state_dir / "confidence.json"
        self.assertTrue(cj.is_file())
        d = json.loads(cj.read_text())
        self.assertEqual(d["score"], rep.score)
        self.assertEqual(len(ConfidenceHistory(self.repo).load()), 1)

    def test_disabled_returns_none(self):
        self.cfg.confidence_enabled = False
        self.assertIsNone(self.led.record_confidence(Plan(steps=[step()]), 0))

    def test_report_line_present(self):
        plan = Plan(steps=[step("1")])
        # mark it done + committed so write_report's coverage block runs
        plan.steps[0].commit_sha = "abc123def"
        path = self.led.write_report(plan, 0)
        self.assertIn("Delivery confidence", path.read_text())


# --------------------------------------------------------------- PR body

class TestDashboardSnapshot(unittest.TestCase):
    def test_snapshot_exposes_confidence(self):
        repo = Path(tempfile.mkdtemp())
        (repo / "app.txt").write_text("v1\n")
        cfg = Config(repo=repo)
        led = Ledger.load_or_start(cfg)
        led.save_plan(Plan(steps=[step("1")]))
        led.record_confidence(led.load_plan(), 0)
        from orchestrator import dashboard
        snap = dashboard.snapshot(repo)
        self.assertIsNotNone(snap.get("confidence"))
        self.assertIn("score", snap["confidence"])


class TestPRBody(unittest.TestCase):
    def test_confidence_in_pr_body(self):
        from orchestrator.github import build_pr_body
        conf = {"score": 82, "band": "High", "meets_bar": True,
                "factors": [{"key": "coverage", "note": "5/5 criteria backed by cited evidence"}]}
        body = build_pr_body("do a thing", [{"status": "done", "goal": "x"}], None, None, [],
                             True, confidence=conf)
        self.assertIn("Delivery confidence", body)
        self.assertIn("82%", body)
        self.assertIn("meets the >75% bar", body)

    def test_no_confidence_ok(self):
        from orchestrator.github import build_pr_body
        body = build_pr_body("t", [{"status": "done", "goal": "x"}], None, None, [], True)
        self.assertIn("Verification", body)


if __name__ == "__main__":
    unittest.main()
