"""Failure Analysis — when a run genuinely can't finish, loopd explains the blocker like a
senior engineer, not an error report.

The diagnosis is GROUNDED in evidence: the planner only asserts what it can infer from the
verification transcripts, developer summaries, and plan. When it isn't sure, it says so.

Two sources, no extra model call:
  1. PRIMARY — the PM's `abort` directive carries a structured `failure_analysis` (the planner
     has already seen the failing gate transcripts and dev summaries, so it diagnoses from
     real evidence). See prompts/pm_system.md.
  2. FALLBACK — for failures with no PM directive (budget/time stops, crashes, replan-cap),
     a DETERMINISTIC analysis is built from the stop reason + failing step.

The result is persisted to <repo>/.agentic/analysis.json and rendered identically by the CLI
(`loopd status` / on-stop) and the dashboard "Needs you" state — two views of one state.

The four conversational beats, used verbatim by both surfaces:
    What happened · Why it happened · What I'd do · Other options
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

CATEGORIES = ["code", "environment", "dependency", "spec", "flaky", "scope", "resource", "unknown"]
# What loopd would do with each option when the user picks it on resume:
#   loopd_fix   — loopd takes this approach itself (replans the stuck step around it)
#   user_action — the user does something (set an env var, install a tool), then resumes
#   descope     — skip the stuck step and carry on
#   abort       — stop here
KINDS = ["loopd_fix", "user_action", "descope", "abort"]

# Section headings — the SAME strings the dashboard uses, so the two surfaces read identically.
BEAT_WHAT = "What happened"
BEAT_WHY = "Why it happened"
BEAT_DO = "What I'd do"
BEAT_OTHER = "Other options"

FAILURE_ANALYSIS_SCHEMA = {
    "type": "object",
    "description": "Your grounded explanation of why the run can't finish. Base it ONLY on the "
                   "verification transcripts, developer summaries, and plan — never guess.",
    "properties": {
        "summary": {"type": "string", "description": "One sentence: what couldn't be finished."},
        "root_cause": {"type": "string",
                       "description": "Why, grounded in the evidence. If you are not sure, say so plainly."},
        "category": {"type": "string", "enum": CATEGORIES},
        "confidence": {"type": "integer", "description": "How sure you are of the root cause, 0–100."},
        "options": {
            "type": "array",
            "description": "2–4 sensible next steps, exactly one marked recommended.",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "The action, e.g. 'Add a Redis test fixture'."},
                    "detail": {"type": "string", "description": "What it entails / the impact."},
                    "kind": {"type": "string", "enum": KINDS},
                    "recommended": {"type": "boolean"},
                },
                "required": ["label", "kind"],
            },
        },
    },
    "required": ["summary", "root_cause", "category", "options"],
}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")[:40] or "option"


def _clampi(x, lo, hi, default=None):
    try:
        return max(lo, min(hi, int(x)))
    except (TypeError, ValueError):
        return default


@dataclass
class Option:
    id: str
    label: str
    detail: str
    kind: str
    recommended: bool = False

    def to_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "detail": self.detail,
                "kind": self.kind, "recommended": self.recommended}


@dataclass
class FailureAnalysis:
    summary: str
    root_cause: str
    category: str
    options: List[Option]
    confidence: Optional[int] = None
    step: str = ""
    source: str = "pm"           # "pm" | "fallback"
    reason: str = ""             # the stop reason (pm_abort, budget_exceeded, …)

    def to_dict(self) -> dict:
        return {"summary": self.summary, "root_cause": self.root_cause, "category": self.category,
                "confidence": self.confidence, "step": self.step, "source": self.source,
                "reason": self.reason, "options": [o.to_dict() for o in self.options]}

    @property
    def recommended(self) -> Optional[Option]:
        return next((o for o in self.options if o.recommended), self.options[0] if self.options else None)

    def option(self, oid: str) -> Optional[Option]:
        return next((o for o in self.options if o.id == oid), None)

    @classmethod
    def from_dict(cls, d: dict) -> "FailureAnalysis":
        d = d or {}
        cat = str(d.get("category", "unknown")).strip().lower()
        if cat not in CATEGORIES:
            cat = "unknown"
        opts: List[Option] = []
        seen = set()
        for i, o in enumerate(d.get("options") or []):
            if not isinstance(o, dict):
                continue
            label = str(o.get("label", "")).strip()
            if not label:
                continue
            kind = str(o.get("kind", "loopd_fix")).strip().lower()
            if kind not in KINDS:
                kind = "loopd_fix"
            oid = str(o.get("id") or _slug(label))
            while oid in seen:
                oid += "-x"
            seen.add(oid)
            opts.append(Option(id=oid, label=label, detail=str(o.get("detail", "")).strip(),
                               kind=kind, recommended=bool(o.get("recommended"))))
        if not opts:  # never leave the user without a way forward
            opts = [Option("retry", "Let me try again", "Resume and re-attempt the step.", "user_action", True),
                    Option("stop", "Stop here", "Leave the completed work as-is.", "abort", False)]
        # Exactly one recommended.
        rec = [o for o in opts if o.recommended]
        if not rec:
            opts[0].recommended = True
        elif len(rec) > 1:
            for o in rec[1:]:
                o.recommended = False
        return cls(
            summary=str(d.get("summary", "")).strip() or "The run stopped before finishing.",
            root_cause=str(d.get("root_cause", "")).strip() or "I couldn't determine the cause with confidence.",
            category=cat, options=opts,
            confidence=_clampi(d.get("confidence"), 0, 100),
            step=str(d.get("step", "")), source=str(d.get("source", "pm")),
            reason=str(d.get("reason", "")),
        )


# --------------------------------------------------------------- deterministic fallback

def fallback(reason: str, step: str = "", detail: str = "", budget_add: float = 15.0) -> Optional[FailureAnalysis]:
    """A basic, no-model analysis for failures the PM didn't diagnose. Returns None for
    non-actionable stops (e.g. the user declining at the forecast) so nothing is shown."""
    reason = (reason or "").strip()
    d1 = (detail or "").strip().splitlines()
    first = d1[0][:200] if d1 else ""
    resume_opt = Option("resume", "Resume and continue", "Pick up exactly where I stopped.", "user_action", True)
    stop_opt = Option("stop", "Stop here", "Leave what's committed so far as-is.", "abort", False)

    if reason == "budget_exceeded":
        return FailureAnalysis(
            summary="I reached the budget for this run before finishing.",
            root_cause="The work needed more budget than was set — nothing is wrong; I simply ran out of room.",
            category="resource", confidence=95, step=step, source="fallback", reason=reason,
            options=[Option("continue", f"Add ${budget_add:.0f} and continue",
                            "I'll resume from where I stopped with more headroom.", "loopd_fix", True), stop_opt])
    if reason == "wall_clock_exceeded":
        return FailureAnalysis(
            summary="I hit the time limit for this run before finishing.",
            root_cause="The run exceeded its wall-clock cap — an operational limit, not a failure in the work.",
            category="resource", confidence=95, step=step, source="fallback", reason=reason,
            options=[resume_opt, stop_opt])
    if reason == "replan_cap_exhausted":
        return FailureAnalysis(
            summary="I revised the plan as many times as allowed and still couldn't get it green.",
            root_cause=(first or "Repeated attempts didn't satisfy the checks. The approach may be wrong, "
                        "or a requirement may be harder than the plan assumed."),
            category="unknown", confidence=35, step=step, source="fallback", reason=reason,
            options=[Option("more", "Give me more attempts and resume",
                            "Raise the replan cap and let me try a different approach.", "user_action", True),
                     Option("skip", "Skip the stuck step", "Descope it and finish the rest.", "descope", False),
                     stop_opt])
    if reason in ("git_error",):
        return FailureAnalysis(
            summary="A git operation failed mid-run.",
            root_cause=first or "The repository was in a state I couldn't safely work with.",
            category="environment", confidence=50, step=step, source="fallback", reason=reason,
            options=[Option("fixgit", "I'll fix the repo, then resume",
                            "Resolve the git issue (locks, disk, worktree) and resume.", "user_action", True), stop_opt])
    if reason in ("pm_turn_failed", "unexpected_error", "invalid_directive", "invalid_replan"):
        return FailureAnalysis(
            summary="The run stopped on an internal error before finishing.",
            root_cause=first or "Something went wrong in the loop itself, not in your project.",
            category="unknown", confidence=25, step=step, source="fallback", reason=reason,
            options=[resume_opt, stop_opt])
    return None  # forecast_declined, invalid_plan (setup), and anything non-actionable


# --------------------------------------------------------------- persistence

def path(repo) -> Path:
    return Path(repo).expanduser().resolve() / ".agentic" / "analysis.json"


def load(repo) -> Optional[FailureAnalysis]:
    p = path(repo)
    if not p.is_file():
        return None
    try:
        return FailureAnalysis.from_dict(json.loads(p.read_text()))
    except (OSError, json.JSONDecodeError):
        return None


def resolve_choice(repo, option_id: Optional[str] = None, recommended: bool = False) -> Optional[dict]:
    """Map the user's pick to a resume action: {kind, step, guidance, label}."""
    fa = load(repo)
    if fa is None:
        return None
    opt = fa.option(option_id) if option_id else (fa.recommended if recommended else None)
    if opt is None:
        return None
    return {"kind": opt.kind, "step": fa.step, "label": opt.label,
            "guidance": (opt.label + (". " + opt.detail if opt.detail else "")).strip()}


