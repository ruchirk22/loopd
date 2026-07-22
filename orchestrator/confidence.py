"""Delivery Confidence — a deterministic, evidence-grounded answer to the one question a forecast
can't: *how confident should you be that this run actually delivered what was asked, correctly?*

The Execution Forecast (forecast.py) predicts cost/time BEFORE a run and grades its own accuracy
after. Delivery Confidence is the complementary signal: it scores the DELIVERY, from ground truth
the agents cannot fabricate — the same facts the review loop already commits to state:

    plan.verification_coverage()   acceptance criteria backed by cited accept-evidence
    step completion                done vs descoped vs unfinished
    verification depth             do the passing gates PROVE behavior (probe flow / http /
                                   isolation / e2e) or only assert units?
    final verification             did the pristine-checkout replay pass?  (code == 0)
    stability                      churn — rejections + replans per delivered step
    integrity                      accepts that needed an integrity_ack (high-risk)

A pure, deterministic scorer turns those into a 0–100 score with a per-factor rationale — no model
call, same inputs ⇒ same number. Every coefficient is named and env-overridable (CONFIDENCE_*),
exactly like forecast.EstimatorConfig, so no magic numbers live in the scorer body.

Two entry points share ONE scorer:

    assess_delivery(cfg, plan, ledger, code)  → the real, evidence-based score after a run.
    assess_plan(cfg, plan)                    → the *ceiling* a plan could reach if every step
                                                completed and verified perfectly. What caps it
                                                below 100 is verification depth: a unit-only plan
                                                is structurally un-provable past a point, and this
                                                surfaces that BEFORE money is spent.

Each finished run appends a record to <repo>/.agentic/confidence.jsonl (mirrors forecasts.jsonl:
lives in .agentic/, survives --fresh) so the score is calibratable against outcomes over time.

The `Scorer` protocol is the extension seam — v1 ships `WeightedScorer`; a future model fit on the
history file can drop in behind the same interface with no pipeline changes.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional, Protocol

from .plan import DONE, SKIPPED, Plan, Step

# Gate strings that PROVE runtime behavior end-to-end, not just unit-level assertions. A step
# whose verify commands include one of these is "behavior-verified". Kept in sync with the
# behavior gates handover._weak_verification_flags nudges the planner toward.
BEHAVIOR_GATES = (
    "probe flow", "probe http", "probe proc-up", "probe isolation", "probe port",
    "probe docker-build", "probe env-file", "playwright", "cypress", "selenium",
    "e2e", "integration", "supertest", "newman", "k6", "locust", "curl ",
)
ISOLATION_GATE = "probe isolation"

BANDS = ("Low", "Moderate", "High", "Very High")


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _has_behavior_gate(step: Step) -> bool:
    blob = " ".join(step.verify).lower()
    return any(g in blob for g in BEHAVIOR_GATES)


def _has_isolation_gate(step: Step) -> bool:
    return ISOLATION_GATE in " ".join(step.verify).lower()


# ---------------------------------------------------------------------------
# 1. Coefficients — every number named and env-overridable (CONFIDENCE_*).
# ---------------------------------------------------------------------------

@dataclass
class ConfidenceConfig:
    """All the knobs the scorer uses. No magic numbers in the scorer body — they live here with
    defaults, each overridable via CONFIDENCE_<UPPER_FIELD>."""

    # --- Factor weights (relative; the score is their weighted mean, so they need not sum to 1) ---
    w_coverage: float = 0.30        # acceptance criteria backed by cited evidence
    w_completion: float = 0.18      # steps delivered vs planned (descopes/unfinished hurt)
    w_final_verify: float = 0.22    # pristine-checkout replay passed
    w_depth: float = 0.20           # gates prove behavior, not just units
    w_stability: float = 0.05       # low churn (rejections + replans)
    w_integrity: float = 0.05       # few high-risk accepts

    # --- Verification-depth shaping ---
    depth_floor: float = 0.50       # a fully unit-only (but green) delivery still earns this much
    tenancy_unproven_mult: float = 0.60  # depth ×this when tenancy is declared but no isolation gate

    # --- Stability shaping ---
    churn_zero_at: float = 3.0      # (rejections+replans)/delivered_step at which stability → 0

    # --- Integrity shaping ---
    high_risk_penalty: float = 0.15  # each high-risk accept subtracts this from the integrity factor

    # --- Band thresholds (score ≥ threshold ⇒ that band). "High" is the >75% north-star line. ---
    band_moderate: float = 50.0
    band_high: float = 75.0
    band_very_high: float = 90.0

    @classmethod
    def from_env(cls) -> "ConfidenceConfig":
        kwargs = {}
        for f in cls.__dataclass_fields__.values():
            kwargs[f.name] = _envf("CONFIDENCE_" + f.name.upper(), float(f.default))
        return cls(**kwargs)

    def band(self, score: float) -> str:
        if score >= self.band_very_high:
            return "Very High"
        if score >= self.band_high:
            return "High"
        if score >= self.band_moderate:
            return "Moderate"
        return "Low"


# ---------------------------------------------------------------------------
# 2. The raw, ground-truth signals the scorer consumes.
# ---------------------------------------------------------------------------

@dataclass
class ConfidenceInputs:
    criteria_evidenced: int = 0
    criteria_total: int = 0
    steps_done: int = 0
    steps_total: int = 0
    steps_behavior_verified: int = 0   # delivered steps with a behavior gate
    steps_depth_pool: int = 0          # delivered steps considered for depth (== steps_done)
    final_verify_passed: bool = False
    rejections: int = 0
    replans: int = 0
    high_risk_accepts: int = 0
    tenancy_declared: bool = False
    isolation_gate_present: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# 3. A single scored factor (name, weight, 0–1 value, human note) + the report.
# ---------------------------------------------------------------------------

@dataclass
class Factor:
    key: str
    label: str
    weight: float
    value: float   # 0..1
    note: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ConfidenceReport:
    kind: str                # 'delivery' | 'plan-ceiling'
    score: int               # 0..100
    band: str
    factors: List[Factor]
    inputs: ConfidenceInputs
    meets_bar: bool          # score ≥ the High band (the >75% north star)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "score": self.score, "band": self.band,
            "meets_bar": self.meets_bar,
            "factors": [f.to_dict() for f in self.factors],
            "inputs": self.inputs.to_dict(),
        }


# ---------------------------------------------------------------------------
# 4. The scorer: pure, deterministic, swappable.
# ---------------------------------------------------------------------------

class Scorer(Protocol):
    def score(self, inp: ConfidenceInputs, kind: str) -> ConfidenceReport: ...


class WeightedScorer:
    """Deterministic weighted-mean scorer. Same inputs ⇒ same score, always."""

    def __init__(self, cfg: Optional[ConfidenceConfig] = None) -> None:
        self.cfg = cfg or ConfidenceConfig()

    def _coverage(self, inp) -> Factor:
        if inp.criteria_total <= 0:
            v, note = 0.0, "no acceptance criteria were backed by evidence yet"
        else:
            v = _clamp(inp.criteria_evidenced / inp.criteria_total, 0, 1)
            note = f"{inp.criteria_evidenced}/{inp.criteria_total} criteria backed by cited evidence"
        return Factor("coverage", "Evidence coverage", self.cfg.w_coverage, v, note)

    def _completion(self, inp) -> Factor:
        if inp.steps_total <= 0:
            v, note = 0.0, "no steps planned"
        else:
            v = _clamp(inp.steps_done / inp.steps_total, 0, 1)
            gap = inp.steps_total - inp.steps_done
            note = f"{inp.steps_done}/{inp.steps_total} steps delivered" + (
                f" ({gap} descoped/unfinished)" if gap else "")
        return Factor("completion", "Scope delivered", self.cfg.w_completion, v, note)

    def _final_verify(self, inp) -> Factor:
        v = 1.0 if inp.final_verify_passed else 0.0
        note = ("pristine-checkout replay passed" if inp.final_verify_passed
                else "final verification did not pass (run incomplete or replay red)")
        return Factor("final_verify", "Final verification", self.cfg.w_final_verify, v, note)

    def _depth(self, inp) -> Factor:
        c = self.cfg
        if inp.steps_depth_pool <= 0:
            v, note = 0.0, "no delivered steps to verify"
        else:
            frac = inp.steps_behavior_verified / inp.steps_depth_pool
            v = c.depth_floor + (1 - c.depth_floor) * _clamp(frac, 0, 1)
            note = (f"{inp.steps_behavior_verified}/{inp.steps_depth_pool} steps prove behavior "
                    f"(flow/http/isolation/e2e)")
            if inp.tenancy_declared and not inp.isolation_gate_present:
                v *= c.tenancy_unproven_mult
                note += "; tenancy declared but no isolation gate"
        return Factor("depth", "Verification depth", c.w_depth, _clamp(v, 0, 1), note)

    def _stability(self, inp) -> Factor:
        c = self.cfg
        if inp.steps_done <= 0:  # nothing delivered ⇒ no clean-delivery signal to credit
            return Factor("stability", "Stability", c.w_stability, 0.0, "no delivered work")
        churn = (inp.rejections + inp.replans) / inp.steps_done
        v = _clamp(1 - churn / c.churn_zero_at, 0, 1) if c.churn_zero_at > 0 else 1.0
        note = f"{inp.rejections} rejection(s) + {inp.replans} replan(s) across {inp.steps_done} step(s)"
        return Factor("stability", "Stability", c.w_stability, v, note)

    def _integrity(self, inp) -> Factor:
        c = self.cfg
        if inp.steps_done <= 0:  # nothing delivered ⇒ no integrity signal to credit
            return Factor("integrity", "Integrity", c.w_integrity, 0.0, "no delivered work")
        v = _clamp(1 - inp.high_risk_accepts * c.high_risk_penalty, 0, 1)
        note = (f"{inp.high_risk_accepts} accept(s) required an integrity acknowledgement"
                if inp.high_risk_accepts else "no high-risk accepts")
        return Factor("integrity", "Integrity", c.w_integrity, v, note)

    def score(self, inp: ConfidenceInputs, kind: str = "delivery") -> ConfidenceReport:
        factors = [self._coverage(inp), self._completion(inp), self._final_verify(inp),
                   self._depth(inp), self._stability(inp), self._integrity(inp)]
        wsum = sum(f.weight for f in factors) or 1.0
        raw = sum(f.weight * f.value for f in factors) / wsum
        score = int(round(_clamp(raw * 100, 0, 100)))
        band = self.cfg.band(score)
        return ConfidenceReport(kind=kind, score=score, band=band, factors=factors, inputs=inp,
                                meets_bar=score >= self.cfg.band_high)


# ---------------------------------------------------------------------------
# 5. Gathering inputs from ground truth (plan + ledger + architecture spine).
# ---------------------------------------------------------------------------

def _tenancy_declared(cfg) -> bool:
    """True if the architecture spine commits to a real tenancy strategy (rls / app-layer)."""
    try:
        from . import architecture
        return architecture.tenancy_strategy(cfg.repo) in ("rls", "app-layer")
    except Exception:
        return False


def inputs_from_delivery(cfg, plan: Optional[Plan], ledger, code: int) -> ConfidenceInputs:
    """Evidence-based inputs measured from what actually happened this run."""
    steps = plan.steps if plan else []
    done = [s for s in steps if s.status == DONE]
    evidenced, total = (plan.verification_coverage() if plan else (0, 0))
    behavior = sum(1 for s in done if _has_behavior_gate(s))
    isolation = any(_has_isolation_gate(s) for s in done)
    st = getattr(ledger, "state", {}) or {}
    return ConfidenceInputs(
        criteria_evidenced=evidenced,
        criteria_total=total,
        steps_done=len(done),
        steps_total=len(steps),
        steps_behavior_verified=behavior,
        steps_depth_pool=len(done),
        final_verify_passed=(code == 0),
        rejections=sum(s.rejections for s in steps),
        replans=int(st.get("replans_used", 0) or 0),
        high_risk_accepts=int(st.get("high_risk_accepts", 0) or 0),
        tenancy_declared=_tenancy_declared(cfg),
        isolation_gate_present=isolation,
    )


def inputs_from_plan(cfg, plan: Optional[Plan]) -> ConfidenceInputs:
    """Optimistic ceiling inputs: assume every step completes and every criterion is evidenced,
    and the final replay passes. The only thing left to cap the ceiling is verification DEPTH,
    computed from the planned verify commands — so an under-verified plan reveals its ceiling."""
    steps = plan.steps if plan else []
    total_criteria = sum(len(s.acceptance_criteria) for s in steps)
    behavior = sum(1 for s in steps if _has_behavior_gate(s))
    isolation = any(_has_isolation_gate(s) for s in steps)
    return ConfidenceInputs(
        criteria_evidenced=total_criteria,   # ceiling: all evidenced
        criteria_total=total_criteria,
        steps_done=len(steps),               # ceiling: all delivered
        steps_total=len(steps),
        steps_behavior_verified=behavior,
        steps_depth_pool=len(steps),
        final_verify_passed=True,            # ceiling: replay passes
        rejections=0, replans=0, high_risk_accepts=0,
        tenancy_declared=_tenancy_declared(cfg),
        isolation_gate_present=isolation,
    )


def assess_delivery(cfg, plan: Optional[Plan], ledger, code: int,
                    scorer: Optional[Scorer] = None) -> ConfidenceReport:
    scorer = scorer or WeightedScorer(ConfidenceConfig.from_env())
    return scorer.score(inputs_from_delivery(cfg, plan, ledger, code), kind="delivery")


def assess_plan(cfg, plan: Optional[Plan],
                scorer: Optional[Scorer] = None) -> ConfidenceReport:
    scorer = scorer or WeightedScorer(ConfidenceConfig.from_env())
    return scorer.score(inputs_from_plan(cfg, plan), kind="plan-ceiling")


# ---------------------------------------------------------------------------
# 6. History: append-only .agentic/confidence.jsonl (survives --fresh).
# ---------------------------------------------------------------------------

class ConfidenceHistory:
    """Cross-run store of delivery-confidence scores + outcomes, so the score can be calibrated
    against reality over time. Mirrors forecasts.jsonl's .agentic placement."""

    def __init__(self, repo) -> None:
        self.path = Path(repo).expanduser().resolve() / ".agentic" / "confidence.jsonl"

    def load(self) -> List[dict]:
        if not self.path.is_file():
            return []
        out: List[dict] = []
        for line in self.path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out

    def append(self, record: dict) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(record) + "\n")
        return self.path

    def average(self) -> Optional[float]:
        """Mean delivery-confidence score over recorded runs, or None if none recorded."""
        scores = [r["score"] for r in self.load()
                  if isinstance(r.get("score"), (int, float))]
        return round(sum(scores) / len(scores), 1) if scores else None


