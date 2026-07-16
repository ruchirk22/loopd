"""Deterministic verification. This is YOUR code, not the agent's opinion — it is
what closes the loop. A step is 'done' only when every command exits 0.

NOTE: the commands come from the PM (an LLM), and run with shell=True. That is
acceptable ONLY because this runs inside the sandbox. Do not run untrusted plans
on your host."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Tuple


def run_gates(commands: List[str], cwd: Path, timeout_s: int = 1800) -> Tuple[bool, str]:
    if not commands:
        return True, "(no verify commands specified)"

    logs: List[str] = []
    for cmd in commands:
        logs.append(f"$ {cmd}")
        try:
            p = subprocess.run(
                cmd, cwd=str(cwd), shell=True,
                capture_output=True, text=True, timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            logs.append(f"[TIMEOUT after {timeout_s}s]")
            return False, "\n".join(logs)

        out = (p.stdout or "") + (p.stderr or "")
        if out.strip():
            logs.append(out.rstrip())
        if p.returncode != 0:
            logs.append(f"[FAILED: exit {p.returncode}]")
            return False, "\n".join(logs)
        logs.append("[ok]")

    return True, "\n".join(logs)
