#!/usr/bin/env python3
"""Entrypoint for the self-hosted PM + Developer agentic loop (PM-Sovereign design).

    python run.py "Add a /health endpoint with a passing test" --repo ../my-service
    python run.py --brief spec.md --repo ../my-service          # curated handover brief
    python run.py --seed-session <uuid> --repo ../my-service    # fork a live CC session
    python run.py --resume-run --repo ../my-service             # continue an interrupted run
    python run.py "Build OAuth" --repo ../svc --forecast-only   # just estimate cost/time, don't run

Before executing, loopd shows an Execution Forecast (estimated cost, runtime, steps, risk) and
— when the budget looks short — asks whether to raise it, proceed constrained, edit it, or abort.

Exit codes: 0 verified done · 1 aborted (see .agentic/escalation.json)
            2 setup/plan failure · 3 budget exceeded (resumable with --resume-run)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from orchestrator.config import Config
from orchestrator.env import load_dotenv
from orchestrator.ledger import StateConflict
from orchestrator.loop import run


def _forecast_only(task, cfg: Config, as_json: bool) -> int:
    """Produce and print an Execution Forecast without starting a run. Brief precedence mirrors
    a real run: an explicit --brief wins, then explicit task text, then a stored
    .agentic/brief.md. Does not start a run."""
    from orchestrator import forecast

    cfg.forecast_enabled = True  # --forecast-only implies you want the forecast
    brief = forecast.resolve_brief(cfg, task)
    if brief is None:
        print("Nothing to forecast: provide a task string, --brief <file>, or an existing "
              ".agentic/brief.md.", file=sys.stderr)
        return 2
    fc = forecast.run_forecast(cfg, brief, cfg.budget_usd, ledger=None)
    if fc is None:
        print("Forecast unavailable (the estimate call failed or forecasting is disabled).",
              file=sys.stderr)
        return 2
    print(json.dumps(fc.to_dict(), indent=2) if as_json else forecast.render_card(fc))
    return 0


def main() -> None:
    load_dotenv()  # pick up ANTHROPIC_API_KEY / model / budget overrides from .env
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
    # --- Execution Forecast ---
    ap.add_argument("--forecast-only", action="store_true",
                    help="Print the Execution Forecast (cost/runtime/steps) and exit without running.")
    ap.add_argument("--json", action="store_true",
                    help="With --forecast-only, print the forecast as JSON instead of a card.")
    ap.add_argument("--no-forecast", action="store_true",
                    help="Skip the pre-run forecast and start immediately.")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="Non-interactively accept the recommended budget if the estimate exceeds it.")
    ap.add_argument("--force", action="store_true",
                    help="Non-interactively proceed at the current budget (constrained if the estimate is short).")
    ap.add_argument("--constrained", action="store_true",
                    help="Force budget-constrained planning (prioritize critical work, defer polish).")
    # --- Failure Analysis (on --resume-run) ---
    ap.add_argument("--fix", action="store_true",
                    help="On resume, apply the recommended option from the failure analysis.")
    ap.add_argument("--option", default=None, metavar="ID",
                    help="On resume, apply a specific failure-analysis option by id.")
    args = ap.parse_args()

    try:
        task = args.task
        if task and task.startswith("@"):
            task = Path(task[1:]).read_text()  # a missing @file must exit 2, not traceback

        cfg = Config(
            repo=Path(args.repo),
            brief_path=Path(args.brief) if args.brief else None,
            seed_session=args.seed_session,
            final_verify_extra=list(args.final_verify),
            budget_explicit=args.budget is not None,
            no_forecast=args.no_forecast,
            assume_yes=args.yes,
            force=args.force,
            forecast_only=args.forecast_only,
            constrained=args.constrained,
        )
        if args.no_forecast:
            cfg.forecast_enabled = False
        if args.budget is not None:
            cfg.budget_usd = args.budget

        if args.forecast_only:
            code = _forecast_only(task, cfg, args.json)
        else:
            resume_choice = None
            if args.resume_run and (args.fix or args.option):
                from orchestrator import analysis
                resume_choice = analysis.resolve_choice(cfg.repo, option_id=args.option,
                                                        recommended=args.fix)
            code = run(task, cfg, resume=args.resume_run, fresh=args.fresh,
                       resume_choice=resume_choice)
    except StateConflict as exc:
        print(f"\n{exc}", file=sys.stderr)
        code = 2
    except KeyboardInterrupt:
        if args.forecast_only:
            print("\nForecast interrupted; nothing was run.", file=sys.stderr)
        else:
            print("\nInterrupted. State is saved — continue with --resume-run.", file=sys.stderr)
        code = 1
    except Exception as exc:  # clean exit instead of a raw traceback
        print(f"\nAborted: {exc}", file=sys.stderr)
        code = 2
    sys.exit(code)


if __name__ == "__main__":
    main()
