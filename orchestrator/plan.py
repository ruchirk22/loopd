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
_NOOP_CMDS = {"true", ":", "echo", "printf", "sleep", "pwd", "export", "tee", ""}
# `test`/`[` forms that are constant-true regardless of args.
_ALWAYS_TRUE_TEST = {"1", "true", "-d .", "-e .", "-d ./", "-f .", "-n x"}

PENDING, IN_PROGRESS, DONE, SKIPPED = "pending", "in_progress", "done", "skipped"


class PlanValidationError(ValueError):
    def __init__(self, problems: List[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


# Tri-state exit classification: always exits 0, always non-zero, or runtime-dependent.
ZERO, NONZERO, DEPENDS = "zero", "nonzero", "depends"
# Commands that verify nothing when used as the exit-status source. `tee` is here because a
# pipeline's exit is its last stage (pipefail off) and `… | tee log` masks the real status.
_WRAPPERS = {"env", "command", "nohup", "time", "exec", "stdbuf", "builtin", "eval"}
# ZERO only with NO path operand (bare `ls`/`cat`/`cd`) — with a path they're real existence
# checks that exit non-zero on a missing target, so `ls dist/app.js` must NOT be flagged.
_COND_NOOP = {"ls", "cat", "cd"}
_KEYWORDS = {"if", "then", "elif", "else", "fi", "while", "until", "for", "do", "done",
             "case", "esac", "in", "select", "function", ";;"}


def _base_exe(token: str) -> str:
    return token.rsplit("/", 1)[-1]


def _atom_class(clause: str) -> str:
    """Exit class of a single operator-free clause, seeing through NAME=val prefixes,
    wrapper words (env/command/exec/…), a leading `!` negation, and subshell/brace groups."""
    clause = clause.strip()
    if not clause or clause.startswith("#"):
        return ZERO
    # Unwrap ( ... ) and { ...; } groups and recurse.
    if clause.startswith("(") and clause.endswith(")"):
        return _expr_class(clause[1:-1])
    if clause.startswith("{") and clause.endswith("}"):
        return _expr_class(clause[1:-1].rstrip(";"))
    try:
        toks = shlex.split(clause, comments=True)
    except ValueError:
        return DEPENDS  # unparseable → assume it checks something (never a false reject)
    # Leading `!` negates the exit class.
    if toks and toks[0] == "!":
        inner = _atom_class(" ".join(shlex.quote(t) for t in toks[1:]))
        return {ZERO: NONZERO, NONZERO: ZERO, DEPENDS: DEPENDS}[inner]
    # Drop NAME=value assignment prefixes and wrapper words.
    while toks and (re.match(r"^[A-Za-z_]\w*=", toks[0]) or _base_exe(toks[0]) in _WRAPPERS):
        toks = toks[1:]
    if not toks:
        return ZERO  # bare assignments / `env` with no command
    exe = _base_exe(toks[0])
    if exe == "false":
        return NONZERO
    if exe == "exit":
        return ZERO if (len(toks) == 1 or toks[1] == "0") else NONZERO
    if exe in _NOOP_CMDS:
        return ZERO
    if exe in _COND_NOOP:
        # ZERO only with no path operand; with a path it's a real existence check.
        operands = [t for t in toks[1:] if not t.startswith("-")]
        return ZERO if not operands else DEPENDS
    if exe in ("test", "["):
        rest = [t for t in toks[1:] if t != "]"]
        return _test_class(rest)
    if exe in _KEYWORDS:
        return DEPENDS  # compound construct handled by the keyword heuristic below
    return DEPENDS


def _is_literal(tok: str) -> bool:
    return bool(tok) and "$" not in tok and "`" not in tok and "*" not in tok and "?" not in tok


def _test_class(args: List[str]) -> str:
    """Classify a `test`/`[` expression that uses only literals as constant true/false."""
    rest = " ".join(args).strip()
    if rest in _ALWAYS_TRUE_TEST:
        return ZERO
    if len(args) == 3 and _is_literal(args[0]) and _is_literal(args[2]):
        a, op, b = args
        if op in ("=", "=="):
            return ZERO if a == b else NONZERO
        if op == "!=":
            return NONZERO if a == b else ZERO
        if op in ("-eq", "-ne", "-lt", "-le", "-gt", "-ge"):
            try:
                x, y = int(a), int(b)
            except ValueError:
                return DEPENDS
            res = {"-eq": x == y, "-ne": x != y, "-lt": x < y,
                   "-le": x <= y, "-gt": x > y, "-ge": x >= y}[op]
            return ZERO if res else NONZERO
    return DEPENDS


def _combine_and(a: str, b: str) -> str:
    # A && B exits 0 iff both do.
    can_zero = a in (ZERO, DEPENDS) and b in (ZERO, DEPENDS)
    can_nonzero = a in (NONZERO, DEPENDS) or (a in (ZERO, DEPENDS) and b in (NONZERO, DEPENDS))
    return _from_flags(can_zero, can_nonzero)


def _combine_or(a: str, b: str) -> str:
    # A || B exits 0 iff A does, or A fails and B does.
    can_zero = a in (ZERO, DEPENDS) or (a in (NONZERO, DEPENDS) and b in (ZERO, DEPENDS))
    can_nonzero = a in (NONZERO, DEPENDS) and b in (NONZERO, DEPENDS)
    return _from_flags(can_zero, can_nonzero)


def _from_flags(can_zero: bool, can_nonzero: bool) -> str:
    if can_zero and can_nonzero:
        return DEPENDS
    return ZERO if can_zero else NONZERO


def _pipe_class(expr: str) -> str:
    # Pipeline exit is the last stage (pipefail off). A trailing `&` (background) always 0.
    if expr.rstrip().endswith("&") and not expr.rstrip().endswith("&&"):
        return ZERO
    return _atom_class(expr.split("|")[-1])


def _unwrap(expr: str) -> str:
    """Remove one layer of surrounding ( ) or { } when it wraps the whole expression
    (balanced), so `{ true; }` / `(true)` are classified by their bodies."""
    e = expr.strip()
    for op, cl in (("(", ")"), ("{", "}")):
        if len(e) >= 2 and e.startswith(op) and e.endswith(cl):
            depth, ok = 0, True
            for ch in e[1:-1]:
                if ch == op:
                    depth += 1
                elif ch == cl:
                    depth -= 1
                    if depth < 0:
                        ok = False
                        break
            if ok and depth == 0:
                return e[1:-1].strip().rstrip(";").strip()
    return e


def _expr_class(expr: str, set_e: bool = False) -> str:
    expr = _unwrap(expr.strip())
    if not expr:
        return ZERO
    # `set -e` in a leading segment makes every later segment's failure propagate.
    segs = [s for s in re.split(r"[;\n]", expr) if s.strip()]
    if len(segs) > 1:
        if re.match(r"^set\s+-\w*e", segs[0].strip()):
            return _seq_class(segs[1:], set_e=True)
        return _seq_class(segs, set_e=set_e)
    # Single segment: fold && / || left-to-right.
    parts = re.split(r"(&&|\|\|)", expr)
    cls = _pipe_class(parts[0])
    i = 1
    while i < len(parts) - 1:
        op, rhs = parts[i].strip(), _pipe_class(parts[i + 1])
        cls = _combine_and(cls, rhs) if op == "&&" else _combine_or(cls, rhs)
        i += 2
    return cls


def _seq_class(segs, set_e: bool) -> str:
    if not set_e:
        return _expr_class(segs[-1])  # only the last segment sets the status
    # Under set -e the sequence behaves like an && chain.
    cls = _expr_class(segs[0])
    for s in segs[1:]:
        cls = _combine_and(cls, _expr_class(s))
    return cls


def _clause_is_noop(clause: str) -> bool:
    return _atom_class(clause) == ZERO


def is_trivial_command(cmd: str) -> bool:
    """True if the command verifies nothing — it always exits 0 regardless of whether the
    real work was done. Models real POSIX exit semantics (; && || | & ! set -e, subshells,
    NAME=val and wrapper prefixes) so it screens the demonstrated no-op bypasses without
    rejecting genuine checks like `pytest || exit 1` or `set -e; npm ci; npm test`."""
    norm = re.sub(r"^timeout=\d+\s*;\s*", "", cmd.strip())
    if not norm or norm.startswith("#"):
        return True
    if _expr_class(norm) == ZERO:
        return True
    # Compound keyword constructs (if/while/for/case) are opaque to the algebra above; flag
    # them as trivial only when every command clause between the keywords is itself a no-op
    # (e.g. `if true; then :; fi`), while a real condition/body (`if [ -f x ]; …`) is kept.
    if re.search(r"\b(if|while|until|for|case)\b", norm):
        skeleton = re.sub(r"\b(if|then|elif|else|fi|while|until|for|do|done|case|esac|in|select)\b",
                          ";", norm)
        clauses = [c for c in re.split(r"&&|\|\||[;|&\n]", skeleton) if c.strip()]
        if clauses and all(_clause_is_noop(c) for c in clauses):
            return True
    return False


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


def _sig(step: Step) -> tuple:
    """Identity for carry-over: normalized goal + verify (not counters/status)."""
    return (" ".join(step.goal.split()),
            tuple(" ".join(v.split()) for v in step.verify))


def apply_mutations(plan: Plan, ops: List[dict]) -> Plan:
    """Apply PM-authored mutations to a COPY of the plan and validate the result.
    Done/skipped steps are immutable. Raises PlanValidationError on any problem."""
    new = copy.deepcopy(plan)
    problems: List[str] = []
    removed_counters: dict = {}  # signature -> (attempts, rejections), for remove+add laundering

    for op in ops or []:
        kind = (op.get("op") or "").strip()
        if kind == "add":
            step = Step.from_dict(op.get("step") or {})
            step.status = PENDING
            carried = removed_counters.get(_sig(step))
            if carried:  # re-adding a just-removed identical step keeps its spent caps
                step.attempts, step.rejections = carried
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
            def _all_fields(s: Step) -> tuple:
                return (" ".join(s.goal.split()),
                        tuple(" ".join(c.split()) for c in s.acceptance_criteria),
                        tuple(" ".join(v.split()) for v in s.verify))

            def _exec_fields(s: Step) -> tuple:
                # the bar the developer must actually clear: goal + verify commands
                return (" ".join(s.goal.split()), tuple(" ".join(v.split()) for v in s.verify))

            all_before, exec_before = _all_fields(target), _exec_fields(target)
            for key in ("goal", "details"):
                if key in patch and patch[key] is not None:
                    setattr(target, key, str(patch[key]))
            for key in ("acceptance_criteria", "verify", "setup", "teardown"):
                if key in patch and patch[key] is not None:
                    val = patch[key]
                    if isinstance(val, str):
                        val = [val]
                    setattr(target, key, [str(v) for v in val])
            if _all_fields(target) == all_before:
                problems.append(f"update: step {target.id!r} does not change goal, acceptance "
                                "criteria, or verify — a cosmetic edit cannot reset the retry caps")
                continue
            # A clean retry slate only when the EXECUTED bar (goal/verify) changed — editing
            # acceptance_criteria alone is allowed but must not launder the retry caps.
            if _exec_fields(target) != exec_before:
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
            if target.attempts or target.rejections:  # so a remove+re-add can't launder caps
                removed_counters[_sig(target)] = (target.attempts, target.rejections)
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
