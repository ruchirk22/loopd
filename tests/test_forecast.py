import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import forecast as F
from orchestrator.claude_cli import ClaudeResult
from orchestrator.config import Config
from orchestrator.forecast import (Calibration, EngineeringAnalysis, EstimatorConfig,
                                    Forecast, ForecastHistory, WeightedEstimator,
                                    accuracy_pct, apply_choice)
from orchestrator.ledger import Ledger
from orchestrator.plan import DONE, Plan, Step

ANALYSIS = {
    "estimated_steps": 11, "complexity": 72, "risk": "medium", "research_required": False,
    "likely_replans": 1, "likely_retries": 4,
    "verification_types": ["unit", "integration"], "confidence": 83,
}


def analysis(**over) -> EngineeringAnalysis:
    d = dict(ANALYSIS)
    d.update(over)
    return EngineeringAnalysis.from_dict(d)


# --------------------------------------------------------------- analysis parsing

class TestEngineeringAnalysis(unittest.TestCase):
    def test_valid_roundtrip(self):
        a = analysis()
        self.assertEqual(a.estimated_steps, 11)
        self.assertEqual(a.risk, "medium")
        self.assertEqual(a.to_dict()["verification_types"], ["unit", "integration"])

    def test_clamps_and_defaults(self):
        a = EngineeringAnalysis.from_dict({"estimated_steps": 0, "complexity": 250,
                                           "risk": "nonsense", "confidence": -5,
                                           "likely_replans": -3, "likely_retries": -1,
                                           "verification_types": []})
        self.assertEqual(a.estimated_steps, 1)          # floored at 1
        self.assertEqual(a.complexity, 100)             # clamped to 100
        self.assertEqual(a.risk, "medium")              # bad enum → medium
        self.assertEqual(a.confidence, 0)               # clamped to 0
        self.assertEqual(a.likely_replans, 0)           # negatives floored
        self.assertEqual(a.likely_retries, 0)
        self.assertEqual(a.verification_types, ["unit"])  # empty → default

    def test_empty_dict_is_safe(self):
        a = EngineeringAnalysis.from_dict({})
        self.assertGreaterEqual(a.estimated_steps, 1)


# --------------------------------------------------------------- deterministic estimator

