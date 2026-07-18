"""The workspace layer — what makes loopd feel like it owns a *project* over time, not a
single run.

Each repository loopd touches is a long-lived workspace that accumulates: run history,
lifetime cost, forecast accuracy, engineering memory, and repository health. This module
keeps a tiny cross-project registry (so you pick a recent project by name, never by path)
and reads the per-project `.agentic/` artifacts to summarize what a workspace has become.

Stdlib only. The registry lives at ~/.loopd/projects.json (override with LOOPD_HOME).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

from . import forecast as _forecast
from . import memory as _memory


def home() -> Path:
    return Path(os.environ.get("LOOPD_HOME", str(Path.home() / ".loopd"))).expanduser()


def _store() -> Path:
    return home() / "projects.json"


def _load() -> dict:
    p = _store()
    if not p.is_file():
        return {"projects": []}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) and "projects" in data else {"projects": []}
    except (OSError, json.JSONDecodeError):
        return {"projects": []}


def _save(data: dict) -> None:
    p = _store()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, p)


def _entry(data: dict, path: str) -> Optional[dict]:
    return next((e for e in data["projects"] if e.get("path") == path), None)


def _now() -> float:
    return time.time()


def register(repo, when: Optional[float] = None) -> None:
    """Record that a workspace was opened (creates the entry if new)."""
    repo = str(Path(repo).expanduser().resolve())
    data = _load()
    e = _entry(data, repo)
    if e is None:
        e = {"path": repo, "name": Path(repo).name, "runs": 0,
             "lifetime_cost_usd": 0.0, "last_code": None}
        data["projects"].append(e)
    e["name"] = Path(repo).name
    e["last_opened"] = when if when is not None else _now()
    _save(data)


def record_run(repo, code: int, cost: float, when: Optional[float] = None) -> None:
    """Update a workspace's accumulated history after a run finishes."""
    repo = str(Path(repo).expanduser().resolve())
    data = _load()
    e = _entry(data, repo)
    if e is None:
        register(repo, when)
        data = _load()
        e = _entry(data, repo)
    e["runs"] = int(e.get("runs", 0)) + 1
    e["lifetime_cost_usd"] = round(float(e.get("lifetime_cost_usd", 0.0)) + float(cost or 0.0), 4)
    e["last_code"] = code
    e["last_opened"] = when if when is not None else _now()
    _save(data)


def recent(limit: int = 8) -> List[dict]:
    """Most-recently-opened workspaces that still exist on disk (newest first)."""
    data = _load()
    live = [e for e in data["projects"] if Path(e.get("path", "")).is_dir()]
    live.sort(key=lambda e: e.get("last_opened", 0), reverse=True)
    return live[:limit]


# ---------------------------------------------------------------- health / summary

def _git(args: List[str], repo) -> str:
    try:
        return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def is_git_repo(repo) -> bool:
    return _git(["rev-parse", "--is-inside-work-tree"], repo) == "true"


def health(repo) -> dict:
    """A workspace's repository health: branch + how many files are uncommitted."""
    repo = Path(repo).expanduser().resolve()
    if not is_git_repo(repo):
        return {"is_repo": False, "branch": "", "dirty": False, "dirty_count": 0}
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo) or "?"
    status = _git(["status", "--porcelain"], repo)
    lines = [ln for ln in status.splitlines() if ln.strip()]
    return {"is_repo": True, "branch": branch, "dirty": bool(lines), "dirty_count": len(lines)}


def run_state(repo) -> dict:
    """What the current run in this workspace looks like (active/paused/finished/none)."""
    repo = Path(repo).expanduser().resolve()
    sp = repo / ".agentic" / "state.json"
    if not sp.is_file():
        return {"exists": False}
    try:
        st = json.loads(sp.read_text())
    except (OSError, json.JSONDecodeError):
        return {"exists": False}
    plan = st.get("plan") or {}
    steps = plan.get("steps", []) if plan else []
    done = sum(1 for s in steps if s.get("status") in ("done", "skipped"))
    finished = bool(st.get("finished"))
    task = (st.get("task") or "").strip().splitlines()
    return {
        "exists": True,
        "finished": finished,
        "paused": not finished,                # unfinished state == resumable work
        "task": task[0][:80] if task else "",
        "steps_done": done,
        "steps_total": len(steps),
        "cost_usd": float(st.get("total_cost_usd", 0.0)),
        "budget_usd": st.get("budget_usd"),
    }


def summary(repo) -> dict:
    """Everything a workspace has become — for the `loopd` home header and `loopd status`."""
    repo = Path(repo).expanduser().resolve()
    data = _load()
    e = _entry(data, str(repo)) or {}
    mem = _memory.load(repo)
    mem_count = sum(len(v) for v in mem.values())
    try:
        accuracy = _forecast.ForecastHistory(repo).accuracy()
    except Exception:
        accuracy = None
    return {
        "path": str(repo),
        "name": repo.name,
        "runs": int(e.get("runs", 0)),
        "lifetime_cost_usd": float(e.get("lifetime_cost_usd", 0.0)),
        "forecast_accuracy": accuracy,      # None until enough graded runs
        "memory_count": mem_count,
        "health": health(repo),
        "run_state": run_state(repo),
    }
