import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.config import Config
from orchestrator.seed import ensure_brief


class FakeLedger:
    """Minimal stand-in — ensure_brief only ever calls .log() in these paths."""
    def __init__(self):
        self.events = []

    def log(self, event):
        self.events.append(event)


class TestEnsureBrief(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.cfg = Config(repo=self.repo)
        self.brief = self.cfg.state_dir / "brief.md"
        self.led = FakeLedger()

    def test_task_text_wins_over_stale_brief_on_fresh_run(self):
        self.brief.write_text("# Task brief\n\n## Objective\nOLD TASK from a prior run\n")
        out = ensure_brief(self.cfg, self.led, "build the NEW thing", resume=False)
        self.assertIn("build the NEW thing", out)
        self.assertNotIn("OLD TASK", out)
        # persisted, so the reincarnated PM sees the new brief too
        self.assertIn("build the NEW thing", self.brief.read_text())

    def test_resume_keeps_existing_brief_even_if_task_text_given(self):
        self.brief.write_text("# Task brief\n\n## Objective\nCURATED brief for the run\n")
        out = ensure_brief(self.cfg, self.led, "some stray task string", resume=True)
        self.assertIn("CURATED brief", out)
        self.assertNotIn("stray task", out)

    def test_no_task_text_uses_existing_brief(self):
        self.brief.write_text("# Task brief\n\n## Objective\nfrom /handoff\n")
        out = ensure_brief(self.cfg, self.led, None, resume=False)
        self.assertIn("from /handoff", out)

    def test_task_text_creates_brief_when_none_exists(self):
        self.assertFalse(self.brief.exists())
        out = ensure_brief(self.cfg, self.led, "first task", resume=False)
        self.assertIn("first task", out)
        self.assertTrue(self.brief.exists())

    def test_no_context_raises(self):
        with self.assertRaises(RuntimeError):
            ensure_brief(self.cfg, self.led, None, resume=False)


if __name__ == "__main__":
    unittest.main()
