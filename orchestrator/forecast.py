"""Execution Forecast — a senior-engineer's estimate of a task's cost, runtime, size, and
risk, produced ONCE before the loop starts executing.

The pipeline is deliberately split so the numbers never come from an LLM hallucinating dollars:

    task ─▶ ForecastPlanner (ONE cheap model call) ─▶ EngineeringAnalysis
                                                          │  (steps, complexity, risk,
                                                          │   retries, replans, confidence…)
                                                          ▼
                              WeightedEstimator + EstimatorConfig (pure Python, deterministic)
                                                          │
                                                          ▼
                                                       Forecast  (cost $, runtime, budget gap,
                                                                  recommended budget)

The model estimates *engineering work only*. A deterministic estimator turns that work into
money and minutes using named, env-overridable coefficients — no magic numbers. Every finished
run appends a predicted-vs-actual record to <repo>/.agentic/forecasts.jsonl; the estimator
folds the resulting calibration factor back in so its numbers get truer over time.

The `Estimator` protocol is the extension seam: v1 ships `WeightedEstimator` (configurable
weighted averages); a future `RegressionEstimator` fit on the history file can drop in behind
the same interface with zero changes to the execution pipeline.

Like memory.md, forecasts.jsonl lives in .agentic/ and therefore survives --fresh (which only
archives state.json/log.jsonl).
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Protocol

from .claude_cli import run_claude
from .config import Config

RISK_LEVELS = ("low", "medium", "high")


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _median(xs: List[float]) -> float:
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return 0.0
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2.0


# ---------------------------------------------------------------------------
# 1. What the cheap model call returns: engineering work, never money/time.
# ---------------------------------------------------------------------------

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "estimated_steps": {"type": "integer",
                            "description": "Small, independently-committable changes (usually 3–15)."},
        "complexity": {"type": "integer", "description": "Overall difficulty, 0–100."},
        "risk": {"type": "string", "enum": list(RISK_LEVELS),
                 "description": "Likelihood of surprises forcing rework."},
        "research_required": {"type": "boolean",
                              "description": "True if a real part of the work is figuring out HOW."},
        "likely_replans": {"type": "integer", "description": "Expected plan revisions mid-run."},
        "likely_retries": {"type": "integer",
                           "description": "Total developer retry attempts across the WHOLE run."},
        "verification_types": {"type": "array", "items": {"type": "string"},
                               "description": "e.g. unit, integration, e2e, build, lint, typecheck, deploy."},
        "confidence": {"type": "integer", "description": "Confidence in THIS estimate, 0–100."},
    },
    "required": ["estimated_steps", "complexity", "risk", "research_required",
                 "likely_replans", "likely_retries", "verification_types", "confidence"],
}


@dataclass
class EngineeringAnalysis:
    """The forecast planner's structured verdict — engineering work, nothing monetary."""
    estimated_steps: int
    complexity: int
    risk: str
    research_required: bool
    likely_replans: int
    likely_retries: int
    verification_types: List[str]
    confidence: int

    @classmethod
    def from_dict(cls, d: dict) -> "EngineeringAnalysis":
        d = d or {}
        risk = str(d.get("risk", "medium")).strip().lower()
        if risk not in RISK_LEVELS:
            risk = "medium"
        vts = [str(v).strip().lower() for v in (d.get("verification_types") or []) if str(v).strip()]
        return cls(
            estimated_steps=max(1, int(d.get("estimated_steps", 1) or 1)),
            complexity=int(_clamp(float(d.get("complexity", 50) or 0), 0, 100)),
            risk=risk,
            research_required=bool(d.get("research_required", False)),
            likely_replans=max(0, int(d.get("likely_replans", 0) or 0)),
            likely_retries=max(0, int(d.get("likely_retries", 0) or 0)),
            verification_types=vts or ["unit"],
            confidence=int(_clamp(float(d.get("confidence", 60) or 0), 0, 100)),
        )

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# 2. Estimator coefficients — every number is named and env-overridable.
# ---------------------------------------------------------------------------

