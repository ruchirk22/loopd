import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import doctor


def fake_which(present):
    """which() that only reports the named commands as installed."""
    return lambda cmd: f"/usr/bin/{cmd}" if cmd in present else None


class FakeRunner:
    """Stand-in for the injected subprocess runner: maps argv -> stdout. Never spawns."""

    def __init__(self, outputs=None):
        # outputs: dict keyed by a joined argv substring -> stdout string
        self.outputs = outputs or {}

    def __call__(self, argv):
        key = " ".join(argv)
        stdout = ""
        for needle, value in self.outputs.items():
            if needle in key:
                stdout = value
                break
        return mock.Mock(stdout=stdout, returncode=0)


# A fully-healthy environment (git identity set, cwd is a repo, claude present).
GOOD_RUNNER = FakeRunner({
    "claude --version": "1.2.3 (Claude Code)",
    "user.name": "Ada Lovelace",
    "user.email": "ada@example.com",
    "--is-inside-work-tree": "true",
})


def render_to_str(results) -> str:
    buf = io.StringIO()
    doctor.render(results, writer=buf)
    return buf.getvalue()


class TestAllPass(unittest.TestCase):
    def test_exit_zero_when_everything_healthy(self):
        results, code = doctor.run_checks(
            which=fake_which({"git", "claude"}),
            runner=GOOD_RUNNER,
            version=(3, 12, 1),
        )
        self.assertEqual(code, 0)
        self.assertTrue(all(r.status != "fail" for r in results))
        self.assertEqual([r.name for r in results], [
            "python", "claude CLI", "auth", "git + identity",
            "cwd-is-a-repo", "effective-config",
        ])


class TestCriticalFailures(unittest.TestCase):
    def test_old_python_fails_and_names_3_10(self):
        results, code = doctor.run_checks(
            which=fake_which({"git", "claude"}),
            runner=GOOD_RUNNER,
            version=(3, 9, 0),
        )
        self.assertEqual(code, 2)
        self.assertIn("3.10", render_to_str(results))

    def test_missing_claude_fails_with_install_hint(self):
        results, code = doctor.run_checks(
            which=fake_which({"git"}),        # claude absent
            runner=GOOD_RUNNER,
            version=(3, 12, 0),
        )
        self.assertEqual(code, 2)
        self.assertIn("npm install -g @anthropic-ai/claude-code", render_to_str(results))

    def test_missing_git_fails(self):
        results, code = doctor.run_checks(
            which=fake_which({"claude"}),     # git absent
            runner=GOOD_RUNNER,
            version=(3, 12, 0),
        )
        self.assertEqual(code, 2)
        git = next(r for r in results if r.name == "git + identity")
        self.assertEqual(git.status, "fail")

    def test_git_without_identity_fails_with_config_hint(self):
        runner = FakeRunner({
            "claude --version": "1.0.0",
            "--is-inside-work-tree": "true",
            # user.name / user.email resolve to "" -> identity missing
        })
        results, code = doctor.run_checks(
            which=fake_which({"git", "claude"}),
            runner=runner,
            version=(3, 12, 0),
        )
        self.assertEqual(code, 2)
        self.assertIn('git config --global user.email', render_to_str(results))


class TestWarningsDoNotFail(unittest.TestCase):
    def test_not_a_repo_and_no_claude_login_still_exit_zero(self):
        # All critical checks pass, but cwd is NOT a repo -> warn, not fail.
        runner = FakeRunner({
            "claude --version": "1.0.0",
            "user.name": "Ada",
            "user.email": "ada@example.com",
            "--is-inside-work-tree": "false",
        })
        results, code = doctor.run_checks(
            which=fake_which({"git", "claude"}),
            runner=runner,
            version=(3, 12, 0),
        )
        self.assertEqual(code, 0)
        cwd = next(r for r in results if r.name == "cwd-is-a-repo")
        self.assertEqual(cwd.status, "warn")

    def test_auth_never_fails(self):
        # Even with claude absent, auth is only ever pass/warn.
        auth = doctor.check_auth(which=fake_which(set()))
        self.assertIn(auth.status, ("pass", "warn"))
        self.assertNotEqual(auth.status, "fail")


class TestNoSideEffect(unittest.TestCase):
    def test_effective_config_creates_no_agentic_dir(self):
        old = Path.cwd()
        tmp = Path(tempfile.mkdtemp())
        os.chdir(tmp)
        self.addCleanup(lambda: os.chdir(old))
        # check_effective_config constructs a Config against a throwaway temp repo, so its
        # __post_init__ mkdir lands there — never a .agentic/ in the invoking directory.
        result = doctor.check_effective_config()
        self.assertEqual(result.status, "pass")
        self.assertFalse((tmp / ".agentic").exists())

    def test_full_runner_creates_no_agentic_dir(self):
        old = Path.cwd()
        tmp = Path(tempfile.mkdtemp())
        os.chdir(tmp)
        self.addCleanup(lambda: os.chdir(old))
        doctor.run_checks(
            which=fake_which({"git", "claude"}),
            runner=GOOD_RUNNER,
            version=(3, 12, 0),
        )
        self.assertFalse((tmp / ".agentic").exists())


class TestGracefulVersionProbe(unittest.TestCase):
    def test_claude_version_probe_failure_still_passes(self):
        def boom(argv):
            raise OSError("cannot spawn")
        result = doctor.check_claude(which=fake_which({"claude"}), runner=boom)
        self.assertEqual(result.status, "pass")


if __name__ == "__main__":
    unittest.main()
