"""Durable run state + git integration. Three things you own:
  1. state.json + a JSONL event log under <repo>/.agentic/ — loadable, so an
     interrupted run RESUMES instead of re-planning from scratch.
  2. A git commit per accepted step — the handoff unit and your rollback points.
     Git failures RAISE; a step is never marked done on a failed commit.
  3. The budget: every CLI call's cost flows through spend(), which kills the run
     the moment the cap is crossed (planning and seeding included).
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Iterator, List, Optional

from .config import Config
from .plan import Plan, Step, DONE, SKIPPED


class BudgetExceeded(RuntimeError):
    pass


class NoChangesError(RuntimeError):
    """Accepting a step that produced no diff is refused — surfaced back to the PM."""


class GitError(RuntimeError):
    pass


class StateConflict(RuntimeError):
    """state.json from a previous run exists; caller must choose --resume-run or --fresh."""


def _git(args: List[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed (exit {p.returncode}): "
                       f"{(p.stderr or p.stdout).strip()[:800]}")
    return p


class Ledger:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.repo = cfg.repo
        self.state_path = cfg.state_dir / "state.json"
        self.log_path = cfg.state_dir / "log.jsonl"
        self.state: dict = {}

    # ---------- lifecycle ----------

    @classmethod
    def load_or_start(cls, cfg: Config, resume: bool = False, fresh: bool = False) -> "Ledger":
        led = cls(cfg)
        if led.state_path.exists():
            if resume:
                led.state = json.loads(led.state_path.read_text())
                led._ensure_git(resume=True)
                led.log({"event": "run_resumed", "total_cost_usd": led.state.get("total_cost_usd", 0)})
                return led
            if not fresh:
                raise StateConflict(
                    f"{led.state_path} exists from a previous run "
                    f"({json.loads(led.state_path.read_text()).get('task', '')!r:.80}). "
                    "Re-run with --resume-run to continue it, or --fresh to archive it and start over.")
            stamp = time.strftime("%Y%m%d-%H%M%S")
            led.state_path.rename(led.state_path.with_name(f"state.{stamp}.json"))
            if led.log_path.exists():
                led.log_path.rename(led.log_path.with_name(f"log.{stamp}.jsonl"))
        elif resume:
            raise StateConflict(f"--resume-run given but {led.state_path} does not exist.")

        led._ensure_git(resume=False)
        led.state = {
            "task": "",
            "started": time.time(),
            "total_cost_usd": 0.0,
            "pm_session_id": None,
            "branch": led._current_branch(),
            "plan": None,
            "checkpoint": None,
            "replans_used": 0,
            "review_turns_since_ckpt": 0,
            "handover_bytes": 0,
            "finished": False,
        }
        led._save()
        return led

    def start(self, task: str) -> None:
        self.state["task"] = task
        self._save()
        self.log({"event": "run_started", "task": task[:2000]})

    # ---------- git ----------

    def _ensure_git(self, resume: bool) -> None:
        if not (self.repo / ".git").exists():
            _git(["init"], self.repo)
        # Identity: required for commits; set repo-locally only if missing.
        for key, val in (("user.name", "agentic-loop"), ("user.email", "agentic-loop@local")):
            if _git(["config", key], self.repo, check=False).returncode != 0:
                _git(["config", key, val], self.repo)
        # Never let orchestrator state pollute the target repo's history.
        exclude = self.repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text() if exclude.exists() else ""
        if ".agentic/" not in existing:
            exclude.write_text(existing.rstrip("\n") + "\n.agentic/\n")
        # Baseline commit so there is always a HEAD to diff/reset against.
        if _git(["rev-parse", "--verify", "HEAD"], self.repo, check=False).returncode != 0:
            _git(["add", "-A"], self.repo)
            _git(["commit", "-m", "agentic-loop: baseline", "--allow-empty"], self.repo)
        if resume:
            branch = self.state.get("branch") if self.state else None
            if branch and self._current_branch() != branch:
                _git(["checkout", branch], self.repo)
        elif self.cfg.use_run_branch:
            base = f"agentic/run-{time.strftime('%Y%m%d-%H%M%S')}"
            branch, n = base, 2
            while _git(["rev-parse", "--verify", branch], self.repo, check=False).returncode == 0:
                branch, n = f"{base}-{n}", n + 1
            _git(["checkout", "-b", branch], self.repo)

    def _current_branch(self) -> str:
        return _git(["rev-parse", "--abbrev-ref", "HEAD"], self.repo).stdout.strip()

    def commit_step(self, step: Step, message: str) -> str:
        _git(["add", "-A"], self.repo)
        if _git(["diff", "--cached", "--quiet"], self.repo, check=False).returncode == 0:
            raise NoChangesError(f"step {step.id}: no changes to commit — nothing was produced")
        msg = message.strip() or f"step {step.id}: {step.goal}"
        _git(["commit", "-m", msg], self.repo)
        sha = _git(["rev-parse", "HEAD"], self.repo).stdout.strip()
        step.commit_sha = sha
        self.log({"event": "step_committed", "step": step.id, "sha": sha, "message": msg[:200]})
        return sha

    def reset_to_head(self, reason: str) -> None:
        """Discard uncommitted work (e.g. an abandoned step before a replan), keeping
        a forensic copy of what was thrown away. Ignored files (.agentic/) survive."""
        diff = _git(["diff", "HEAD"], self.repo, check=False).stdout
        if diff.strip():
            dump = self.cfg.state_dir / "discarded"
            dump.mkdir(exist_ok=True)
            path = dump / f"{time.strftime('%Y%m%d-%H%M%S')}.diff"
            path.write_text(diff)
            self.log({"event": "work_discarded", "reason": reason, "diff_file": str(path)})
        _git(["reset", "--hard", "HEAD"], self.repo)
        _git(["clean", "-fd"], self.repo)

    def diff_against_head(self, cap: int) -> dict:
        stat = _git(["diff", "HEAD", "--stat"], self.repo, check=False).stdout
        full = _git(["diff", "HEAD"], self.repo, check=False).stdout
        untracked = _git(["ls-files", "--others", "--exclude-standard"], self.repo, check=False).stdout
        changed = _git(["diff", "HEAD", "--name-only"], self.repo, check=False).stdout.splitlines()
        changed += [u for u in untracked.splitlines() if u.strip()]
        for path in untracked.splitlines():
            p = self.repo / path
            if p.is_file():
                try:
                    body = p.read_text(errors="replace")
                except OSError:
                    continue
                full += f"\n--- /dev/null\n+++ b/{path} (untracked)\n{body}"
        truncated = len(full) > cap
        return {
            "stat": stat.strip(),
            "diff": full[:cap] + ("\n[... diff truncated ...]" if truncated else ""),
            "changed_files": [c.strip() for c in changed if c.strip()],
            "empty": not full.strip(),
        }

    @contextlib.contextmanager
    def pristine_worktree(self) -> Iterator[Path]:
        """A clean checkout of HEAD for final verification: proves the accepted commits
        reproduce from scratch, not from leftover state in the dev's working tree."""
        wt = Path(tempfile.mkdtemp(prefix="agentic-final-"))
        _git(["worktree", "add", "--detach", str(wt), "HEAD"], self.repo)
        try:
            yield wt
        finally:
            _git(["worktree", "remove", "--force", str(wt)], self.repo, check=False)

    # ---------- money ----------

    def spend(self, cost: float, step: Optional[Step] = None) -> None:
        cost = float(cost or 0.0)
        self.state["total_cost_usd"] += cost
        if step is not None:
            step.cost_usd += cost
        self._save()
        if self.state["total_cost_usd"] > self.cfg.budget_usd:
            self.log({"event": "budget_exceeded", "total": self.state["total_cost_usd"]})
            raise BudgetExceeded(
                f"Budget ${self.cfg.budget_usd:.2f} exceeded "
                f"(spent ${self.state['total_cost_usd']:.2f}). "
                "Raise BUDGET_USD/--budget and re-run with --resume-run to continue.")

    # ---------- plan / PM session / checkpoint ----------

    def save_plan(self, plan: Plan) -> None:
        self.state["plan"] = plan.to_dict()
        self._save()

    def load_plan(self) -> Optional[Plan]:
        return Plan.from_dict(self.state["plan"]) if self.state.get("plan") else None

    def set_pm_session(self, session_id: Optional[str]) -> None:
        self.state["pm_session_id"] = session_id
        self._save()

    def save_checkpoint(self, ckpt: dict) -> None:
        self.state["checkpoint"] = ckpt
        self.state["review_turns_since_ckpt"] = 0
        self.state["handover_bytes"] = 0
        self._save()
        self.log({"event": "pm_checkpoint", "mission": str(ckpt.get("mission_summary", ""))[:300]})

    def note_review_turn(self, handover_bytes: int) -> None:
        self.state["review_turns_since_ckpt"] = self.state.get("review_turns_since_ckpt", 0) + 1
        self.state["handover_bytes"] = self.state.get("handover_bytes", 0) + handover_bytes
        self._save()

    def needs_checkpoint(self) -> bool:
        return (self.state.get("review_turns_since_ckpt", 0) >= self.cfg.checkpoint_every_reviews
                or self.state.get("handover_bytes", 0) >= self.cfg.handover_bytes_cap)

    def bump_replans(self) -> int:
        self.state["replans_used"] = self.state.get("replans_used", 0) + 1
        self._save()
        return self.state["replans_used"]

    # ---------- persistence / reporting ----------

    def log(self, event: dict) -> None:
        event["ts"] = time.time()
        with self.log_path.open("a") as f:
            f.write(json.dumps(event) + "\n")

    def _save(self) -> None:
        # Atomic: a crash mid-write must never corrupt run state.
        fd, tmp = tempfile.mkstemp(dir=str(self.cfg.state_dir), prefix=".state-", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(self.state, f, indent=2)
        os.replace(tmp, self.state_path)

    def report(self, plan: Optional[Plan]) -> str:
        lines = [f"Run report | total cost ${self.state.get('total_cost_usd', 0.0):.4f} "
                 f"| branch {self.state.get('branch', '?')}"]
        if plan:
            done = len([s for s in plan.steps if s.status == DONE])
            skipped = len([s for s in plan.steps if s.status == SKIPPED])
            lines[0] += f" | {done}/{len(plan.steps)} steps done" + (f", {skipped} descoped" if skipped else "")
            for s in plan.steps:
                sha = f" {s.commit_sha[:9]}" if s.commit_sha else ""
                lines.append(f"  [{s.status:>11}] {s.id}: {s.goal}  "
                             f"(attempts={s.attempts}, rejections={s.rejections}, ${s.cost_usd:.4f}){sha}")
        return "\n".join(lines)

    def write_escalation(self, reason: str, plan: Optional[Plan], detail: str = "",
                         pm_reasoning: str = "", step_id: str = "") -> Path:
        payload = {
            "reason": reason,
            "step": step_id,
            "pm_reasoning": pm_reasoning[:4000],
            "detail": detail[:8000],
            "total_cost_usd": self.state.get("total_cost_usd", 0.0),
            "report": self.report(plan),
            "ts": time.time(),
        }
        path = self.cfg.state_dir / "escalation.json"
        path.write_text(json.dumps(payload, indent=2))
        self.log({"event": "escalation", "reason": reason, "step": step_id})
        return path

    def finish(self) -> None:
        self.state["finished"] = True
        self._save()
        self.log({"event": "run_finished", "total_cost_usd": self.state["total_cost_usd"]})
