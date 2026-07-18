import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.plan import (DONE, Plan, PlanValidationError, Step,
                               apply_mutations, is_trivial_command, validate)


def step_dict(sid="1", **kw):
    d = {"id": sid, "goal": f"goal {sid}", "acceptance_criteria": ["it works"],
         "verify": ["pytest -q"]}
    d.update(kw)
    return d


class TestTrivialCommands(unittest.TestCase):
    def test_trivial(self):
        for cmd in ["true", ":", "exit 0", "echo ok", "echo done  ", "printf hi",
                    "sleep 5", "timeout=60;true", "ls", "pwd",
                    "test -d .", "test 1 -eq 1", "test 1 = 1", "test 0 = 0", "[ 1 = 1 ]",
                    "/usr/bin/true", "# verified manually",
                    "true || pytest", "pytest || true", "echo x && true",
                    "sleep 1 && true", "pytest ; true", "make check || echo skipped",
                    "pytest | tee log", "npm test | tee out.log",
                    # exotic always-exit-0 bypasses the tri-state evaluator must catch:
                    "(true)", "( exit 0 )", "{ true; }", "! false", "env true",
                    "command true", "FOO=1 true", "pytest &", "if true; then :; fi"]:
            self.assertTrue(is_trivial_command(cmd), cmd)

    def test_not_trivial(self):
        for cmd in ["pytest -q", "npm test", "echo hi | grep hi",
                    "test -f out.txt && echo ok", "python3 -m orchestrator.probe port --port 80",
                    "timeout=900;npm run build", "true ; pytest -q", "npm ci && npm test",
                    "test -f out.txt",
                    # existence checks with a path operand are REAL checks, not no-ops:
                    "ls dist/bundle.js", "cat README.md", "ls -la build/artifact.tar",
                    "test 1 = 2", "test 3 -eq 4",
                    # genuine checks using fail-on-error idioms MUST NOT be rejected:
                    "pytest || exit 1", "test -f dist/app.js || exit 1", "pytest || false",
                    "set -e; npm ci; npm test; echo OK", "! test -f forbidden.txt",
                    "make build && make test", "if [ -f x ]; then echo y; else exit 1; fi"]:
            self.assertFalse(is_trivial_command(cmd), cmd)