class TestWeightedEstimator(unittest.TestCase):
    def setUp(self):
        self.est = WeightedEstimator(EstimatorConfig())

    def test_deterministic(self):
        a = analysis()
        f1 = self.est.estimate(a, 25.0, Calibration())
        f2 = self.est.estimate(a, 25.0, Calibration())
        self.assertEqual(f1.to_dict(), f2.to_dict())

    def test_positive_and_shaped(self):
        f = self.est.estimate(analysis(), 25.0, Calibration())
        self.assertGreater(f.estimated_cost_usd, 0)
        self.assertGreater(f.estimated_runtime_min, 0)
        self.assertEqual(f.estimated_steps, 11)
        self.assertEqual(f.confidence, 83)
        self.assertEqual(f.risk, "medium")

    def test_more_steps_costs_more(self):
        lo = self.est.estimate(analysis(estimated_steps=3), 25.0, Calibration())
        hi = self.est.estimate(analysis(estimated_steps=20), 25.0, Calibration())
        self.assertGreater(hi.estimated_cost_usd, lo.estimated_cost_usd)
        self.assertGreater(hi.estimated_runtime_min, lo.estimated_runtime_min)

    def test_complexity_raises_cost(self):
        lo = self.est.estimate(analysis(complexity=10), 25.0, Calibration())
        hi = self.est.estimate(analysis(complexity=95), 25.0, Calibration())
        self.assertGreater(hi.estimated_cost_usd, lo.estimated_cost_usd)

    def test_research_raises_cost(self):
        no = self.est.estimate(analysis(research_required=False), 25.0, Calibration())
        yes = self.est.estimate(analysis(research_required=True), 25.0, Calibration())
        self.assertGreater(yes.estimated_cost_usd, no.estimated_cost_usd)

    def test_more_verification_raises_cost(self):
        one = self.est.estimate(analysis(verification_types=["unit"]), 25.0, Calibration())
        many = self.est.estimate(
            analysis(verification_types=["unit", "integration", "e2e", "deploy"]), 25.0, Calibration())
        self.assertGreater(many.estimated_cost_usd, one.estimated_cost_usd)

    def test_retries_add_dev_calls(self):
        few = self.est.estimate(analysis(likely_retries=0), 25.0, Calibration())
        lots = self.est.estimate(analysis(likely_retries=12), 25.0, Calibration())
        self.assertGreater(lots.estimated_cost_usd, few.estimated_cost_usd)

    def test_constrained_and_gap(self):
        f = self.est.estimate(analysis(), 5.0, Calibration())
        self.assertTrue(f.constrained)
        self.assertAlmostEqual(f.budget_gap_usd, round(f.estimated_cost_usd - 5.0, 2))
        big = self.est.estimate(analysis(), 1000.0, Calibration())
        self.assertFalse(big.constrained)
        self.assertLess(big.budget_gap_usd, 0)

    def test_recommended_clears_estimate_and_scales_with_risk(self):
        f = self.est.estimate(analysis(), 25.0, Calibration())
        self.assertGreaterEqual(f.recommended_budget_usd, f.estimated_cost_usd)
        low = self.est.estimate(analysis(risk="low", confidence=90), 25.0, Calibration())
        high = self.est.estimate(analysis(risk="high", confidence=90), 25.0, Calibration())
        # same base work, higher risk ⇒ at least as much recommended headroom
        self.assertGreaterEqual(high.recommended_budget_usd, low.recommended_budget_usd)

    def test_calibration_scales_cost_and_runtime(self):
        base = self.est.estimate(analysis(), 25.0, Calibration(1.0, 1.0, 5))
        hotter = self.est.estimate(analysis(), 25.0, Calibration(1.5, 1.5, 5))
        self.assertAlmostEqual(hotter.estimated_cost_usd, base.estimated_cost_usd * 1.5, delta=0.5)
        self.assertGreater(hotter.estimated_runtime_min, base.estimated_runtime_min)


class TestEstimatorConfig(unittest.TestCase):
    def test_from_env_overrides(self):
        with mock.patch.dict(os.environ, {"FORECAST_COST_PER_PM_CALL_USD": "9.0",
                                          "FORECAST_CALIBRATION_MIN_SAMPLES": "7"}):
            c = EstimatorConfig.from_env()
        self.assertEqual(c.cost_per_pm_call_usd, 9.0)
        self.assertEqual(c.calibration_min_samples, 7)

    def test_defaults_when_unset(self):
        c = EstimatorConfig.from_env()
        self.assertEqual(c.cost_per_pm_call_usd, EstimatorConfig().cost_per_pm_call_usd)


# --------------------------------------------------------------- history + calibration

