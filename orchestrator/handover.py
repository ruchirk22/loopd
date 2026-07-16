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
# Files that DEFINE what a gate command actually runs (npm test -> package.json, etc.).
# Editing these games the gate without touching the verify string, so substring
# matching misses it — flag them by name.
_GATE_CONFIG = re.compile(r"(^|/)(package\.json|Makefile|GNUmakefile|pyproject\.toml|setup\.cfg|"
                          r"pytest\.ini|tox\.ini|conftest\.py|\.mocharc[.\w]*|jest\.config[.\w]*|"
                          r"\.pre-commit-config\.yaml|noxfile\.py|justfile)$", re.IGNORECASE)


@dataclass
class Handover:
    text: str
    bytes: int
    gates_passed: bool
    flags: List[str] = field(default_factory=list)
    high_risk: bool = False
    dev_summary: str = ""
    # The PROOF bodies only (dev summary + real diff + gate transcript), with none of the
    # packet's fixed headers/placeholders — this is what accept-evidence quotes must match,
    # so a PM can't cite scaffolding that appears in every handover.
    evidence_corpus: str = ""


def _integrity_flags(diff: dict, verify_cmds: List[str], tests_expected: bool):
    """Returns (flags, high_risk). high_risk flags force the PM to justify acceptance."""
    flags, high_risk = [], False
    if diff["empty"]:
        high_risk = True
        flags.append("NO_OP_DIFF: the developer produced no changes relative to the last commit — "
                     "if you believe the work is already present, say why in integrity_ack.")
    touched_tests = [f for f in diff["changed_files"] if _TEST_PATH.search(f)]
    if touched_tests and not tests_expected:
        high_risk = True
        flags.append(f"TESTS_TOUCHED: test files changed ({', '.join(touched_tests[:8])}) — "
                     "confirm they were strengthened, not weakened, to make gates pass.")
    verify_blob = "\n".join(verify_cmds)
    gate_targets = [f for f in diff["changed_files"]
                    if f and (f in verify_blob or (f.rsplit("/", 1)[-1] in verify_blob and len(f.rsplit("/", 1)[-1]) > 6))]
    if gate_targets:
        high_risk = True
        flags.append(f"GATE_TARGETS_TOUCHED: files referenced by the verify commands were modified "
                     f"({', '.join(gate_targets[:8])}) — confirm the checks were not gamed.")
    gate_config = [f for f in diff["changed_files"] if _GATE_CONFIG.search(f)]
    if gate_config:
        high_risk = True
        flags.append(f"GATE_CONFIG_TOUCHED: files that DEFINE what the gate commands run were modified "
                     f"({', '.join(gate_config[:8])}) — e.g. a weakened `npm test`/pytest config makes "
                     "green gates meaningless. Inspect the actual check definition.")
    return flags, high_risk


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
    flags, high_risk = _integrity_flags(diff, step.verify, tests_expected)
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
        if high_risk:
            sections += ["", "To `accept` despite these flags you MUST provide an `integrity_ack` "
                         "that names each flag and cites the specific diff evidence that clears it."]

    text = "\n".join(sections)
    # Proof corpus = GROUND TRUTH only: the real git diff/stat and the orchestrator's gate
    # transcript. The developer's self-reported summary is deliberately EXCLUDED — accept
    # evidence must be quoted from what actually happened, not from the dev's own claims.
    # Section placeholders are excluded so they can never be quoted as "evidence".
    corpus_parts = []
    if diff["stat"]:
        corpus_parts.append(diff["stat"])
    if not diff["empty"]:
        corpus_parts.append(diff["diff"])
    if gate_log.strip():
        corpus_parts.append(gate_log)
    evidence_corpus = "\n".join(corpus_parts)
    return Handover(text=text, bytes=len(text.encode()), gates_passed=gates_passed,
                    flags=flags, high_risk=high_risk, dev_summary=dev_summary,
                    evidence_corpus=evidence_corpus)
