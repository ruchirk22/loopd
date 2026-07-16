# agentic-loop

A **self-hosted PM + Developer agentic loop** built on Claude Code's headless mode.
This is the "you control both agents" design: the orchestration policy lives in *your*
code, and it drives two headless `claude -p` sessions — a **PM** that plans and a
**developer** that codes — with a deterministic verification gate closing the loop.

```
Task + spec
     │
     ▼
[ PM agent ]  ── headless claude -p · Opus ──▶ structured plan (schema'd steps + verify cmds)
     │
     ▼   for each step
┌─ sandbox ─────────────────────────────┐
│  [ Dev agent ] ── headless · Sonnet    │
│        │                               │
│        ▼                               │
│  [ Verification gate ]  tests·build·lint
└────────┬──────────────────────────────┘
         │ pass → git commit + ledger      fail → --resume same dev session (≤N), else stop
         ▼
    next step … → final report
```

You **reuse** the Claude Agent SDK runtime (the agent loop, context management, sessions,
tool permissions, cost metering). You **build** only the thin policy layer in `orchestrator/`.

## Layout

```
orchestrator/
  config.py       knobs: models, tool allowlists, caps, budget, paths
  claude_cli.py   subprocess wrapper around `claude -p --output-format json`
  planner.py      PM: task -> validated plan (via --json-schema)
  developer.py    Dev: run a step in the sandbox; --resume for retries
  gates.py        deterministic verification (shell checks; exit 0 == pass)
  ledger.py       durable state (.agentic/state.json + log.jsonl) + git commit per step
  loop.py         the control plane tying it together
prompts/
  pm_system.md    PM persona (appended to Claude Code's default)
  dev_system.md   developer persona
run.py            entrypoint
Dockerfile        the sandbox
```

## Prerequisites

- **Python 3.10+** (standard library only — no pip installs).
- **git** on PATH.
- **Claude Code CLI**: `npm install -g @anthropic-ai/claude-code` (needs Node 18+).
- Auth: `export ANTHROPIC_API_KEY=sk-ant-...`

## Run it (local)

Point it at a target repo. Start with something small and testable:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python run.py "Add a /health endpoint that returns {\"status\":\"ok\"} plus a passing test" \
  --repo ../my-service
```

Or read the task from a file: `python run.py @spec.md --repo ../my-service`.

Because the developer runs with `--permission-mode bypassPermissions`, prefer the
container below for anything real.

## Run it (sandbox — recommended)

```bash
docker build -t agentic-loop .
docker run --rm -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \
  -v "$(pwd)/../my-service:/work" agentic-loop \
  "Add a /health endpoint returning {\"status\":\"ok\"} with a passing test"
```

The developer's blast radius is the container. Nothing touches your host or your real
credentials.

## How the loop works

1. **Plan.** The PM reads the repo (read-only) and returns a schema-validated plan —
   ordered steps, each with `acceptance_criteria` and `verify` (shell commands). Validated
   JSON, so there's no prose to regex.
2. **Execute + verify.** For each step the developer implements the change in the sandbox,
   then the orchestrator runs the step's `verify` commands itself. Exit 0 on all = done.
3. **Retry or commit.** Pass → `git commit` (a rollback point + handoff unit) and move on.
   Fail → `--resume` the *same* developer session with the failure output, up to
   `MAX_ATTEMPTS_PER_STEP`.
4. **Control.** Every call's `total_cost_usd` is summed; the run aborts if it crosses
   `BUDGET_USD`. If a step never passes, the run stops with a report instead of pretending
   to succeed.

State and a JSONL event log live under `<repo>/.agentic/`, so a run is observable and its
progress is recorded.

## Configuration

All via env vars (see `.env.example`): `PM_MODEL`, `DEV_MODEL`, `DEV_ALLOWED_TOOLS`,
`PM_ALLOWED_TOOLS`, `MAX_ATTEMPTS_PER_STEP`, `MAX_TURNS_PER_CALL`, `BUDGET_USD`,
`DEV_PERMISSION_MODE`.

## Safety notes

- **Sandbox is mandatory for real work.** `bypassPermissions` skips approval prompts; that
  is only acceptable inside the container / a throwaway worktree.
- **Gate commands come from an LLM** and run with `shell=True`. Fine inside the sandbox;
  never run an untrusted plan on your host.
- **Billing:** headless `claude -p` / Agent-SDK usage is metered separately from
  interactive Claude Code. Keep `BUDGET_USD` conservative while tuning.

## Build it with Claude Code (bootstrap the rest)

This starter runs, but it's deliberately minimal. The fastest way to extend it is to let
Claude Code work on *this* repo — you'll be using Claude Code to build the thing that
drives Claude Code:

```bash
cd agentic-loop
claude   # then, interactively:
# "Add an escalation path in loop.py: when a step fails all attempts, call the PM to
#  re-plan the remaining steps instead of stopping. Keep the ledger accurate."
```

For bigger changes, spin up a team (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 claude`) or
define a `developer` subagent in `.claude/agents/`. The verification discipline is the
same at every level.

### Hardening TODO (good first tasks to hand to Claude Code)

- PM-driven **re-planning** on step failure (hook is marked in `loop.py`).
- Per-step **git branch/worktree** isolation so parallel steps don't collide.
- **Parallel** independent steps (the plan already implies a dependency order).
- Richer **observability** (stream-json for live progress; a run summary artifact).
- A **dry-run** mode using `workflow_dry_run`-style simulation before real edits.
- Structured **escalation report** when the loop stops (what failed, last diff, cost).
