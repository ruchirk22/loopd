"""The PM agent: ONE persistent Opus session that plans, authors every developer
prompt verbatim, reviews every handover, and answers each turn with a schema-validated
DIRECTIVE. The verdict enum is built dynamically per turn — when gates are red,
`accept` simply is not in the schema, so rubber-stamping is structurally impossible.

Python (the loop) stays in charge of enforcement; this module owns the session,
the schemas, payload construction, and directive shape/evidence validation.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from .claude_cli import run_claude
from .config import Config
from .ledger import Ledger
from .plan import Plan, Step, is_trivial_command


class PMTurnError(RuntimeError):
    pass


# ---------------------------------------------------------------- schemas

_MUTATION_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": ["add", "update", "remove", "reorder", "set_summary"]},
            "step": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "goal": {"type": "string"},
                    "details": {"type": "string"},
                    "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                    "verify": {"type": "array", "items": {"type": "string"}},
                    "setup": {"type": "array", "items": {"type": "string"}},
                    "teardown": {"type": "array", "items": {"type": "string"}},
                },
            },
            "after_id": {"type": "string"},
            "order": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": "string"},
        },
        "required": ["op"],
    },
}

CHECKPOINT_SCHEMA = {
    "type": "object",
    "properties": {
        "mission_summary": {"type": "string"},
        "key_decisions": {"type": "array", "items": {"type": "string"}},
        "open_risks": {"type": "array", "items": {"type": "string"}},
        "remaining_plan_note": {"type": "string"},
        "advice_to_successor": {"type": "string"},
    },
    "required": ["mission_summary", "key_decisions", "open_risks",
                 "remaining_plan_note", "advice_to_successor"],
}


def directive_schema(verdicts: List[str]) -> dict:
    """Only fields relevant to the allowed verdicts appear; only verdicts the rules
    permit RIGHT NOW are in the enum."""
    props = {
        "verdict": {"type": "string", "enum": list(verdicts)},
        "reasoning": {"type": "string"},
    }
    if "dispatch" in verdicts or "reject" in verdicts:
        props["next_prompt"] = {"type": "string"}
        props["dev_session"] = {"type": "string", "enum": ["fresh", "resume"]}
    if "accept" in verdicts:
        props["commit_message"] = {"type": "string"}
        props["integrity_ack"] = {"type": "string",
                                  "description": "Required to accept when integrity flags are raised: "
                                                 "name each flag and cite the diff evidence that clears it."}
        props["criteria_evidence"] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "criterion": {"type": "string"},
                    "satisfied": {"type": "boolean"},
                    "evidence": {"type": "string"},
                },
                "required": ["criterion", "satisfied", "evidence"],
            },
        }
    if "plan" in verdicts or "replan" in verdicts:
        props["plan_mutations"] = _MUTATION_SCHEMA
    if "task_complete" in verdicts:
        props["final_verify"] = {"type": "array", "items": {"type": "string"}}
    return {"type": "object", "properties": props, "required": ["verdict", "reasoning"]}


# ---------------------------------------------------------------- validation

def _norm(s: str) -> str:
    return " ".join(s.split())


# Boilerplate that is present in EVERY green handover — quoting it proves nothing.
_BANNED_EVIDENCE = {"all gates passed", "gates failed", "gate verdict", "ground truth",
                    "ok", "done", "pass", "passed", "tests pass", "it works",
                    "looks good", "lgtm", "correct", "verified", "n/a"}
_MIN_QUOTE = 12
# Only these packet sections are real proof — not the gate-verdict banner or headings.
_EVIDENCE_SECTIONS = ("Developer's structured summary", "Diff vs last accepted commit",
                      "Gate transcript")


def _evidence_haystack(handover_text: str) -> str:
    """The parts of the packet that constitute proof: dev summary, diff, gate transcript
    — excluding the banner/headers a PM could quote without reading anything."""
    hay = handover_text
    for marker in _EVIDENCE_SECTIONS:
        idx = handover_text.find(marker)
        if idx != -1:
            return _norm(handover_text[idx:])
    return _norm(hay)


def _match_criterion(quoted: str, criteria: List[str]) -> Optional[int]:
    q = _norm(quoted).lower()
    for i, c in enumerate(criteria):
        if q and (q in _norm(c).lower() or _norm(c).lower() in q):
            return i
    return None


def verify_evidence(directive: dict, step: Step, handover_text: str) -> List[str]:
    """Accept requires, per acceptance criterion, an exact quote from the PROOF sections
    of the handover packet. Empty/short/boilerplate quotes, fabricated quotes, duplicate
    quotes, and unmatched criteria are all refused — this is the anti-rubber-stamp rail."""
    problems = []
    criteria = [c for c in step.acceptance_criteria if c.strip()]
    evidence = directive.get("criteria_evidence") or []
    hay = _evidence_haystack(handover_text)
    covered = set()
    seen_quotes = set()

    for i, e in enumerate(evidence):
        label = f"evidence entry {i + 1}"
        if not e.get("satisfied", False):
            problems.append(f"{label}: criterion marked unsatisfied — you cannot accept; reject or replan instead")
            continue
        quote = _norm(str(e.get("evidence", "")))
        if not quote:
            problems.append(f"{label}: empty evidence — quote the exact text from the diff or gate transcript that proves it")
            continue
        if quote.lower() in _BANNED_EVIDENCE:
            problems.append(f"{label}: {quote!r} is boilerplate, not proof — quote specific diff/output text")
            continue
        if len(quote) < _MIN_QUOTE:
            problems.append(f"{label}: evidence {quote!r} is too short to verify — quote at least {_MIN_QUOTE} characters verbatim from the packet")
            continue
        if quote in seen_quotes:
            problems.append(f"{label}: the same quote is reused for multiple criteria — cite distinct evidence per criterion")
            continue
        seen_quotes.add(quote)
        if quote not in hay:
            problems.append(f"{label}: not an exact quote from the handover's proof sections: {quote[:120]!r}")
            continue
        idx = _match_criterion(str(e.get("criterion", "")), criteria)
        if idx is None:
            problems.append(f"{label}: criterion {str(e.get('criterion',''))[:80]!r} does not match any of the step's acceptance criteria")
            continue
        covered.add(idx)

    missing = [criteria[i] for i in range(len(criteria)) if i not in covered]
    if missing:
        problems.append("no verified evidence for acceptance criteria: "
                        + "; ".join(m[:80] for m in missing))
    return problems


def validate_directive(directive: dict, allowed: List[str], step: Optional[Step],
                       handover_text: str = "", require_integrity_ack: bool = False) -> List[str]:
    problems = []
    verdict = directive.get("verdict", "")
    if verdict not in allowed:
        return [f"verdict {verdict!r} is not permitted here; allowed: {allowed}"]
    if not str(directive.get("reasoning", "")).strip():
        problems.append("reasoning is required")

    if verdict in ("dispatch", "reject") and not str(directive.get("next_prompt", "")).strip():
        problems.append(f"{verdict} requires a non-empty next_prompt (the developer's instructions, verbatim)")
    if verdict == "accept":
        if not str(directive.get("commit_message", "")).strip():
            problems.append("accept requires a commit_message")
        if require_integrity_ack and len(str(directive.get("integrity_ack", "")).strip()) < 40:
            problems.append("integrity flags were raised: accept requires a substantive integrity_ack "
                            "naming each flag and citing the diff evidence that clears it")
        if step is not None:
            problems += verify_evidence(directive, step, handover_text)
    if verdict in ("plan", "replan") and not directive.get("plan_mutations"):
        problems.append(f"{verdict} requires non-empty plan_mutations")
    if verdict == "task_complete":
        final = [c for c in (directive.get("final_verify") or []) if str(c).strip()]
        if not final:
            problems.append("task_complete requires non-empty final_verify commands")
        trivial = [c for c in final if is_trivial_command(str(c))]
        if trivial:
            problems.append(f"final_verify contains trivially-true command(s): {trivial!r}")
    return problems


# ---------------------------------------------------------------- the session

class PMSession:
    def __init__(self, cfg: Config, ledger: Ledger, brief: str) -> None:
        self.cfg = cfg
        self.ledger = ledger
        self.brief = brief
        self.system = cfg.prompt("pm_system.md")
        self.session_id: Optional[str] = ledger.state.get("pm_session_id")

    # ----- session plumbing -----

    def _seed_text(self, plan: Optional[Plan]) -> str:
        parts = [
            "You are joining (or re-joining) an in-progress automated build run as its PM. "
            "Everything you need is below; the orchestrator enforces the rails.",
            "\n## Task brief\n" + self.brief,
        ]
        ckpt = self.ledger.state.get("checkpoint")
        if ckpt:
            parts.append(
                "\n## Checkpoint from your predecessor PM session\n"
                f"Mission: {ckpt.get('mission_summary', '')}\n"
                "Key decisions:\n" + "\n".join(f"- {d}" for d in ckpt.get("key_decisions", [])) + "\n"
                "Open risks:\n" + "\n".join(f"- {r}" for r in ckpt.get("open_risks", [])) + "\n"
                f"Remaining plan: {ckpt.get('remaining_plan_note', '')}\n"
                f"Advice to you: {ckpt.get('advice_to_successor', '')}")
        if plan is not None and plan.steps:
            parts.append("\n## Current plan state (ledger digest — ground truth)\n" + plan.digest())
        return "\n".join(parts)

    def turn(self, payload: str, schema: dict, label: str,
             plan: Optional[Plan] = None, degradable: bool = True) -> dict:
        """One PM turn. Retry ladder adapts to the failure: a stale/invalid session goes
        straight to a fresh reincarnation (no pointless second resume); a timeout retries
        the same session once (the CLI may have persisted a partial answer) before
        reincarnating. `degradable=False` (checkpoints) forbids the fresh fallback, so a
        context-dependent turn never fabricates memory from a blank session.

        Raises PMTurnError after the ladder is exhausted."""
        last_err = ""
        attempts, max_attempts = 0, 4
        while attempts < max_attempts:
            attempts += 1
            resume = self.session_id
            if resume is None:
                if not degradable and self.ledger.state.get("pm_session_id") is not None:
                    # Non-degradable turn on a lost session: refuse rather than fabricate.
                    raise PMTurnError(f"PM turn {label!r} needs live session context that is unavailable")
                prompt = self._seed_text(plan) + "\n\n---\n\n" + payload
            else:
                prompt = payload
            res = run_claude(
                prompt,
                cwd=self.cfg.repo,
                model=self.cfg.pm_model,
                append_system_prompt=self.system,
                allowed_tools=self.cfg.pm_allowed_tools,
                permission_mode="default",
                resume_session=resume,
                json_schema=schema,
                max_turns=self.cfg.max_turns_per_call,
                timeout_s=self.cfg.call_timeout_s,
                timeout_cost_usd=self.cfg.timeout_cost_usd,
            )
            # Persist the session id BEFORE charging, so a budget stop still resumes the
            # right session and doesn't re-pay for this turn.
            if res.session_id:
                self.session_id = res.session_id
                self.ledger.set_pm_session(res.session_id)
            self.ledger.spend(res.cost_usd)
            if res.ok and res.structured is not None:
                self.ledger.log({"event": "pm_turn", "label": label,
                                 "verdict": res.structured.get("verdict"), "cost": res.cost_usd})
                return res.structured

            last_err = res.text[:1500]
            is_timeout = res.raw.get("error") == "timeout"
            self.ledger.log({"event": "pm_turn_failed", "label": label,
                             "timeout": is_timeout, "error": last_err[:500]})
            if resume is not None and not is_timeout:
                # A resumed session failed fast (stale/invalid) — reincarnate now.
                if not degradable:
                    raise PMTurnError(f"PM turn {label!r} lost its session and cannot degrade: {last_err}")
                self.ledger.log({"event": "pm_degraded_resume", "label": label})
                self.session_id = None
                self.ledger.set_pm_session(None)
            elif resume is not None and is_timeout:
                pass  # retry the SAME session once; the CLI may have persisted a partial turn
        raise PMTurnError(f"PM turn {label!r} failed after {attempts} attempts: {last_err}")

    def reincarnate(self) -> None:
        """Drop the session; the next turn() seeds a fresh one from brief+checkpoint+digest."""
        self.session_id = None
        self.ledger.set_pm_session(None)

    # ----- rails shown to the PM every turn -----

    def _rails(self, step: Optional[Step] = None) -> str:
        st = self.ledger.state
        remaining = self.cfg.budget_usd - st.get("total_cost_usd", 0.0)
        parts = [f"budget ${remaining:.2f} of ${self.cfg.budget_usd:.2f} remaining",
                 f"replans used {st.get('replans_used', 0)}/{self.cfg.max_replans}"]
        if step is not None:
            parts.append(f"step {step.id}: rejections used {step.rejections}/{self.cfg.max_rejections_per_step}")
        return "RAILS (enforced by the orchestrator): " + "; ".join(parts) + "."

    # ----- turns -----

    def plan_turn(self, plan: Plan) -> dict:
        payload = (
            "Create the plan for the task in the brief. Read the repository first (you have "
            "read-only access) so steps, acceptance criteria, and verify commands are grounded "
            "in the real code and its real build/test commands.\n\n"
            "Respond with verdict `plan` and plan_mutations (`add` one step at a time, in order). "
            "Every step needs: a unique id, a goal a developer can finish in one focused session, "
            "acceptance_criteria (each independently checkable), and verify commands that exit 0 "
            "ONLY when the step is genuinely done. Use `abort` only if the brief is impossible to "
            "act on.\n\n" + self._rails())
        return self.turn(payload, directive_schema(["plan", "abort"]), "plan", plan)

    def dispatch_turn(self, step: Step, plan: Plan) -> dict:
        payload = (
            f"Next up:\n\n{step.brief()}\n\n"
            "Author the developer's instructions for this step — verdict `dispatch` with "
            "next_prompt written verbatim for the developer (it has no memory of your planning; "
            "include everything it needs). Set dev_session to `fresh` unless you deliberately "
            "want to continue this step's previous developer session. "
            "Use `replan` if the plan needs changing first.\n\n" + self._rails(step))
        return self.turn(payload, directive_schema(["dispatch", "replan", "abort"]), f"dispatch:{step.id}", plan)

    def review_turn(self, step: Step, handover_text: str, allowed: List[str], plan: Plan,
                    high_risk: bool = False) -> dict:
        accept_help = ("accept — criteria met; provide commit_message and, per acceptance criterion, "
                       "criteria_evidence with an EXACT verbatim quote (>=12 chars) from the diff or gate "
                       "transcript — not the gate banner, not paraphrase; each criterion needs its own quote")
        if high_risk:
            accept_help += "; integrity flags are raised, so accept also requires an integrity_ack"
        menu = {
            "accept": accept_help,
            "reject": "reject — gates passed but the work does not meet the criteria; next_prompt is your "
                      "feedback (dev_session defaults to the same developer; set fresh to start over)",
            "replan": "replan — this step (or the plan) is wrong; provide plan_mutations",
            "descope": "descope — skip this step; reasoning must state the impact of skipping it",
            "abort": "abort — the run cannot continue; reasoning must say why",
        }
        options = "\n".join(f"- {menu[v]}" for v in allowed if v in menu)
        payload = (
            f"Review the developer's handover for step {step.id}.\n\n"
            f"Step definition:\n{step.brief()}\n\n"
            f"{handover_text}\n\n"
            f"Your options now:\n{options}\n\n"
            "Judge against the acceptance criteria and the diff — not the developer's claims. "
            "Address every INTEGRITY FLAG explicitly in your reasoning.\n\n" + self._rails(step))
        return self.turn(payload, directive_schema(allowed), f"review:{step.id}", plan)

    def finalize_turn(self, plan: Plan, allowed: List[str], failure_transcript: str = "") -> dict:
        if failure_transcript:
            payload = (
                "FINAL VERIFICATION FAILED. The task cannot be declared complete. "
                "Transcript from the pristine-worktree run:\n\n```\n"
                + failure_transcript[-self.cfg.gate_log_tail:] + "\n```\n\n"
                "Respond with `replan` (plan_mutations adding the steps needed to fix this) "
                "or `abort`.\n\n" + self._rails())
        else:
            extra = ""
            if self.cfg.final_verify_extra:
                extra = ("\nThe owner requires these commands in the final verification as well: "
                         + "; ".join(self.cfg.final_verify_extra))
            payload = (
                "All planned steps are accepted:\n\n" + plan.digest() + "\n\n"
                "If the WHOLE task in the brief is now genuinely delivered, respond with "
                "`task_complete` and final_verify: the full-proof command list the orchestrator "
                "will run in a PRISTINE fresh checkout (include dependency install and build from "
                "scratch — e.g. `npm ci`, build, tests, container build, smoke probes). "
                "Every accepted step's verify commands will also be replayed there. "
                "If work remains, respond with `replan` and plan_mutations." + extra + "\n\n" + self._rails())
        return self.turn(payload, directive_schema(allowed), "finalize", plan)

    def checkpoint_turn(self, plan: Plan) -> dict:
        payload = (
            "Context checkpoint. Summarize this run for a successor PM who will take over with "
            "ONLY your summary, the original brief, and the ledger digest: mission_summary, "
            "key_decisions (with the WHY), open_risks, remaining_plan_note, advice_to_successor "
            "(dead ends, gotchas, what to watch).")
        # Non-degradable: a checkpoint from a blank fresh session would launder away the
        # real run memory it exists to preserve.
        return self.turn(payload, CHECKPOINT_SCHEMA, "checkpoint", plan, degradable=False)

    def corrective_turn(self, problems: List[str], allowed: List[str], plan: Plan,
                        step: Optional[Step] = None) -> dict:
        payload = (
            "Your previous directive was REFUSED by the orchestrator:\n"
            + "\n".join(f"- {p}" for p in problems)
            + "\n\nIssue a corrected directive. This is your only retry.\n\n" + self._rails(step))
        return self.turn(payload, directive_schema(allowed), "corrective", plan)
