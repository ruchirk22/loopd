#!/usr/bin/env python3
"""Entrypoint for the self-hosted PM + Developer agentic loop (PM-Sovereign design).

    python run.py "Add a /health endpoint with a passing test" --repo ../my-service
    python run.py --brief spec.md --repo ../my-service          # curated handover brief
    python run.py --seed-session <uuid> --repo ../my-service    # fork a live CC session
    python run.py --resume-run --repo ../my-service             # continue an interrupted run

Exit codes: 0 verified done · 1 aborted (see .agentic/escalation.json)
            2 setup/plan failure · 3 budget exceeded (resumable with --resume-run)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from orchestrator.config import Config
from orchestrator.ledger import StateConflict
from orchestrator.loop import run


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Self-hosted PM+Developer agentic loop on Claude Code (headless).")
    ap.add_argument("task", nargs="?", default=None,
                    help="Task description, or @path to read it from a file. "
                         "Optional when --brief / --seed-session / --resume-run is given.")
    ap.add_argument("--repo", required=True, help="Path to the repo the developer agent works in.")
    ap.add_argument("--brief", default=None, help="Path to a handover brief (e.g. written by /handoff).")
    ap.add_argument("--seed-session", default=None, metavar="SESSION_ID",
                    help="Fork this interactive Claude Code session (opened in --repo) into a brief.")
    ap.add_argument("--resume-run", action="store_true",
                    help="Continue the interrupted run recorded in <repo>/.agentic/state.json.")
    ap.add_argument("--fresh", action="store_true",
                    help="Archive any previous run state and start over.")
    ap.add_argument("--budget", type=float, default=None, help="Override BUDGET_USD for this run.")
    ap.add_argument("--final-verify", action="append", default=[], metavar="CMD",
                    help="Extra command required in the final whole-task verification (repeatable).")
    args = ap.parse_args()

    task = args.task
    if task and task.startswith("@"):
        task = Path(task[1:]).read_text()

    cfg = Config(
        repo=Path(args.repo),
        brief_path=Path(args.brief) if args.brief else None,
        seed_session=args.seed_session,
        final_verify_extra=list(args.final_verify),
    )
    if args.budget is not None:
        cfg.budget_usd = args.budget

    try:
        code = run(task, cfg, resume=args.resume_run, fresh=args.fresh)
    except StateConflict as exc:
        print(f"\n{exc}", file=sys.stderr)
        code = 2
    except KeyboardInterrupt:
        print("\nInterrupted. State is saved — continue with --resume-run.", file=sys.stderr)
        code = 1
    except Exception as exc:  # clean exit instead of a raw traceback
        print(f"\nAborted: {exc}", file=sys.stderr)
        code = 2
    sys.exit(code)


if __name__ == "__main__":
    main()
