"""The control plane. This loop is the whole point of building your own: YOU decide
step order, when the developer retries, the budget ceiling, and when to stop.

  plan -> for each step: [dev -> gate] -> pass? commit : retry (<=N) -> else stop
"""
from __future__ import annotations

from . import developer, gates
from .config import Config
from .ledger import Ledger
from .planner import make_plan


class BudgetExceeded(RuntimeError):
    pass


def run(task: str, cfg: Config) -> int:
    ledger = Ledger(cfg)

    print("Planning…")
    summary, steps, plan_cost = make_plan(task, cfg)
    ledger.start(task, summary, steps)
    ledger.add_cost(plan_cost)
    print(f"Plan: {summary}")
    print(f"{len(steps)} step(s). Planning cost ${plan_cost:.4f}.\n")

    for step in steps:
        print(f"→ Step {step.id}: {step.goal}")
        session = None
        gate_log = ""
        passed = False

        for attempt in range(1, cfg.max_attempts_per_step + 1):
            ledger.update_step(step.id, status="in_progress", attempts=attempt)

            if session is None:
                res = developer.run_step(step, cfg)
            else:
                res = developer.resume_with_feedback(session, gate_log, cfg)
            session = res.session_id or session
            ledger.add_step_cost(step.id, res.cost_usd)

            if ledger.state["total_cost_usd"] > cfg.budget_usd:
                ledger.log({"event": "budget_exceeded", "total": ledger.state["total_cost_usd"]})
                print("\n" + ledger.report())
                raise BudgetExceeded(f"Budget ${cfg.budget_usd:.2f} exceeded.")

            if not res.ok:
                gate_log = f"[developer call errored]\n{res.text[:2000]}"
                print(f"   attempt {attempt}: developer error")
                ledger.log({"event": "dev_error", "step": step.id, "attempt": attempt})
                continue

            passed, gate_log = gates.run_gates(step.verify, cfg.repo)
            print(f"   attempt {attempt}: {'PASS' if passed else 'fail'}")
            if passed:
                ledger.commit_step(step)
                break
            ledger.log({"event": "gate_failed", "step": step.id,
                        "attempt": attempt, "log": gate_log[:2000]})

        if not passed:
            ledger.update_step(step.id, status="failed")
            ledger.log({"event": "step_failed", "step": step.id})
            print("\n" + ledger.report())
            print(f"\nStopping: step {step.id} did not pass after "
                  f"{cfg.max_attempts_per_step} attempt(s).")
            print("Last verification output:\n" + gate_log[-2000:])
            # Escalation hook: this is where you could call the PM to re-plan
            # the remaining work instead of stopping. Left as a hardening TODO.
            return 1

    print("\n" + ledger.report())
    print("\nAll steps complete. \u2705")
    return 0
