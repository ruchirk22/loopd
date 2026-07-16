"""Thin, defensive wrapper around the Claude Code CLI in headless mode (`claude -p`).

Headless print mode IS the Agent SDK: it runs the full agent loop (gather context,
act, verify) and exits. We drive it over subprocess so the orchestration policy lives
here, in your code, and is robust to SDK version drift.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence


@dataclass
class ClaudeResult:
    ok: bool
    text: str                    # final assistant text (.result)
    session_id: Optional[str]    # capture this to --resume the same session
    cost_usd: float
    structured: Optional[dict]   # parsed structured output when a --json-schema was used
    raw: dict                    # full parsed JSON envelope, for debugging


def run_claude(
    prompt: str,
    *,
    cwd: Path,
    model: Optional[str] = None,
    append_system_prompt: Optional[str] = None,
    allowed_tools: Optional[str] = None,
    permission_mode: Optional[str] = None,
    resume_session: Optional[str] = None,
    json_schema: Optional[dict] = None,
    add_dirs: Optional[Sequence[Path]] = None,
    max_turns: Optional[int] = None,
    timeout_s: int = 3600,
) -> ClaudeResult:
    cmd = ["claude", "-p", prompt, "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    if append_system_prompt:
        cmd += ["--append-system-prompt", append_system_prompt]
    if allowed_tools:
        cmd += ["--allowedTools", allowed_tools]
    if permission_mode:
        cmd += ["--permission-mode", permission_mode]
    if resume_session:
        cmd += ["--resume", resume_session]
    if json_schema is not None:
        cmd += ["--json-schema", json.dumps(json_schema)]
    if max_turns:
        cmd += ["--max-turns", str(max_turns)]
    for d in (add_dirs or []):
        cmd += ["--add-dir", str(d)]

    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout_s)
    data = _parse_json(proc.stdout.strip())

    if data is None:
        return ClaudeResult(
            ok=False,
            text=(proc.stderr or proc.stdout or "no output"),
            session_id=None,
            cost_usd=0.0,
            structured=None,
            raw={"returncode": proc.returncode, "stderr": proc.stderr, "stdout": proc.stdout},
        )

    subtype = data.get("subtype")
    is_error = (proc.returncode != 0) or (subtype == "error") or (data.get("is_error") is True)
    text = data.get("result") or data.get("content") or ""
    if not isinstance(text, str):
        text = json.dumps(text)
    session_id = data.get("session_id")
    cost = float(data.get("total_cost_usd") or data.get("cost_usd") or 0.0)

    # Structured output lands in `structured_output` on current CLIs; if your version
    # doesn't populate it, fall back to parsing the final message as JSON.
    structured = data.get("structured_output")
    if structured is None and json_schema is not None:
        structured = _parse_json(text)

    return ClaudeResult(ok=not is_error, text=text, session_id=session_id,
                        cost_usd=cost, structured=structured, raw=data)


def _parse_json(s: str):
    """Parse `--output-format json`. Handles the single-object shape, the older
    array shape (take last element), and a stream-json fallback (last parseable line)."""
    if not s:
        return None
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        for line in reversed(s.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        return None
    if isinstance(obj, list):
        return obj[-1] if obj else None
    return obj
