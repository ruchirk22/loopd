import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.claude_cli import _parse_json, run_claude


class TestParseJson(unittest.TestCase):
    def test_single_object(self):
        self.assertEqual(_parse_json('{"a": 1}'), {"a": 1})

    def test_array_takes_last_dict(self):
        self.assertEqual(_parse_json('[{"a":1},{"b":2}]'), {"b": 2})

    def test_scalar_is_not_an_envelope(self):
        self.assertIsNone(_parse_json("3"))
        self.assertIsNone(_parse_json("true"))
        self.assertIsNone(_parse_json('"done"'))
        self.assertIsNone(_parse_json("[1, 2, 3]"))

    def test_stream_fallback_skips_scalar_lines(self):
        s = 'noise\n{"type":"msg"}\n42\n'
        self.assertEqual(_parse_json(s), {"type": "msg"})

    def test_garbage(self):
        self.assertIsNone(_parse_json(""))
        self.assertIsNone(_parse_json("not json at all"))


class FakeClaudeMixin:
    """Puts a fake `claude` executable at the front of PATH."""

    def make_fake(self, body: str):
        self.bindir = Path(tempfile.mkdtemp())
        exe = self.bindir / "claude"
        exe.write_text("#!/bin/sh\n" + body + "\n")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        self._old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{self.bindir}:{self._old_path}"
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ["PATH"] = self._old_path


class TestRunClaude(FakeClaudeMixin, unittest.TestCase):
    def test_happy_envelope(self):
        env = {"type": "result", "subtype": "success", "is_error": False,
               "result": "hi", "session_id": "s-123", "total_cost_usd": 0.5,
               "structured_output": {"verdict": "plan"}}
        self.make_fake(f"echo '{json.dumps(env)}'")
        res = run_claude("x", cwd=Path("."), json_schema={"type": "object"})
        self.assertTrue(res.ok)
        self.assertEqual(res.text, "hi")
        self.assertEqual(res.session_id, "s-123")
        self.assertAlmostEqual(res.cost_usd, 0.5)
        self.assertEqual(res.structured, {"verdict": "plan"})

    def test_structured_fallback_parses_result_text(self):
        env = {"result": json.dumps({"a": 1}), "session_id": "s", "total_cost_usd": 0.1}
        self.make_fake(f"echo '{json.dumps(env)}'")
        res = run_claude("x", cwd=Path("."), json_schema={"type": "object"})
        self.assertEqual(res.structured, {"a": 1})

    def test_non_dict_structured_discarded(self):
        env = {"result": "[1,2]", "total_cost_usd": 0.1}
        self.make_fake(f"echo '{json.dumps(env)}'")
        res = run_claude("x", cwd=Path("."), json_schema={"type": "object"})
        self.assertIsNone(res.structured)

    def test_scalar_stdout_is_error_not_crash(self):
        self.make_fake("echo 3")
        res = run_claude("x", cwd=Path("."))
        self.assertFalse(res.ok)

    def test_nonzero_exit_is_error(self):
        env = {"result": "boom", "total_cost_usd": 0.2}
        self.make_fake(f"echo '{json.dumps(env)}'; exit 1")
        res = run_claude("x", cwd=Path("."))
        self.assertFalse(res.ok)
        self.assertAlmostEqual(res.cost_usd, 0.2)  # cost still counted on errors

    def test_timeout_returns_failed_result(self):
        self.make_fake("sleep 5")
        res = run_claude("x", cwd=Path("."), timeout_s=1)
        self.assertFalse(res.ok)
        self.assertIn("timed out", res.text)

    def test_flags_passed_through(self):
        # fake dumps its argv to a file; we assert the flag layout
        argfile = Path(tempfile.mkdtemp()) / "args.txt"
        self.make_fake(f'printf \'%s\\n\' "$@" > {argfile}; echo \'{{"result":"ok"}}\'')
        run_claude("PROMPT", cwd=Path("."), model="claude-opus-4-8",
                   resume_session="sid-1", fork_session=True, max_turns=7)
        args = argfile.read_text().splitlines()
        self.assertIn("claude-opus-4-8", args)
        self.assertIn("--resume", args)
        self.assertIn("sid-1", args)
        self.assertIn("--fork-session", args)
        self.assertEqual(args[args.index("--max-turns") + 1], "7")


if __name__ == "__main__":
    unittest.main()
