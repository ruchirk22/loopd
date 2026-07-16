"""The control plane. The PM decides WHAT happens (it plans, authors every developer
prompt, reviews every handover); this loop decides what is ALLOWED to happen:

  - gates are run here, never by an agent, and an empty gate list fails;
  - `accept` is only offered when gates are green, and is refused without a real
    diff and verifiable per-criterion evidence;
  - `task_complete` triggers final verification + a regression sweep of every
    accepted step in a PRISTINE worktree before exit 0;
  - attempts / rejections / replans / budget / wall-clock caps are enforced after
    every single CLI call, and every state transition is persisted for resume.

  plan -> [dispatch -> dev<->gates (inner retries) -> handover -> PM review]* -> finalize
"""
from __future__ import annotations

import time
from typing import List, Optional, Tuple

from . import developer, gates
from .config import Config
from .handover import Handover, build_handover
from .ledger import BudgetExceeded, Ledger, NoChangesError
from .plan import (DONE, IN_PROGRESS, SKIPPED, Plan, PlanValidationError, Step,
                   apply_mutations)
from .pm import PMSession, PMTurnError, validate_directive
from . import seed


class RunAborted(RuntimeError):
    def __init__(self, code: int, reason: str):
        self.code = code
        super().__init__(reason)


def run(task: Optional[str], cfg: Config, resume: bool = False, fresh: bool = False) -> int:
    ledger = Ledger.load_or_start(cfg, resume=resume, fresh=fresh)
    try:
        return _run(task, cfg, ledger, resume)
    except BudgetExceeded as exc:
        plan = ledger.load_plan()
        path = ledger.write_escalation("budget_exceeded", plan, detail=str(exc))
        print("\n" + ledger.report(plan))
        print(f"\n{exc}\nEscalation report: {path}")
        return 3
    except PMTurnError as exc:
        plan = ledger.load_plan()
        path = ledger.write_escalation("pm_turn_failed", plan, detail=str(exc))
        print("\n" + ledger.report(plan))
        print(f"\nStopping: {exc}\nEscalation report: {path}")
        return 1
    except RunAborted as exc:
        return exc.code


def _run(task: Optional[str], cfg: Config, ledger: Ledger, resume: bool) -> int:
    t0 = time.time()
    if not resume:
        ledger.start(task or "(from brief)")
    brief = seed.ensure_brief(cfg, ledger, task)
    pm = PMSession(cfg, ledger, brief)

    plan = ledger.load_plan()
    if plan is None or not plan.steps:
        print("Planning…")
        plan = _plan_phase(pm, ledger, cfg)
        print(f"Plan: {plan.summary or '(no summary)'} — {len(plan.steps)} step(s), "
              f"cost so far ${ledger.state['total_cost_usd']:.4f}\n")
    else:
        print(f"Resuming: {plan.digest()}\n")

    while True:
        if cfg.max_wall_clock_min and (time.time() - t0) > cfg.max_wall_clock_min * 60:
            path = ledger.write_escalation("wall_clock_exceeded", plan)
            print("\n" + ledger.report(plan))
            print(f"\nWall-clock cap ({cfg.max_wall_clock_min} min) reached. "
                  f"Re-run with --resume-run to continue. Escalation report: {path}")
            return 1

        step = plan.next_pending()
        if step is None:
            rc, plan = _finalize_phase(pm, plan, ledger, cfg)
            if rc is not None:
                return rc
            continue

        plan = _step_phase(pm, step, plan, ledger, cfg)

        if ledger.needs_checkpoint():
            _checkpoint(pm, plan, ledger)


# --------------------------------------------------------------- phases

