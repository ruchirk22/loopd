"""The living plan. Steps are authored and mutated ONLY by the PM (via schema-validated
plan_mutations directives), but validated and frozen here: the developer can never edit
its own bar, and the PM cannot author unverifiable or trivially-verified steps.
"""
from __future__ import annotations

import copy
import re
import shlex
from dataclasses import dataclass, field, asdict
from typing import List, Optional

# Executables that, on their own, verify nothing (regardless of args).
_NOOP_CMDS = {"true", ":", "false", "echo", "printf", "sleep", "pwd", "cat", "ls",
              "cd", "export", "exit", "read", ""}
# `test`/`[` forms that are constant-true.
_ALWAYS_TRUE_TEST = {"1", "true", "-d .", "-e .", "-d ./", "-f .", "1 -eq 1",
                     "0 -eq 0", "-n x", "x = x"}

PENDING, IN_PROGRESS, DONE, SKIPPED = "pending", "in_progress", "done", "skipped"


class PlanValidationError(ValueError):
    def __init__(self, problems: List[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def _base_exe(token: str) -> str:
    """Strip a path and env-var prefixes: '/usr/bin/true' -> 'true'."""
    return token.rsplit("/", 1)[-1]


def _clause_is_noop(clause: str) -> bool:
    """Does this single (operator-free) clause verify nothing?"""
    clause = clause.strip()
    if not clause or clause.startswith("#"):
        return True
    try:
        toks = shlex.split(clause, comments=True)
    except ValueError:
        return False  # unparseable → not obviously trivial; let it run
    if not toks:
        return True
    exe = _base_exe(toks[0])
    if exe in _NOOP_CMDS:
        return True
    if exe in ("test", "["):
        rest = " ".join(t for t in toks[1:] if t != "]").strip()
        if rest in _ALWAYS_TRUE_TEST:
            return True
    return False


def _can_fail(expr: str) -> bool:
    """Can this shell expression ever exit non-zero — i.e. does it actually check
    anything? Models real exit-status semantics for ; && || and pipes, so it screens
    the demonstrated bypasses ('true || pytest', 'echo && true', 'pytest || true') while
    NOT flagging genuine checks ('test -f x && echo ok')."""
    expr = expr.strip()
    if not expr:
        return False
    # `;` and newline: only the LAST segment sets the exit status (earlier fails masked).
    seq = [s for s in re.split(r"[;\n]", expr) if s.strip()]
    if len(seq) > 1:
        return _can_fail(seq[-1])
    # `&&` / `||` (equal precedence, left-associative). Fold left tracking can_fail.
    parts = re.split(r"(&&|\|\|)", expr)
    cf = _can_fail_pipe(parts[0])
    i = 1
    while i < len(parts) - 1:
        op, rhs = parts[i].strip(), parts[i + 1]
        rhs_cf = _can_fail_pipe(rhs)
        cf = (cf or rhs_cf) if op == "&&" else (cf and rhs_cf)
        i += 2
    return cf


def _can_fail_pipe(expr: str) -> bool:
    # In a pipeline `A | B | C` the exit status is the last stage (pipefail off).
    stages = expr.split("|")
    return not _clause_is_noop(stages[-1])


def is_trivial_command(cmd: str) -> bool:
    """True if the command verifies nothing — it is (or can short-circuit to) a command
    that always exits 0 regardless of whether the real work was done."""
    norm = cmd.strip()
    norm = re.sub(r"^timeout=\d+\s*;\s*", "", norm)
    if not norm or norm.startswith("#"):
        return True
    return not _can_fail(norm)


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
    base_sha: str = ""       # HEAD when this step started — detects a crash-window commit
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
            base_sha=str(d.get("base_sha", "") or ""),
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
            before = (target.goal, target.details, tuple(target.acceptance_criteria),
                      tuple(target.verify), tuple(target.setup), tuple(target.teardown))
            for key in ("goal", "details"):
                if key in patch and patch[key] is not None:
                    setattr(target, key, str(patch[key]))
            for key in ("acceptance_criteria", "verify", "setup", "teardown"):
                if key in patch and patch[key] is not None:
                    val = patch[key]
                    if isinstance(val, str):
                        val = [val]
                    setattr(target, key, [str(v) for v in val])
            after = (target.goal, target.details, tuple(target.acceptance_criteria),
                     tuple(target.verify), tuple(target.setup), tuple(target.teardown))
            if after == before:
                problems.append(f"update: step {target.id!r} changes nothing — an "
                                "empty update cannot be used to reset the retry caps")
                continue
            # only a MATERIALLY changed step earns a clean retry slate
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
