"""The run's terminal surface — one calm cockpit instead of scattered prints.

On a TTY it keeps a single live status line (phase · step · elapsed · spend) pinned at the
bottom and prints milestone lines above it; off a TTY (the dashboard subprocess, CI, tests)
it prints plain milestone lines only, so captured output and the dashboard log stay stable.

All run output goes through ONE reporter so the live status line is always cleared before a
milestone or block prints — nothing interleaves. Rendering is pure and unit-tested; the
terminal cursor I/O is a thin wrapper. A single active reporter per process is fine: the
dashboard launches each run as its own subprocess.
"""
from __future__ import annotations

import sys
import time
from typing import Callable, Optional


def _fmt_dur(seconds: float) -> str:
    s = int(max(0, seconds))
    h, m, x = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}h{m:02d}m" if h else (f"{m}m{x:02d}s" if m else f"{x}s")


def _money(x: float) -> str:
    return f"${x:,.2f}"


class Reporter:
    def __init__(self, stream=None, live: Optional[bool] = None) -> None:
        self.stream = stream if stream is not None else sys.stdout
        try:
            self.live = self.stream.isatty() if live is None else live
        except (AttributeError, ValueError):
            self.live = False
        self._start: Optional[float] = None
        self._cost: Callable[[], float] = lambda: 0.0
        self._phase = "starting"
        self._step = ""          # e.g. "3/8"
        self._painted = False    # is a live status line currently on screen?

    def attach(self, start_time: float, cost_fn: Callable[[], float]) -> None:
        """Give the live status line a clock and a spend source."""
        self._start = start_time
        self._cost = cost_fn

    # ---------- pure formatting (unit-tested) ----------

    def status_text(self) -> str:
        bits = [f"▸ {self._phase}"]
        if self._step:
            bits.append(f"step {self._step}")
        if self._start is not None:
            bits.append(_fmt_dur(time.time() - self._start))
        try:
            bits.append(_money(self._cost()))
        except Exception:
            pass
        return "   ".join(bits)

    # ---------- cursor I/O (thin) ----------

    def _clear(self) -> None:
        if self.live and self._painted:
            self.stream.write("\r\033[K")
            self._painted = False

    def _paint(self) -> None:
        if self.live:
            self.stream.write("\r\033[K" + self.status_text())
            self.stream.flush()
            self._painted = True

    def line(self, text: str) -> None:
        """A milestone line — printed above the live status, which is then repainted."""
        self._clear()
        self.stream.write(text + "\n")
        self.stream.flush()
        self._paint()

    def block(self, text: str) -> None:
        """A multi-line block (a report, a card). Clears the status and does NOT repaint —
        blocks appear at natural pause/stop points."""
        self._clear()
        self.stream.write(text.rstrip("\n") + "\n")
        self.stream.flush()

    def finish(self) -> None:
        """Drop the live status line for good (end of run)."""
        self._clear()

    # ---------- semantic events (what the loop calls) ----------

    def phase(self, name: str) -> None:
        self._phase = name
        self._paint()

    def planning(self) -> None:
        self._phase = "planning"
        self.line("Planning…")

    def planned(self, plan, cost: float) -> None:
        self._phase = "building"
        self.line(f"Plan ready: {plan.summary or '(no summary)'} — {len(plan.steps)} step(s), "
                  f"cost so far {_money(cost)}")

    def resuming(self, digest: str) -> None:
        self._phase = "building"
        self.line(f"Resuming: {digest}")

    def step_start(self, step, index: int, total: int) -> None:
        self._phase = "building"
        self._step = f"{index}/{total}"
        self.line(f"→ Step {step.id}: {step.goal}")

    def attempt(self, n: int) -> None:
        self._phase = f"developing (attempt {n})"
        if self.live:
            self._paint()
        else:
            self.line(f"   developer working (attempt {n})…")

    def dev_errored(self) -> None:
        self.line("   developer call errored — retrying")

    def gate(self, passed: bool) -> None:
        self._phase = "verifying"
        self.line(f"   gates: {'PASS' if passed else 'FAIL'}")

    def accepted(self, sha: str, adopted: bool = False) -> None:
        self.line(f"   ✓ accepted ({'already committed as ' if adopted else 'committed '}{sha[:9]})")

    def rejected(self, n: int, mx: int) -> None:
        self.line(f"   ✗ rejected — feedback sent to the developer (rejection {n}/{mx})")

    def descoped(self, reason: str) -> None:
        self.line(f"   ⤳ descoped: {reason[:120]}")

    def replanned(self, used: int, mx: int) -> None:
        self.line(f"   ↻ plan revised by the planner (replan {used}/{mx})")

    def checkpoint(self, skipped: bool = False) -> None:
        self.line("   … context checkpoint (fresh planner session next turn)"
                  if not skipped else "   … checkpoint skipped (keeping prior context)")

    def finalizing(self) -> None:
        self._phase = "final verification"
        self.line("Final verification in a pristine checkout…")

    def final_failed(self) -> None:
        self.line("   final verification FAILED — the planner will revise or stop")

    def completed(self, summary: str) -> None:
        self.finish()
        self.block(summary)


def render_completion(plan, ledger, cfg) -> str:
    """The end-of-run handover: what shipped, how thoroughly it was proven, and what's next.
    Pure — takes the finished plan + ledger. Reused by the CLI success path."""
    from .plan import DONE, SKIPPED  # local import avoids any import cycle
    st = ledger.state
    done = [s for s in plan.steps if s.status == DONE]
    skipped = [s for s in plan.steps if s.status == SKIPPED]
    cost = float(st.get("total_cost_usd", 0.0))
    started = st.get("started")
    elapsed = _fmt_dur(time.time() - started) if started else "?"
    ev, tot = plan.verification_coverage()

    lines = [
        "",
        "  ┌─────────────────────────────────────────────┐",
        "  │        ✓  TASK COMPLETE — VERIFIED            │",
        "  └─────────────────────────────────────────────┘",
        "",
        f"  Steps       {len(done)} done"
        + (f", {len(skipped)} descoped" if skipped else "") + f" of {len(plan.steps)}",
        f"  Verified    {ev}/{tot} acceptance criteria backed by evidence"
        + (f" ({round(100 * ev / tot)}%)" if tot else ""),
        f"  Cost        {_money(cost)}",
        f"  Elapsed     {elapsed}",
        f"  Branch      {st.get('branch', '?')}",
    ]
    if done:
        lines += ["", "  Shipped:"]
        for s in done:
            sha = (s.commit_sha or "")[:9]
            lines.append(f"    {sha}  {s.goal[:66]}")
    lines += [
        "",
        "  Every accepted step's checks + a full replay passed in a clean checkout.",
        "  Next:  loopd pr   ·   git diff   ·   loopd report",
        "",
    ]
    return "\n".join(lines)


# ---------------- one active reporter per process ----------------

_active: Optional[Reporter] = None


def active() -> Reporter:
    global _active
    if _active is None:
        _active = Reporter()
    return _active


def configure(stream=None, live: Optional[bool] = None) -> Reporter:
    global _active
    _active = Reporter(stream=stream, live=live)
    return _active
