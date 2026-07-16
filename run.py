#!/usr/bin/env python3
"""Entrypoint for the self-hosted PM + Developer agentic loop.

    python run.py "Add a /health endpoint returning {status:ok} with a passing test" --repo ../my-service
    python run.py @spec.md --repo ../my-service        # read the task from a file
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from orchestrator.config import Config
from orchestrator.loop import run


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Self-hosted PM+Developer agentic loop on Claude Code (headless)."
    )
    ap.add_argument("task", help="Task/feature description, or @path to read it from a file.")
    ap.add_argument("--repo", required=True, help="Path to the repo the developer agent works in.")
    args = ap.parse_args()

    task = args.task
    if task.startswith("@"):
        task = Path(task[1:]).read_text()

    cfg = Config(repo=Path(args.repo))
    try:
        code = run(task, cfg)
    except Exception as exc:  # clean exit instead of a raw traceback
        print(f"\nAborted: {exc}", file=sys.stderr)
        code = 2
    sys.exit(code)


if __name__ == "__main__":
    main()
