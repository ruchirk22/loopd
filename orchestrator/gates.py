"""Deterministic verification. This is YOUR code, not either agent's opinion — it is
what closes the loop. A step is 'done' only when every check command exits 0.

An EMPTY check list is a hard FAIL: a step that cannot be verified cannot be accepted.

Commands may carry a per-command timeout prefix: "timeout=900;npm run build".
Optional `setup` commands run first (a setup failure fails the gate); optional
`teardown` commands ALWAYS run afterwards (failures are logged, not fatal).

Each command runs in its OWN process group; on timeout the whole group is killed, so
a check that backgrounds a server (or a build that spawns workers) cannot leak
processes that poison later gates or the final regression sweep.

NOTE: the commands come from the PM (an LLM), and run with shell=True. That is
acceptable ONLY because this runs inside the sandbox. Do not run untrusted plans
on your host. Gate authorship is a trust boundary, not a guarantee — see plan.py's
trivial-command screening and the README's honesty notes."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import List, Optional, Tuple

# Only treat "timeout=N;" as our prefix when the remainder does NOT reference $timeout
# (otherwise we would eat a legitimate shell variable assignment).
_TIMEOUT_PREFIX = re.compile(r"^timeout=(\d+)\s*;\s*(.+)$", re.DOTALL)


def _split_timeout(cmd: str, default_s: int) -> Tuple[int, str]:
    m = _TIMEOUT_PREFIX.match(cmd.strip())
    if m and not re.search(r"\$\{?timeout\b", m.group(2)):
        return int(m.group(1)), m.group(2)
    return default_s, cmd


def _run_one(cmd: str, cwd: Path, timeout_s: int, logs: List[str]) -> bool:
    per_timeout, real_cmd = _split_timeout(cmd, timeout_s)
    logs.append(f"$ {real_cmd}")
    # start_new_session=True => the child is its own process-group leader (pgid == pid),
    # so we can kill the whole tree on timeout.
    proc = subprocess.Popen(real_cmd, cwd=str(cwd), shell=True, start_new_session=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        out, _ = proc.communicate(timeout=per_timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out = ""
        if out and out.strip():
            logs.append(out.rstrip())
        logs.append(f"[TIMEOUT after {per_timeout}s — process group killed]")
        return False
    finally:
        # Reap any process the command backgrounded (`cmd &`, a spawned server) so it can't
        # leak into later gates or the pristine final sweep. No-op if the group is empty.
        if not _group_gone(proc.pid):
            _kill_group(proc)
    if out and out.strip():
        logs.append(out.rstrip())
    if proc.returncode != 0:
        logs.append(f"[FAILED: exit {proc.returncode}]")
        return False
    logs.append("[ok]")
    return True


def _group_gone(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return False
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return True


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill the whole process group, escalating to SIGKILL. Success is the GROUP being
    gone (leader death is not enough — a SIGTERM-trapping child keeps the group alive)."""
    pgid = proc.pid  # pgid == pid thanks to start_new_session
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            break
        deadline = time.time() + (3 if sig == signal.SIGTERM else 2)
        while time.time() < deadline:
            if _group_gone(pgid):
                proc.wait(timeout=1) if proc.poll() is None else None
                return
            time.sleep(0.1)
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


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
                passed = False
                break
        if passed:
            for cmd in commands:
                if not _run_one(cmd, cwd, timeout_s, logs):
                    passed = False
                    break
    finally:
        for cmd in (teardown or []):
            logs.append("[teardown]")
            if not _run_one(cmd, cwd, timeout_s, logs):
                logs.append("[teardown failure ignored]")
    # Built AFTER the finally block so teardown output is always in the transcript.
    return passed, "\n".join(logs)
