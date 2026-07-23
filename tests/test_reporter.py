import io
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import reporter
from orchestrator.plan import DONE, Plan, Step


def plain(**kw):
    return reporter.Reporter(stream=io.StringIO(), live=False, **kw)


class TestStatusFormatting(unittest.TestCase):
    def test_status_text_has_phase_step_elapsed_cost(self):
        r = reporter.Reporter(stream=io.StringIO(), live=True)
        r.attach(time.time() - 65, lambda: 4.5)
        r._phase, r._step = "building", "3/8"
        s = r.status_text()
        self.assertIn("building", s)
        self.assertIn("step 3/8", s)
        self.assertIn("1m", s)          # ~65s elapsed
        self.assertIn("$4.50", s)


class TestPlainOutput(unittest.TestCase):
    def _out(self, fn):
        r = plain()
        fn(r)
        return r.stream.getvalue()

    def test_milestones_are_plain_lines(self):
        r = plain()
        step = Step(id="2", goal="add rate limiter", acceptance_criteria=["a"], verify=["true"])
        r.planning()
        r.step_start(step, 2, 5)
        r.gate(True)
        r.accepted("abcdef1234")
        r.rejected(1, 2)
        r.descoped("not needed after all")
        r.replanned(1, 3)
        out = r.stream.getvalue()
        self.assertIn("Planning…", out)
        self.assertIn("→ Step 2: add rate limiter", out)
        self.assertIn("gates: PASS", out)
        self.assertIn("✓ accepted (committed abcdef123", out)
        self.assertIn("✗ rejected", out)
        self.assertIn("⤳ descoped: not needed", out)
        self.assertIn("↻ plan revised", out)

    def test_plain_mode_never_emits_cursor_codes(self):
        r = plain()
        r.attach(time.time(), lambda: 1.0)
        step = Step(id="1", goal="g", acceptance_criteria=["a"], verify=["true"])
        r.step_start(step, 1, 1)
        r.attempt(2)
        r.gate(False)
        self.assertNotIn("\033[K", r.stream.getvalue())   # no ANSI clear codes off a TTY
        self.assertNotIn("\r", r.stream.getvalue())


class TestLiveOutput(unittest.TestCase):
    def test_live_paints_status_and_does_not_crash(self):
        r = reporter.Reporter(stream=io.StringIO(), live=True)
        r.attach(time.time(), lambda: 2.0)
        step = Step(id="1", goal="g", acceptance_criteria=["a"], verify=["true"])
        r.step_start(step, 1, 3)   # milestone line + repainted status
        r.attempt(1)               # status-only update
        r.gate(True)               # milestone
        r.finish()                 # clears the line
        out = r.stream.getvalue()
        self.assertIn("\033[K", out)     # it used the clear-to-EOL code
        self.assertIn("→ Step 1: g", out)
        self.assertIn("▸", out)          # a status line was painted


class TestCompletionSummary(unittest.TestCase):
    def test_render_completion(self):
        s1 = Step(id="1", goal="build the widget", acceptance_criteria=["renders", "tested"],
                  verify=["true"], status=DONE, commit_sha="abcdef1234",
                  criteria_evidence=[{"criterion": "renders", "satisfied": True},
                                     {"criterion": "tested", "satisfied": True}])
        plan = Plan(summary="do it", steps=[s1])

        class _L:
            state = {"total_cost_usd": 3.2, "started": time.time() - 120,
                     "branch": "agentic/run-x"}

        out = reporter.render_completion(plan, _L(), None)
        self.assertIn("TASK COMPLETE", out)
        self.assertIn("1 done", out)
        self.assertIn("2/2 acceptance criteria", out)
        self.assertIn("$3.20", out)
        self.assertIn("abcdef123", out)
        self.assertIn("build the widget", out)
        self.assertIn("loopd pr", out)


class TestHeartbeat(unittest.TestCase):
    def _spun(self, out: str) -> bool:
        return any(ch in out for ch in reporter.Reporter._SPIN)

    def test_paint_shows_a_spinner_frame_when_live(self):
        r = reporter.Reporter(stream=io.StringIO(), live=True)
        r.attach(time.time(), lambda: 1.0)
        r._paint()
        out = r.stream.getvalue()
        self.assertTrue(self._spun(out))       # an animated spinner glyph is present
        self.assertIn("starting", out)         # alongside the phase

    def test_start_is_noop_off_tty(self):
        r = plain()                            # live=False
        r.start()
        self.assertIsNone(r._hb_thread)        # no background thread spun up
        r.finish()

    def test_start_spins_then_finish_stops_the_thread(self):
        r = reporter.Reporter(stream=io.StringIO(), live=True)
        r.attach(time.time(), lambda: 0.0)
        r.start(interval=0.02)
        self.assertIsNotNone(r._hb_thread)
        self.assertTrue(r._hb_thread.is_alive())
        time.sleep(0.08)                       # let a few heartbeats paint
        r.finish()
        self.assertIsNone(r._hb_thread)        # finish() joined + cleared the thread
        self.assertTrue(self._spun(r.stream.getvalue()))

    def test_paused_suspends_painting(self):
        r = reporter.Reporter(stream=io.StringIO(), live=True)
        r.attach(time.time(), lambda: 0.0)
        r._paint()                             # paints once (spinner on screen)
        with r.paused():
            before = r.stream.getvalue()
            r._paint()                         # must be suppressed while paused
            self.assertEqual(r.stream.getvalue(), before)
        # a clean-line clear was emitted entering the pause
        self.assertIn("\033[K", r.stream.getvalue())

    def test_long_phase_adds_still_working_hint(self):
        r = reporter.Reporter(stream=io.StringIO(), live=True)
        r.attach(time.time(), lambda: 0.0)
        r._phase_since = time.time() - 30       # phase has been running 30s
        r._paint()
        self.assertIn("still working", r.stream.getvalue())


if __name__ == "__main__":
    unittest.main()
