"""Deterministic verification. This is YOUR code, not either agent's opinion — it is
what closes the loop. A step is 'done' only when every check command exits 0.

An EMPTY check list is a hard FAIL: a step that cannot be verified cannot be accepted.

Commands may carry a per-command timeout prefix: "timeout=900;npm run build".
Optional `setup` commands run first (a setup failure fails the gate); optional
`teardown` commands ALWAYS run afterwards (failures are logged, not fatal).

NOTE: the commands come from the PM (an LLM), and run with shell=True. That is
acceptable ONLY because this runs inside the sandbox. Do not run untrusted plans
on your host."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

_TIMEOUT_PREFIX = re.compile(r"^timeout=(\d+)\s*;\s*(.+)$", re.DOTALL)


def _split_timeout(cmd: str, default_s: int) -> Tuple[int, str]:
    m = _TIMEOUT_PREFIX.match(cmd.strip())
    if m:
        return int(m.group(1)), m.group(2)
    return default_s, cmd


def _run_one(cmd: str, cwd: Path, timeout_s: int, logs: List[str]) -> bool:
    per_timeout, real_cmd = _split_timeout(cmd, timeout_s)
    logs.append(f"$ {real_cmd}")
    try:
        p = subprocess.run(
            real_cmd, cwd=str(cwd), shell=True,
            capture_output=True, text=True, timeout=per_timeout,
        )
    except subprocess.TimeoutExpired:
        logs.append(f"[TIMEOUT after {per_timeout}s]")
        return False
    out = (p.stdout or "") + (p.stderr or "")
    if out.strip():
        logs.append(out.rstrip())
    if p.returncode != 0:
        logs.append(f"[FAILED: exit {p.returncode}]")
        return False
    logs.append("[ok]")
    return True


def run_gates(
    commands: List[str],
    cwd: Path,
    timeout_s: int = 1800,
    setup: Optional[List[str]] = None,
    teardown: Optional[List[str]] = None,
) -> Tuple[bool, str]:
    commands = [c for c in (commands or []) if c and c.strip()]
    if not commands:
        return False, "GATE FAILED: no verify commands — an unverifiable step cannot be accepted."

    logs: List[str] = []
    passed = True
    try:
        for cmd in (setup or []):
            logs.append("[setup]")
            if not _run_one(cmd, cwd, timeout_s, logs):
                return False, "\n".join(logs)
        for cmd in commands:
            if not _run_one(cmd, cwd, timeout_s, logs):
                passed = False
                break
    finally:
        for cmd in (teardown or []):
            logs.append("[teardown]")
            if not _run_one(cmd, cwd, timeout_s, logs):
                logs.append("[teardown failure ignored]")

    return passed, "\n".join(logs)