def _plan_phase(pm: PMSession, ledger: Ledger, cfg: Config) -> Plan:
    empty = Plan()
    allowed = ["plan", "abort"]
    d = pm.plan_turn(empty)
    for attempt in (1, 2):
        problems = validate_directive(d, allowed, None)
        if not problems:
            if d["verdict"] == "abort":
                _abort(ledger, None, d)
            try:
                new_plan = apply_mutations(empty, d.get("plan_mutations") or [])
                if not new_plan.summary:
                    new_plan.summary = str(d.get("reasoning", ""))[:200]
                ledger.save_plan(new_plan)
                return new_plan
            except PlanValidationError as e:
                problems = e.problems
        if attempt == 1:
            ledger.log({"event": "directive_refused", "label": "plan", "problems": problems})
            d = pm.corrective_turn(problems, allowed, empty)
        else:
            ledger.write_escalation("invalid_plan", None, detail="; ".join(problems))
            print("PM could not produce a valid plan:\n  - " + "\n  - ".join(problems))
            raise RunAborted(2, "invalid plan")


def _step_phase(pm: PMSession, step: Step, plan: Plan, ledger: Ledger, cfg: Config) -> Plan:
    print(f"→ Step {step.id}: {step.goal}")
    step.status = IN_PROGRESS
    ledger.save_plan(plan)

    d = pm.dispatch_turn(step, plan)
    d = _valid_directive(pm, d, ["dispatch", "replan", "abort"], plan, ledger, cfg, step)
    if d["verdict"] == "abort":
        _abort(ledger, plan, d, step.id)
    if d["verdict"] == "replan":
        return _apply_replan(pm, d, plan, ledger, cfg, step)

    original_prompt = d["next_prompt"]
    prompt = original_prompt
    resume_sid = step.dev_session_id if (d.get("dev_session") == "resume" and step.dev_session_id) else None

    while True:  # rejection cycles
        passed, gate_log, dev_res, dev_err = _inner_dev_loop(
            prompt, resume_sid, original_prompt, step, plan, ledger, cfg)

        ho = build_handover(step, dev_res, passed, gate_log, ledger, cfg, dev_error=dev_err)
        _save_handover(cfg, step, ho)
        ledger.note_review_turn(ho.bytes)

        if passed:
            allowed = ["accept", "reject", "replan", "descope", "abort"]
            if step.rejections >= cfg.max_rejections_per_step:
                allowed.remove("reject")
        else:
            # gates red after all inner retries: accepting is structurally impossible
            allowed = ["replan", "descope", "abort"]

        d = pm.review_turn(step, ho.text, allowed, plan)
        d = _valid_directive(pm, d, allowed, plan, ledger, cfg, step, ho.text)

        while True:  # resolve the verdict for THIS handover
            v = d["verdict"]
            if v == "accept":
                step.dev_summary = (ho.dev_summary or "")[:2000]
                try:
                    sha = ledger.commit_step(step, d.get("commit_message", ""))
                except NoChangesError as e:
                    allowed = [a for a in allowed if a != "accept"]
                    ledger.log({"event": "accept_refused_no_changes", "step": step.id})
                    d = pm.corrective_turn(
                        [f"{e} — `accept` is refused for a no-op; choose another action"],
                        allowed, plan, step)
                    d = _valid_directive(pm, d, allowed, plan, ledger, cfg, step, ho.text)
                    continue
                step.status = DONE
                ledger.save_plan(plan)
                print(f"   ✓ accepted, committed {sha[:9]}\n")
                return plan
            if v == "reject":
                step.rejections += 1
                ledger.save_plan(plan)
                ledger.log({"event": "step_rejected", "step": step.id,
                            "rejections": step.rejections})
                print(f"   ✗ PM rejected (feedback sent to developer, "
                      f"rejection {step.rejections}/{cfg.max_rejections_per_step})")
                prompt = d["next_prompt"]
                resume_sid = step.dev_session_id or None
                break  # back to the dev loop with the PM's feedback
            if v == "replan":
                return _apply_replan(pm, d, plan, ledger, cfg, step)
            if v == "descope":
                step.status = SKIPPED
                step.skip_reason = str(d.get("reasoning", ""))[:500]
                ledger.reset_to_head(f"descope step {step.id}")
                ledger.save_plan(plan)
                ledger.log({"event": "step_descoped", "step": step.id,
                            "reason": step.skip_reason[:300]})
                print(f"   ⤳ descoped: {step.skip_reason[:120]}\n")
                return plan
            if v == "abort":
                _abort(ledger, plan, d, step.id)


