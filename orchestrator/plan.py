"""The living plan. Steps are authored and mutated ONLY by the PM (via schema-validated
plan_mutations directives), but validated and frozen here: the developer can never edit
its own bar, and the PM cannot author unverifiable or trivially-verified steps.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field, asdict
from typing import List, Optional

# Commands that "verify" nothing. Matched against the normalized command string.
_TRIVIAL_EXACT = {"true", ":", "exit 0", "test 1", "test true"}
_TRIVIAL_RE = [
    re.compile(r"^echo\b[^|&;>]*$"),      # bare echo (not piped/chained into a real check)
    re.compile(r"^printf\b[^|&;>]*$"),
    re.compile(r"^sleep\s+\d+$"),
]

PENDING, IN_PROGRESS, DONE, SKIPPED = "pending", "in_progress", "done", "skipped"


class PlanValidationError(ValueError):
    def __init__(self, problems: List[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def is_trivial_command(cmd: str) -> bool:
    norm = " ".join(cmd.strip().split())
    # strip a timeout= prefix before judging
    norm = re.sub(r"^timeout=\d+\s*;\s*", "", norm)
    if norm.lower() in _TRIVIAL_EXACT:
        return True
    return any(rx.match(norm) for rx in _TRIVIAL_RE)


@dataclass
class Step:
    id: str
    goal: str
    acceptance_criteria: List[str]
    verify: List[str]
    details: str = ""
    setup: List[str] = field(default_factory=list)
    teardown: List[str] = field(default_factory=list)
    status: str = PENDING
    attempts: int = 0
    rejections: int = 0
    cost_usd: float = 0.0
    commit_sha: str = ""
    dev_session_id: str = ""
    dev_summary: str = ""
    skip_reason: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Step":
        crit = d.get("acceptance_criteria") or []
        if isinstance(crit, str):
            crit = [crit]
        return cls(
            id=str(d.get("id", "")),
            goal=str(d.get("goal", "")),
            acceptance_criteria=[str(c) for c in crit],
            verify=[str(v) for v in (d.get("verify") or [])],
            details=str(d.get("details", "") or ""),
            setup=[str(v) for v in (d.get("setup") or [])],
            teardown=[str(v) for v in (d.get("teardown") or [])],
            status=str(d.get("status", PENDING)),
            attempts=int(d.get("attempts", 0)),
            rejections=int(d.get("rejections", 0)),
            cost_usd=float(d.get("cost_usd", 0.0)),
            commit_sha=str(d.get("commit_sha", "") or ""),
            dev_session_id=str(d.get("dev_session_id", "") or ""),
            dev_summary=str(d.get("dev_summary", "") or ""),
            skip_reason=str(d.get("skip_reason", "") or ""),
        )

    def brief(self) -> str:
        lines = [f"STEP {self.id}: {self.goal}"]
        if self.details:
            lines.append(f"DETAILS: {self.details}")
        lines.append("ACCEPTANCE CRITERIA:")
        lines += [f"  {i + 1}. {c}" for i, c in enumerate(self.acceptance_criteria)]
        lines.append("VERIFY COMMANDS (run by the orchestrator, frozen — the developer cannot change them):")
        lines += [f"  $ {v}" for v in self.verify]
        return "\n".join(lines)


@dataclass
class Plan:
    summary: str = ""
    steps: List[Step] = field(default_factory=list)

    def next_pending(self) -> Optional[Step]:
        for s in self.steps:
            if s.status in (PENDING, IN_PROGRESS):
                return s
        return None

    def get(self, step_id: str) -> Optional[Step]:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None

    def done_steps(self) -> List[Step]:
        return [s for s in self.steps if s.status == DONE]

    def to_dict(self) -> dict:
        return {"summary": self.summary, "steps": [asdict(s) for s in self.steps]}

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        return cls(summary=d.get("summary", ""),
                   steps=[Step.from_dict(s) for s in d.get("steps", [])])

    def digest(self) -> str:
        """One line per step — the ledger digest used to seed reincarnated PM sessions."""
        lines = [f"PLAN: {self.summary}"] if self.summary else []
        for s in self.steps:
            extra = ""
            if s.status == DONE and s.commit_sha:
                extra = f" commit={s.commit_sha[:9]}"
            if s.status == SKIPPED and s.skip_reason:
                extra = f" (descoped: {s.skip_reason[:120]})"
            summary = f" — {s.dev_summary[:160]}" if (s.status == DONE and s.dev_summary) else ""
            lines.append(f"  [{s.status:>11}] {s.id}: {s.goal}{extra}{summary}")
        return "\n".join(lines)


def validate_step_fields(s: Step) -> List[str]:
    problems = []
    if not s.id.strip():
        problems.append(f"step has an empty id (goal: {s.goal[:60]!r})")
    if not s.goal.strip():
        problems.append(f"step {s.id!r} has an empty goal")
    if not [c for c in s.acceptance_criteria if c.strip()]:
        problems.append(f"step {s.id!r} has no acceptance criteria")
    real_verify = [v for v in s.verify if v.strip()]
    if not real_verify:
        problems.append(f"step {s.id!r} has no verify commands — unverifiable steps are not allowed")
    trivial = [v for v in real_verify if is_trivial_command(v)]
    if trivial:
        problems.append(
            f"step {s.id!r} has trivially-true verify command(s) {trivial!r} — "
            "use real checks (tests, builds, linters, orchestrator probes)")
    return problems


def validate(plan: Plan) -> List[str]:
    problems = []
    if not plan.steps:
        problems.append("plan has no steps")
    seen = set()
    for s in plan.steps:
        if s.id in seen:
            problems.append(f"duplicate step id {s.id!r}")
        seen.add(s.id)
        if s.status in (PENDING, IN_PROGRESS):
            problems += validate_step_fields(s)
    return problems


def apply_mutations(plan: Plan, ops: List[dict]) -> Plan:
    """Apply PM-authored mutations to a COPY of the plan and validate the result.
    Done/skipped steps are immutable. Raises PlanValidationError on any problem."""
    new = copy.deepcopy(plan)
    problems: List[str] = []

    for op in ops or []:
        kind = (op.get("op") or "").strip()
        if kind == "add":
            step = Step.from_dict(op.get("step") or {})
            step.status = PENDING
            after_id = op.get("after_id")
            if after_id:
                idx = next((i for i, s in enumerate(new.steps) if s.id == after_id), None)
                if idx is None:
                    problems.append(f"add: after_id {after_id!r} not found")
                    continue
                new.steps.insert(idx + 1, step)
            else:
                new.steps.append(step)
        elif kind == "update":
            patch = op.get("step") or {}
            target = new.get(str(patch.get("id", "")))
            if target is None:
                problems.append(f"update: step id {patch.get('id')!r} not found")
                continue
            if target.status in (DONE, SKIPPED):
                problems.append(f"update: step {target.id!r} is {target.status} and immutable")
                continue
            for key in ("goal", "details"):
                if key in patch and patch[key] is not None:
                    setattr(target, key, str(patch[key]))
            for key in ("acceptance_criteria", "verify", "setup", "teardown"):
                if key in patch and patch[key] is not None:
                    val = patch[key]
                    if isinstance(val, str):
                        val = [val]
                    setattr(target, key, [str(v) for v in val])
            # a materially updated step gets a clean slate for retries
            target.attempts = 0
            target.rejections = 0
        elif kind == "remove":
            sid = str((op.get("step") or {}).get("id") or op.get("step_id") or "")
            target = new.get(sid)
            if target is None:
                problems.append(f"remove: step id {sid!r} not found")
                continue
            if target.status in (DONE, SKIPPED):
                problems.append(f"remove: step {sid!r} is {target.status} and immutable")
                continue
            new.steps.remove(target)
        elif kind == "reorder":
            order = [str(x) for x in (op.get("order") or [])]
            if sorted(order) != sorted(s.id for s in new.steps):
                problems.append("reorder: `order` must list every current step id exactly once")
                continue
            done_ids = [s.id for s in new.steps if s.status in (DONE, SKIPPED)]
            if [i for i in order if i in done_ids] != done_ids:
                problems.append("reorder: done/skipped steps must keep their relative order")
                continue
            new.steps.sort(key=lambda s: order.index(s.id))
        elif kind == "set_summary":
            new.summary = str(op.get("summary") or (op.get("step") or {}).get("goal") or new.summary)
        else:
            problems.append(f"unknown mutation op {kind!r}")

    problems += validate(new)
    if problems:
        raise PlanValidationError(problems)
    return new