# ---------------------------------------------------------------------------
# 7. Rendering — the delivery-confidence card (mirrors the forecast card).
# ---------------------------------------------------------------------------

def _bar(pct: float, width: int = 22) -> str:
    filled = int(round(_clamp(pct, 0, 100) / 100.0 * width))
    return "█" * filled + "░" * (width - filled)


def render_card(rep: ConfidenceReport) -> str:
    title = "DELIVERY CONFIDENCE" if rep.kind == "delivery" else "PLAN CONFIDENCE (CEILING)"
    pad = (45 - len(title)) // 2
    header = " " * pad + title
    lines = [
        "",
        "  ┌─────────────────────────────────────────────┐",
        f"  │{header:<45}│",
        "  └─────────────────────────────────────────────┘",
        "",
        f"  Score              {rep.score}%   {_bar(rep.score)}",
        f"  Band               {rep.band}" + ("   ✓ meets the >75% bar" if rep.meets_bar else ""),
        "",
        "  Factors (weighted):",
    ]
    for f in rep.factors:
        lines.append(f"    {f.label:<20}{int(round(f.value * 100)):>3}%   {f.note}")
    depth = next((f for f in rep.factors if f.key == "depth"), None)
    if rep.kind == "plan-ceiling" and depth is not None and depth.value < 1.0:
        lines += ["",
                  "  ⓘ Verification depth caps this plan's ceiling — add behavior gates",
                  "    (probe flow / http / isolation, e2e) so the delivery can be *proven*, not",
                  "    just asserted."]
    lines.append("")
    return "\n".join(lines)


def one_line(rep: ConfidenceReport) -> str:
    """A compact single-line summary for reports / PR bodies / the dashboard."""
    return f"{rep.score}% ({rep.band})"