@dataclass
class EstimatorConfig:
    """All the knobs the deterministic estimator uses. No magic numbers live in the estimator
    body; they live here with sensible defaults, each overridable via a FORECAST_* env var.

    Defaults are tuned for Opus-4.8-on-both-agents loopd runs. Recalibrate for cheaper models
    by lowering the per-call costs (or just let the history calibration do it automatically)."""

    # --- Cost model: expected model calls × per-call cost ---
    cost_per_pm_call_usd: float = 0.30      # a planner turn (plan / dispatch / review)
    cost_per_dev_call_usd: float = 0.60     # a developer turn (more tool use, longer)
    pm_calls_base: float = 2.0              # initial plan + task_complete finalize
    pm_calls_per_step: float = 2.0          # author dev prompt + review the handover
    pm_calls_per_replan: float = 1.5        # a replan turn (+ knock-on review)
    dev_calls_per_step: float = 1.0         # first developer attempt at a step
    # (retries add dev calls directly: dev_calls = dev_calls_per_step*steps + likely_retries)

    # --- Per-call scaling: harder/unfamiliar work means pricier, longer turns ---
    complexity_cost_weight: float = 0.80    # cost ×(1 + complexity/100 · weight)
    complexity_time_weight: float = 0.50    # runtime ×(1 + complexity/100 · weight)
    research_cost_multiplier: float = 1.25  # applied to cost when research_required
    verification_scale_weight: float = 0.10 # ×(1 + weight·(n_verification_types − 1))

    # --- Runtime model (seconds; calls run sequentially in loopd) ---
    seconds_per_pm_call: float = 45.0
    seconds_per_dev_call: float = 110.0
    gate_seconds_per_step: float = 30.0

    # --- Recommended-budget contingency (headroom for retries/surprises) ---
    base_contingency: float = 0.15
    risk_contingency_low: float = 0.05
    risk_contingency_medium: float = 0.15
    risk_contingency_high: float = 0.30
    confidence_contingency_weight: float = 0.40  # + (1 − confidence/100) · weight
    recommend_round_usd: float = 5.0             # round the recommendation up to this step

    # --- History calibration bounds ---
    calibration_min_samples: int = 3    # need at least this many runs before trusting it
    calibration_recent: int = 20        # consider the most recent N records
    calibration_clamp_lo: float = 0.5
    calibration_clamp_hi: float = 2.0

    def risk_contingency(self, risk: str) -> float:
        return {"low": self.risk_contingency_low,
                "medium": self.risk_contingency_medium,
                "high": self.risk_contingency_high}.get(risk, self.risk_contingency_medium)

    @classmethod
    def from_env(cls) -> "EstimatorConfig":
        """Load every coefficient from FORECAST_<UPPER_FIELD> if set, else the default."""
        kwargs = {}
        for f in cls.__dataclass_fields__.values():
            env_name = "FORECAST_" + f.name.upper()
            default = f.default
            if isinstance(default, int) and not isinstance(default, bool):
                try:
                    kwargs[f.name] = int(os.environ.get(env_name, default))
                except (TypeError, ValueError):
                    kwargs[f.name] = default
            else:
                kwargs[f.name] = _envf(env_name, float(default))
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# 3. Calibration learned from the history file (v1: simple robust ratios).
# ---------------------------------------------------------------------------

@dataclass
class Calibration:
    cost_factor: float = 1.0
    runtime_factor: float = 1.0
    samples: int = 0


# ---------------------------------------------------------------------------
# 4. The forecast the user sees / the pipeline acts on.
# ---------------------------------------------------------------------------

@dataclass
class Forecast:
    analysis: EngineeringAnalysis
    estimated_cost_usd: float
    estimated_runtime_min: float
    estimated_steps: int
    confidence: int
    risk: str
    budget_usd: float
    budget_gap_usd: float          # estimated_cost − budget (positive ⇒ short)
    recommended_budget_usd: float
    constrained: bool              # would running at the current budget be constrained?
    expected_pm_calls: float
    expected_dev_calls: float
    calibration_samples: int
    already_spent_usd: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["analysis"] = self.analysis.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Forecast":
        d = dict(d or {})
        d.pop("actual", None)          # tolerated but not a constructor field
        d.pop("chosen_budget_usd", None)
        analysis = EngineeringAnalysis.from_dict(d.pop("analysis", {}))
        known = cls.__dataclass_fields__.keys()
        return cls(analysis=analysis, **{k: v for k, v in d.items() if k in known and k != "analysis"})


# ---------------------------------------------------------------------------
# 5. The estimator: pure, deterministic, swappable.
# ---------------------------------------------------------------------------

class Estimator(Protocol):
    """The extension seam. v1 is WeightedEstimator; a future regression model fit on the
    history file implements the same method and drops into run_forecast() unchanged."""
    def estimate(self, analysis: EngineeringAnalysis, budget_usd: float,
                 calibration: Calibration, already_spent_usd: float = 0.0) -> Forecast: ...


