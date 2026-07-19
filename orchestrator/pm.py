"""The PM agent: ONE persistent Opus session that plans, authors every developer
prompt verbatim, reviews every handover, and answers each turn with a schema-validated
DIRECTIVE. The verdict enum is built dynamically per turn — when gates are red,
`accept` simply is not in the schema, so rubber-stamping is structurally impossible.

Python (the loop) stays in charge of enforcement; this module owns the session,
the schemas, payload construction, and directive shape/evidence validation.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from . import analysis, memory
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
    if "abort" in verdicts:
        # When you give up, explain the blocker like a senior engineer would (grounded in the
        # transcripts you've seen). Surfaced to the owner in the CLI and dashboard.
        props["failure_analysis"] = analysis.FAILURE_ANALYSIS_SCHEMA
    if "task_complete" in verdicts:
        props["final_verify"] = {"type": "array", "items": {"type": "string"}}
        props["memory"] = {
            "type": "object",
            "description": "Durable project knowledge to record for future runs.",
            "properties": {
                "decisions": {"type": "array", "items": {"type": "string"},
                              "description": "Architecture/technical decisions made this run."},
                "failures": {"type": "array", "items": {"type": "string"},
                             "description": "Dead ends / failures hit, so future runs avoid them."},
                "todos": {"type": "array", "items": {"type": "string"},
                          "description": "Follow-ups discovered but out of scope."},
            },
        }
    return {"type": "object", "properties": props, "required": ["verdict", "reasoning"]}


# ---------------------------------------------------------------- validation

def _norm(s: str) -> str:
    return " ".join(s.split())


# Boilerplate/framework chrome that proves nothing even if present in the proof corpus.
_BANNED_EVIDENCE = {"all gates passed", "gates failed", "gate verdict", "ground truth",
                    "ok", "done", "pass", "passed", "tests pass", "it works",
                    "looks good", "lgtm", "correct", "verified", "n/a",
                    "no changes", "empty", "no gate output", "no developer output",
                    "self-reported", "verify against the diff",
                    "test session starts", "collected", "rootdir", "platform"}
_BANNED_RE = re.compile(r"^(=+|-+|\d+ passed.*|\d+ failed.*|collected \d+ items?.*)$")
# Generic markers only. NOTE: `$ <cmd>` gate-command lines are NOT scaffolding — for
# assertion-style checks that print nothing on success (python3 -c "assert ...", test -f x),
# the command line is the ONLY per-criterion evidence, and since accept is offered only when
# the whole gate is green, quoting a command that ran IS valid proof it passed.
_SCAFFOLD = ("###", "```", "diff --git", "index ", "@@", "[setup]", "[teardown]", "[ok]")


def _proof_lines(corpus: str):
    """Yield (strict, loose) content lines: `strict` drops all scaffolding; `loose` also
    keeps diff +/- body lines with their marker stripped, so a contiguous hunk copy still
    matches its content. Gate `$ command` lines are kept (see _SCAFFOLD note)."""
    strict, loose = [], []
    for line in corpus.splitlines():
        s = line.strip()
        if not s or s.startswith(_SCAFFOLD):
            continue
        if s.startswith(("+++ ", "--- ")):
            continue
        if s.startswith("$ "):  # gate command line: keep verbatim, don't diff-strip it
            strict.append(s)
            loose.append(s)
            continue
        if s[:1] in "+-" and not s.startswith(("+++", "---")):
            loose.append(s[1:].strip())  # diff body content without the +/- marker
            continue
        strict.append(s)
        loose.append(s)
    return strict, loose


def _proof_haystack(corpus: str):
    strict, loose = _proof_lines(corpus)
    return _norm("\n".join(strict)), _norm("\n".join(loose)), {_norm(l) for l in loose if l}


def _match_criterion(quoted: str, criteria: List[str], covered: set) -> Optional[int]:
    q = _norm(quoted).lower()
    if not q:
        return None
    # Prefer an exact (normalized) match, then the longest substring match, skipping
    # criteria already covered — so overlapping criteria don't starve each other.
    exact = [i for i, c in enumerate(criteria) if i not in covered and _norm(c).lower() == q]
    if exact:
        return exact[0]
    subs = [i for i, c in enumerate(criteria)
            if i not in covered and (q in _norm(c).lower() or (len(q) >= 6 and _norm(c).lower() in q))]
    if subs:
        return max(subs, key=lambda i: len(criteria[i]))
    return None


def _sig_tokens(s: str) -> set:
    """Significant tokens (≥3 chars) — the grounding unit. Token overlap tolerates
    paraphrase, collapsed multi-line quotes, and truncation while still catching a
    fabricated quote (whose tokens don't appear in the proof)."""
    return {t for t in re.findall(r"[a-z0-9_]+", s.lower()) if len(t) >= 3}


def verify_evidence(directive: dict, step: Step, proof_corpus: str) -> List[str]:
    """Accept-evidence must be GROUNDED in the proof corpus (real diff / gate transcript):
    most of a quote's significant tokens must appear there. This catches fabrication and
    rubber-stamping without demanding byte-exact quotes — a PM that paraphrases or collapses
    a real diff/test block still passes, but one citing text it never read does not. Empty,
    boilerplate, and unmatched-criterion evidence are refused; every criterion must be
    covered by a grounded, matching entry."""
    problems = []
    criteria = [c for c in step.acceptance_criteria if c.strip()]
    evidence = directive.get("criteria_evidence") or []
    _, loose_hay, _ = _proof_haystack(proof_corpus)
    corpus_tokens = _sig_tokens(loose_hay)
    covered: set = set()

    for i, e in enumerate(evidence):
        label = f"evidence entry {i + 1}"
        if not e.get("satisfied", False):
            problems.append(f"{label}: criterion marked unsatisfied — you cannot accept; reject or replan instead")
            continue
        quote = _norm(re.sub(r"@@[^@]*@@", " ", str(e.get("evidence", ""))))
        if not quote:
            problems.append(f"{label}: empty evidence — cite the diff or gate transcript text that proves it")
            continue
        if quote.lower() in _BANNED_EVIDENCE or _BANNED_RE.match(quote.lower()):
            problems.append(f"{label}: {quote!r} is boilerplate/scaffolding, not proof — cite specific diff/output text")
            continue
        qtok = _sig_tokens(quote)
        grounded = qtok & corpus_tokens
        if len(grounded) < 2 or (qtok and len(grounded) < 0.6 * len(qtok)):
            problems.append(f"{label}: evidence is not grounded in the diff/gate transcript "
                            f"(fabricated or paraphrased too far): {quote[:100]!r}")
            continue
        idx = _match_criterion(str(e.get("criterion", "")), criteria, covered)
        if idx is None:
            problems.append(f"{label}: criterion {str(e.get('criterion',''))[:80]!r} does not match a distinct acceptance criterion")
            continue
        covered.add(idx)

    missing = [criteria[i] for i in range(len(criteria)) if i not in covered]
    if missing:
        problems.append("no grounded evidence for acceptance criteria: "
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
        # Evidence is ENFORCED only when integrity flags fired (tests/gate-config/no-op —
        # the real gaming-risk cases). On a clean green-gate accept the deterministic gates
        # are the arbiter, so imperfect citation is advisory (logged by the loop), never a
        # run-ending abort — see loop._step_phase.
        if require_integrity_ack:
            if len(str(directive.get("integrity_ack", "")).strip()) < 40:
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
        # True once the session was lost mid-stream and reseeded fresh (context not
        # continuous); blocks non-degradable turns like checkpoints from fabricating.
        self.degraded: bool = False
        # Set on a resume-after-blocker: the approach the owner chose in Failure Analysis,
        # injected into the seed so the planner continues that way.
        self.resume_guidance: Optional[str] = None

    # ----- session plumbing -----

    def _seed_text(self, plan: Optional[Plan]) -> str:
        parts = [
            "You are joining (or re-joining) an in-progress automated build run as its PM. "
            "Everything you need is below; the orchestrator enforces the rails.",
        ]
        mem = memory.as_prompt(self.cfg.repo) if self.cfg.update_memory else ""
        if mem:
            parts.append("\n## Project memory (loopd) — honor these decisions, avoid the past "
                         "failures, consider the TODOs\n" + mem)
        parts.append("\n## Task brief\n" + self.brief)
        if getattr(self.cfg, "constrained", False):
            parts.append(
                "\n## BUDGET-CONSTRAINED EXECUTION (the owner chose to proceed under a tight budget)\n"
                "Treat the budget as a hard PLANNING constraint, not just a kill switch. You MUST:\n"
                "- prioritize the critical acceptance criteria first (highest-value work early);\n"
                "- keep the plan as short as possible — prefer fewer, larger steps;\n"
                "- defer optional improvements, polish, and non-essential refactors;\n"
                "- descope aggressively (with a stated impact) rather than risk running out mid-step;\n"
                "- aim to finish the most high-value work that fits, so a budget stop still leaves a "
                "coherent, working result.")
        ckpt = self.ledger.state.get("checkpoint")
        if ckpt:
            parts.append(
                "\n## Checkpoint from your predecessor PM session\n"
                f"Mission: {ckpt.get('mission_summary', '')}\n"
                "Key decisions:\n" + "\n".join(f"- {d}" for d in ckpt.get("key_decisions", [])) + "\n"
                "Open risks:\n" + "\n".join(f"- {r}" for r in ckpt.get("open_risks", [])) + "\n"
                f"Remaining plan: {ckpt.get('remaining_plan_note', '')}\n"
                f"Advice to you: {ckpt.get('advice_to_successor', '')}")
        if self.resume_guidance:
            parts.append(
                "\n## Owner's decision on the blocker\n"
                "The run had stopped and you explained why. The owner reviewed it and chose "
                "this way forward:\n  " + self.resume_guidance + "\n"
                "Take this approach for the stuck step, then continue the plan.")
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
        # A non-degradable turn (checkpoint) must run on genuinely continuous context: if
        # the current session was born from a degraded fallback, refuse rather than
        # fabricate memory from it.
        if not degradable and self.degraded:
            raise PMTurnError(f"PM turn {label!r} needs continuous session context that was lost")
        last_err = ""
        attempts, max_attempts, timeout_retries = 0, 4, 0
        while attempts < max_attempts:
            attempts += 1
            resume = self.session_id
            if resume is None:
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
                timeout_cost_usd=self.ledger.timeout_cost(),
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
            if resume is not None and is_timeout and timeout_retries == 0:
                timeout_retries += 1  # retry the SAME session ONCE (CLI may have a partial turn)
                continue
            if resume is not None:
                # Fast/stale failure, or a second timeout — the session is unusable.
                if not degradable:
                    raise PMTurnError(f"PM turn {label!r} lost its session and cannot degrade: {last_err}")
                self.ledger.log({"event": "pm_degraded_resume", "label": label})
                self.session_id = None
                self.ledger.set_pm_session(None)
                self.degraded = True  # a fresh reseed from here is NOT continuous context
        raise PMTurnError(f"PM turn {label!r} failed after {attempts} attempts: {last_err}")

    def reincarnate(self) -> None:
        """Drop the session; the next turn() seeds a fresh one from brief+checkpoint+digest.
        This is a DELIBERATE, context-preserving handoff (post-checkpoint), so it clears the
        degraded flag — unlike a mid-stream session loss."""
        self.session_id = None
        self.ledger.set_pm_session(None)
        self.degraded = False

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
