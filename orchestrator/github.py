"""GitHub integration — an ENHANCEMENT, never a dependency.

loopd never handles, stores, or asks for GitHub tokens. It reuses the user's existing
authentication by shelling out to the official `gh` CLI (`gh auth login`, done once, stored
in the OS keychain). The engine (loop.py) stays completely GitHub-agnostic; everything here
is called only from the product surface (CLI and dashboard).

Two directions:
  - Issues in  — `gh issue view` distills an issue into the run's brief (no model call).
  - PRs out    — after a successful run, `gh pr create` opens a PR with a senior-engineer
                 handover body. Never automatic; always one explicit confirmation.

Every function degrades gracefully: if `gh` is missing or unauthenticated, it returns a
friendly explanation with the exact command to fix it, and loopd keeps working without it.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

INSTALL_HINT = "Install the GitHub CLI (https://cli.github.com) — e.g. `brew install gh`."
AUTH_HINT = "Connect GitHub once with `gh auth login` (loopd never sees your token)."


def _run(cmd: List[str], cwd=None, timeout: int = 30) -> Tuple[Optional[int], str, str]:
    """Run a binary. Returns (returncode, stdout, stderr); returncode is None if the binary
    isn't installed at all (so callers can distinguish 'missing' from 'failed')."""
    try:
        p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return None, "", "not installed"
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)


def _gh(args: List[str], cwd=None, timeout: int = 30):
    return _run(["gh", *args], cwd=cwd, timeout=timeout)


def _git(args: List[str], cwd=None, timeout: int = 60):
    return _run(["git", *args], cwd=cwd, timeout=timeout)


# --------------------------------------------------------------- availability

def available() -> dict:
    """{'ok': bool, 'reason', 'hint'} — is `gh` installed AND authenticated?"""
    rc, _, _ = _gh(["auth", "status"])
    if rc is None:
        return {"ok": False, "reason": "gh-missing", "hint": INSTALL_HINT}
    if rc != 0:
        return {"ok": False, "reason": "not-authed", "hint": AUTH_HINT}
    return {"ok": True, "reason": "", "hint": ""}


def has_remote(repo) -> bool:
    rc, out, _ = _git(["remote"], cwd=repo)
    return rc == 0 and bool(out.strip())


# --------------------------------------------------------------- issues in

_ISSUE_URL = re.compile(r"github\.com/([^/]+/[^/]+)/issues/(\d+)")


def parse_issue_ref(ref: str) -> Optional[Tuple[Optional[str], int]]:
    """'#142' -> (None, 142); an issue URL -> ('owner/repo', 142)."""
    ref = (ref or "").strip()
    m = _ISSUE_URL.search(ref)
    if m:
        return m.group(1), int(m.group(2))
    m = re.match(r"^#?(\d+)$", ref)
    if m:
        return None, int(m.group(1))
    return None


def fetch_issue(repo, ref: str) -> Optional[dict]:
    """Read an issue via `gh issue view`. Returns {number,title,body,url,labels} or None."""
    parsed = parse_issue_ref(ref)
    if parsed is None:
        return None
    slug, num = parsed
    args = ["issue", "view", str(num), "--json", "number,title,body,url,labels"]
    if slug:
        args += ["--repo", slug]
    rc, out, _ = _gh(args, cwd=repo)
    if rc != 0 or not out.strip():
        return None
    try:
        d = json.loads(out)
    except json.JSONDecodeError:
        return None
    return {"number": d.get("number"), "title": (d.get("title") or "").strip(),
            "body": (d.get("body") or "").strip(), "url": d.get("url", ""),
            "labels": [l.get("name") for l in (d.get("labels") or []) if l.get("name")]}


def issue_to_brief(issue: dict) -> str:
    """Turn an issue into the run's brief — grounded in the ticket, no model call."""
    labels = f"\n\n_Labels: {', '.join(issue['labels'])}_" if issue.get("labels") else ""
    return (f"# {issue.get('title', 'GitHub issue')}\n\n{issue.get('body', '')}{labels}\n\n"
            f"---\n_Building from GitHub issue #{issue.get('number')}: {issue.get('url', '')}_\n")


def write_issue_context(repo, issue: dict) -> None:
    """Persist the issue as the brief + a marker so a later PR can link back to it."""
    ad = Path(repo).expanduser().resolve() / ".agentic"
    ad.mkdir(parents=True, exist_ok=True)
    (ad / "brief.md").write_text(issue_to_brief(issue))
    (ad / "github.json").write_text(json.dumps(
        {"issue_number": issue.get("number"), "issue_url": issue.get("url", "")}))


def _issue_marker(repo) -> dict:
    p = Path(repo).expanduser().resolve() / ".agentic" / "github.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


# --------------------------------------------------------------- repo / PR status

def repo_meta(repo) -> Optional[dict]:
    """{'slug': 'owner/repo', 'default_branch': 'main'} via `gh repo view`, or None."""
    rc, out, _ = _gh(["repo", "view", "--json", "nameWithOwner,defaultBranchRef"], cwd=repo)
    if rc != 0 or not out.strip():
        return None
    try:
        d = json.loads(out)
    except json.JSONDecodeError:
        return None
    return {"slug": d.get("nameWithOwner", ""),
            "default_branch": (d.get("defaultBranchRef") or {}).get("name") or "main"}