class TestForecastHistory(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.h = ForecastHistory(self.repo)

    def _record(self, pc, ac, pr=10.0, ar=10.0, success=True):
        return {"predicted": {"estimated_cost_usd": pc, "estimated_runtime_min": pr},
                "actual": {"cost_usd": ac, "runtime_min": ar}, "run_success": success}

    def test_append_and_load_roundtrip(self):
        self.h.append(self._record(10, 12))
        self.h.append(self._record(20, 18))
        recs = self.h.load()
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["actual"]["cost_usd"], 12)

    def test_survives_in_agentic_dir(self):
        self.h.append(self._record(10, 12))
        self.assertTrue((self.repo / ".agentic" / "forecasts.jsonl").is_file())

    def test_load_skips_garbage_lines(self):
        self.h.path.parent.mkdir(parents=True, exist_ok=True)
        self.h.path.write_text('{"predicted":{}}\nnot json\n\n[1,2]\n')
        self.assertEqual(len(self.h.load()), 1)  # only the dict line

    def test_calibration_identity_below_min_samples(self):
        cfg = EstimatorConfig()
        self.h.append(self._record(10, 20))  # 1 sample < min (3)
        cal = self.h.calibration(cfg)
        self.assertEqual(cal.cost_factor, 1.0)
        self.assertEqual(cal.samples, 1)

    def test_calibration_uses_median_ratio_when_enough(self):
        cfg = EstimatorConfig()
        for _ in range(4):
            self.h.append(self._record(10, 20))  # actual double predicted → ratio 2.0
        cal = self.h.calibration(cfg)
        self.assertGreater(cal.cost_factor, 1.0)
        self.assertLessEqual(cal.cost_factor, cfg.calibration_clamp_hi)  # clamped
        self.assertEqual(cal.samples, 4)

    def test_calibration_ignores_failed_runs(self):
        cfg = EstimatorConfig()
        for _ in range(4):
            self.h.append(self._record(10, 50, success=False))
        cal = self.h.calibration(cfg)
        self.assertEqual(cal.cost_factor, 1.0)  # no successful samples → identity

    def test_accuracy_none_when_empty(self):
        self.assertIsNone(self.h.accuracy())

    def test_accuracy_computed(self):
        self.h.append(self._record(10, 10, 10, 10))  # perfect
        self.assertEqual(self.h.accuracy(), 100.0)

    def test_calibration_recent_zero_returns_identity(self):
        cfg = EstimatorConfig()
        cfg.calibration_recent = 0
        for _ in range(5):
            self.h.append(self._record(10, 20))
        cal = self.h.calibration(cfg)
        self.assertEqual(cal.cost_factor, 1.0)   # empty window → no calibration

    def test_history_dedupes_by_run_id(self):
        # A run resumed after a stop appends a partial then a final row with the SAME run_id;
        # accuracy/calibration must count it once (latest wins).
        partial = {**self._record(10, 5, success=False), "run_id": "run-A"}
        final = {**self._record(10, 10, success=True), "run_id": "run-A"}
        self.h.append(partial)
        self.h.append(final)
        self.assertEqual(self.h.accuracy(), 100.0)  # only the final (perfect) row counts


# --------------------------------------------------------------- decision mapping

class TestApplyChoice(unittest.TestCase):
    def setUp(self):
        self.fc = WeightedEstimator(EstimatorConfig()).estimate(analysis(), 5.0, Calibration())

    def test_raise_uses_recommended(self):
        d = apply_choice(self.fc, "raise")
        self.assertEqual(d.action, "raise")
        self.assertEqual(d.budget_usd, self.fc.recommended_budget_usd)
        self.assertFalse(d.constrained)

    def test_continue_when_short_is_constrained(self):
        d = apply_choice(self.fc, "continue")
        self.assertEqual(d.budget_usd, self.fc.budget_usd)
        self.assertTrue(d.constrained)

    def test_continue_when_covered_is_not_constrained(self):
        fc = WeightedEstimator(EstimatorConfig()).estimate(analysis(), 1000.0, Calibration())
        d = apply_choice(fc, "continue")
        self.assertEqual(d.action, "proceed")
        self.assertFalse(d.constrained)

    def test_edit_sets_budget_and_constrained_by_comparison(self):
        low = apply_choice(self.fc, "edit", 1.0)
        self.assertEqual(low.budget_usd, 1.0)
        self.assertTrue(low.constrained)
        high = apply_choice(self.fc, "edit", 999.0)
        self.assertFalse(high.constrained)

    def test_abort(self):
        self.assertEqual(apply_choice(self.fc, "abort").action, "abort")


