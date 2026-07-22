"""Program orchestration — turn a whole PRD into a sequence of governed epics.

For app-scale work that overflows a single plan, the Program Planner decomposes the PRD into
ordered epics. Each epic then runs as a FULL, independently-verified loop run — its own plan,
its own gates, its own commits — while sharing the binding architecture spine and the
accumulating engineering memory, so the pieces stay coherent. Between epics the owner has a
checkpoint (governed autonomy). Progress lives at <repo>/.agentic/program.json, so a program
is resumable epic-by-epic.

This layer is deliberately thin: it decides WHAT to build in what order and when to pause;
the existing loop does the building and the proving.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional

from . import reporter
from .claude_cli import run_claude
from .config import Config

PENDING, IN_PROGRESS, DONE, FAILED, SKIPPED = "pending", "in_progress", "done", "failed", "skipped"


def _path(repo) -> Path:
    return Path(repo).expanduser().resolve() / ".agentic" / "program.json"


def exists(repo) -> bool:
    return _path(repo).is_file()


def load(repo) -> dict:
    try:
        return json.loads(_path(repo).read_text())
    except (OSError, ValueError):
        return {}


def save(repo, prog: dict) -> Path:
    p = _path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(prog, indent=2))
    return p


PROGRAM_SCHEMA = {
    "type": "object",
    "properties": {
        "epics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "short kebab-case id, e.g. 'auth' or 'calibration'"},
                    "title": {"type": "string", "description": "one-line name of the epic"},
                    "objective": {"type": "string",
                                  "description": "a self-contained, independently-testable objective for this epic "
                                                 "— everything a fresh build session needs, since it has no memory of "
                                                 "the others beyond the shared architecture and repo."},
                },
                "required": ["id", "title", "objective"],
            },
        },
    },
    "required": ["epics"],
}


def decompose(cfg: Config, prd: str, ledger=None) -> List[dict]:
    """One model call: the Program Planner splits the PRD into ordered epics. Returns [] on
    failure (the caller then just runs the PRD as a single plan)."""
    prompt = (
        "Decompose this PRD into an ordered list of epics — coherent, independently-buildable "
        "chunks, each of which a focused build session could finish and verify on its own. "
        "Order them so each builds on the last (foundations/data model first). Keep it as few "
        "epics as the work honestly needs. Return only the structured JSON.\n\n## PRD\n"
        + (prd or "").strip()
    )
    try:
        system = cfg.prompt("program_system.md")
    except Exception:
        system = None
    res = run_claude(
        prompt,
        cwd=cfg.repo,
        model=cfg.program_model,
        append_system_prompt=system,
        allowed_tools=cfg.pm_allowed_tools,
        permission_mode="default",
        json_schema=PROGRAM_SCHEMA,
        max_turns=cfg.program_max_turns,
        timeout_s=cfg.call_timeout_s,
        timeout_cost_usd=(ledger.timeout_cost() if ledger is not None else cfg.timeout_cost_usd),
    )
    if ledger is not None:
        ledger.spend(res.cost_usd)
    if not res.ok or not isinstance(res.structured, dict):
        return []
    epics = res.structured.get("epics")
    if not isinstance(epics, list):
        return []
    out, seen = [], set()
    for e in epics:
        if not isinstance(e, dict) or not str(e.get("objective", "")).strip():
            continue
        eid = str(e.get("id") or f"epic-{len(out) + 1}").strip()
        while eid in seen:
            eid += "-2"
        seen.add(eid)
        out.append({"id": eid, "title": str(e.get("title", eid)).strip(), "objective": str(e["objective"]).strip()})
    return out


def _epic_brief(epic: dict, prd: str) -> str:
    """The brief handed to a single epic's run — its objective, plus PRD context. The binding
    architecture spine is injected automatically by the planner, so it isn't repeated here."""
    parts = [f"# Epic: {epic['title']}", "", "## Objective", epic["objective"]]
    if prd:
        parts += ["", "## Wider program context (for reference — build ONLY this epic's objective)",
                  prd[:4000]]
    return "\n".join(parts)


def _read_prd(cfg: Config, task: Optional[str]) -> str:
    if task and task.strip():
        return task.strip()
    bf = cfg.state_dir / "brief.md"
    return bf.read_text(errors="replace") if bf.is_file() else ""


def _render_epics(prog: dict) -> str:
    lines = ["", "  Program plan:"]
    for i, e in enumerate(prog["epics"], 1):
        mark = {DONE: "✓", FAILED: "✗", SKIPPED: "–", IN_PROGRESS: "▸"}.get(e["status"], " ")
        lines.append(f"   {mark} {i}. {e['title']}")
    return "\n".join(lines)


