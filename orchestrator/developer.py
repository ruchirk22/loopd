"""The developer agent. It receives the PM's verbatim prompt, implements, and ends
with a structured summary (schema-forced) that becomes part of the handover packet.
On gate failure the orchestrator resumes the SAME session with the gate transcript,
so the developer keeps its context for the retry — without spending a PM turn."""
from __future__ import annotations

from typing import Optional

from .claude_cli import ClaudeResult, run_claude
from .config import Config

DEV_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string",
                    "description": "What you changed and why, concise but complete."},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "commands_run": {"type": "array", "items": {"type": "string"},
                         "description": "Verification commands you actually executed."},
        "concerns": {"type": "array", "items": {"type": "string"},
                     "description": "Anything the reviewer should know: risks, shortcuts, open questions."},
    },
    "required": ["summary", "files_changed", "commands_run", "concerns"],
}


def run_prompt(prompt: str, cfg: Config, resume_session: Optional[str] = None) -> ClaudeResult:
    return run_claude(
        prompt,
        cwd=cfg.repo,
        model=cfg.dev_model,
        append_system_prompt=cfg.prompt("dev_system.md"),
        allowed_tools=cfg.dev_allowed_tools,
        permission_mode=cfg.dev_permission_mode,  # bypass — safe only in the sandbox
        resume_session=resume_session,
        json_schema=DEV_SUMMARY_SCHEMA,
        max_turns=cfg.max_turns_per_call,
        timeout_s=cfg.call_timeout_s,
        timeout_cost_usd=cfg.timeout_cost_usd,
    )


def gate_feedback_prompt(gate_log: str) -> str:
    return (
        "The orchestrator ran the step's verification commands and they FAILED. "
        "Transcript below:\n\n"
        f"{gate_log}\n\n"
        "Fix the code so every verification command passes. Run them yourself to confirm "
        "before you finish. Do not weaken any check."
    )


def error_retry_prompt(original_prompt: str, error_context: str) -> str:
    """Used when the previous developer call died without a resumable session:
    the fresh session needs the full brief AND what went wrong last time."""
    return (
        f"{original_prompt}\n\n"
        "NOTE: a previous attempt at this step ended abnormally. Context from that attempt "
        "(error output and the last verification transcript, if any):\n\n"
        f"{error_context}\n\n"
        "Inspect the repository's current state first — partial changes from the failed "
        "attempt may already be present."
    )