def _inner_dev_loop(prompt: str, resume_sid: Optional[str], original_prompt: str,
                    step: Step, plan: Plan, ledger: Ledger, cfg: Config):
    """dev <-> gates without spending PM turns: retry with the gate transcript until
    green or the attempt cap for this cycle is spent."""
    gate_log, dev_err = "", ""
    dev_res = None
    sid = resume_sid
    for _ in range(cfg.max_attempts_per_step):
        step.attempts += 1
        ledger.save_plan(plan)
        print(f"   dev attempt {step.attempts}…")
        res = developer.run_prompt(prompt, cfg, resume_session=sid)
        ledger.spend(res.cost_usd, step)
        dev_res = res

        if not res.ok:
            dev_err = res.text[:2000]
            ledger.log({"event": "dev_error", "step": step.id, "error": dev_err[:500]})
            print("   developer call errored")
            sid = res.session_id or sid
            context = (f"[previous attempt ended abnormally]\n{dev_err}\n\n"
                       f"[last verification transcript]\n{gate_log[-2000:]}")
            if sid:
                prompt = ("Your previous run on this step ended abnormally.\n\n" + context +
                          "\n\nInspect the repository's current state, then continue the step.")
            else:  # fresh session needs the full brief again — feedback is never dropped
                prompt = developer.error_retry_prompt(original_prompt, context)
            continue

        dev_err = ""
        sid = res.session_id or sid
        if sid:
            step.dev_session_id = sid
            ledger.save_plan(plan)

        passed, gate_log = gates.run_gates(step.verify, cfg.repo, timeout_s=cfg.gate_timeout_s,
                                           setup=step.setup, teardown=step.teardown)
        ledger.log({"event": "gates", "step": step.id, "passed": passed})
        print(f"   gates: {'PASS' if passed else 'fail'}")
        if passed:
            return True, gate_log, dev_res, ""
        prompt = developer.gate_feedback_prompt(gate_log[-cfg.gate_log_tail:])

    return False, (gate_log or dev_err), dev_res, dev_err


def _finalize_phase(pm: PMSession, plan: Plan, ledger: Ledger,
                    cfg: Config) -> Tuple[Optional[int], Plan]:
    allowed = ["task_complete", "replan", "abort"]
    d = pm.finalize_turn(plan, allowed)
    d = _valid_directive(pm, d, allowed, plan, ledger, cfg)
    if d["verdict"] == "abort":
        _abort(ledger, plan, d)
    if d["verdict"] == "replan":
        return None, _apply_replan(pm, d, plan, ledger, cfg)

    final_cmds = [str(c) for c in d["final_verify"] if str(c).strip()] + list(cfg.final_verify_extra)
    print("Final verification in a pristine worktree…")
    ok, transcript = _final_verification(final_cmds, plan, ledger, cfg)
    if ok:
        ledger.finish()
        print("\n" + ledger.report(plan))
        print("\nTask complete — final verification and regression sweep passed. ✅")
        return 0, plan

    print("   final verification FAILED")
    ledger.log({"event": "final_verify_failed"})
    d = pm.finalize_turn(plan, ["replan", "abort"], failure_transcript=transcript)
    d = _valid_directive(pm, d, ["replan", "abort"], plan, ledger, cfg)
    if d["verdict"] == "abort":
        _abort(ledger, plan, d)
    return None, _apply_replan(pm, d, plan, ledger, cfg)