def _render_done(prog: dict) -> str:
    done = sum(1 for e in prog["epics"] if e["status"] == DONE)
    skipped = sum(1 for e in prog["epics"] if e["status"] == SKIPPED)
    return (f"\n  ✓ Program complete — {done} epic(s) delivered"
            + (f", {skipped} skipped" if skipped else "") + f" of {len(prog['epics'])}.\n")


def _auto(cfg: Config) -> bool:
    """True when we should proceed without prompting (flags or non-interactive)."""
    if getattr(cfg, "assume_yes", False) or getattr(cfg, "force", False):
        return True
    try:
        return not sys.stdin.isatty()
    except (AttributeError, ValueError):
        return True


def _approve_plan(cfg: Config) -> bool:
    if _auto(cfg):
        return True
    while True:
        print("\n  Build this program?   [Y] yes  ·  [A] abort")
        try:
            ans = input("  > ").strip().lower()
        except EOFError:
            return True
        if ans in ("", "y", "yes"):
            return True
        if ans in ("a", "abort", "n", "no"):
            return False


def _epic_checkpoint(cfg: Config, epic: dict) -> str:
    """Governed boundary before each epic: 'continue' | 'skip' | 'abort'."""
    if _auto(cfg):
        return "continue"
    while True:
        print(f"\n  Next epic — {epic['title']}   [C] continue  ·  [S] skip  ·  [A] abort")
        try:
            ans = input("  > ").strip().lower()
        except EOFError:
            return "continue"
        if ans in ("", "c", "continue", "y", "yes"):
            return "continue"
        if ans in ("s", "skip"):
            return "skip"
        if ans in ("a", "abort", "n", "no"):
            return "abort"


def _default_run_epic(brief: str, cfg: Config, fresh: bool, resume: bool) -> int:
    from . import loop  # lazy import avoids any import-order coupling
    return loop.run(brief, cfg, resume=resume, fresh=fresh)


def run_program(task: Optional[str], cfg: Config, resume: bool = False,
                run_epic: Optional[Callable] = None,
                decompose_fn: Optional[Callable] = None) -> int:
    """Decompose the PRD into epics and run each as a governed, independently-verified loop.
    Returns 0 when every epic is delivered; the failing epic's exit code otherwise (resumable
    with `--resume`). `run_epic`/`decompose_fn` are injectable for testing."""
    run_epic = run_epic or _default_run_epic
    decompose_fn = decompose_fn or decompose
    rep = reporter.active()
    repo = cfg.repo

    if resume and exists(repo):
        prog = load(repo)
        done = sum(1 for e in prog.get("epics", []) if e["status"] == DONE)
        rep.block(f"Resuming program — {done}/{len(prog['epics'])} epics done.")
    else:
        prd = _read_prd(cfg, task)
        if not prd:
            print("No PRD to build a program from — provide a task string or a brief.", file=sys.stderr)
            return 2
        epics = decompose_fn(cfg, prd)
        if len(epics) <= 1:
            rep.block("This is a single coherent unit — running it as one plan (no program needed).")
            return run_epic(task, cfg, fresh=not resume, resume=resume)
        prog = {"prd": prd[:8000], "created": time.time(),
                "epics": [{**e, "status": PENDING} for e in epics]}
        save(repo, prog)
        rep.block(_render_epics(prog))
        if not _approve_plan(cfg):
            rep.block("  Program aborted before building.")
            return 1

    # The epic checkpoint is the governance gate; don't also prompt a forecast per epic.
    cfg.forecast_enabled = False

    for epic in prog["epics"]:
        if epic["status"] in (DONE, SKIPPED):
            continue
        decision = _epic_checkpoint(cfg, epic)
        if decision == "abort":
            rep.block("  Program paused — resume with `loopd build --resume`.")
            return 1
        if decision == "skip":
            epic["status"] = SKIPPED
            save(repo, prog)
            continue
        was_started = epic["status"] in (IN_PROGRESS, FAILED)  # resume its run vs a fresh one
        epic["status"] = IN_PROGRESS
        save(repo, prog)
        rep.block(f"\n═══ Epic: {epic['title']} ═══")
        rc = run_epic(_epic_brief(epic, prog.get("prd", "")), cfg,
                      fresh=not was_started, resume=was_started)
        epic["status"] = DONE if rc == 0 else FAILED
        save(repo, prog)
        if rc != 0:
            rep.block(f"  Epic '{epic['title']}' stopped (exit {rc}). "
                      "Fix or adjust, then `loopd build --resume`.")
            return rc

    rep.block(_render_done(prog))
    return 0
