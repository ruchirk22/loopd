from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Config:
    """All the knobs you control. Everything here is policy that lives in YOUR code —
    none of it is hidden inside Claude Code."""

    # The repository the DEVELOPER agent works in (your sandboxed target project).
    repo: Path

    # --- Models. Owner requirement: Opus 4.8 for BOTH agents, pinned by full id
    # (aliases like "opus" float across releases; the pinned id does not). ---
    pm_model: str = field(default_factory=lambda: _env("PM_MODEL", "claude-opus-4-8"))
    dev_model: str = field(default_factory=lambda: _env("DEV_MODEL", "claude-opus-4-8"))

    # --- Tool allowlists (exactly what each agent may touch) ---
    dev_allowed_tools: str = field(default_factory=lambda: _env("DEV_ALLOWED_TOOLS", "Read,Edit,Write,Bash,Glob,Grep"))
    # PM is read-only: it plans and reviews against the real code but never edits.
    pm_allowed_tools: str = field(default_factory=lambda: _env("PM_ALLOWED_TOOLS", "Read,Glob,Grep"))

    # --- Loop control ---
    # Dev <-> gates inner retries per review cycle (no PM turn while gates are red).
    max_attempts_per_step: int = field(default_factory=lambda: int(_env("MAX_ATTEMPTS_PER_STEP", "3")))
    # PM rejections of green-gated work before it must replan/descope/abort instead.
    max_rejections_per_step: int = field(default_factory=lambda: int(_env("MAX_REJECTIONS_PER_STEP", "2")))
    # Plan mutations + failed finalization attempts share this cap.
    max_replans: int = field(default_factory=lambda: int(_env("MAX_REPLANS", "3")))
    max_turns_per_call: int = field(default_factory=lambda: int(_env("MAX_TURNS_PER_CALL", "40")))
    # Opus 4.8 on both agents: default budget sized for a real multi-step run.
    budget_usd: float = field(default_factory=lambda: float(_env("BUDGET_USD", "25")))
    # 0 disables the wall-clock cap.
    max_wall_clock_min: int = field(default_factory=lambda: int(_env("MAX_WALL_CLOCK_MIN", "0")))

    # --- PM context management (checkpoint & reincarnate) ---
    checkpoint_every_reviews: int = field(default_factory=lambda: int(_env("CHECKPOINT_EVERY_REVIEWS", "8")))
    handover_bytes_cap: int = field(default_factory=lambda: int(_env("HANDOVER_BYTES_CAP", "150000")))

    # --- Handover packet caps ---
    handover_diff_cap: int = field(default_factory=lambda: int(_env("HANDOVER_DIFF_CAP", "20000")))
    gate_log_tail: int = field(default_factory=lambda: int(_env("GATE_LOG_TAIL", "8000")))

    # --- Timeouts ---
    call_timeout_s: int = field(default_factory=lambda: int(_env("CALL_TIMEOUT_S", "3600")))
    gate_timeout_s: int = field(default_factory=lambda: int(_env("GATE_TIMEOUT_S", "1800")))
    # A timed-out CLI call reports $0 (the process was killed) but the API work was
    # billed. Charge at least this, or the largest per-call cost seen so far, so the
    # budget rail is not blind on the exact call it most needs to catch.
    timeout_cost_usd: float = field(default_factory=lambda: float(_env("TIMEOUT_COST_USD", "1.0")))

    # Permission mode for the DEVELOPER. `bypassPermissions` means no approval prompts —
    # ONLY safe because the developer runs inside the sandbox (container / worktree).
    dev_permission_mode: str = field(default_factory=lambda: _env("DEV_PERMISSION_MODE", "bypassPermissions"))

    # Isolate each run on its own git branch (agentic/run-<ts>) in the target repo.
    use_run_branch: bool = field(default_factory=lambda: _env("USE_RUN_BRANCH", "1") not in ("0", "false", ""))

    # --- Per-run inputs (set by run.py, not env) ---
    brief_path: Optional[Path] = None          # --brief: existing handover brief
    seed_session: Optional[str] = None         # --seed-session: fork an interactive session
    final_verify_extra: List[str] = field(default_factory=list)  # --final-verify (repeatable)

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

    def prompt(self, name: str) -> str:
        return (self.prompts_dir / name).read_text()