def current_branch(repo) -> str:
    rc, out, _ = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
    return out.strip() if rc == 0 else ""


def pr_status(repo, branch: str) -> Optional[dict]:
    """The open PR for this branch, if any: {number,state,url,draft,title}."""
    if not branch:
        return None
    rc, out, _ = _gh(["pr", "view", branch, "--json", "number,state,url,isDraft,title"], cwd=repo)
    if rc != 0 or not out.strip():
        return None
    try:
        d = json.loads(out)
    except json.JSONDecodeError:
        return None
    return {"number": d.get("number"), "state": d.get("state", ""), "url": d.get("url", ""),
            "draft": bool(d.get("isDraft")), "title": d.get("title", "")}


# --------------------------------------------------------------- PR body + open

def _fmt_money(x):
    try:
        return f"${float(x):.2f}"
    except (TypeError, ValueError):
        return "?"


def build_pr_body(task: str, steps: List[dict], forecast: Optional[dict],
                  issue: Optional[dict], decisions: List[str], finished: bool) -> str:
    """A handover from a senior engineer — what was built, how it was verified, and why —
    not an autogenerated changelog."""
    done = [s for s in steps if s.get("status") == "done"]
    lines = ["## What I built", ""]
    lines.append(task.strip() or "See the commits below.")
    if done:
        lines.append("")
        for s in done:
            sha = f" (`{(s.get('commit_sha') or '')[:9]}`)" if s.get("commit_sha") else ""
            lines.append(f"- {s.get('goal', s.get('id', ''))}{sha}")

    lines += ["", "## Verification", ""]
    if finished:
        lines.append("- ✅ Every step's checks passed.")
        lines.append("- ✅ Full replay of all accepted steps in a clean, from-scratch checkout passed.")
    else:
        lines.append("- Every committed step passed its checks; the run did not reach final "
                     "whole-project verification.")

    if isinstance(forecast, dict) and forecast.get("actual"):
        a, p = forecast["actual"], forecast
        lines += ["", "## Forecast vs actual", "",
                  "| | Predicted | Actual |", "|---|---|---|",
                  f"| Cost | {_fmt_money(p.get('estimated_cost_usd'))} | {_fmt_money(a.get('cost_usd'))} |",
                  f"| Steps | {p.get('estimated_steps', '?')} | {a.get('steps_done', '?')} |"]

    if decisions:
        lines += ["", "## Notable decisions", ""] + [f"- {d}" for d in decisions[:6]]

    if issue and issue.get("issue_number"):
        lines += ["", f"Closes #{issue['issue_number']}"]

    lines += ["", "---", "*Opened by [loopd](https://github.com/ruchirk22/loopd) — "
              "planned, built, and verified before this PR.*"]
    return "\n".join(lines)


def assemble_pr(repo, decisions: Optional[List[str]] = None) -> Optional[dict]:
    """Build the PR payload from what the run recorded. Returns {branch,base,title,body,issue}
    or None if there's nothing to open a PR from."""
    ad = Path(repo).expanduser().resolve() / ".agentic"
    sp = ad / "state.json"
    if not sp.is_file():
        return None
    try:
        st = json.loads(sp.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    plan = st.get("plan") or {}
    steps = plan.get("steps", [])
    task = (st.get("task") or plan.get("summary") or "").strip()
    branch = st.get("branch") or current_branch(repo)
    meta = repo_meta(repo)
    marker = _issue_marker(repo)
    title = (task.splitlines()[0] if task else "loopd changes")[:120]
    body = build_pr_body(task, steps, st.get("forecast"), marker, decisions or [],
                         bool(st.get("finished")))
    return {"branch": branch, "base": (meta or {}).get("default_branch", ""),
            "title": title, "body": body, "issue": marker.get("issue_number")}


def open_pr(repo, branch: str, base: str, title: str, body: str, draft: bool = False) -> dict:
    """Push the branch and open a PR. Idempotent: if a PR already exists for the branch, its
    URL is returned rather than erroring."""
    if not branch:
        return {"ok": False, "error": "no branch to open a PR from"}
    prc, _, perr = _git(["push", "-u", "origin", branch], cwd=repo)
    if prc not in (0, None):
        # A push failure (no remote, no access) is not fatal to loopd — report it plainly.
        if prc is None:
            return {"ok": False, "error": "git is not available"}
        return {"ok": False, "error": "couldn't push the branch: " + (perr.strip().splitlines()[-1:] or [""])[0]}
    existing = pr_status(repo, branch)
    if existing:
        return {"ok": True, "url": existing["url"], "existing": True, "number": existing["number"]}
    args = ["pr", "create", "--head", branch, "--title", title, "--body", body]
    if base:
        args += ["--base", base]
    if draft:
        args += ["--draft"]
    rc, out, err = _gh(args, cwd=repo)
    if rc != 0:
        return {"ok": False, "error": (err.strip() or "gh pr create failed").splitlines()[-1]}
    url = next((ln.strip() for ln in reversed(out.splitlines()) if ln.strip().startswith("http")), out.strip())
    return {"ok": True, "url": url}
