from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Sequence, Tuple

from orchestrator import config

# ------------------------------------------------------------------ value object


@dataclass
class CheckResult:
    """One preflight verdict. `hint` carries the fix and is only rendered on failure;
    `critical` marks a check that, when it fails, must fail the whole preflight (exit 2)."""

    name: str
    status: str        # "pass" | "fail" | "warn"
    detail: str        # one line the user reads
    hint: str = ""     # the actionable fix; empty when passing / not applicable
    critical: bool = False


# ------------------------------------------------------------------ probes (injected)

# A subprocess runner is injected so tests never spawn a real external process. The real
# default captures output and swallows nothing — callers decide how to degrade on failure.
Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess"]


def _run(argv: Sequence[str]) -> "subprocess.CompletedProcess":
    # Local, offline, read-only identity/version probes only (git config, claude --version).
    # stdin is closed and a timeout is set so a probe can NEVER block a preflight on a prompt
    # (e.g. git reaching for a tty) — preflight must always terminate.
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=10,
                          stdin=subprocess.DEVNULL)


# ------------------------------------------------------------------ the six checks


def check_python(version=sys.version_info) -> CheckResult:
    """loopd targets modern syntax; anything below 3.10 is a hard stop."""
    ok = (version[0], version[1]) >= (3, 10)
    ver = f"{version[0]}.{version[1]}.{version[2]}"
    if ok:
        return CheckResult("python", "pass", f"Python {ver}", critical=True)
    return CheckResult(
        "python", "fail", f"Python {ver} is too old",
        hint="loopd needs Python 3.10+ — install a newer interpreter and re-run.",
        critical=True,
    )


def check_claude(which: Callable[[str], object] = shutil.which,
                 runner: Runner = _run) -> CheckResult:
    """The whole loop drives the Claude Code CLI; without it nothing can run."""
    path = which("claude")
    if not path:
        return CheckResult(
            "claude CLI", "fail", "Claude Code CLI not found on PATH",
            hint="npm install -g @anthropic-ai/claude-code",
            critical=True,
        )
    detail = "Claude Code CLI found"
    # Best-effort version. A probe failure must never demote a present CLI to a fail.
    try:
        cp = runner(["claude", "--version"])
        ver = (getattr(cp, "stdout", "") or "").strip()
        if ver:
            detail = f"Claude Code CLI found ({ver})"
    except Exception:
        pass
    return CheckResult("claude CLI", "pass", detail, critical=True)


def check_auth(which: Callable[[str], object] = shutil.which) -> CheckResult:
    """Informational only — loopd rides the Claude Code login, so we never make a network/API
    call to prove auth. We only note where headless/CI credentials would come from, and whether
    any are set right now. Status is never 'fail' — a missing login is a warning, not a blocker."""
    # Read straight from the environment (never a probe) so CI/headless creds are surfaced.
    present = [k for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN") if os.environ.get(k)]
    creds = (f"{' and '.join(present)} set for headless/CI" if present
             else "ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN (if set) are used for headless/CI")
    if which("claude"):
        return CheckResult("auth", "pass",
                           f"loopd reuses your Claude Code login; {creds}")
    return CheckResult("auth", "warn",
                       f"Claude Code login unconfirmed; {creds}",
                       hint="Run `claude login` once Claude Code is installed.")


def check_git(which: Callable[[str], object] = shutil.which,
              runner: Runner = _run) -> CheckResult:
    """git is load-bearing: run branches, diffs and commits all go through it, and commits
    need an identity."""
    if not which("git"):
        return CheckResult(
            "git + identity", "fail", "git not found on PATH",
            hint="Install git (e.g. `brew install git` / your OS package manager) and re-run.",
            critical=True,
        )

    def _cfg(key: str) -> str:
        # Global identity is what commits use in a fresh sandbox/branch; read it read-only.
        try:
            cp = runner(["git", "config", "--global", key])
            return (getattr(cp, "stdout", "") or "").strip()
        except Exception:
            return ""

    name, email = _cfg("user.name"), _cfg("user.email")
    if not name or not email:
        return CheckResult(
            "git + identity", "fail", "git found but commit identity is not set",
            hint='Set it: git config --global user.email "..." (and user.name "...")',
            critical=True,
        )
    return CheckResult("git + identity", "pass", f"git identity: {name} <{email}>", critical=True)


def check_cwd_repo(which: Callable[[str], object] = shutil.which,
                   runner: Runner = _run) -> CheckResult:
    """Warn-only: loopd can create/clone a repo for you, so being outside one is not fatal."""
    inside = False
    if which("git"):
        try:
            cp = runner(["git", "rev-parse", "--is-inside-work-tree"])
            inside = (getattr(cp, "stdout", "") or "").strip() == "true"
        except Exception:
            inside = False
    if inside:
        return CheckResult("cwd-is-a-repo", "pass", "current directory is inside a git repo")
    return CheckResult(
        "cwd-is-a-repo", "warn", "current directory is not a git repo",
        hint="Start one with `loopd new` or bring code in with `loopd clone`.",
    )


def check_effective_config() -> CheckResult:
    """Show what a run would actually use — resolved through the REAL Config so this can never
    drift from config.py's keys/defaults. Config.__post_init__ mkdirs a .agentic/ state dir, so
    we point `repo` at a throwaway temp dir: any state dir lands there and is removed with the
    context, and nothing is ever created in the user's cwd/repo."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = config.Config(repo=Path(tmp))
        return CheckResult(
            "effective-config", "pass",
            f"pm_model={cfg.pm_model}  dev_model={cfg.dev_model}  budget_usd={cfg.budget_usd:g}",
        )


# ------------------------------------------------------------------ runner

# Critical set is exactly these three; warnings never move the exit code.
_CRITICAL = {"python", "claude CLI", "git + identity"}


def run_checks(which: Callable[[str], object] = shutil.which,
               runner: Runner = _run,
               version=sys.version_info) -> Tuple[List[CheckResult], int]:
    """Run the six checks in fixed order. Exit code 2 iff any critical check failed."""
    results = [
        check_python(version),
        check_claude(which, runner),
        check_auth(which),
        check_git(which, runner),
        check_cwd_repo(which, runner),
        check_effective_config(),
    ]
    failed_critical = any(r.critical and r.status == "fail" for r in results)
    return results, (2 if failed_critical else 0)


# ------------------------------------------------------------------ rendering

# Local color/glyph helpers mirroring cli.py's voice (no import of cli.py — that would be a
# cycle). Color is gated on a real TTY so rendered output is plain and testable off-terminal.
_TTY = sys.stdout.isatty()


def _c(s: str, code: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _TTY else s


_GLYPH = {"pass": "✓", "fail": "✗", "warn": "⚠"}
_COLOR = {"pass": "38;5;42", "fail": "38;5;196", "warn": "38;5;214"}


def render(results: List[CheckResult], writer=None) -> None:
    """One line per check (glyph + name + detail); a failing check also prints its hint so the
    fix is always in the rendered text."""
    out = writer if writer is not None else sys.stdout
    for r in results:
        glyph = _c(_GLYPH.get(r.status, "?"), _COLOR.get(r.status, "0"))
        print(f"{glyph} {r.name}: {r.detail}", file=out)
        if r.status == "fail" and r.hint:
            print(f"    → {r.hint}", file=out)


def main() -> int:
    """`loopd doctor` entry point (wired into cli.py in a later step)."""
    results, code = run_checks()
    render(results)
    return code