class WeightedEstimator:
    """Deterministic weighted-average estimator. Same input ⇒ same output, always."""

    def __init__(self, cfg: Optional[EstimatorConfig] = None) -> None:
        self.cfg = cfg or EstimatorConfig()

    def _expected_calls(self, a: EngineeringAnalysis) -> tuple[float, float]:
        c = self.cfg
        pm = c.pm_calls_base + c.pm_calls_per_step * a.estimated_steps \
            + c.pm_calls_per_replan * a.likely_replans
        dev = c.dev_calls_per_step * a.estimated_steps + a.likely_retries
        return pm, dev

    def estimate(self, analysis: EngineeringAnalysis, budget_usd: float,
                 calibration: Optional[Calibration] = None,
                 already_spent_usd: float = 0.0) -> Forecast:
        c = self.cfg
        calibration = calibration or Calibration()
        pm_calls, dev_calls = self._expected_calls(analysis)

        cost_scale = (1 + analysis.complexity / 100.0 * c.complexity_cost_weight)
        cost_scale *= (c.research_cost_multiplier if analysis.research_required else 1.0)
        cost_scale *= (1 + c.verification_scale_weight * max(0, len(analysis.verification_types) - 1))

        base_cost = pm_calls * c.cost_per_pm_call_usd + dev_calls * c.cost_per_dev_call_usd
        cost = base_cost * cost_scale * calibration.cost_factor

        time_scale = 1 + analysis.complexity / 100.0 * c.complexity_time_weight
        base_seconds = (pm_calls * c.seconds_per_pm_call
                        + dev_calls * c.seconds_per_dev_call
                        + analysis.estimated_steps * c.gate_seconds_per_step)
        runtime_min = base_seconds * time_scale * calibration.runtime_factor / 60.0

        contingency = (c.base_contingency + c.risk_contingency(analysis.risk)
                       + (1 - analysis.confidence / 100.0) * c.confidence_contingency_weight)
        recommended = _ceil_to(cost * (1 + contingency), c.recommend_round_usd)
        # A recommendation that fails to clear the estimate itself is useless.
        recommended = max(recommended, _ceil_to(cost, c.recommend_round_usd))

        gap = cost - budget_usd
        return Forecast(
            analysis=analysis,
            estimated_cost_usd=round(cost, 2),
            estimated_runtime_min=round(runtime_min, 1),
            estimated_steps=analysis.estimated_steps,
            confidence=analysis.confidence,
            risk=analysis.risk,
            budget_usd=round(budget_usd, 2),
            budget_gap_usd=round(gap, 2),
            recommended_budget_usd=round(recommended, 2),
            constrained=gap > 0,
            expected_pm_calls=round(pm_calls, 1),
            expected_dev_calls=round(dev_calls, 1),
            calibration_samples=calibration.samples,
            already_spent_usd=round(already_spent_usd, 4),
        )


def _ceil_to(x: float, step: float) -> float:
    if step <= 0:
        return math.ceil(x)
    return math.ceil(x / step) * step


# ---------------------------------------------------------------------------
# 6. History: append-only .agentic/forecasts.jsonl (survives --fresh).
# ---------------------------------------------------------------------------

class ForecastHistory:
    """Cross-run learning store. One JSON object per line: the forecast, the actuals, and the
    run outcome. Mirrors memory.md's .agentic placement so it survives --fresh automatically."""

    def __init__(self, repo) -> None:
        self.path = Path(repo).expanduser().resolve() / ".agentic" / "forecasts.jsonl"

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

    def _dedup_by_run(self, recs: List[dict]) -> List[dict]:
        """Keep one record per logical run (the latest), so a run resumed after a stop can't
        contribute several partial rows. Records without a run_id are each kept as-is."""
        out, by_run = [], {}
        for r in recs:
            rid = r.get("run_id")
            if rid is None:
                out.append(r)
            else:
                by_run[rid] = r  # later record wins
        return out + list(by_run.values())

    def calibration(self, cfg: EstimatorConfig) -> Calibration:
        """Robust ratios of actual/predicted over recent successful runs. Below the sample
        floor we return the identity (trust the coefficients, not thin data)."""
        recs = self._dedup_by_run(
            [r for r in self.load()
             if isinstance(r.get("predicted"), dict) and isinstance(r.get("actual"), dict)
             and r.get("run_success")])
        recs = recs[-cfg.calibration_recent:] if cfg.calibration_recent > 0 else []
        cost_ratios, time_ratios = [], []
        for r in recs:
            p, a = r["predicted"], r["actual"]
            pc, ac = p.get("estimated_cost_usd", 0) or 0, a.get("cost_usd", 0) or 0
            if pc > 0 and ac > 0:
                cost_ratios.append(ac / pc)
            pr, ar = p.get("estimated_runtime_min", 0) or 0, a.get("runtime_min", 0) or 0
            if pr > 0 and ar > 0:
                time_ratios.append(ar / pr)
        n = len(recs)
        if n < cfg.calibration_min_samples:
            return Calibration(1.0, 1.0, n)
        cf = _clamp(_median(cost_ratios) or 1.0, cfg.calibration_clamp_lo, cfg.calibration_clamp_hi)
        rf = _clamp(_median(time_ratios) or 1.0, cfg.calibration_clamp_lo, cfg.calibration_clamp_hi)
        return Calibration(cost_factor=cf, runtime_factor=rf, samples=n)

    def accuracy(self) -> Optional[float]:
        """Mean prediction accuracy (%) over recorded runs (one row per logical run), or None
        if none recorded."""
        accs = []
        for r in self._dedup_by_run(self.load()):
            p, a = r.get("predicted"), r.get("actual")
            if isinstance(p, dict) and isinstance(a, dict):
                accs.append(accuracy_pct(p, a))
        return round(sum(accs) / len(accs), 1) if accs else None


