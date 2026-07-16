import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.plan import Step
from orchestrator.pm import directive_schema, validate_directive, verify_evidence


def step():
    return Step(id="1", goal="g", acceptance_criteria=["endpoint returns 200", "test added"],
                verify=["pytest -q"])


# Includes the proof-section marker so _evidence_haystack scopes to real evidence.
HANDOVER = ("### Gate verdict\nALL GATES PASSED\n"
            "### Diff vs last accepted commit\n"
            "```diff\n+def health(): return {'status': 'ok'}\n```\n"
            "### Gate transcript\ntest_health.py passed 1 test in 0.2s")

GOOD_EVIDENCE = [
    {"criterion": "endpoint returns 200", "satisfied": True,
     "evidence": "def health(): return {'status': 'ok'}"},
    {"criterion": "test added", "satisfied": True,
     "evidence": "test_health.py passed 1 test in 0.2s"},
]


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
             "criteria_evidence": GOOD_EVIDENCE}
        self.assertEqual(validate_directive(d, ["accept", "reject"], step(), HANDOVER), [])

    def test_integrity_ack_required_when_flagged(self):
        d = {"verdict": "accept", "reasoning": "r", "commit_message": "m",
             "criteria_evidence": GOOD_EVIDENCE}
        # no ack -> refused when require_integrity_ack
        probs = validate_directive(d, ["accept"], step(), HANDOVER, require_integrity_ack=True)
        self.assertTrue(any("integrity_ack" in p for p in probs))
        d["integrity_ack"] = "GATE_TARGETS_TOUCHED: the diff adds a real assertion, not a weakened check."
        self.assertEqual(validate_directive(d, ["accept"], step(), HANDOVER, require_integrity_ack=True), [])


class TestEvidence(unittest.TestCase):
    def test_valid_evidence_passes(self):
        self.assertEqual(verify_evidence({"criteria_evidence": GOOD_EVIDENCE}, step(), HANDOVER), [])

    def test_missing_evidence_entries(self):
        probs = verify_evidence({"criteria_evidence": []}, step(), HANDOVER)
        self.assertTrue(any("no verified evidence" in p for p in probs))

    def test_fabricated_quote_detected(self):
        d = {"criteria_evidence": [
            {"criterion": "endpoint returns 200", "satisfied": True,
             "evidence": "this precise text is nowhere in the packet at all"},
            {"criterion": "test added", "satisfied": True,
             "evidence": "test_health.py passed 1 test in 0.2s"},
        ]}
        probs = verify_evidence(d, step(), HANDOVER)
        self.assertTrue(any("not an exact quote" in p for p in probs))

    def test_empty_evidence_rejected(self):
        d = {"criteria_evidence": [
            {"criterion": "endpoint returns 200", "satisfied": True, "evidence": ""},
            {"criterion": "test added", "satisfied": True, "evidence": "test_health.py passed 1 test"},
        ]}
        probs = verify_evidence(d, step(), HANDOVER)
        self.assertTrue(any("empty evidence" in p for p in probs))

    def test_boilerplate_banner_rejected(self):
        d = {"criteria_evidence": [
            {"criterion": "endpoint returns 200", "satisfied": True, "evidence": "ALL GATES PASSED"},
            {"criterion": "test added", "satisfied": True, "evidence": "test_health.py passed 1 test"},
        ]}
        probs = verify_evidence(d, step(), HANDOVER)
        self.assertTrue(any("boilerplate" in p for p in probs))

    def test_short_quotes_rejected(self):
        d = {"criteria_evidence": [
            {"criterion": "endpoint returns 200", "satisfied": True, "evidence": "ret 200"},
            {"criterion": "test added", "satisfied": True, "evidence": "test_health.py passed 1 test"},
        ]}
        probs = verify_evidence(d, step(), HANDOVER)
        self.assertTrue(any("too short" in p for p in probs))

    def test_same_quote_may_cover_distinct_criteria(self):
        # a one-line change can legitimately satisfy two criteria derived from that line
        q = "test_health.py passed 1 test in 0.2s"
        d = {"criteria_evidence": [
            {"criterion": "endpoint returns 200", "satisfied": True, "evidence": q},
            {"criterion": "test added", "satisfied": True, "evidence": q},
        ]}
        self.assertEqual(verify_evidence(d, step(), HANDOVER), [])

    def test_unmatched_criterion_rejected(self):
        d = {"criteria_evidence": [
            {"criterion": "totally unrelated criterion text", "satisfied": True,
             "evidence": "def health(): return {'status': 'ok'}"},
            {"criterion": "another unrelated one", "satisfied": True,
             "evidence": "test_health.py passed 1 test in 0.2s"},
        ]}
        probs = verify_evidence(d, step(), HANDOVER)
        self.assertTrue(any("does not match" in p for p in probs))

    def test_unsatisfied_criterion_blocks_accept(self):
        d = {"criteria_evidence": [
            {"criterion": "endpoint returns 200", "satisfied": False, "evidence": "whatever long text"},
            {"criterion": "test added", "satisfied": True, "evidence": "test_health.py passed 1 test"},
        ]}
        probs = verify_evidence(d, step(), HANDOVER)
        self.assertTrue(any("unsatisfied" in p for p in probs))

    def test_section_header_quotes_rejected(self):
        # The bypass round 3 found: quoting the packet's own scaffolding must NOT pass.
        for header in ["Diff vs last accepted commit", "Gate transcript (tail)",
                       "self-reported — verify against the diff", "no changes"]:
            d = {"criteria_evidence": [
                {"criterion": "endpoint returns 200", "satisfied": True, "evidence": header},
                {"criterion": "test added", "satisfied": True,
                 "evidence": "test_health.py passed 1 test in 0.2s"},
            ]}
            probs = verify_evidence(d, step(), HANDOVER)
            self.assertTrue(probs, f"header {header!r} was wrongly accepted as evidence")

    def test_overlapping_criteria_do_not_starve(self):
        s = Step(id="1", goal="g", verify=["pytest -q"],
                 acceptance_criteria=["GET /health returns 200",
                                      "GET /health returns 200 with a JSON body"])
        corpus = ("built the endpoint: GET /health returns 200\n"
                  "response asserted: GET /health returns 200 with a JSON body payload")
        d = {"criteria_evidence": [
            {"criterion": "GET /health returns 200", "satisfied": True,
             "evidence": "built the endpoint: GET /health returns 200"},
            {"criterion": "GET /health returns 200 with a JSON body", "satisfied": True,
             "evidence": "GET /health returns 200 with a JSON body payload"},
        ]}
        self.assertEqual(verify_evidence(d, s, corpus), [])

    def test_whitespace_normalized_quotes_match(self):
        d = {"criteria_evidence": [
            {"criterion": "endpoint returns 200", "satisfied": True,
             "evidence": "def health():   return {'status': 'ok'}"},
            {"criterion": "test added", "satisfied": True,
             "evidence": "test_health.py passed 1  test in 0.2s"},
        ]}
        self.assertEqual(verify_evidence(d, step(), HANDOVER), [])


if __name__ == "__main__":
    unittest.main()
