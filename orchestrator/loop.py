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

import sys
import time
from typing import List, Optional, Tuple

from . import analysis, developer, forecast, gates, memory, reporter
from .config import Config
from .handover import Handover, build_handover
from .ledger import BudgetExceeded, GitError, Ledger, NoChangesError
from .plan import (DONE, IN_PROGRESS, SKIPPED, Plan, PlanValidationError, Step,
                   apply_mutations)
from .pm import PMSession, PMTurnError, validate_directive, verify_evidence
from . import seed


class RunAborted(RuntimeError):
    def __init__(self, code: int, reason: str):
        self.code = code
        super().__init__(reason)


def run(task: Optional[str], cfg: Config, resume: bool = False, fresh: bool = False,
        on_start=None, resume_choice: Optional[dict] = None) -> int:
    # StateConflict (bad/missing/finished state, dirty tree w/o run branch) is a pre-loop
    # setup failure (exit 2, handled by run.py). A GitError while (re)establishing the repo
    # is resumable but has no ledger yet to escalate through — report and exit 1.
    # `on_start` (optional callable) fires once the run begins building, after the forecast
    # decision — the CLI uses it for its "I've got it from here" delegation reassurance.
    try:
        ledger = Ledger.load_or_start(cfg, resume=resume, fresh=fresh)
    except GitError as exc:
        print(f"\nStopping on a git error during setup: {exc}\n"
              "Fix the repo state, then retry (--resume-run if a run was in progress).")
        return 1
    session_t0 = time.time()  # active time of THIS invocation (excludes idle between resumes)
    rep = reporter.active()
    rep.attach(session_t0, lambda: ledger.state.get("total_cost_usd", 0.0))
    code, detail = 1, ""
    try:
        code = _run(task, cfg, ledger, resume, on_start=on_start, resume_choice=resume_choice)
    except BudgetExceeded as exc:
        code, detail = 3, str(exc)
        ledger.write_escalation("budget_exceeded", ledger.load_plan(), detail=detail)
        rep.block(f"\n{exc}")
    except PMTurnError as exc:
        # A PM failure before any plan exists is a setup/plan failure (exit 2), not a
        # resumable mid-run abort.
        plan = ledger.load_plan()
        code, detail = (2 if (plan is None or not plan.steps) else 1), str(exc)
        ledger.write_escalation("pm_turn_failed", plan, detail=detail)
        rep.block(f"\nStopping: {exc}")
    except RunAborted as exc:
        code, detail = exc.code, str(exc)  # escalation + summary already emitted upstream
    except GitError as exc:
        # A mid-run git failure (index.lock, disk full, worktree add) is resumable.
        code, detail = 1, str(exc)
        ledger.write_escalation("git_error", ledger.load_plan(), detail=detail)
        rep.block(f"\nStopping on a git error: {exc}\n"
                  "State is saved — fix the repo and continue with --resume-run.")
    except Exception as exc:  # never let an unexpected error escape as a bare traceback
        code, detail = 1, f"{type(exc).__name__}: {exc}"
        ledger.write_escalation("unexpected_error", ledger.load_plan(), detail=detail)
        rep.block(f"\nStopping on an unexpected error ({type(exc).__name__}): {exc}\n"
                  "State is saved — continue with --resume-run.")
    finally:
        rep.finish()  # drop the live status line before any end-of-run output
        # Accumulate this session's active runtime so a resumed run's actuals exclude the
        # idle wall-clock between sessions (state['started'] alone would include it).
        try:
            ledger.state["active_runtime_s"] = (ledger.state.get("active_runtime_s", 0.0)
                                                + (time.time() - session_t0))
            ledger._save()
        except Exception:
            pass
        # An end-of-run report on EVERY terminal outcome (success or failure).
        try:
            path = ledger.write_report(ledger.load_plan(), code, detail)
            print(f"\nRun report: {path}")
        except Exception:  # a report must never mask the real exit code
            pass
        # Grade the forecast: attach actuals, append to history, show predicted-vs-actual.
        # Runs on every terminal outcome (budget-exceeded is exactly where a miss matters).
        try:
            graded = ledger.record_forecast_actuals(ledger.load_plan(), code)
            if graded:
                predicted, actual = graded
                print(forecast.render_comparison(predicted, actual))
        except Exception:  # grading must never mask the real exit code
            pass
        # If the run stopped unfinished and the PM didn't already diagnose it (budget/time
        # stop, crash, replan-cap), leave a basic deterministic explanation so the CLI and
        # dashboard 'needs you' state always have something grounded to show.
        try:
            if code != 0 and analysis.load(cfg.repo) is None:
                esc = _read_escalation(cfg)
                fa = analysis.fallback(esc.get("reason", ""), esc.get("step", ""),
                                       esc.get("detail") or esc.get("pm_reasoning", ""))
                if fa:
                    ledger.write_analysis(fa, source="fallback")
        except Exception:
            pass
        # Record a durable failure note in project memory only for genuine mid-run aborts —
        # skip success (0), setup/plan (2), budget (3), and operational stops like the
        # wall-clock cap. Success-path memory is written by the PM via task_complete.
        operational = ledger.state.get("_operational_stop", False)
        if code not in (0, 2, 3) and not operational and cfg.update_memory:
            try:
                task = (ledger.state.get("task") or "").strip().splitlines()
                task = task[0][:80] if task else "task"
                # Prefer the grounded root cause from failure analysis, if we have one.
                note = ledger.state.get("_failure_note")
                reason = (note or (detail.strip().splitlines()[0] if detail else "run stopped"))[:160]
                memory.merge(cfg.repo, {memory.FAILURES: [f"{task}: {reason}"]})
            except Exception:
                pass
    return code