class TestMutations(unittest.TestCase):
    def test_add_and_order(self):
        p = apply_mutations(Plan(), [
            {"op": "add", "step": step_dict("1")},
            {"op": "add", "step": step_dict("3")},
            {"op": "add", "step": step_dict("2"), "after_id": "1"},
        ])
        self.assertEqual([s.id for s in p.steps], ["1", "2", "3"])

    def test_duplicate_ids_rejected(self):
        with self.assertRaises(PlanValidationError) as cm:
            apply_mutations(Plan(), [{"op": "add", "step": step_dict("1")},
                                     {"op": "add", "step": step_dict("1")}])
        self.assertTrue(any("duplicate" in p for p in cm.exception.problems))

    def test_empty_plan_rejected(self):
        with self.assertRaises(PlanValidationError):
            apply_mutations(Plan(), [])

    def test_empty_verify_rejected(self):
        with self.assertRaises(PlanValidationError) as cm:
            apply_mutations(Plan(), [{"op": "add", "step": step_dict("1", verify=[])}])
        self.assertTrue(any("no verify" in p for p in cm.exception.problems))

    def test_trivial_verify_rejected(self):
        with self.assertRaises(PlanValidationError) as cm:
            apply_mutations(Plan(), [{"op": "add", "step": step_dict("1", verify=["echo ok"])}])
        self.assertTrue(any("trivially-true" in p for p in cm.exception.problems))

    def test_done_steps_immutable(self):
        base = apply_mutations(Plan(), [{"op": "add", "step": step_dict("1")},
                                        {"op": "add", "step": step_dict("2")}])
        base.steps[0].status = DONE
        with self.assertRaises(PlanValidationError):
            apply_mutations(base, [{"op": "update", "step": {"id": "1", "goal": "new"}}])
        with self.assertRaises(PlanValidationError):
            apply_mutations(base, [{"op": "remove", "step": {"id": "1"}}])

    def test_update_verify_change_resets_caps_original_untouched(self):
        base = apply_mutations(Plan(), [{"op": "add", "step": step_dict("1")}])
        base.steps[0].attempts = 2
        new = apply_mutations(base, [{"op": "update", "step": {"id": "1", "goal": "sharper",
                                                              "verify": ["pytest -q -k new"]}}])
        self.assertEqual(new.steps[0].goal, "sharper")
        self.assertEqual(new.steps[0].attempts, 0)          # executed bar changed -> reset
        self.assertEqual(base.steps[0].goal, "goal 1")      # deep copy: original untouched

    def test_goal_only_edit_applies_but_does_not_reset_caps(self):
        base = apply_mutations(Plan(), [{"op": "add", "step": step_dict("1")}])
        base.steps[0].attempts = 2
        new = apply_mutations(base, [{"op": "update", "step": {"id": "1", "goal": "reworded"}}])
        self.assertEqual(new.steps[0].goal, "reworded")     # edit applies
        self.assertEqual(new.steps[0].attempts, 2)          # but caps NOT laundered

    def test_setup_only_edit_is_not_cosmetic(self):
        base = apply_mutations(Plan(), [{"op": "add", "step": step_dict("1")}])
        new = apply_mutations(base, [{"op": "update", "step": {"id": "1", "setup": ["docker compose up -d"]}}])
        self.assertEqual(new.steps[0].setup, ["docker compose up -d"])  # applies, not rejected

    def test_remove_add_across_directives_keeps_caps(self):
        base = apply_mutations(Plan(), [{"op": "add", "step": step_dict("1")}])
        base.steps[0].attempts, base.steps[0].rejections = 3, 2
        after_remove = apply_mutations(base, [{"op": "remove", "step": {"id": "1"}},
                                              {"op": "add", "step": step_dict("2")}])
        # separate later directive re-adds the identical step -> caps carried via plan.retired
        re_added = apply_mutations(after_remove, [{"op": "add", "step": step_dict("1")}])
        s = re_added.get("1")
        self.assertEqual((s.attempts, s.rejections), (3, 2))

    def test_reorder_keeps_done_relative_order(self):
        base = apply_mutations(Plan(), [{"op": "add", "step": step_dict("1")},
                                        {"op": "add", "step": step_dict("2")},
                                        {"op": "add", "step": step_dict("3")},
                                        {"op": "add", "step": step_dict("4")}])
        base.steps[0].status = DONE
        base.steps[1].status = DONE
        new = apply_mutations(base, [{"op": "reorder", "order": ["1", "2", "4", "3"]}])
        self.assertEqual([s.id for s in new.steps], ["1", "2", "4", "3"])
        with self.assertRaises(PlanValidationError):  # done steps 1,2 swapped
            apply_mutations(base, [{"op": "reorder", "order": ["2", "1", "3", "4"]}])
        with self.assertRaises(PlanValidationError):  # order must be a permutation
            apply_mutations(base, [{"op": "reorder", "order": ["1", "2", "3"]}])

    def test_roundtrip(self):
        p = apply_mutations(Plan(summary="s"), [{"op": "add", "step": step_dict("1")}])
        p.steps[0].status = DONE
        p.steps[0].commit_sha = "abc123def"
        q = Plan.from_dict(p.to_dict())
        self.assertEqual(q.summary, "s")
        self.assertEqual(q.steps[0].commit_sha, "abc123def")
        self.assertEqual(q.steps[0].status, DONE)

    def test_validate_ignores_done_step_fields(self):
        # a done step with (historically) empty verify must not block future mutations
        p = Plan(steps=[Step(id="1", goal="g", acceptance_criteria=["a"], verify=["x"], status=DONE)])
        p.steps[0].verify = []
        self.assertEqual([w for w in validate(p) if "verify" in w], [])


if __name__ == "__main__":
    unittest.main()
