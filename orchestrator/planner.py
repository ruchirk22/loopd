"""The PM agent. Turns a task into a structured plan of small, independently
verifiable steps. Uses --json-schema so we get validated JSON, not prose to regex."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from .claude_cli import run_claude
from .config import Config

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "goal": {"type": "string"},
                    "details": {"type": "string"},
                    "acceptance_criteria": {"type": "string"},
                    # Shell commands that MUST exit 0 only when the step is truly done.
                    "verify": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "goal", "acceptance_criteria", "verify"],
            },
        },
    },
    "required": ["summary", "steps"],
}


@dataclass
class Step:
    id: str
    goal: str
    acceptance_criteria: str
    verify: List[str]
    details: str = ""


def make_plan(task: str, cfg: Config) -> Tuple[str, List[Step], float]:
    system = (cfg.prompts_dir / "pm_system.md").read_text()
    prompt = (
        "Break the following task into the smallest sequence of independently "
        "verifiable steps. Each step MUST include one or more shell commands in "
        "`verify` that exit 0 ONLY when the step is genuinely done (tests, build, "
        "typecheck, lint). Keep the plan minimal and ordered; do not invent scope.\n\n"
        f"TASK:\n{task}\n"
    )
    res = run_claude(
        prompt,
        cwd=cfg.repo,
        model=cfg.pm_model,
        append_system_prompt=system,
        allowed_tools=cfg.pm_allowed_tools,   # read-only: plan against the real code
        permission_mode="default",
        json_schema=PLAN_SCHEMA,
        max_turns=cfg.max_turns_per_call,
    )
    if not res.ok or not res.structured:
        raise RuntimeError(
            f"PM planning failed (cost ${res.cost_usd:.4f}).\n{res.text[:2000]}"
        )
    data = res.structured
    steps = [
        Step(
            id=str(s["id"]),
            goal=s["goal"],
            acceptance_criteria=s["acceptance_criteria"],
            verify=list(s.get("verify", [])),
            details=s.get("details", ""),
        )
        for s in data["steps"]
    ]
    return data.get("summary", ""), steps, res.cost_usd