def _read_escalation(cfg: Config) -> dict:
    import json as _json
    p = cfg.state_dir / "escalation.json"
    if not p.is_file():
        return {}
    try:
        return _json.loads(p.read_text())
    except (OSError, _json.JSONDecodeError):
        return {}


def _run(task: Optional[str], cfg: Config, ledger: Ledger, resume: bool,
         on_start=None, resume_choice: Optional[dict] = None) -> int:
    t0 = time.time()
    if not resume:
        ledger.start(task or "(from brief)")
    brief = seed.ensure_brief(cfg, ledger, task, resume=resume)

    if not resume and ledger.load_plan() is None:
        _forecast_phase(cfg, ledger, brief)   # may raise RunAborted if the user declines
    elif resume:
        _restore_constrained(cfg, ledger)     # re-forecasting is skipped; honor the prior choice

    # Apply the owner's choice from Failure Analysis (they picked one option; we never auto-act):
    # a descope skips the stuck step now; a "loopd_fix" becomes guidance the planner follows.
    resume_guidance = _apply_resume_choice(ledger, resume, resume_choice)
    ledger.clear_analysis()  # the blocker has been acted on — clear the 'needs you' state

    # The decision is made; real work is about to begin. This is where the CLI says
    # "I've got it from here" — it must land after any negotiation, not before.
    if on_start:
        on_start()

    pm = PMSession(cfg, ledger, brief)
    if resume_guidance:
        pm.resume_guidance = resume_guidance      # the planner sees the owner's chosen approach
        pm.session_id = None                      # reseed so the decision reaches it
        ledger.set_pm_session(None)

    rep = reporter.active()
    plan = ledger.load_plan()
    if plan is None or not plan.steps:
        rep.planning()
        plan = _plan_phase(pm, ledger, cfg)
        rep.planned(plan, ledger.state["total_cost_usd"])
    else:
        rep.resuming(plan.digest())

    while True:
        if cfg.max_wall_clock_min and (time.time() - t0) > cfg.max_wall_clock_min * 60:
            ledger.state["_operational_stop"] = True  # resumable/operational — keep out of memory
            path = ledger.write_escalation("wall_clock_exceeded", plan)
            rep.finish()
            rep.block("\n" + ledger.report(plan))
            rep.block(f"\nWall-clock cap ({cfg.max_wall_clock_min} min) reached. "
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


# --------------------------------------------------------- execution forecast

def _forecast_phase(cfg: Config, ledger: Ledger, brief: str) -> None:
    """Estimate the run BEFORE planning, show the forecast card, and let the user decide
    whether to raise the budget, proceed constrained, edit it, or abort. Mutates cfg.budget_usd
    / ledger.state['budget_usd'] / cfg.constrained per the decision and persists the forecast."""
    requested_constrained = bool(cfg.constrained)  # an explicit --constrained must never be lost
    try:
        fc = forecast.run_forecast(cfg, brief, cfg.budget_usd, ledger=ledger)
    except BudgetExceeded:
        # The (cheap) estimate call itself crossed an already-tiny budget. Skip the forecast
        # and let planning hit the real budget wall — don't stop before showing anything.
        ledger.log({"event": "forecast_skipped", "reason": "budget"})
        return
    if fc is None:
        return  # forecasting disabled or the estimate failed — proceed exactly as before
    print(forecast.render_card(fc))

    choice, edited = _forecast_choice(cfg, fc)
    dec = forecast.apply_choice(fc, choice, edited)
    if dec.action == "abort":
        # A cost decision, not an engineering failure: mark it operational so the finally
        # block doesn't launder it into project memory, and leave a real escalation so the
        # report's references resolve.
        ledger.state["_operational_stop"] = True
        ledger.log({"event": "forecast_aborted"})
        ledger.write_escalation("forecast_declined", None,
                                detail="User declined the run at the execution forecast.")
        raise RunAborted(1, "aborted at the execution forecast")

    # The forecast can only ADD constraint; an explicit --constrained is never downgraded.
    constrained = dec.constrained or requested_constrained
    # Apply to BOTH the live rail (cfg.budget_usd, what spend() checks) and the persisted
    # state (carried forward on --resume-run). Setting only one silently diverges.
    cfg.budget_usd = dec.budget_usd
    ledger.state["budget_usd"] = dec.budget_usd
    cfg.constrained = constrained
    fc_dict = fc.to_dict()
    fc_dict["constrained"] = constrained
    fc_dict["chosen_budget_usd"] = dec.budget_usd
    ledger.save_forecast(fc_dict)
    ledger.log({"event": "forecast", "action": dec.action,
                "predicted_cost": fc.estimated_cost_usd, "budget": dec.budget_usd,
                "constrained": constrained})
    if constrained:
        print(forecast.CONSTRAINED_WARNING)


def _forecast_choice(cfg: Config, fc: "forecast.Forecast"):
    """Resolve the forecast decision to (choice, edited_budget). Flags win; otherwise prompt
    on a TTY; otherwise proceed at the current budget (never auto-spend, never block CI)."""
    if not fc.constrained:
        return "continue", None                      # budget already covers it — just proceed
    if cfg.assume_yes:
        return "raise", None
    if cfg.force:
        return "continue", None
    if not sys.stdin.isatty():
        print("  (non-interactive: proceeding at the current budget in constrained mode — "
              "pass --yes to raise it to the recommendation, or --budget to set your own.)")
        return "continue", None
    return _prompt_forecast(fc)


def _prompt_forecast(fc: "forecast.Forecast"):
    while True:
        print(f"\n  Increase budget to ${fc.recommended_budget_usd:,.2f}?   "
              "[Y] raise  ·  [C] continue anyway  ·  [E] edit budget  ·  [A] abort")
        try:
            ans = input("  > ").strip().lower()
        except EOFError:
            return "continue", None
        if ans in ("", "y", "yes"):
            return "raise", None
        if ans in ("c", "continue"):
            return "continue", None
        if ans in ("a", "abort", "q"):
            return "abort", None
        if ans in ("e", "edit"):
            try:
                return "edit", float(input("  new budget $ ").strip())
            except (ValueError, EOFError):
                print("  (not a number)")
                continue
        print("  (choose Y, C, E, or A)")


def _restore_constrained(cfg: Config, ledger: Ledger) -> None:
    """On resume we do not re-forecast; honor the constrained choice made on the first run."""
    fc = ledger.state.get("forecast")
    if isinstance(fc, dict) and fc.get("constrained"):
        cfg.constrained = True


def _apply_resume_choice(ledger: Ledger, resume: bool, choice: Optional[dict]) -> Optional[str]:
    """Act on the option the owner picked in Failure Analysis. Returns planner guidance for a
    'loopd_fix', or None. A 'descope' skips the stuck step right here; other kinds just resume."""
    if not (resume and choice):
        return None
    kind = choice.get("kind")
    if kind == "descope":
        plan = ledger.load_plan()
        step = next((s for s in (plan.steps if plan else []) if s.id == choice.get("step")), None)
        if step:
            step.status = SKIPPED
            step.skip_reason = ("owner chose to skip: " + str(choice.get("label", "")))[:500]
            ledger.reset_to_head(f"skip step {step.id} (owner)")
            ledger.save_plan(plan)
            ledger.log({"event": "step_descoped", "step": step.id, "reason": "owner_choice"})
        return None
    if kind == "loopd_fix":
        return str(choice.get("guidance") or "").strip() or None
    return None  # user_action / abort → a plain resume


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
            reporter.active().block("Planner could not produce a valid plan:\n  - "
                                    + "\n  - ".join(problems))
            raise RunAborted(2, "invalid plan")


def _step_phase(pm: PMSession, step: Step, plan: Plan, ledger: Ledger, cfg: Config) -> Plan:
    try:
        idx = plan.steps.index(step) + 1
    except ValueError:
        idx = len([s for s in plan.steps if s.status == DONE]) + 1
    reporter.active().step_start(step, idx, len(plan.steps))
    step.status = IN_PROGRESS
    # Baseline for detecting out-of-band commits. On a resume mid-crash-window, keep the
    # marker's ORIGINAL base (the current HEAD already includes the crash commit, which would
    # otherwise blind adoption and revert).
    marker = ledger.state.get("pending_commit")
    if marker and marker.get("step_id") == step.id and marker.get("base_sha"):
        step.base_sha = marker["base_sha"]
    else:
        step.base_sha = ledger.head_sha()
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

        d = pm.review_turn(step, ho.text, allowed, plan, high_risk=ho.high_risk)
        d = _valid_directive(pm, d, allowed, plan, ledger, cfg, step, ho.evidence_corpus,
                             require_integrity_ack=ho.high_risk)

        while True:  # resolve the verdict for THIS handover
            v = d["verdict"]
            if v == "accept":
                # On a clean (non-high-risk) accept, evidence wasn't hard-enforced; still
                # record any weak citation to the audit trail without blocking the run —
                # the green gate already verified the work.
                if not ho.high_risk:
                    ev = verify_evidence(d, step, ho.evidence_corpus)
                    if ev:
                        ledger.log({"event": "weak_evidence", "step": step.id, "notes": ev[:5]})
                step.dev_summary = (ho.dev_summary or "")[:2000]
                try:
                    sha = ledger.commit_step(step, d.get("commit_message", ""))
                except NoChangesError as e:
                    # Crash-window reconciliation: if this step's work is ALREADY in HEAD
                    # (accepted+committed just before a crash, now re-run and no-op), adopt
                    # that commit instead of forcing a futile reject/descope.
                    adopted = ledger.adopt_head_if_matches(step)
                    if adopted:
                        step.status = DONE
                        ledger.save_plan(plan)          # durably records the adoption...
                        ledger.clear_pending_commit()   # ...then the marker is safe to drop
                        reporter.active().accepted(adopted, adopted=True)
                        return plan
                    allowed = [a for a in allowed if a != "accept"]
                    ledger.log({"event": "accept_refused_no_changes", "step": step.id})
                    d = pm.corrective_turn(
                        [f"{e} — `accept` is refused for a no-op; choose another action"],
                        allowed, plan, step)
                    d = _valid_directive(pm, d, allowed, plan, ledger, cfg, step,
                                         ho.evidence_corpus, require_integrity_ack=ho.high_risk)
                    continue
                step.status = DONE
                step.criteria_evidence = [e for e in (d.get("criteria_evidence") or [])
                                          if isinstance(e, dict)]  # for the coverage report
                ledger.save_plan(plan)      # durably records the commit...
                ledger.clear_pending_commit()  # ...only now is the crash-window marker safe to drop
                reporter.active().accepted(sha)
                return plan
            if v == "reject":
                step.rejections += 1
                ledger.save_plan(plan)
                ledger.log({"event": "step_rejected", "step": step.id,
                            "rejections": step.rejections})
                reporter.active().rejected(step.rejections, cfg.max_rejections_per_step)
                prompt = d["next_prompt"]
                # Honor the PM's choice of a fresh developer session (e.g. the current one
                # is contaminated); clear the stored session so later cycles can't fall back
                # to the discarded one. Default is to resume the same session.
                if d.get("dev_session") == "fresh":
                    resume_sid = None
                    step.dev_session_id = ""
                    ledger.save_plan(plan)
                else:
                    resume_sid = step.dev_session_id or None
                break  # back to the dev loop with the PM's feedback
            if v == "replan":
                return _apply_replan(pm, d, plan, ledger, cfg, step)
            if v == "descope":
                step.status = SKIPPED
                step.skip_reason = str(d.get("reasoning", ""))[:500]
                ledger.revert_unclaimed_commits(step, plan, f"descope step {step.id}")
                ledger.reset_to_head(f"descope step {step.id}")
                ledger.save_plan(plan)
                ledger.log({"event": "step_descoped", "step": step.id,
                            "reason": step.skip_reason[:300]})
                reporter.active().descoped(step.skip_reason)
                return plan
            if v == "abort":
                _abort(ledger, plan, d, step.id)


def _inner_dev_loop(prompt: str, resume_sid: Optional[str], original_prompt: str,
                    step: Step, plan: Plan, ledger: Ledger, cfg: Config):
    """dev <-> gates without spending PM turns: retry with the gate transcript until
    green or the attempt cap for this cycle is spent."""
    cycle_prompt = prompt  # this cycle's starting instructions (dispatch, or reject feedback)
    gate_log, dev_err = "", ""
    dev_res = None
    sid = resume_sid
    rep = reporter.active()
    for _ in range(cfg.max_attempts_per_step):
        step.attempts += 1
        ledger.save_plan(plan)
        rep.attempt(step.attempts)
        res = developer.run_prompt(prompt, cfg, resume_session=sid,
                                   timeout_cost_usd=ledger.timeout_cost())
        # Persist the session id BEFORE charging (for both ok and error), so a budget stop
        # resumes the right dev session rather than a stale one.
        if res.session_id:
            sid = res.session_id
            step.dev_session_id = sid
            ledger.save_plan(plan)
        ledger.spend(res.cost_usd, step)
        dev_res = res

        if not res.ok:
            dev_err = res.text[:2000]
            ledger.log({"event": "dev_error", "step": step.id, "error": dev_err[:500]})
            rep.dev_errored()
            context = (f"[previous attempt ended abnormally]\n{dev_err}\n\n"
                       f"[last verification transcript]\n{gate_log[-2000:]}")
            if sid:
                prompt = ("Your previous run on this step ended abnormally.\n\n" + context +
                          "\n\nInspect the repository's current state, then continue the step.")
            else:  # fresh session: re-send THIS cycle's instructions + context (never dropped)
                prompt = developer.error_retry_prompt(cycle_prompt, context)
            continue

        dev_err = ""
        passed, gate_log = gates.run_gates(step.verify, cfg.repo, timeout_s=cfg.gate_timeout_s,
                                           setup=step.setup, teardown=step.teardown)
        ledger.log({"event": "gates", "step": step.id, "passed": passed})
        rep.gate(passed)
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
        return None, _apply_replan(pm, d, plan, ledger, cfg, require_pending=True)

    final_cmds = [str(c) for c in d["final_verify"] if str(c).strip()] + list(cfg.final_verify_extra)
    rep = reporter.active()
    rep.finalizing()
    ok, transcript = _final_verification(final_cmds, plan, ledger, cfg)
    if ok:
        if cfg.update_memory and d.get("memory"):
            memory.merge(cfg.repo, memory.from_directive_memory(d["memory"]))
            ledger.log({"event": "memory_updated"})
        ledger.finish()
        rep.completed(reporter.render_completion(plan, ledger, cfg))
        return 0, plan

    rep.final_failed()
    ledger.log({"event": "final_verify_failed"})
    d = pm.finalize_turn(plan, ["replan", "abort"], failure_transcript=transcript)
    d = _valid_directive(pm, d, ["replan", "abort"], plan, ledger, cfg)
    if d["verdict"] == "abort":
        _abort(ledger, plan, d)
    return None, _apply_replan(pm, d, plan, ledger, cfg, require_pending=True)


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
                     handover_text: str = "", require_integrity_ack: bool = False) -> dict:
    problems = validate_directive(d, allowed, step, handover_text, require_integrity_ack)
    if not problems:
        return d
    ledger.log({"event": "directive_refused", "problems": problems[:10]})
    d = pm.corrective_turn(problems, allowed, plan, step)
    problems = validate_directive(d, allowed, step, handover_text, require_integrity_ack)
    if problems:
        detail = "; ".join(problems)
        ledger.write_escalation("invalid_directive", plan, detail=detail,
                                pm_reasoning=str(d.get("reasoning", "")),
                                step_id=step.id if step else "")
        reporter.active().block(f"\nStopping: the planner repeatedly issued invalid directives: {detail}")
        raise RunAborted(1, "invalid directive")
    return d


