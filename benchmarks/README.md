# Benchmarks — does the loop actually do better?

Turns "I built a better agent loop" into numbers. Each task runs in two **arms**, in
fresh directories, judged by the **same independent check** the agent never sees:

- **`baseline`** — `claude -p "<task>"` headless with `bypassPermissions`: the raw Claude
  Code agent, no PM, no gates, no loop.
- **`loop`** — `python run.py @spec --repo <dir>`: the full PM+Developer framework.

Success is decided by each task's `check.py` (owned here, not by the agent), so neither
arm can pass by writing weak tests. Per run we record success, `cost_usd`, wall time, and
— for the loop — steps done/skipped, attempts, and replans.

## Run it

```bash
export ANTHROPIC_API_KEY=sk-ant-...     # or CLAUDE_CODE_OAUTH_TOKEN

# cheap first pass on Haiku (both arms), 3 reps per task:
python3 benchmarks/run_benchmark.py --model haiku --repeat 3 --budget 3

# the real comparison on Opus 4.8:
python3 benchmarks/run_benchmark.py --model claude-opus-4-8 --repeat 3 --budget 8

# subset / one arm / just list tasks:
python3 benchmarks/run_benchmark.py --tasks roman,primes --arms loop
python3 benchmarks/run_benchmark.py --list
```

Results (a `summary.md` table + `rows.csv` + each run's working dir) land under
`benchmarks/results/<timestamp>/` (git-ignored). Agents are stochastic — use `--repeat 3`
or more so success rates and costs mean something.

## Methodology notes

- **Same model, both arms** (`--model`) so you compare *structure*, not model tiers.
- **Independent ground truth.** `check.py` re-implements the acceptance test; the agent's
  own tests are irrelevant to scoring, which is the point — the loop's value is verified,
  gate-enforced correctness, so the benchmark must verify correctness the same way.
- **Fair baseline.** `claude -p` runs the full headless agent to completion (many turns);
  it's the honest "Claude Code, used autonomously" comparison, just without our scaffolding.
- **Cost** comes from the CLI JSON envelope (baseline) and `.agentic/state.json` (loop).

## Add a task

Create `benchmarks/tasks/<name>/` with:
- `spec.md` — the task brief (fed to both arms; passed to the loop via `@spec.md`).
- `check.py` — `python3 check.py <repo_dir>`, exit 0 iff the deliverable is correct.
  Make it independent of any tests the agent might write.

Validate your check against a known-good and known-bad implementation before trusting it
(see `selftest.py`).
