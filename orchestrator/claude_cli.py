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
    fork_session: bool = False,
    json_schema: Optional[dict] = None,
    add_dirs: Optional[Sequence[Path]] = None,
    max_turns: Optional[int] = None,
    timeout_s: int = 3600,
    timeout_cost_usd: float = 0.0,
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
    if fork_session:
        cmd += ["--fork-session"]
    if json_schema is not None:
        cmd += ["--json-schema", json.dumps(json_schema)]
    if max_turns:
        cmd += ["--max-turns", str(max_turns)]
    for d in (add_dirs or []):
        cmd += ["--add-dir", str(d)]

    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                              errors="replace", timeout=timeout_s)
    except subprocess.TimeoutExpired:
        # A hung call is a retryable failure, not a run-ending crash. The process was
        # killed but the API work was billed; charge an estimate so the budget rail
        # is not blind here (raw.error='timeout' lets the caller branch its retry).
        return ClaudeResult(
            ok=False,
            text=f"[claude CLI timed out after {timeout_s}s]",
            session_id=None,
            cost_usd=float(timeout_cost_usd or 0.0),
            structured=None,
            raw={"error": "timeout", "timeout_s": timeout_s},
        )
    except OSError as exc:
        # e.g. E2BIG when a huge prompt/schema overflows the argv limit — a retryable
        # failure, not a crash that escapes the exit-code contract.
        return ClaudeResult(
            ok=False,
            text=f"[claude CLI could not be launched: {exc}]",
            session_id=None,
            cost_usd=0.0,
            structured=None,
            raw={"error": "oserror", "detail": str(exc)},
        )

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

    # Real CLI error subtypes are error_max_turns / error_during_execution — never a
    # bare "error". Treat any "error*" subtype as an error so a max-turns-truncated run
    # isn't mistaken for a clean completion.
    subtype = data.get("subtype")
    subtype_error = bool(subtype) and str(subtype).startswith("error")
    is_error = (proc.returncode != 0) or subtype_error or (data.get("is_error") is True)
    text = data.get("result") or data.get("content") or ""
    if not isinstance(text, str):
        text = json.dumps(text)
    if is_error and proc.stderr:
        text = f"{text}\n[stderr] {proc.stderr.strip()}" if text else proc.stderr.strip()
    session_id = data.get("session_id")
    try:
        cost = float(data.get("total_cost_usd") or data.get("cost_usd") or 0.0)
    except (TypeError, ValueError):
        cost = 0.0

    # Structured output lands in `structured_output` on current CLIs; if your version
    # doesn't populate it, fall back to parsing the final message as JSON.
    structured = data.get("structured_output")
    if structured is None and json_schema is not None:
        structured = _parse_json(text)
    if structured is not None and not isinstance(structured, dict):
        structured = None  # all our schemas are objects; anything else is garbage

    return ClaudeResult(ok=not is_error, text=text, session_id=session_id,
                        cost_usd=cost, structured=structured, raw=data)


def _parse_json(s: str) -> Optional[dict]:
    """Parse `--output-format json`. Handles the single-object shape, the older
    array shape (take last dict element), and a stream-json fallback (last parseable
    dict line). Only ever returns a dict — bare scalars/lists are not envelopes."""
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
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None
    if isinstance(obj, list):
        for item in reversed(obj):
            if isinstance(item, dict):
                return item
        return None
    return obj if isinstance(obj, dict) else None