def _apply_replan(pm: PMSession, d: dict, plan: Plan, ledger: Ledger, cfg: Config,
                  step: Optional[Step] = None, require_pending: bool = False) -> Plan:
    used = ledger.bump_replans()
    if used > cfg.max_replans:
        path = ledger.write_escalation("replan_cap_exhausted", plan,
                                       pm_reasoning=str(d.get("reasoning", "")),
                                       step_id=step.id if step else "")
        rep = reporter.active()
        rep.finish()
        rep.block("\n" + ledger.report(plan))
        rep.block(f"\nStopping: replan cap ({cfg.max_replans}) exhausted. Escalation report: {path}")
        raise RunAborted(1, "replan cap exhausted")
    if step is not None:
        ledger.revert_unclaimed_commits(step, plan, "replan")
    ledger.reset_to_head("replan")
    for attempt in (1, 2):
        problems: List[str] = []
        if d.get("verdict") != "replan" or not d.get("plan_mutations"):
            problems = ["replan requires non-empty plan_mutations"]
        else:
            try:
                new_plan = apply_mutations(plan, d.get("plan_mutations") or [])
                if require_pending and new_plan.next_pending() is None:
                    problems = ["a replan at finalize must add or reopen at least one step "
                                "to work on — these mutations leave nothing pending"]
                else:
                    ledger.save_plan(new_plan)
                    ledger.log({"event": "replanned", "replans_used": used})
                    reporter.active().replanned(used, cfg.max_replans)
                    return new_plan
            except PlanValidationError as e:
                problems = e.problems
        if attempt == 1:
            d = pm.corrective_turn(problems, ["replan", "abort"], plan, step)
            if d.get("verdict") == "abort":
                _abort(ledger, plan, d, step.id if step else "")
        else:
            ledger.write_escalation("invalid_replan", plan, detail="; ".join(problems))
            reporter.active().block("Planner could not produce valid plan mutations:\n  - "
                                    + "\n  - ".join(problems))
            raise RunAborted(1, "invalid replan")


