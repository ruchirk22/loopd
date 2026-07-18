"""Engineering memory — a small, structured, human-editable record of what loopd has
learned about a PROJECT, persisted across runs at <repo>/.agentic/memory.md.

Not embeddings, not vector search: plain markdown sections of bullet facts. The planner
reads it at the start of every run (honor decisions, avoid past failures, consider TODOs)
and appends durable knowledge back to it at the end of a run.

It deliberately lives in .agentic/ (so it is loopd-managed and survives --fresh, which only
archives state.json/log.jsonl) and is a normal file you can hand-edit.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List

# The canonical sections (order preserved on render). The parser also keeps any extra
# sections a human adds.
DECISIONS = "Architecture decisions"
FAILURES = "Past failures"
TODOS = "Known TODOs"
SECTIONS = [DECISIONS, FAILURES, TODOS]

_MAX_PER_SECTION = 60  # keep the file legible; oldest fall off


def _path(repo) -> Path:
    return Path(repo).expanduser().resolve() / ".agentic" / "memory.md"


def _norm(s: str) -> str:
    return " ".join(str(s).lower().split())


def load(repo) -> Dict[str, List[str]]:
    """Parse memory.md into {section: [bullets]}. Forgiving of hand edits."""
    p = _path(repo)
    data: Dict[str, List[str]] = {}
    if not p.is_file():
        return data
    cur = None
    for line in p.read_text(errors="replace").splitlines():
        s = line.strip()
        if s.startswith("## "):
            cur = s[3:].strip()
            data.setdefault(cur, [])
        elif cur and s[:2] in ("- ", "* "):
            item = s[2:].strip()
            if item:  # the "_(none yet)_" placeholder is not a bullet, so it's already excluded
                data[cur].append(item)
    return data


def render(data: Dict[str, List[str]]) -> str:
    lines = ["# loopd project memory", "",
             "_Bullet facts loopd has learned about this project. Hand-editable — keep entries "
             "as `-` bullets under each `##` section; loopd appends to them._", ""]
    order = [s for s in SECTIONS if s in data] + [s for s in data if s not in SECTIONS]
    if not order:
        order = SECTIONS
    for sec in order:
        items = data.get(sec) or []
        lines.append(f"## {sec}")
        lines += [f"- {i}" for i in items] if items else ["_(none yet)_"]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def merge(repo, updates: Dict[str, List[str]]) -> Path:
    """Add new bullets under each section (dedup by normalized text, cap per section).
    Creates the file/sections as needed. Returns the memory path."""
    data = load(repo)
    changed = False
    for sec, items in (updates or {}).items():
        items = [str(i).strip() for i in (items or []) if str(i).strip()]
        if not items:
            continue
        data.setdefault(sec, [])
        seen = {_norm(x) for x in data[sec]}
        for it in items:
            if _norm(it) not in seen:
                data[sec].append(it)
                seen.add(_norm(it))
                changed = True
        if len(data[sec]) > _MAX_PER_SECTION:
            data[sec] = data[sec][-_MAX_PER_SECTION:]
            changed = True
    p = _path(repo)
    if changed or not p.is_file():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(render(data))
    return p


def as_prompt(repo, cap: int = 12000) -> str:
    """The memory text injected into the planner's seed (empty string if none). If it
    exceeds `cap`, drop the OLDEST items structurally (never cut a section mid-line) so the
    newest, most relevant knowledge is what survives."""
    p = _path(repo)
    if not p.is_file():
        return ""
    data = load(repo)
    txt = render(data)
    if len(txt) <= cap:
        return txt.strip()
    while len(render(data)) > cap and any(data.values()):
        biggest = max(data, key=lambda s: len(data.get(s) or []))
        if data[biggest]:
            data[biggest].pop(0)  # drop the oldest bullet in the largest section
        else:
            data.pop(biggest)
    return (render(data).rstrip() + "\n\n_(memory truncated — oldest entries dropped)_")


def from_directive_memory(m: dict) -> Dict[str, List[str]]:
    """Map a PM directive's `memory` object to section updates."""
    m = m or {}
    return {DECISIONS: m.get("decisions") or [],
            FAILURES: m.get("failures") or [],
            TODOS: m.get("todos") or []}
