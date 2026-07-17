#!/usr/bin/env python3
"""Benchmark harness: does the PM+Developer loop actually do better than a raw one-shot
agent? For each task it runs two ARMS in fresh directories and judges BOTH with the same
independent, agent-proof check:

  - baseline : `claude -p "<task>"` headless, bypassPermissions — the raw Claude Code
               agent with no PM, no gates, no loop.
  - loop     : `python run.py @spec --repo <dir>` — our full framework.

The task's own `check.py` (owned by the harness, never by the agent) is the ground truth
for success, so neither arm can pass by writing weak tests. Metrics per run:
success, cost_usd, wall_s, and — for the loop — steps done/skipped, attempts, replans.

Stdlib only. Example:

  # cheap first pass on Haiku, 3 reps each:
  python3 benchmarks/run_benchmark.py --model haiku --repeat 3 --budget 3
  # the real comparison on Opus 4.8:
  python3 benchmarks/run_benchmark.py --model claude-opus-4-8 --repeat 3 --budget 8
  python3 benchmarks/run_benchmark.py --tasks roman,fizzbuzz --arms loop --list
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
TASKS_DIR = ROOT / "tasks"
RESULTS_DIR = ROOT / "results"

sys.path.insert(0, str(REPO_ROOT))
from orchestrator.env import load_dotenv  # noqa: E402

load_dotenv()  # so both arms inherit ANTHROPIC_API_KEY from .env, no export needed


def discover_tasks() -> list[str]:
    return sorted(p.name for p in TASKS_DIR.iterdir()
                  if p.is_dir() and (p / "spec.md").exists() and (p / "check.py").exists())


def run_check(task: str, workdir: Path) -> tuple[bool, str]:
    """The independent objective check — ground truth for success."""
    check = TASKS_DIR / task / "check.py"
    try:
        p = subprocess.run([sys.executable, str(check), str(workdir)],
                           capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return False, "[check timed out]"
    out = (p.stdout + p.stderr).strip().splitlines()
    return p.returncode == 0, (out[-1] if out else "")


def run_baseline(task: str, workdir: Path, model: str, timeout_s: int,
                 max_turns: int) -> dict:
    spec = (TASKS_DIR / task / "spec.md").read_text()
    workdir.mkdir(parents=True, exist_ok=True)
    cmd = ["claude", "-p", spec, "--output-format", "json",
           "--permission-mode", "bypassPermissions", "--model", model,
           "--max-turns", str(max_turns)]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=str(workdir), capture_output=True, text=True,
                           errors="replace", timeout=timeout_s)
        wall = time.time() - t0
    except subprocess.TimeoutExpired:
        return {"cost_usd": 0.0, "wall_s": time.time() - t0, "extra": "", "note": "timeout"}
    cost, turns = 0.0, ""
    try:
        env = json.loads(p.stdout)
        if isinstance(env, list):
            env = next((x for x in reversed(env) if isinstance(x, dict)), {})
        cost = float(env.get("total_cost_usd") or 0.0)
        turns = str(env.get("num_turns", ""))
    except (json.JSONDecodeError, TypeError, ValueError):
        turns = "?"
    return {"cost_usd": cost, "wall_s": wall, "extra": f"turns={turns}",
            "note": "" if p.returncode == 0 else f"exit {p.returncode}"}


def run_loop(task: str, workdir: Path, model: str, timeout_s: int, budget: float) -> dict:
    spec = TASKS_DIR / task / "spec.md"
    workdir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PM_MODEL=model, DEV_MODEL=model)
    cmd = [sys.executable, str(REPO_ROOT / "run.py"), f"@{spec}",
           "--repo", str(workdir), "--budget", str(budget)]
    t0 = time.time()
    try:
        subprocess.run(cmd, cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
                       errors="replace", timeout=timeout_s)
        wall = time.time() - t0
        note = ""
    except subprocess.TimeoutExpired:
        wall = time.time() - t0
        note = "timeout"
    cost, extra = 0.0, ""
    state_path = workdir / ".agentic" / "state.json"
    if state_path.exists():
        try:
            st = json.loads(state_path.read_text())
            cost = float(st.get("total_cost_usd") or 0.0)
            steps = st.get("plan", {}).get("steps", []) if st.get("plan") else []
            done = sum(1 for s in steps if s.get("status") == "done")
            skipped = sum(1 for s in steps if s.get("status") == "skipped")
            attempts = sum(int(s.get("attempts", 0)) for s in steps)
            extra = (f"steps={done}/{len(steps)} skipped={skipped} "
                     f"attempts={attempts} replans={st.get('replans_used', 0)}")
        except (json.JSONDecodeError, OSError, AttributeError):
            extra = "state unreadable"
    return {"cost_usd": cost, "wall_s": wall, "extra": extra, "note": note}


def main() -> int:
    ap = argparse.ArgumentParser(description="Benchmark the loop vs a raw one-shot agent.")
    ap.add_argument("--tasks", default="", help="comma-separated task names (default: all)")
    ap.add_argument("--arms", default="baseline,loop", help="baseline,loop")
    ap.add_argument("--model", default=os.environ.get("BENCH_MODEL", "haiku"),
                    help="model alias/id for BOTH arms (e.g. haiku, claude-opus-4-8)")
    ap.add_argument("--repeat", type=int, default=1, help="reps per task/arm (agents are stochastic)")
    ap.add_argument("--budget", type=float, default=5.0, help="per-loop-run USD budget")
    ap.add_argument("--timeout", type=int, default=1200, help="per-run timeout (s)")
    ap.add_argument("--baseline-max-turns", type=int, default=60)
    ap.add_argument("--list", action="store_true", help="list tasks and exit")
    args = ap.parse_args()

    all_tasks = discover_tasks()
    if args.list:
        print("Tasks:", ", ".join(all_tasks) or "(none)")
        return 0
    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()] or all_tasks
    unknown = [t for t in tasks if t not in all_tasks]
    if unknown:
        print(f"Unknown task(s): {unknown}. Available: {all_tasks}", file=sys.stderr)
        return 2
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = RESULTS_DIR / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []

    for task in tasks:
        for arm in arms:
            for rep in range(1, args.repeat + 1):
                wd = out_dir / task / arm / f"rep{rep}"
                if wd.exists():
                    shutil.rmtree(wd)
                print(f"[{task}/{arm}/rep{rep}] running…", flush=True)
                if arm == "baseline":
                    m = run_baseline(task, wd, args.model, args.timeout, args.baseline_max_turns)
                else:
                    m = run_loop(task, wd, args.model, args.timeout, args.budget)
                ok, detail = run_check(task, wd)
                row = {"task": task, "arm": arm, "rep": rep, "success": ok,
                       "cost_usd": round(m["cost_usd"], 4), "wall_s": round(m["wall_s"], 1),
                       "extra": m["extra"], "note": m["note"], "check": detail}
                rows.append(row)
                print(f"    -> success={ok} cost=${row['cost_usd']} {row['wall_s']}s "
                      f"{m['extra']} {('['+m['note']+']') if m['note'] else ''}", flush=True)

    _write_outputs(out_dir, rows, arms, tasks, args.model)
    return 0


def _agg(rows, arm, key):
    vals = [r[key] for r in rows if r["arm"] == arm and isinstance(r[key], (int, float))]
    return statistics.mean(vals) if vals else 0.0


def _write_outputs(out_dir, rows, arms, tasks, model):
    csv_path = out_dir / "rows.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else
                           ["task", "arm", "rep", "success", "cost_usd", "wall_s", "extra", "note", "check"])
        w.writeheader()
        w.writerows(rows)

    lines = [f"# Benchmark results ({model})", ""]
    lines.append("## Summary by arm")
    lines.append("")
    lines.append("| arm | runs | success rate | mean cost | mean wall |")
    lines.append("|---|---|---|---|---|")
    for arm in arms:
        ar = [r for r in rows if r["arm"] == arm]
        n = len(ar)
        succ = sum(1 for r in ar if r["success"])
        rate = f"{succ}/{n} ({100 * succ / n:.0f}%)" if n else "-"
        lines.append(f"| {arm} | {n} | {rate} | ${_agg(rows, arm, 'cost_usd'):.4f} | {_agg(rows, arm, 'wall_s'):.0f}s |")
    lines += ["", "## By task", "", "| task | arm | success | mean cost | mean wall |",
              "|---|---|---|---|---|"]
    for task in tasks:
        for arm in arms:
            tr = [r for r in rows if r["task"] == task and r["arm"] == arm]
            if not tr:
                continue
            succ = sum(1 for r in tr if r["success"])
            mc = statistics.mean([r["cost_usd"] for r in tr])
            mw = statistics.mean([r["wall_s"] for r in tr])
            lines.append(f"| {task} | {arm} | {succ}/{len(tr)} | ${mc:.4f} | {mw:.0f}s |")
    lines += ["", "## Every run", "", "| task | arm | rep | success | cost | wall | detail | note |",
              "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['task']} | {r['arm']} | {r['rep']} | {'✓' if r['success'] else '✗'} "
                     f"| ${r['cost_usd']} | {r['wall_s']}s | {r['extra']} | {r['note']} |")
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n")

    print("\n" + "\n".join(lines))
    print(f"\nWrote {out_dir/'summary.md'} and {csv_path}")


if __name__ == "__main__":
    sys.exit(main())
