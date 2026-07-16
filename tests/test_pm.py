import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.plan import Step
from orchestrator.pm import directive_schema, validate_directive, verify_evidence


def step():
    return Step(id="1", goal="g", acceptance_criteria=["endpoint returns 200", "test added"],
                verify=["pytest -q"])


HANDOVER = ("### Gate verdict\nALL GATES PASSED\n"
            "```diff\n+def health(): return {'status': 'ok'}\n```\n"
            "test_health.py passed 1 test in 0.2s")


class TestDynamicSchema(unittest.TestCase):
    def test_verdict_enum_is_exactly_what_is_allowed(self):
        s = directive_schema(["replan", "descope", "abort"])
        self.assertEqual(s["properties"]["verdict"]["enum"], ["replan", "descope", "abort"])
        self.assertNotIn("commit_message", s["properties"])   # accept fields absent
        self.assertNotIn("final_verify", s["properties"])

    def test_accept_fields_only_when_accept_allowed(self):
        s = directive_schema(["accept", "reject", "abort"])
        self.assertIn("commit_message", s["properties"])
        self.assertIn("criteria_evidence", s["properties"])
        self.assertIn("next_prompt", s["properties"])         # for reject


class TestValidateDirective(unittest.TestCase):
    def test_disallowed_verdict(self):
        probs = validate_directive({"verdict": "accept", "reasoning": "r"},
                                   ["replan", "abort"], step())
        self.assertTrue(any("not permitted" in p for p in probs))

    def test_dispatch_requires_prompt(self):
        probs = validate_directive({"verdict": "dispatch", "reasoning": "r"},
                                   ["dispatch"], None)
        self.assertTrue(any("next_prompt" in p for p in probs))

    def test_replan_requires_mutations(self):
        probs = validate_directive({"verdict": "replan", "reasoning": "r"},
                                   ["replan"], None)
        self.assertTrue(any("plan_mutations" in p for p in probs))

    def test_task_complete_rejects_trivial_final_verify(self):
        d = {"verdict": "task_complete", "reasoning": "r", "final_verify": ["echo done"]}
        probs = validate_directive(d, ["task_complete"], None)
        self.assertTrue(any("trivially-true" in p for p in probs))
        d["final_verify"] = []
        probs = validate_directive(d, ["task_complete"], None)
        self.assertTrue(any("non-empty final_verify" in p for p in probs))

    def test_valid_accept_passes(self):
        d = {"verdict": "accept", "reasoning": "looks right", "commit_message": "step 1",
             "criteria_evidence": [
                 {"criterion": "endpoint returns 200", "satisfied": True,
                  "evidence": "def health(): return {'status': 'ok'}"},
                 {"criterion": "test added", "satisfied": True,
                  "evidence": "test_health.py passed 1 test in 0.2s"},
             ]}
        self.assertEqual(validate_directive(d, ["accept", "reject"], step(), HANDOVER), [])


class TestEvidence(unittest.TestCase):
    def test_missing_evidence_entries(self):
        probs = verify_evidence({"criteria_evidence": []}, step(), HANDOVER)
        self.assertTrue(any("every acceptance criterion" in p for p in probs))

    def test_fabricated_quote_detected(self):
        d = {"criteria_evidence": [
            {"criterion": "c1", "satisfied": True, "evidence": "this text is nowhere in the packet"},
            {"criterion": "c2", "satisfied": True, "evidence": "ALL GATES PASSED"},
        ]}
        probs = verify_evidence(d, step(), HANDOVER)
        self.assertEqual(len(probs), 1)
        self.assertIn("not an exact quote", probs[0])

    def test_unsatisfied_criterion_blocks_accept(self):
        d = {"criteria_evidence": [
            {"criterion": "c1", "satisfied": False, "evidence": "whatever"},
            {"criterion": "c2", "satisfied": True, "evidence": "ALL GATES PASSED"},
        ]}
        probs = verify_evidence(d, step(), HANDOVER)
        self.assertTrue(any("cannot accept" in p for p in probs))

    def test_whitespace_normalized_quotes_match(self):
        d = {"criteria_evidence": [
            {"criterion": "c1", "satisfied": True,
             "evidence": "def health():   return {'status': 'ok'}"},
            {"criterion": "c2", "satisfied": True, "evidence": "ALL  GATES  PASSED"},
        ]}
        self.assertEqual(verify_evidence(d, step(), HANDOVER), [])

    def test_short_quotes_skip_verification(self):
        d = {"criteria_evidence": [
            {"criterion": "c1", "satisfied": True, "evidence": "ok"},
            {"criterion": "c2", "satisfied": True, "evidence": "passed"},
        ]}
        self.assertEqual(verify_evidence(d, step(), HANDOVER), [])


if __name__ == "__main__":
    unittest.main()