class TestAccuracy(unittest.TestCase):
    def test_perfect(self):
        self.assertEqual(accuracy_pct({"estimated_cost_usd": 10, "estimated_runtime_min": 5},
                                      {"cost_usd": 10, "runtime_min": 5}), 100.0)

    def test_off_is_less_than_100(self):
        self.assertLess(accuracy_pct({"estimated_cost_usd": 10, "estimated_runtime_min": 10},
                                     {"cost_usd": 20, "runtime_min": 5}), 100.0)

    def test_missing_fields_safe(self):
        self.assertIsInstance(accuracy_pct({}, {}), float)


class TestRendering(unittest.TestCase):
    def test_card_has_key_lines(self):
        fc = WeightedEstimator(EstimatorConfig()).estimate(analysis(), 5.0, Calibration())
        card = F.render_card(fc)
        self.assertIn("EXECUTION FORECAST", card)
        self.assertIn("Estimated Cost", card)
        self.assertIn("Recommended", card)  # shown because constrained

    def test_comparison_has_accuracy(self):
        fc = WeightedEstimator(EstimatorConfig()).estimate(analysis(), 25.0, Calibration())
        out = F.render_comparison(fc.to_dict(),
                                  {"cost_usd": fc.estimated_cost_usd, "runtime_min": 5, "steps_done": 3})
        self.assertIn("Prediction Accuracy", out)
        self.assertIn("Predicted", out)


# --------------------------------------------------------------- planner call + full forecast

def _fake_claude(structured, ok=True, cost=0.01):
    return lambda *a, **k: ClaudeResult(ok=ok, text=json.dumps(structured or {}), session_id=None,
                                        cost_usd=cost, structured=structured, raw={})


class TestRunForecast(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.cfg = Config(repo=self.repo)

    def test_disabled_returns_none(self):
        self.cfg.forecast_enabled = False
        self.assertIsNone(F.run_forecast(self.cfg, "build a thing", 25.0))

    def test_analysis_failure_degrades_to_none(self):
        with mock.patch.object(F, "run_claude", _fake_claude(None, ok=False)):
            self.assertIsNone(F.run_forecast(self.cfg, "build a thing", 25.0))

    def test_happy_path_returns_forecast(self):
        with mock.patch.object(F, "run_claude", _fake_claude(ANALYSIS)):
            fc = F.run_forecast(self.cfg, "build a thing", 25.0)
        self.assertIsInstance(fc, Forecast)
        self.assertEqual(fc.estimated_steps, 11)

    def test_charges_ledger_when_provided(self):
        led = Ledger.load_or_start(self.cfg)
        before = led.state["total_cost_usd"]
        with mock.patch.object(F, "run_claude", _fake_claude(ANALYSIS, cost=0.05)):
            F.run_forecast(self.cfg, "build a thing", 25.0, ledger=led)
        self.assertAlmostEqual(led.state["total_cost_usd"], before + 0.05)


# --------------------------------------------------------------- ledger actuals integration

class TestLedgerForecastActuals(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        (self.repo / "app.txt").write_text("v1\n")
        self.cfg = Config(repo=self.repo)
        self.led = Ledger.load_or_start(self.cfg)

    def _commit_step(self, sid, fname):
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.repo),
                              capture_output=True, text=True).stdout.strip()
        step = Step(id=sid, goal="g", acceptance_criteria=["a"], verify=["true"])
        step.base_sha = base
        (self.repo / fname).write_text("content\n")
        step.commit_sha = self.led.commit_step(step, f"step {sid}")
        step.status = DONE
        step.attempts = 2
        step.rejections = 1
        return step

    def test_save_forecast_persists(self):
        self.led.save_forecast({"estimated_cost_usd": 10})
        st = json.loads((self.cfg.state_dir / "state.json").read_text())
        self.assertEqual(st["forecast"]["estimated_cost_usd"], 10)

    def test_run_actuals_from_plan(self):
        plan = Plan(steps=[self._commit_step("1", "a.txt"), self._commit_step("2", "b.txt")])
        self.led.spend(3.0)
        act = self.led.run_actuals(plan)
        self.assertEqual(act["steps_done"], 2)
        self.assertEqual(act["attempts"], 4)      # 2 + 2
        self.assertEqual(act["retries"], 2)       # (2-1)+(2-1)
        self.assertEqual(act["rejections"], 2)
        self.assertEqual(act["files_changed"], 2)  # union of each step's own base..commit diff
        self.assertAlmostEqual(act["cost_usd"], 3.0)

    def test_run_actuals_prefers_active_runtime(self):
        self.led.state["active_runtime_s"] = 120.0  # 2 minutes of active work
        self.led.state["started"] = 0.0             # would be a huge wall-clock if used
        act = self.led.run_actuals(Plan(steps=[]))
        self.assertEqual(act["runtime_min"], 2.0)   # uses active runtime, not since-start

    def test_record_includes_run_id_and_string_task(self):
        self.led.state["task"] = "build the thing\nmore detail"
        self.led.save_forecast({"estimated_cost_usd": 10})
        self.led.record_forecast_actuals(Plan(steps=[]), 0)
        rec = ForecastHistory(self.repo).load()[0]
        self.assertEqual(rec["run_id"], self.led.state["started"])
        self.assertIsInstance(rec["task"], str)          # a capped string, not a list
        self.assertEqual(rec["task"], "build the thing")

    def test_record_forecast_actuals_appends_history(self):
        plan = Plan(steps=[self._commit_step("1", "a.txt")])
        self.led.save_forecast({"estimated_cost_usd": 10, "estimated_runtime_min": 5,
                                "estimated_steps": 1})
        graded = self.led.record_forecast_actuals(plan, 0)
        self.assertIsNotNone(graded)
        predicted, actual = graded
        self.assertIn("actual", self.led.state["forecast"])
        recs = ForecastHistory(self.repo).load()
        self.assertEqual(len(recs), 1)
        self.assertTrue(recs[0]["run_success"])

    def test_record_forecast_actuals_none_without_forecast(self):
        plan = Plan(steps=[self._commit_step("1", "a.txt")])
        self.assertIsNone(self.led.record_forecast_actuals(plan, 0))