def accuracy_pct(predicted: dict, actual: dict) -> float:
    """Blended accuracy of cost + runtime predictions, 0–100. 100 = perfect."""
    parts = []
    for pk, ak in (("estimated_cost_usd", "cost_usd"), ("estimated_runtime_min", "runtime_min")):
        p, a = predicted.get(pk), actual.get(ak)
        if p is None or a is None:
            continue
        p, a = float(p), float(a)
        hi = max(abs(p), abs(a))
        if hi <= 0:
            parts.append(100.0)
        else:
            parts.append(_clamp(100.0 * (1 - abs(a - p) / hi), 0, 100))
    return round(sum(parts) / len(parts), 1) if parts else 0.0


# ---------------------------------------------------------------------------
# 7. The cheap model call (the ForecastPlanner) + the top-level entry point.
# ---------------------------------------------------------------------------

def analyze(cfg: Config, brief: str, ledger=None) -> Optional[EngineeringAnalysis]:
    """ONE cheap model call that sizes the engineering work. Returns None (never raises) if the
    call fails — a forecast is advisory, so a failure must not block the run."""
    prompt = (
        "Size the engineering work for the task described in this brief. Estimate WORK ONLY — "
        "steps, complexity, risk, retries, replans, verification — never dollars or minutes. "
        "You may take a shallow glance at the repo's shape, but do not read every file. Return "
        "only the structured JSON.\n\n## Task brief\n" + (brief or "").strip()
    )
    try:
        system = cfg.prompt("forecast_system.md")
    except Exception:
        system = None
    res = run_claude(
        prompt,
        cwd=cfg.repo,
        model=cfg.forecast_model,
        append_system_prompt=system,
        allowed_tools=cfg.pm_allowed_tools,   # read-only skim
        permission_mode="default",
        json_schema=ANALYSIS_SCHEMA,
        max_turns=cfg.forecast_max_turns,
        timeout_s=cfg.forecast_timeout_s,
        timeout_cost_usd=(ledger.timeout_cost() if ledger is not None else cfg.timeout_cost_usd),
    )
    if ledger is not None:
        ledger.spend(res.cost_usd)   # keep the budget rail honest even for the estimate
    if not res.ok or not isinstance(res.structured, dict):
        return None
    try:
        return EngineeringAnalysis.from_dict(res.structured)
    except Exception:
        return None


def run_forecast(cfg: Config, brief: str, budget_usd: float, ledger=None,
                 estimator: Optional[Estimator] = None) -> Optional[Forecast]:
    """Full forecast: cheap analysis → deterministic estimate (with history calibration).
    Returns None if forecasting is disabled or the analysis call fails (run proceeds normally)."""
    if not cfg.forecast_enabled:
        return None
    analysis = analyze(cfg, brief, ledger=ledger)
    if analysis is None:
        return None
    est_cfg = EstimatorConfig.from_env()
    calibration = ForecastHistory(cfg.repo).calibration(est_cfg)
    estimator = estimator or WeightedEstimator(est_cfg)
    already = float(ledger.state.get("total_cost_usd", 0.0)) if ledger is not None else 0.0
    return estimator.estimate(analysis, budget_usd, calibration, already_spent_usd=already)


# ---------------------------------------------------------------------------
# 8. The user decision (pure mapping; interactivity/IO lives in the caller).
# ---------------------------------------------------------------------------

