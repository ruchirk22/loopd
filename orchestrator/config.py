from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    """All the knobs you control. Everything here is yours — none of it is hidden
    inside Claude Code."""

    # The repository the DEVELOPER agent works in (your sandboxed target project).
    repo: Path

    # --- Models (aliases: opus | sonnet | haiku, or a full model id) ---
    pm_model: str = os.environ.get("PM_MODEL", "opus")       # planning / judgement
    dev_model: str = os.environ.get("DEV_MODEL", "sonnet")   # the coding grind

    # --- Tool allowlists (exactly what each agent may touch) ---
    dev_allowed_tools: str = os.environ.get("DEV_ALLOWED_TOOLS", "Read,Edit,Write,Bash,Glob,Grep")
    pm_allowed_tools: str = os.environ.get("PM_ALLOWED_TOOLS", "Read,Glob,Grep")  # read-only: plan against real code

    # --- Loop control ---
    max_attempts_per_step: int = int(os.environ.get("MAX_ATTEMPTS_PER_STEP", "3"))
    max_turns_per_call: int = int(os.environ.get("MAX_TURNS_PER_CALL", "40"))
    budget_usd: float = float(os.environ.get("BUDGET_USD", "10"))

    # Permission mode for the DEVELOPER. `bypassPermissions` means no approval prompts —
    # ONLY safe because the developer runs inside the sandbox (container / worktree).
    dev_permission_mode: str = os.environ.get("DEV_PERMISSION_MODE", "bypassPermissions")

    # Locations (filled in __post_init__)
    state_dir: Path = field(default=None)
    prompts_dir: Path = field(default=None)

    def __post_init__(self) -> None:
        self.repo = Path(self.repo).resolve()
        if self.state_dir is None:
            self.state_dir = self.repo / ".agentic"
        if self.prompts_dir is None:
            self.prompts_dir = Path(__file__).resolve().parent.parent / "prompts"
        self.state_dir.mkdir(parents=True, exist_ok=True)
