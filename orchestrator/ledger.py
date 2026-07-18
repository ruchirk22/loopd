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

# Bump when the state.json shape changes incompatibly; older files are refused on resume.
SCHEMA_VERSION = 2

# Terminal outcome labels for the end-of-run report, keyed by exit code.
_OUTCOME = {
    0: ("✅", "complete — verified done"),
    1: ("⛔", "stopped"),
    2: ("⚠️", "setup / plan failure"),
    3: ("💸", "budget exceeded (resumable with --resume-run)"),
}


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    if h:
        return f"{h}h {m}m {sec}s"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


class BudgetExceeded(RuntimeError):
    pass


class NoChangesError(RuntimeError):
    """Accepting a step that produced no diff is refused — surfaced back to the PM."""


class GitError(RuntimeError):
    pass


class StateConflict(RuntimeError):
    """state.json from a previous run exists; caller must choose --resume-run or --fresh."""


# Injected into EVERY git call: dev-planted hooks must never fire under orchestrator
# privileges (e.g. during `worktree add`), and untracked filenames must come back raw
# (not C-quoted) so non-ASCII names aren't dropped from the diff.
_GIT_SAFE = ["-c", "core.hooksPath=/dev/null", "-c", "core.quotePath=false"]


def _looks_binary(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return b"\0" in f.read(8192)
    except OSError:
        return True


def _git(args: List[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(["git", *_GIT_SAFE, *args], cwd=str(cwd),
                       capture_output=True, text=True, errors="replace")
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
                led.state = led._load_valid_state()
                led.state["budget_usd"] = cfg.budget_usd  # reflect this invocation's --budget
                led.state["pm_model"] = cfg.pm_model
                led.state["dev_model"] = cfg.dev_model
                led._ensure_git(resume=True)
                led._save()
                led.log({"event": "run_resumed", "total_cost_usd": led.state.get("total_cost_usd", 0)})
                return led
            if not fresh:
                try:
                    task = json.loads(led.state_path.read_text()).get("task", "")
                except (json.JSONDecodeError, OSError):
                    task = "(unreadable)"
                raise StateConflict(
                    f"{led.state_path} exists from a previous run ({task!r:.80}). "
                    "Re-run with --resume-run to continue it, or --fresh to archive it and start over.")
            stamp = time.strftime("%Y%m%d-%H%M%S")
            led.state_path.rename(led.state_path.with_name(f"state.{stamp}.json"))
            if led.log_path.exists():
                led.log_path.rename(led.log_path.with_name(f"log.{stamp}.jsonl"))
        elif resume:
            raise StateConflict(f"--resume-run given but {led.state_path} does not exist.")

        led._ensure_git(resume=False)
        led.state = {
            "schema_version": SCHEMA_VERSION,
            "task": "",
            "started": time.time(),
            "total_cost_usd": 0.0,
            "budget_usd": cfg.budget_usd,
            "pm_model": cfg.pm_model,
            "dev_model": cfg.dev_model,
            "pm_session_id": None,
            "branch": led._current_ref(),
            "plan": None,
            "checkpoint": None,
            "pending_commit": None,
            "replans_used": 0,
            "review_turns_since_ckpt": 0,
            "handover_bytes": 0,
            "finished": False,
        }
        led._save()
        return led

    def _load_valid_state(self) -> dict:
        try:
            state = json.loads(self.state_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            raise StateConflict(f"{self.state_path} is unreadable/corrupt ({exc}); "
                                "start over with --fresh.")
        if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
            raise StateConflict(
                f"{self.state_path} was written by an incompatible version "
                f"(schema_version={state.get('schema_version') if isinstance(state, dict) else '?'}, "
                f"expected {SCHEMA_VERSION}); start over with --fresh.")
        required = {"task", "total_cost_usd", "branch", "finished"}
        missing = [k for k in required if k not in state]
        if missing:
            raise StateConflict(f"{self.state_path} is missing keys {missing}; start over with --fresh.")
        if state.get("finished"):
            raise StateConflict(f"the run in {self.state_path} already finished; nothing to resume "
                                "(use --fresh for a new run).")
        state.setdefault("total_cost_usd", 0.0)
        return state

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
            ref = self.state.get("branch") if self.state else None
            if ref and self._current_ref() != ref:
                _git(["checkout", ref], self.repo)
            return
        dirty = bool(_git(["status", "--porcelain"], self.repo, check=False).stdout.strip())
        if self.cfg.use_run_branch:
            base = f"agentic/run-{time.strftime('%Y%m%d-%H%M%S')}"
            branch, n = base, 2
            while _git(["rev-parse", "--verify", branch], self.repo, check=False).returncode == 0:
                branch, n = f"{base}-{n}", n + 1
            _git(["checkout", "-b", branch], self.repo)
            # Isolate any pre-existing uncommitted work so step commits/resets never touch
            # it: snapshot it as its own commit on the RUN branch (recoverable via that
            # branch), leaving a clean HEAD to build on.
            if dirty:
                _git(["add", "-A"], self.repo)
                _git(["commit", "-m", "agentic-loop: pre-run snapshot of your uncommitted work",
                      "--allow-empty"], self.repo)
                self.log({"event": "pre_run_snapshot", "branch": branch})
        elif dirty:
            # No run branch to quarantine onto — committing here would rewrite the user's
            # own branch history. Refuse instead.
            raise StateConflict(
                "the target repo has uncommitted changes and USE_RUN_BRANCH is off. "
                "Commit or stash your work first, or enable run branches (USE_RUN_BRANCH=1) "
                "so the orchestrator can isolate the run.")

    def _current_ref(self) -> str:
        """Branch name, or the commit SHA when HEAD is detached (so resume can restore
        the true position — 'HEAD' as a branch name is a checkout no-op)."""
        if _git(["symbolic-ref", "-q", "HEAD"], self.repo, check=False).returncode == 0:
            return _git(["rev-parse", "--abbrev-ref", "HEAD"], self.repo).stdout.strip()
        return _git(["rev-parse", "HEAD"], self.repo).stdout.strip()

    def commit_step(self, step: Step, message: str) -> str:
        _git(["add", "-A"], self.repo)
        if _git(["diff", "--cached", "--quiet"], self.repo, check=False).returncode == 0:
            raise NoChangesError(f"step {step.id}: no changes to commit — nothing was produced")
        msg = message.strip() or f"step {step.id}: {step.goal}"
        # Record intent BEFORE committing so a crash any time before the plan durably records
        # the commit can be reconciled precisely (adopt_head_if_matches), not by adopting a
        # random HEAD move. The marker is cleared by clear_pending_commit() AFTER save_plan.
        self.state["pending_commit"] = {"step_id": step.id, "base_sha": step.base_sha}
        self._save()
        _git(["commit", "-m", msg], self.repo)
        sha = _git(["rev-parse", "HEAD"], self.repo).stdout.strip()
        step.commit_sha = sha
        self.log({"event": "step_committed", "step": step.id, "sha": sha, "message": msg[:200]})
        return sha

    def clear_pending_commit(self) -> None:
        if self.state.get("pending_commit") is not None:
            self.state["pending_commit"] = None
            self._save()

    def head_sha(self) -> str:
        return _git(["rev-parse", "HEAD"], self.repo, check=False).stdout.strip()

    def adopt_head_if_matches(self, step: Step) -> Optional[str]:
        """Crash-window recovery: adopt HEAD as this step's commit ONLY when the orchestrator
        recorded (via commit_step) that it was mid-commit for THIS step and the commit sits
        directly on the step's base — never adopt an arbitrary HEAD advance (e.g. a developer
        self-commit or a gate-command commit)."""
        marker = self.state.get("pending_commit")
        if not marker or marker.get("step_id") != step.id:
            return None
        sha = self.head_sha()
        base = marker.get("base_sha")
        if not sha or sha == base:
            return None
        parent = _git(["rev-parse", "HEAD^"], self.repo, check=False).stdout.strip()
        if base and parent != base:
            return None
        plan = self.load_plan()
        if plan and sha in {s.commit_sha for s in plan.steps}:
            return None
        step.commit_sha = sha
        # Do NOT clear the marker here — the caller clears it via clear_pending_commit()
        # only AFTER save_plan durably records the adoption, preserving crash-safety.
        self.log({"event": "step_adopted_head", "step": step.id, "sha": sha})
        return sha

    def revert_unclaimed_commits(self, step: Step, plan: Plan, reason: str) -> None:
        """On descope/replan, if HEAD advanced past the step's base with commits no done
        step claims (a dev self-commit, or a crash-window commit being abandoned), roll the
        branch back to the step's base so 'skipped' code doesn't silently ship. reset_to_head
        only discards UNcommitted work, so this handles the committed case."""
        base = step.base_sha
        head = self.head_sha()
        if not base or head == base:
            return
        claimed = {s.commit_sha for s in plan.done_steps()}
        # Walk base..head; if every commit there is unclaimed, it's this step's abandoned work.
        revs = _git(["rev-list", f"{base}..{head}"], self.repo, check=False).stdout.split()
        if not revs or any(r in claimed for r in revs):
            return  # a done step's commit is in the range — don't destroy it
        bundle = self.cfg.state_dir / "discarded" / f"reverted-{time.strftime('%Y%m%d-%H%M%S')}"
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "reverted.diff").write_text(
            _git(["diff", base, head], self.repo, check=False).stdout, errors="replace")
        _git(["reset", "--hard", base], self.repo)
        self.log({"event": "unclaimed_commits_reverted", "step": step.id, "reason": reason,
                  "count": len(revs), "bundle": str(bundle)})

    def reset_to_head(self, reason: str) -> None:
        """Discard uncommitted work (e.g. an abandoned step before a replan), keeping a
        forensic copy of BOTH tracked edits (as a diff) and untracked files (copied
        verbatim) — `git clean -fd` deletes untracked files permanently. Ignored files
        (.agentic/, node_modules, ...) survive the clean."""
        dump = self.cfg.state_dir / "discarded" / time.strftime("%Y%m%d-%H%M%S")
        # --binary + raw bytes so modified binary/non-UTF-8 tracked files are recoverable
        # (a plain text diff would lose them before `reset --hard` deletes them).
        diff_bytes = subprocess.run(["git", *_GIT_SAFE, "diff", "HEAD", "--binary"],
                                    cwd=str(self.repo), capture_output=True).stdout
        untracked = self._untracked_files()
        if diff_bytes.strip() or untracked:
            dump.mkdir(parents=True, exist_ok=True)
            if diff_bytes.strip():
                (dump / "tracked.diff").write_bytes(diff_bytes)
            for rel in untracked:
                src = self.repo / rel
                if src.is_file():
                    target = dump / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        target.write_bytes(src.read_bytes())
                    except OSError:
                        pass
            self.log({"event": "work_discarded", "reason": reason, "dir": str(dump),
                      "untracked_saved": len(untracked)})
        _git(["reset", "--hard", "HEAD"], self.repo)
        _git(["clean", "-fd"], self.repo)  # -fd (not -x): keeps ignored files incl. .agentic/

    def _untracked_files(self) -> List[str]:
        out = _git(["ls-files", "--others", "--exclude-standard", "-z"], self.repo, check=False).stdout
        return [p for p in out.split("\0") if p.strip()]

    def diff_against_head(self, cap: int, stat_line_cap: int = 200) -> dict:
        stat = _git(["diff", "HEAD", "--stat"], self.repo, check=False).stdout.strip()
        stat_lines = stat.splitlines()
        if len(stat_lines) > stat_line_cap:  # keep the prompt argv well under ARG_MAX
            stat = "\n".join(stat_lines[:stat_line_cap] + [f"[... {len(stat_lines) - stat_line_cap} more lines ...]"])
        full = _git(["diff", "HEAD"], self.repo, check=False).stdout
        untracked = self._untracked_files()
        changed = [c for c in _git(["diff", "HEAD", "--name-only"], self.repo,
                                   check=False).stdout.splitlines() if c.strip()] + untracked
        # Append untracked file bodies up to the remaining byte budget; never slurp a
        # whole tree (node_modules etc.) into memory, and flag binaries instead of
        # embedding mojibake.
        skipped = []
        for path in untracked:
            remaining = cap - len(full)
            if remaining <= 0:
                skipped.append(path)
                continue
            p = self.repo / path
            if not p.is_file():
                continue
            try:
                if p.stat().st_size > max(remaining, 65536) or _looks_binary(p):
                    full += f"\n--- /dev/null\n+++ b/{path} (untracked, {p.stat().st_size} bytes — not shown)\n"
                    continue
                body = p.read_text(errors="replace")[:remaining]
            except OSError:
                continue
            full += f"\n--- /dev/null\n+++ b/{path} (untracked)\n{body}"
        truncated = len(full) > cap
        note = f"\n[... {len(skipped)} more untracked file(s) omitted ...]" if skipped else ""
        return {
            "stat": stat,
            "diff": full[:cap] + ("\n[... diff truncated ...]" if truncated else "") + note,
            "changed_files": changed,
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

    def write_report(self, plan: Optional[Plan], code: int, detail: str = "") -> Path:
        """Human-readable end-of-run report at <repo>/.agentic/report.md, written on every
        terminal outcome. All from data already tracked in state + the plan."""
        emoji, label = _OUTCOME.get(code, ("⛔", "stopped"))
        st = self.state
        started = st.get("started")
        elapsed = _fmt_duration(time.time() - started) if started else "?"
        task = (st.get("task") or "").strip().splitlines()
        task = task[0][:200] if task else "(from brief)"

        lines = [
            "# loopd run report", "",
            f"- **Outcome:** {emoji} {label} (exit {code})",
            f"- **Task:** {task}",
            f"- **Branch:** `{st.get('branch', '?')}`",
            f"- **Elapsed (since run start):** {elapsed}",
            f"- **Cost:** ${st.get('total_cost_usd', 0.0):.4f} of ${self.cfg.budget_usd:.2f} budget",
            f"- **Replans used:** {st.get('replans_used', 0)}/{self.cfg.max_replans}",
        ]
        if plan and plan.steps:
            done = [s for s in plan.steps if s.status == DONE]
            skipped = [s for s in plan.steps if s.status == SKIPPED]
            lines.append(f"- **Steps:** {len(done)} done, {len(skipped)} descoped "
                         f"of {len(plan.steps)}")
            lines += ["", "## Steps", "",
                      "| step | status | attempts | rejections | cost | commit |",
                      "|---|---|---|---|---|---|"]
            for s in plan.steps:
                sha = s.commit_sha[:9] if s.commit_sha else "—"
                lines.append(f"| {s.id}: {s.goal[:60]} | {s.status} | {s.attempts} "
                             f"| {s.rejections} | ${s.cost_usd:.4f} | {sha} |")
            if done:
                lines += ["", "## Changes committed", ""]
                for s in done:
                    summ = (s.dev_summary or "").strip().splitlines()
                    summ = f" — {summ[0][:120]}" if summ else ""
                    lines.append(f"- `{s.commit_sha[:9]}` **{s.id}** {s.goal}{summ}")
            incomplete = [s for s in plan.steps if s.status != DONE]
            if incomplete:
                lines += ["", "## Not completed", ""]
                for s in incomplete:
                    extra = f" — {s.skip_reason}" if s.skip_reason else ""
                    lines.append(f"- **{s.id}** ({s.status}) {s.goal}{extra}")
        else:
            lines.append("- **Steps:** (no plan was produced)")

        if code != 0 and detail:
            lines += ["", "## Why it stopped", "", detail.strip()[:2000],
                      "", "See `.agentic/escalation.json` for the full context."]
        lines += ["", "---",
                  "State: `.agentic/state.json` · events: `.agentic/log.jsonl`"
                  + (" · escalation: `.agentic/escalation.json`" if code != 0 else "")]

        path = self.cfg.state_dir / "report.md"
        path.write_text("\n".join(lines) + "\n")
        self.log({"event": "report_written", "code": code})
        return path
