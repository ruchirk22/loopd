import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import architecture as A
from orchestrator.claude_cli import ClaudeResult
from orchestrator.config import Config
from orchestrator.ledger import Ledger

PROPOSAL = {
    "stack": ["Python", "FastAPI", "Postgres"],
    "data_model": ["User", "Goal (owner = User)"],
    "module_boundaries": ["api/", "core/", "db/"],
    "api_conventions": ["REST under /v1", "JSON errors {error}"],
    "tenancy": {"strategy": "rls", "details": "every table has tenant_id; RLS policies enforce it"},
    "deploy": ["Cloud Run", "Cloud SQL (Postgres)"],
    "conventions": ["pytest", "ruff"],
    "invariants": ["no client-supplied tenant_id"],
}


class TestSpineModule(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())

    def test_from_proposal_maps_sections_and_tenancy(self):
        data = A.from_proposal(PROPOSAL)
        self.assertEqual(data[A.STACK], ["Python", "FastAPI", "Postgres"])
        self.assertIn("Strategy: rls", data[A.TENANCY])
        self.assertTrue(any("RLS policies" in b for b in data[A.TENANCY]))

    def test_save_render_load_roundtrip(self):
        A.save(self.repo, A.from_proposal(PROPOSAL))
        self.assertTrue(A.exists(self.repo))
        back = A.load(self.repo)
        self.assertEqual(back[A.STACK], ["Python", "FastAPI", "Postgres"])
        self.assertEqual(back[A.DATA_MODEL], ["User", "Goal (owner = User)"])

    def test_as_prompt_empty_then_present(self):
        self.assertEqual(A.as_prompt(self.repo), "")
        A.save(self.repo, A.from_proposal(PROPOSAL))
        p = A.as_prompt(self.repo)
        self.assertIn("Tenancy & isolation", p)
        self.assertIn("Strategy: rls", p)

    def test_tenancy_strategy(self):
        A.save(self.repo, A.from_proposal(PROPOSAL))
        self.assertEqual(A.tenancy_strategy(self.repo), "rls")
        self.assertEqual(A.tenancy_strategy(Path(tempfile.mkdtemp())), "")  # none saved

    def test_discard(self):
        A.save(self.repo, A.from_proposal(PROPOSAL))
        A.discard(self.repo)
        self.assertFalse(A.exists(self.repo))

    def test_unknown_tenancy_strategy_becomes_other(self):
        data = A.from_proposal({**PROPOSAL, "tenancy": {"strategy": "wat", "details": "x"}})
        self.assertIn("Strategy: other", data[A.TENANCY])


class TestPropose(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        (self.repo / "app.txt").write_text("x")
        self.cfg = Config(repo=self.repo)
        self.led = Ledger.load_or_start(self.cfg)

    def _fake(self, structured, ok=True, cost=0.05):
        return lambda *a, **k: ClaudeResult(ok=ok, text=json.dumps(structured or {}),
                                            session_id=None, cost_usd=cost,
                                            structured=structured, raw={})

    def test_propose_happy_and_charges_ledger(self):
        before = self.led.state["total_cost_usd"]
        with mock.patch.object(A, "run_claude", self._fake(PROPOSAL, cost=0.07)):
            spine = A.propose(self.cfg, "build an app", ledger=self.led)
        self.assertIsNotNone(spine)
        self.assertIn("Strategy: rls", spine[A.TENANCY])
        self.assertAlmostEqual(self.led.state["total_cost_usd"], before + 0.07)

    def test_propose_degrades_on_failure(self):
        with mock.patch.object(A, "run_claude", self._fake(None, ok=False)):
            self.assertIsNone(A.propose(self.cfg, "build an app", ledger=self.led))


if __name__ == "__main__":
    unittest.main()
