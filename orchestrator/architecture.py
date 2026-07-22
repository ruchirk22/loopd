"""Architecture spine — the binding, per-project decisions loopd holds constant across every
step (and, later, every epic) of a build, so an app-scale plan stays coherent.

This is DISTINCT from engineering memory:
  - memory.md is what loopd *learned* across runs — advisory, accumulated.
  - architecture.md is what THIS build *must honor* — binding: the stack, data model, module
    boundaries, API/contract conventions, the tenancy/isolation strategy chosen for this
    project, deploy/services, coding conventions, and invariants.

It's established once — the Architect proposes it from the brief, the owner approves it
(governed) — then injected into every planner turn as hard context. It lives at
<repo>/.agentic/architecture.md, is hand-editable, and survives --fresh (like memory).
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from .claude_cli import run_claude
from .config import Config

STACK = "Stack"
DATA_MODEL = "Data model"
MODULES = "Module boundaries"
API = "API & contracts"
TENANCY = "Tenancy & isolation"
DEPLOY = "Deploy & services"
CONVENTIONS = "Conventions"
INVARIANTS = "Invariants"
SECTIONS = [STACK, DATA_MODEL, MODULES, API, TENANCY, DEPLOY, CONVENTIONS, INVARIANTS]

_TENANCY_STRATEGIES = ("rls", "app-layer", "none", "other")


def _path(repo) -> Path:
    return Path(repo).expanduser().resolve() / ".agentic" / "architecture.md"


def exists(repo) -> bool:
    return _path(repo).is_file()


def load(repo) -> Dict[str, List[str]]:
    """Parse architecture.md into {section: [bullets]}. Forgiving of hand edits."""
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
            if item:
                data[cur].append(item)
    return data


def render(data: Dict[str, List[str]]) -> str:
    lines = ["# loopd architecture spine", "",
             "_Binding decisions for THIS project — every step must honor them. Hand-editable; "
             "keep entries as `-` bullets under each `##` section._", ""]
    order = [s for s in SECTIONS if s in data] + [s for s in data if s not in SECTIONS]
    if not order:
        order = SECTIONS
    for sec in order:
        items = data.get(sec) or []
        lines.append(f"## {sec}")
        lines += [f"- {i}" for i in items] if items else ["_(unspecified)_"]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def save(repo, data: Dict[str, List[str]]) -> Path:
    p = _path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render(data))
    return p


def discard(repo) -> None:
    p = _path(repo)
    if p.is_file():
        p.unlink()


def as_prompt(repo, cap: int = 8000) -> str:
    """The binding-context text injected into the planner seed (empty if no spine)."""
    if not exists(repo):
        return ""
    txt = render(load(repo)).strip()
    return txt if len(txt) <= cap else (txt[:cap] + "\n_(architecture truncated)_")


def tenancy_strategy(repo) -> str:
    """The chosen isolation strategy (rls / app-layer / none / other / ''), from the spine's
    Tenancy section 'Strategy: X' line — lets probes/gates know what to assume."""
    for b in load(repo).get(TENANCY, []):
        low = b.lower()
        if low.startswith("strategy:"):
            val = low.split(":", 1)[1].strip()
            for s in _TENANCY_STRATEGIES:
                if val.startswith(s):
                    return s
    return ""


def from_proposal(d: dict) -> Dict[str, List[str]]:
    """Map the Architect's structured output to spine sections."""
    d = d or {}

    def _list(key):
        return [str(x).strip() for x in (d.get(key) or []) if str(x).strip()]

    ten = d.get("tenancy") or {}
    strategy = str(ten.get("strategy", "none")).strip().lower()
    if strategy not in _TENANCY_STRATEGIES:
        strategy = "other"
    tenancy = [f"Strategy: {strategy}"]
    if str(ten.get("details", "")).strip():
        tenancy.append(str(ten["details"]).strip())

    return {
        STACK: _list("stack"),
        DATA_MODEL: _list("data_model"),
        MODULES: _list("module_boundaries"),
        API: _list("api_conventions"),
        TENANCY: tenancy,
        DEPLOY: _list("deploy"),
        CONVENTIONS: _list("conventions"),
        INVARIANTS: _list("invariants"),
    }


ARCHITECTURE_SCHEMA = {
    "type": "object",
    "properties": {
        "stack": {"type": "array", "items": {"type": "string"},
                  "description": "Languages, frameworks, database, key libraries — grounded in the repo/brief."},
        "data_model": {"type": "array", "items": {"type": "string"},
                       "description": "Core entities and their key relationships, one per line."},
        "module_boundaries": {"type": "array", "items": {"type": "string"},
                              "description": "How the code is partitioned and what each module owns."},
        "api_conventions": {"type": "array", "items": {"type": "string"},
                            "description": "API/contract conventions (routing, error shape, auth, versioning)."},
        "tenancy": {
            "type": "object",
            "properties": {
                "strategy": {"type": "string", "enum": list(_TENANCY_STRATEGIES),
                             "description": "Chosen isolation strategy for THIS project: rls (Postgres row-level "
                                            "security), app-layer (query-level tenant scoping), none (single-tenant), or other."},
                "details": {"type": "string", "description": "How isolation is enforced and what every query/table must do."},
            },
            "required": ["strategy", "details"],
        },
        "deploy": {"type": "array", "items": {"type": "string"},
                   "description": "Target and the services/datastores the app needs (for gates and deploy)."},
        "conventions": {"type": "array", "items": {"type": "string"},
                        "description": "Coding + testing conventions the build must follow."},
        "invariants": {"type": "array", "items": {"type": "string"},
                       "description": "Things that must ALWAYS hold (security, data, correctness)."},
    },
    "required": ["stack", "data_model", "module_boundaries", "api_conventions",
                 "tenancy", "conventions", "invariants"],
}


def propose(cfg: Config, brief: str, ledger=None) -> Optional[Dict[str, List[str]]]:
    """One model call: the Architect reads the brief (and skims the repo) and proposes the
    binding spine, choosing the tenancy strategy for this project. Returns spine sections, or
    None if it fails (a spine is establishable by hand, so a failure must not block the run)."""
    prompt = (
        "Establish the BINDING architecture for this project — the decisions every later step "
        "must honor. Choose the tenancy/isolation strategy that fits this stack and brief and "
        "state exactly how it's enforced. Skim the repo's shape if helpful; do not write code. "
        "Return only the structured JSON.\n\n## Task brief\n" + (brief or "").strip()
    )
    try:
        system = cfg.prompt("architect_system.md")
    except Exception:
        system = None
    res = run_claude(
        prompt,
        cwd=cfg.repo,
        model=cfg.architect_model,
        append_system_prompt=system,
        allowed_tools=cfg.pm_allowed_tools,   # read-only skim
        permission_mode="default",
        json_schema=ARCHITECTURE_SCHEMA,
        max_turns=cfg.architect_max_turns,
        timeout_s=cfg.call_timeout_s,
        timeout_cost_usd=(ledger.timeout_cost() if ledger is not None else cfg.timeout_cost_usd),
    )
    if ledger is not None:
        ledger.spend(res.cost_usd)
    if not res.ok or not isinstance(res.structured, dict):
        return None
    try:
        return from_proposal(res.structured)
    except Exception:
        return None
