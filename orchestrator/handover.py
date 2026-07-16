"""The handover packet: everything the PM sees when reviewing a step. Assembled by
Python from ground truth (git, the gate runner) — the agents cannot fabricate it.

Integrity flags force the PM into heightened scrutiny when the evidence smells:
a no-op diff, test files touched, or files referenced by the verify commands touched.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from .claude_cli import ClaudeResult
from .config import Config
from .ledger import Ledger
from .plan import Step

_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)|(^|/|_)test[_.]|[_.](test|spec)\.", re.IGNORECASE)


@dataclass
class Handover:
    text: str
    bytes: int
    gates_passed: bool
    flags: List[str] = field(default_factory=list)
    dev_summary: str = ""


def _integrity_flags(diff: dict, verify_cmds: List[str], tests_expected: bool) -> List[str]:
    flags = []
    if diff["empty"]:
        flags.append("NO_OP_DIFF: the developer produced no changes relative to the last commit.")
    touched_tests = [f for f in diff["changed_files"] if _TEST_PATH.search(f)]
    if touched_tests and not tests_expected:
        flags.append(f"TESTS_TOUCHED: test files changed ({', '.join(touched_tests[:8])}) — "
                     "check they were strengthened, not weakened, to make gates pass.")
    verify_blob = "\n".join(verify_cmds)
    gate_targets = [f for f in diff["changed_files"]
                    if f and (f in verify_blob or (f.rsplit("/", 1)[-1] in verify_blob and len(f.rsplit("/", 1)[-1]) > 6))]
    if gate_targets:
        flags.append(f"GATE_TARGETS_TOUCHED: files referenced by the verify commands were modified "
                     f"({', '.join(gate_targets[:8])}) — confirm the checks were not gamed.")
    return flags


def _dev_summary_text(res: Optional[ClaudeResult]) -> str:
    if res is None:
        return "(no developer output)"
    s = res.structured or {}
    if s:
        parts = [str(s.get("summary", "")).strip()]
        if s.get("files_changed"):
            parts.append("Files changed (developer-claimed): " + ", ".join(map(str, s["files_changed"][:30])))
        if s.get("commands_run"):
            parts.append("Commands the developer says it ran: " + ", ".join(map(str, s["commands_run"][:20])))
        if s.get("concerns"):
            parts.append("Developer concerns: " + "; ".join(map(str, s["concerns"][:10])))
        return "\n".join(p for p in parts if p)
    return (res.text or "(empty)")[:4000]


def build_handover(
    step: Step,
    dev_res: Optional[ClaudeResult],
    gates_passed: bool,
    gate_log: str,
    ledger: Ledger,
    cfg: Config,
    dev_error: str = "",
) -> Handover:
    diff = ledger.diff_against_head(cfg.handover_diff_cap)
    tests_expected = bool(re.search(r"\btests?\b", (step.goal + " " + step.details), re.IGNORECASE))
    flags = _integrity_flags(diff, step.verify, tests_expected)
    dev_summary = _dev_summary_text(dev_res)

    sections = [
        f"## Handover for step {step.id} "
        f"(attempt {step.attempts}/{cfg.max_attempts_per_step} of this cycle, "
        f"rejections so far {step.rejections}/{cfg.max_rejections_per_step})",
        "",
        "### Gate verdict (run by the orchestrator — ground truth)",
        "ALL GATES PASSED" if gates_passed else "GATES FAILED (retries exhausted for this cycle)",
    ]
    if dev_error:
        sections += ["", "### Developer call error", dev_error[:2000]]
    sections += [
        "",
        "### Developer's structured summary (self-reported — verify against the diff)",
        dev_summary,
        "",
        "### Diff vs last accepted commit (git, ground truth)",
        "```",
        diff["stat"] or "(no changes)",
        "```",
        "```diff",
        diff["diff"] or "(empty)",
        "```",
        "",
        "### Gate transcript (tail)",
        "```",
        gate_log[-cfg.gate_log_tail:] if gate_log else "(no gate output)",
        "```",
    ]
    if flags:
        sections += ["", "### INTEGRITY FLAGS — address each one explicitly in your reasoning"]
        sections += [f"- {f}" for f in flags]

    text = "\n".join(sections)
    return Handover(text=text, bytes=len(text.encode()), gates_passed=gates_passed,
                    flags=flags, dev_summary=dev_summary)
