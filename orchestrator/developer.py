"""The developer agent. Implements one step at a time inside the sandboxed repo.
On gate failure the orchestrator calls resume_with_feedback, which --resumes the
SAME session so the developer keeps its context for the retry."""
from __future__ import annotations

from .claude_cli import ClaudeResult, run_claude
from .config import Config
from .planner import Step


def _brief(step: Step) -> str:
    checks = "\n".join(f"  $ {c}" for c in step.verify) or "  (none specified)"
    return (
        "Implement this step in the current repository.\n\n"
        f"GOAL: {step.goal}\n"
        f"DETAILS: {step.details}\n"
        f"ACCEPTANCE CRITERIA: {step.acceptance_criteria}\n\n"
        "Before you finish, run these commands yourself and make sure they all pass:\n"
        f"{checks}\n\n"
        "Work only inside this repository. End with a concise summary of what you changed."
    )


def run_step(step: Step, cfg: Config) -> ClaudeResult:
    system = (cfg.prompts_dir / "dev_system.md").read_text()
    return run_claude(
        _brief(step),
        cwd=cfg.repo,
        model=cfg.dev_model,
        append_system_prompt=system,
        allowed_tools=cfg.dev_allowed_tools,
        permission_mode=cfg.dev_permission_mode,  # bypass — safe only in the sandbox
        max_turns=cfg.max_turns_per_call,
    )


def resume_with_feedback(session_id: str, gate_log: str, cfg: Config) -> ClaudeResult:
    system = (cfg.prompts_dir / "dev_system.md").read_text()
    prompt = (
        "The verification commands failed. Output below:\n\n"
        f"{gate_log[:6000]}\n\n"
        "Fix the code so every verification command passes, then confirm."
    )
    return run_claude(
        prompt,
        cwd=cfg.repo,
        model=cfg.dev_model,
        append_system_prompt=system,
        allowed_tools=cfg.dev_allowed_tools,
        permission_mode=cfg.dev_permission_mode,
        resume_session=session_id,
        max_turns=cfg.max_turns_per_call,
    )