# --------------------------------------------------------------- rendering (CLI)

def confidence_phrase(c: Optional[int]) -> str:
    if c is None:
        return ""
    if c >= 75:
        return f"~{c}% sure"
    if c >= 45:
        return f"~{c}% sure — worth confirming"
    return "I'm not certain here"


def render(fa: FailureAnalysis) -> str:
    """The CLI 'needs you' card. Conversational: what happened · why · what I'd do · options."""
    where = f"  (paused at step {fa.step})" if fa.step else ""
    lines = ["", f"  ▲ I need you for a moment{where}", ""]
    lines += [f"  {BEAT_WHAT}", f"  {fa.summary}", ""]
    conf = confidence_phrase(fa.confidence)
    lines += [f"  {BEAT_WHY}" + (f"   ({conf})" if conf else ""), f"  {fa.root_cause}", ""]
    rec = fa.recommended
    if rec:
        lines += [f"  {BEAT_DO}", f"  → {rec.label}"]
        if rec.detail:
            lines.append(f"    {rec.detail}")
        lines.append("")
    others = [o for o in fa.options if o is not rec]
    if others:
        lines.append(f"  {BEAT_OTHER}")
        for o in others:
            tail = f" — {o.detail}" if o.detail else ""
            lines.append(f"  · {o.label}{tail}")
        lines.append("")
    lines.append("  " + _resume_hint(fa))
    lines.append("")
    return "\n".join(lines)


def _resume_hint(fa: FailureAnalysis) -> str:
    rec = fa.recommended
    if rec and rec.kind == "abort":
        return "Nothing more to do here — the work so far is committed."
    return "Continue with `loopd resume` (pick an option), or `loopd resume --yes` for the recommended one."
