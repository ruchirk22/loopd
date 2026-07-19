import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import github as G


class FakeShell:
    """Stand-in for github._run. Maps a command prefix to a (rc, stdout, stderr) triple."""
    def __init__(self, table):
        self.table = table
        self.calls = []

    def __call__(self, cmd, cwd=None, timeout=30):
        self.calls.append(cmd)
        for prefix, resp in self.table:
            if cmd[:len(prefix)] == prefix:
                return resp
        return (1, "", "no match")

    def ran(self, *prefix):
        return any(c[:len(prefix)] == list(prefix) for c in self.calls)


def patch_shell(test, table):
    p = mock.patch.object(G, "_run", FakeShell(table))
    fake = p.start(); test.addCleanup(p.stop)
    return fake


ISSUE_JSON = json.dumps({"number": 142, "title": "Users can't reset password",
                         "body": "The reset link 404s.", "url": "https://github.com/o/r/issues/142",
                         "labels": [{"name": "bug"}]})


class TestAvailability(unittest.TestCase):
    def test_missing(self):
        patch_shell(self, [(["gh", "auth", "status"], (None, "", "not installed"))])
        av = G.available()
        self.assertFalse(av["ok"]); self.assertEqual(av["reason"], "gh-missing")

    def test_not_authed(self):
        patch_shell(self, [(["gh", "auth", "status"], (1, "", "not logged in"))])
        self.assertEqual(G.available()["reason"], "not-authed")

    def test_ok(self):
        patch_shell(self, [(["gh", "auth", "status"], (0, "logged in", ""))])
        self.assertTrue(G.available()["ok"])


class TestIssueRef(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(G.parse_issue_ref("#142"), (None, 142))
        self.assertEqual(G.parse_issue_ref("142"), (None, 142))
        self.assertEqual(G.parse_issue_ref("https://github.com/o/r/issues/142"), ("o/r", 142))
        self.assertIsNone(G.parse_issue_ref("build a thing"))

    def test_fetch_issue(self):
        patch_shell(self, [(["gh", "issue", "view"], (0, ISSUE_JSON, ""))])
        iss = G.fetch_issue("/tmp", "#142")
        self.assertEqual(iss["number"], 142)
        self.assertEqual(iss["labels"], ["bug"])

    def test_fetch_issue_failure(self):
        patch_shell(self, [(["gh", "issue", "view"], (1, "", "not found"))])
        self.assertIsNone(G.fetch_issue("/tmp", "#999"))

    def test_issue_to_brief(self):
        b = G.issue_to_brief({"number": 142, "title": "T", "body": "B", "url": "U", "labels": ["bug"]})
        self.assertIn("# T", b); self.assertIn("B", b); self.assertIn("#142", b)

    def test_write_issue_context(self):
        repo = Path(tempfile.mkdtemp())
        G.write_issue_context(repo, {"number": 142, "title": "T", "body": "B", "url": "U", "labels": []})
        self.assertIn("# T", (repo / ".agentic" / "brief.md").read_text())
        self.assertEqual(json.loads((repo / ".agentic" / "github.json").read_text())["issue_number"], 142)


class TestRepoAndPR(unittest.TestCase):
    def test_repo_meta(self):
        patch_shell(self, [(["gh", "repo", "view"],
                            (0, json.dumps({"nameWithOwner": "o/r", "defaultBranchRef": {"name": "main"}}), ""))])
        m = G.repo_meta("/tmp")
        self.assertEqual(m["slug"], "o/r"); self.assertEqual(m["default_branch"], "main")

    def test_pr_status_present_and_absent(self):
        patch_shell(self, [(["gh", "pr", "view"],
                            (0, json.dumps({"number": 58, "state": "OPEN", "url": "U", "isDraft": False}), ""))])
        self.assertEqual(G.pr_status("/tmp", "b")["number"], 58)
        patch_shell(self, [(["gh", "pr", "view"], (1, "", "no pr"))])
        self.assertIsNone(G.pr_status("/tmp", "b"))


class TestOpenPR(unittest.TestCase):
    def test_creates_pr(self):
        sh = patch_shell(self, [
            (["git", "push"], (0, "", "")),
            (["gh", "pr", "view"], (1, "", "no pr")),          # none exists yet
            (["gh", "pr", "create"], (0, "https://github.com/o/r/pull/58\n", "")),
        ])
        r = G.open_pr("/tmp", "agentic/run-x", "main", "t", "b")
        self.assertTrue(r["ok"]); self.assertEqual(r["url"], "https://github.com/o/r/pull/58")
        self.assertTrue(sh.ran("git", "push"))

    def test_returns_existing_pr(self):
        patch_shell(self, [
            (["git", "push"], (0, "", "")),
            (["gh", "pr", "view"], (0, json.dumps({"number": 58, "state": "OPEN", "url": "U", "isDraft": False}), "")),
        ])
        r = G.open_pr("/tmp", "b", "main", "t", "b")
        self.assertTrue(r["ok"]); self.assertTrue(r["existing"])

    def test_push_failure_is_graceful(self):
        patch_shell(self, [(["git", "push"], (1, "", "no remote"))])
        r = G.open_pr("/tmp", "b", "main", "t", "b")
        self.assertFalse(r["ok"]); self.assertIn("push", r["error"].lower())


class TestBuildBodyAndAssemble(unittest.TestCase):
    def test_body_reads_like_a_handover(self):
        steps = [{"id": "1", "goal": "add login", "status": "done", "commit_sha": "abcdef1234"}]
        fc = {"estimated_cost_usd": 38, "estimated_steps": 13, "actual": {"cost_usd": 34, "steps_done": 11}}
        body = G.build_pr_body("Add OAuth", steps, fc, {"issue_number": 142}, ["Auth uses JWT"], finished=True)
        self.assertIn("## What I built", body)
        self.assertIn("add login", body)
        self.assertIn("clean, from-scratch checkout", body)
        self.assertIn("Forecast vs actual", body)
        self.assertIn("Auth uses JWT", body)
        self.assertIn("Closes #142", body)

    def test_assemble_from_state(self):
        repo = Path(tempfile.mkdtemp()); (repo / ".agentic").mkdir()
        (repo / ".agentic" / "state.json").write_text(json.dumps({
            "task": "Add OAuth", "branch": "agentic/run-x", "finished": True,
            "plan": {"summary": "oauth", "steps": [{"id": "1", "goal": "login", "status": "done"}]}}))
        patch_shell(self, [(["gh", "repo", "view"],
                            (0, json.dumps({"nameWithOwner": "o/r", "defaultBranchRef": {"name": "main"}}), ""))])
        p = G.assemble_pr(repo)
        self.assertEqual(p["branch"], "agentic/run-x")
        self.assertEqual(p["base"], "main")
        self.assertIn("What I built", p["body"])

    def test_assemble_none_without_state(self):
        self.assertIsNone(G.assemble_pr(Path(tempfile.mkdtemp())))


if __name__ == "__main__":
    unittest.main()