class TestForecastOnlyBriefPrecedence(unittest.TestCase):
    """run.py --forecast-only must resolve the brief with run-like precedence:
    --brief > explicit task > stored .agentic/brief.md."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        (self.repo / ".agentic").mkdir(parents=True, exist_ok=True)

    def _forecast_brief(self, task=None, brief_path=None):
        import run as runmod
        cfg = Config(repo=self.repo)
        if brief_path:
            cfg.brief_path = Path(brief_path)
        holder = {}
        fc = WeightedEstimator(EstimatorConfig()).estimate(analysis(), 25.0, Calibration())

        def fake(cfg_, brief, budget, ledger=None):
            holder["brief"] = brief
            return fc
        with mock.patch.object(F, "run_forecast", fake):
            code = runmod._forecast_only(task, cfg, as_json=True)
        return code, holder.get("brief")

    def test_explicit_task_beats_stale_brief_md(self):
        (self.repo / ".agentic" / "brief.md").write_text("OLD stale brief")
        code, brief = self._forecast_brief(task="build the NEW thing")
        self.assertEqual(code, 0)
        self.assertIn("build the NEW thing", brief)
        self.assertNotIn("OLD stale", brief)

    def test_brief_file_wins_over_everything(self):
        bf = self.repo / "spec.md"
        bf.write_text("CURATED brief from a file")
        (self.repo / ".agentic" / "brief.md").write_text("stale")
        code, brief = self._forecast_brief(task="some task", brief_path=bf)
        self.assertIn("CURATED brief", brief)

    def test_stored_brief_used_when_no_task(self):
        (self.repo / ".agentic" / "brief.md").write_text("from handoff")
        code, brief = self._forecast_brief(task=None)
        self.assertIn("from handoff", brief)

    def test_nothing_to_forecast_exits_2(self):
        code, brief = self._forecast_brief(task=None)  # no brief.md, no task
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
