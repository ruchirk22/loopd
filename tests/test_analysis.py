import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import analysis as A

PM_RAW = {
    "summary": "Step 6 couldn't pass its checks.",
    "root_cause": "The revoke test needs Redis; the transcript shows 'Connection refused'. "
                  "Environment gap, not a code bug.",
    "category": "environment", "confidence": 82,
    "options": [
        {"label": "Add a Redis test fixture", "detail": "Spin Redis up for the test.",
         "kind": "loopd_fix", "recommended": True},
        {"label": "You set REDIS_URL", "detail": "Point at your own Redis.", "kind": "user_action"},
        {"label": "Skip this step", "kind": "descope"},
    ],
}


class TestFromDict(unittest.TestCase):
    def test_parses_and_ids(self):
        fa = A.FailureAnalysis.from_dict({**PM_RAW, "step": "6"})
        self.assertEqual(fa.category, "environment")
        self.assertEqual(fa.confidence, 82)
        self.assertEqual(len(fa.options), 3)
        self.assertTrue(all(o.id for o in fa.options))
        self.assertEqual(fa.recommended.label, "Add a Redis test fixture")

    def test_exactly_one_recommended(self):
        raw = {**PM_RAW, "options": [{"label": "A", "kind": "abort"}, {"label": "B", "kind": "abort"}]}
        fa = A.FailureAnalysis.from_dict(raw)
        self.assertEqual(sum(o.recommended for o in fa.options), 1)  # first, since none marked

    def test_multiple_recommended_collapsed(self):
        raw = {**PM_RAW, "options": [{"label": "A", "kind": "abort", "recommended": True},
                                     {"label": "B", "kind": "abort", "recommended": True}]}
        fa = A.FailureAnalysis.from_dict(raw)
        self.assertEqual(sum(o.recommended for o in fa.options), 1)

    def test_bad_category_and_kind_default(self):
        fa = A.FailureAnalysis.from_dict({"summary": "x", "root_cause": "y", "category": "nonsense",
                                          "options": [{"label": "go", "kind": "weird"}]})
        self.assertEqual(fa.category, "unknown")
        self.assertEqual(fa.options[0].kind, "loopd_fix")

    def test_empty_options_gets_safe_default(self):
        fa = A.FailureAnalysis.from_dict({"summary": "x", "root_cause": "y", "options": []})
        self.assertGreaterEqual(len(fa.options), 1)
        self.assertIsNotNone(fa.recommended)

    def test_confidence_clamped(self):
        self.assertEqual(A.FailureAnalysis.from_dict({**PM_RAW, "confidence": 250}).confidence, 100)
        self.assertIsNone(A.FailureAnalysis.from_dict({**PM_RAW, "confidence": "x"}).confidence)


class TestFallback(unittest.TestCase):
    def test_budget(self):
        fa = A.fallback("budget_exceeded", "3", "Budget $25 exceeded")
        self.assertEqual(fa.category, "resource")
        self.assertIn("continue", fa.recommended.label.lower())

    def test_replan_cap_low_confidence(self):
        fa = A.fallback("replan_cap_exhausted", "2", "kept failing")
        self.assertLess(fa.confidence, 50)
        self.assertTrue(any(o.kind == "descope" for o in fa.options))

    def test_non_actionable_returns_none(self):
        self.assertIsNone(A.fallback("forecast_declined"))
        self.assertIsNone(A.fallback("invalid_plan"))

    def test_git_and_internal(self):
        self.assertEqual(A.fallback("git_error").category, "environment")
        self.assertEqual(A.fallback("unexpected_error").category, "unknown")


class TestPersistAndResolve(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        (self.repo / ".agentic").mkdir()

    def _write(self, fa):
        d = fa.to_dict()
        (self.repo / ".agentic" / "analysis.json").write_text(json.dumps(d))

    def test_load_roundtrip(self):
        self._write(A.FailureAnalysis.from_dict({**PM_RAW, "step": "6"}))
        fa = A.load(self.repo)
        self.assertIsNotNone(fa)
        self.assertEqual(fa.step, "6")

    def test_load_missing(self):
        self.assertIsNone(A.load(Path(tempfile.mkdtemp())))

    def test_resolve_recommended(self):
        self._write(A.FailureAnalysis.from_dict({**PM_RAW, "step": "6"}))
        ch = A.resolve_choice(self.repo, recommended=True)
        self.assertEqual(ch["kind"], "loopd_fix")
        self.assertEqual(ch["step"], "6")
        self.assertIn("Redis", ch["guidance"])

    def test_resolve_by_id(self):
        fa = A.FailureAnalysis.from_dict({**PM_RAW, "step": "6"})
        self._write(fa)
        skip = next(o for o in fa.options if o.kind == "descope")
        ch = A.resolve_choice(self.repo, option_id=skip.id)
        self.assertEqual(ch["kind"], "descope")

    def test_resolve_none_when_no_analysis(self):
        self.assertIsNone(A.resolve_choice(Path(tempfile.mkdtemp()), recommended=True))


class TestRender(unittest.TestCase):
    def test_has_all_four_beats(self):
        out = A.render(A.FailureAnalysis.from_dict({**PM_RAW, "step": "6"}))
        for beat in (A.BEAT_WHAT, A.BEAT_WHY, A.BEAT_DO, A.BEAT_OTHER):
            self.assertIn(beat, out)
        self.assertIn("Add a Redis test fixture", out)
        self.assertIn("~82% sure", out)

    def test_confidence_phrase_bands(self):
        self.assertIn("sure", A.confidence_phrase(90))
        self.assertIn("confirming", A.confidence_phrase(50))
        self.assertIn("not certain", A.confidence_phrase(20))
        self.assertEqual(A.confidence_phrase(None), "")


if __name__ == "__main__":
    unittest.main()
