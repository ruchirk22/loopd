"""Durable run state. Two things you own:
  1. A state file + JSONL event log under <repo>/.agentic/ (resume + observability).
  2. A git commit per passing step — the handoff unit and your rollback points.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from .config import Config
from .planner import Step


def _git(args, cwd: Path):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


class Ledger:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.repo = cfg.repo
        self.state_path = cfg.state_dir / "state.json"
        self.log_path = cfg.state_dir / "log.jsonl"
        self.state: dict = {}
        self._ensure_git()

    def _ensure_git(self) -> None:
        if not (self.repo / ".git").exists():
            _git(["init"], self.repo)
            _git(["add", "-A"], self.repo)
            _git(["commit", "-m", "agentic-loop: baseline", "--allow-empty"], self.repo)

    def start(self, task: str, summary: str, steps: list[Step]) -> None:
        self.state = {
            "task": task,
            "summary": summary,
            "started": time.time(),
            "total_cost_usd": 0.0,
            "steps": [
                {"id": s.id, "goal": s.goal, "status": "pending", "attempts": 0, "cost_usd": 0.0}
                for s in steps
            ],
        }
        self._save()

    def log(self, event: dict) -> None:
        event["ts"] = time.time()
        with self.log_path.open("a") as f:
            f.write(json.dumps(event) + "\n")

    def add_cost(self, c: float) -> None:
        self.state["total_cost_usd"] += c
        self._save()

    def add_step_cost(self, step_id: str, c: float) -> None:
        for s in self.state["steps"]:
            if s["id"] == step_id:
                s["cost_usd"] += c
        self.state["total_cost_usd"] += c
        self._save()

    def update_step(self, step_id: str, **kw) -> None:
        for s in self.state["steps"]:
            if s["id"] == step_id:
                s.update(kw)
        self._save()

    def commit_step(self, step: Step) -> None:
        _git(["add", "-A"], self.repo)
        _git(["commit", "-m", f"step {step.id}: {step.goal}", "--allow-empty"], self.repo)
        self.update_step(step.id, status="done")
        self.log({"event": "step_done", "step": step.id})

    def _save(self) -> None:
        self.state_path.write_text(json.dumps(self.state, indent=2))

    def report(self) -> str:
        done = sum(1 for s in self.state["steps"] if s["status"] == "done")
        total = len(self.state["steps"])
        lines = [f"Run report: {done}/{total} steps done | total cost ${self.state['total_cost_usd']:.4f}"]
        for s in self.state["steps"]:
            lines.append(
                f"  [{s['status']:>11}] {s['id']}: {s['goal']}  "
                f"(attempts={s['attempts']}, ${s['cost_usd']:.4f})"
            )
        return "\n".join(lines)