def _checkpoint(pm: PMSession, plan: Plan, ledger: Ledger) -> None:
    reporter.active().checkpoint()
    try:
        ckpt = pm.checkpoint_turn(plan)
    except PMTurnError:
        # The live session is gone; a checkpoint from a blank session would fabricate.
        # Keep the existing checkpoint and current session rather than laundering memory.
        ledger.log({"event": "checkpoint_skipped_degraded"})
        reporter.active().checkpoint(skipped=True)
        return
    ledger.save_checkpoint(ckpt)
    pm.reincarnate()


def _abort(ledger: Ledger, plan: Optional[Plan], d: dict, step_id: str = "") -> None:
    reason = str(d.get("reasoning", ""))[:2000]
    ledger.write_escalation("pm_abort", plan, pm_reasoning=reason, step_id=step_id)
    # Explain the blocker like a senior engineer: prefer the PM's grounded failure_analysis;
    # if it didn't provide one, synthesize a minimal analysis from its reasoning.
    fa_raw = d.get("failure_analysis")
    if isinstance(fa_raw, dict) and fa_raw.get("options"):
        fa = analysis.FailureAnalysis.from_dict({**fa_raw, "step": step_id, "reason": "pm_abort"})
        source = "pm"
    else:
        fa = analysis.FailureAnalysis.from_dict({
            "summary": "I've stopped — I can't finish this from here.",
            "root_cause": reason or "See the reasoning above.", "category": "unknown",
            "confidence": 40, "step": step_id, "reason": "pm_abort",
            "options": [{"label": "Let me try again", "kind": "user_action", "recommended": True,
                         "detail": "Resume and re-attempt."},
                        {"label": "Stop here", "kind": "abort"}]})
        source = "fallback"
    ledger.write_analysis(fa, source=source)
    ledger.state["_failure_note"] = f"{fa.summary} {fa.root_cause}"[:180]  # richer memory note
    rep = reporter.active()
    rep.finish()
    rep.block("\n" + ledger.report(plan))
    rep.block(analysis.render(fa))
    raise RunAborted(1, "PM abort")


def _save_handover(cfg: Config, step: Step, ho: Handover) -> None:
    d = cfg.state_dir / "handovers"
    d.mkdir(exist_ok=True)
    (d / f"step-{step.id}-attempt-{step.attempts}.md").write_text(ho.text)
