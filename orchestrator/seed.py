"""Context seeding: how the owner's knowledge reaches the PM.

All paths converge on ONE durable artifact — <repo>/.agentic/brief.md — which seeds
the initial PM session and every reincarnated one:

  1. /handoff (recommended): the owner's live interactive Claude Code session writes
     brief.md itself (see commands/handoff.md). We just load it.
  2. --brief <path>: an existing brief file is copied into place.
  3. --seed-session <id>: fork the interactive session headlessly (--fork-session, so
     the original session is untouched) and have the fork emit a structured brief.
  4. Plain task text: wrapped into a minimal brief.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from .claude_cli import run_claude
from .config import Config
from .ledger import Ledger

BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "objective": {"type": "string",
                      "description": "What must exist when this is done, in testable terms."},
        "repo_facts": {"type": "array", "items": {"type": "string"},
                       "description": "Stack, layout, build/test commands verified to work."},
        "decisions": {"type": "array", "items": {"type": "string"},
                      "description": "Decisions already made, each WITH its rationale."},
        "environment": {"type": "array", "items": {"type": "string"},
                        "description": "Target infra, emulators available, secret NAMES (never values)."},
        "gotchas": {"type": "array", "items": {"type": "string"},
                    "description": "Dead ends already hit, quirks, things that look wrong but are right."},
        "out_of_scope": {"type": "array", "items": {"type": "string"}},
        "done_definition": {"type": "array", "items": {"type": "string"},
                            "description": "Checkable statements that all must hold for 'done'."},
    },
    "required": ["objective", "repo_facts", "decisions", "environment",
                 "gotchas", "out_of_scope", "done_definition"],
}

_FORK_PROMPT = (
    "You are handing this task off to an automated PM+Developer build loop that has NONE of "
    "this conversation's context. Distill everything that matters into the structured brief: "
    "objective (testable), repo facts (verified build/test commands), decisions WITH rationale, "
    "environment (secret NAMES only, never values), gotchas/dead ends, out-of-scope, and a "
    "done-definition of independently checkable statements. Be exhaustive — whatever you leave "
    "out, the loop will not know."
)


def _render(data: dict) -> str:
    def sect(title: str, items) -> str:
        if isinstance(items, str):
            return f"## {title}\n{items}\n"
        body = "\n".join(f"- {i}" for i in items) or "- (none)"
        return f"## {title}\n{body}\n"

    return "\n".join([
        "# Task brief",
        "",
        sect("Objective", data.get("objective", "")),
        sect("Repo facts", data.get("repo_facts", [])),
        sect("Decisions already made", data.get("decisions", [])),
        sect("Environment", data.get("environment", [])),
        sect("Gotchas / dead ends", data.get("gotchas", [])),
        sect("Out of scope", data.get("out_of_scope", [])),
        sect("Definition of done", data.get("done_definition", [])),
    ])


def ensure_brief(cfg: Config, ledger: Ledger, task: Optional[str],
                 resume: bool = False) -> str:
    """Produce (or load) <repo>/.agentic/brief.md and return its text.

    Precedence: --brief and --seed-session always win. Otherwise, on a RESUME the
    brief.md written at run start is authoritative; on a FRESH run explicit task text
    wins over any leftover brief.md (which may belong to a previous, unrelated task —
    brief.md survives --fresh, so a stale one must never silently override a new task)."""
    brief_file = cfg.state_dir / "brief.md"

    if cfg.brief_path:
        src = Path(cfg.brief_path).resolve()
        if not src.is_file():
            raise FileNotFoundError(f"--brief {src} does not exist")
        if src != brief_file.resolve():
            shutil.copyfile(src, brief_file)
        ledger.log({"event": "brief_loaded", "source": str(src)})
        return brief_file.read_text()

    if cfg.seed_session:
        print(f"Seeding brief from interactive session {cfg.seed_session} (forked, original untouched)…")
        res = run_claude(
            _FORK_PROMPT,
            cwd=cfg.repo,  # sessions are keyed by project dir — must match the interactive session's cwd
            model=cfg.pm_model,
            resume_session=cfg.seed_session,
            fork_session=True,
            json_schema=BRIEF_SCHEMA,
            max_turns=cfg.max_turns_per_call,
            timeout_s=cfg.call_timeout_s,
            timeout_cost_usd=cfg.timeout_cost_usd,
        )
        ledger.spend(res.cost_usd)
        if not res.ok or not res.structured:
            raise RuntimeError(
                f"--seed-session fork failed: {res.text[:800]}\n"
                "Hint: the session id must belong to a session opened in the SAME directory as "
                "--repo. Fall back to /handoff (writes .agentic/brief.md) if this persists.")
        brief_file.write_text(_render(res.structured))
        ledger.log({"event": "brief_forked", "session": cfg.seed_session, "cost": res.cost_usd})
        return brief_file.read_text()

    # On resume, the brief written at run start is authoritative — never regenerate it.
    if resume and brief_file.exists():
        ledger.log({"event": "brief_loaded", "source": str(brief_file)})
        return brief_file.read_text()

    # Fresh run: explicit task text wins over any leftover brief.md from a prior task.
    if task and task.strip():
        brief_file.write_text(f"# Task brief\n\n## Objective\n{task.strip()}\n")
        ledger.log({"event": "brief_from_task_text"})
        return brief_file.read_text()

    # No task text (e.g. /handoff wrote the brief itself): use whatever is on disk.
    if brief_file.exists():
        ledger.log({"event": "brief_loaded", "source": str(brief_file)})
        return brief_file.read_text()

    raise RuntimeError(
        "No task context: provide a task string, --brief <file>, --seed-session <id>, "
        "or write .agentic/brief.md via the /handoff command in your interactive session.")