@dataclass
class Decision:
    action: str          # 'proceed' | 'raise' | 'constrain' | 'abort'
    budget_usd: float    # the budget the run should use
    constrained: bool    # tell the planner it is budget-constrained?


def apply_choice(fc: Forecast, choice: str, edited_budget: Optional[float] = None) -> Decision:
    """Map a chosen action to a concrete Decision. Pure and fully unit-testable.

    choice:
      'raise'    → run at the recommended budget (never constrained)
      'continue' → run at the current budget (constrained iff it is short)
      'edit'     → run at `edited_budget` (constrained iff that is short)
      'abort'    → stop before executing
    """
    choice = (choice or "").strip().lower()
    if choice == "abort":
        return Decision("abort", fc.budget_usd, False)
    if choice == "raise":
        return Decision("raise", fc.recommended_budget_usd, False)
    if choice == "edit":
        b = float(edited_budget if edited_budget is not None else fc.budget_usd)
        return Decision("edit", round(b, 2), b < fc.estimated_cost_usd)
    # default: continue at current budget
    return Decision("proceed" if not fc.constrained else "constrain",
                    fc.budget_usd, fc.constrained)


# ---------------------------------------------------------------------------
# 9. Rendering — the forecast card and the predicted-vs-actual comparison.
# ---------------------------------------------------------------------------

def _bar(pct: float, width: int = 22) -> str:
    filled = int(round(_clamp(pct, 0, 100) / 100.0 * width))
    return "█" * filled + "░" * (width - filled)


def _money(x) -> str:
    try:
        return f"${float(x):,.2f}"
    except (TypeError, ValueError):
        return "$?"


def _mins(x) -> str:
    try:
        m = float(x)
    except (TypeError, ValueError):
        return "?"
    if m < 1:
        return f"{int(round(m * 60))} sec"
    if m < 90:
        return f"{int(round(m))} min"
    h, r = divmod(int(round(m)), 60)
    return f"{h}h {r}m"


def render_card(fc: Forecast) -> str:
    gap = fc.budget_gap_usd
    if gap > 0:
        gap_line = f"Budget Gap        +{_money(gap)}  (short — see options below)"
    else:
        gap_line = f"Budget Headroom   {_money(-gap)}"
    lines = [
        "",
        "  ┌─────────────────────────────────────────────┐",
        "  │              EXECUTION FORECAST              │",
        "  └─────────────────────────────────────────────┘",
        "",
        f"  Estimated Cost     {_money(fc.estimated_cost_usd)}",
        f"  Estimated Runtime  {_mins(fc.estimated_runtime_min)}",
        f"  Estimated Steps    {fc.estimated_steps}",
        f"  Confidence         {fc.confidence}%   {_bar(fc.confidence)}",
        f"  Risk               {fc.risk.capitalize()}",
        "",
        f"  Current Budget     {_money(fc.budget_usd)}",
        f"  {gap_line}",
    ]
    if fc.constrained:
        lines += [
            f"  Recommended        {_money(fc.recommended_budget_usd)}  (room for retries)",
        ]
    if fc.calibration_samples:
        lines.append(f"  Calibrated on {fc.calibration_samples} prior run(s) in this repo.")
    lines.append("")
    return "\n".join(lines)


CONSTRAINED_WARNING = (
    "  ⚠  Running in CONSTRAINED mode.\n"
    "     The planner will prioritize core functionality first, defer polish and optional\n"
    "     refactors, and may stop before every acceptance criterion is completed."
)


def render_comparison(predicted: dict, actual: dict) -> str:
    """The after-run predicted-vs-actual card. `predicted` is Forecast.to_dict(); `actual` is
    the run's measured facts (cost_usd, runtime_min, steps_done…)."""
    def row(label, p, a):
        return f"  {label:<20}{str(p):<14}{str(a)}"

    acc = accuracy_pct(predicted, actual)
    lines = [
        "",
        "  ┌─────────────────────────────────────────────┐",
        "  │           EXECUTION FORECAST · ACTUAL        │",
        "  └─────────────────────────────────────────────┘",
        "",
        row("", "Predicted", "Actual"),
        row("Cost", _money(predicted.get("estimated_cost_usd")), _money(actual.get("cost_usd"))),
        row("Runtime", _mins(predicted.get("estimated_runtime_min")), _mins(actual.get("runtime_min"))),
        row("Steps", predicted.get("estimated_steps", "?"),
            f"{actual.get('steps_done', '?')}"),
        "",
        f"  Prediction Accuracy   {acc}%   {_bar(acc)}",
        "",
    ]
    return "\n".join(lines)