def _final_verification(final_cmds: List[str], plan: Plan, ledger: Ledger,
                        cfg: Config) -> Tuple[bool, str]:
    with ledger.pristine_worktree() as wt:
        ok, transcript = gates.run_gates(final_cmds, wt, timeout_s=cfg.gate_timeout_s)
        if not ok:
            return False, "[final_verify]\n" + transcript
        parts = ["[final_verify]\n" + transcript]
        for s in plan.done_steps():
            ok2, log2 = gates.run_gates(s.verify, wt, timeout_s=cfg.gate_timeout_s,
                                        setup=s.setup, teardown=s.teardown)
            parts.append(f"[regression: step {s.id}]\n{log2}")
            if not ok2:
                return False, "\n\n".join(parts)
    return True, "\n\n".join(parts)


# --------------------------------------------------------------- helpers

def _valid_directive(pm: PMSession, d: dict, allowed: List[str], plan: Plan,
                     ledger: Ledger, cfg: Config, step: Optional[Step] = None,
                     handover_text: str = "") -> dict:
    problems = validate_directive(d, allowed, step, handover_text)
    if not problems:
        return d
    ledger.log({"event": "directive_refused", "problems": problems[:10]})
    d = pm.corrective_turn(problems, allowed, plan, step)
    problems = validate_directive(d, allowed, step, handover_text)
    if problems:
        detail = "; ".join(problems)
        ledger.write_escalation("invalid_directive", plan, detail=detail,
                                pm_reasoning=str(d.get("reasoning", "")),
                                step_id=step.id if step else "")
        print(f"\nStopping: PM repeatedly issued invalid directives: {detail}")
        raise RunAborted(1, "invalid directive")
    return d


def _apply_replan(pm: PMSession, d: dict, plan: Plan, ledger: Ledger, cfg: Config,
                  step: Optional[Step] = None) -> Plan:
    used = ledger.bump_replans()
    if used > cfg.max_replans:
        path = ledger.write_escalation("replan_cap_exhausted", plan,
                                       pm_reasoning=str(d.get("reasoning", "")),
                                       step_id=step.id if step else "")
        print("\n" + ledger.report(plan))
        print(f"\nStopping: replan cap ({cfg.max_replans}) exhausted. Escalation report: {path}")
        raise RunAborted(1, "replan cap exhausted")
    ledger.reset_to_head("replan")
    for attempt in (1, 2):
        try:
            new_plan = apply_mutations(plan, d.get("plan_mutations") or [])
            ledger.save_plan(new_plan)
            ledger.log({"event": "replanned", "replans_used": used})
            print(f"   ↻ plan updated by PM (replan {used}/{cfg.max_replans})\n")
            return new_plan
        except PlanValidationError as e:
            if attempt == 1:
                d = pm.corrective_turn(e.problems, ["replan", "abort"], plan, step)
                if d.get("verdict") == "abort":
                    _abort(ledger, plan, d, step.id if step else "")
            else:
                ledger.write_escalation("invalid_replan", plan, detail="; ".join(e.problems))
                print("PM could not produce valid plan mutations:\n  - " + "\n  - ".join(e.problems))
                raise RunAborted(1, "invalid replan")


def _checkpoint(pm: PMSession, plan: Plan, ledger: Ledger) -> None:
    print("   … PM context checkpoint (fresh PM session next turn)")
    ckpt = pm.checkpoint_turn(plan)
    ledger.save_checkpoint(ckpt)
    pm.reincarnate()


def _abort(ledger: Ledger, plan: Optional[Plan], d: dict, step_id: str = "") -> None:
    reason = str(d.get("reasoning", ""))[:2000]
    path = ledger.write_escalation("pm_abort", plan, pm_reasoning=reason, step_id=step_id)
    print("\n" + ledger.report(plan))
    print(f"\nPM aborted the run: {reason}\nEscalation report: {path}")
    raise RunAborted(1, "PM abort")


def _save_handover(cfg: Config, step: Step, ho: Handover) -> None:
    d = cfg.state_dir / "handovers"
    d.mkdir(exist_ok=True)
    (d / f"step-{step.id}-attempt-{step.attempts}.md").write_text(ho.text)
