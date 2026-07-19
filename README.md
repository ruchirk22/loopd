# loopd

[![CI](https://github.com/ruchirk22/loopd/actions/workflows/ci.yml/badge.svg)](https://github.com/ruchirk22/loopd/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)

An autonomous coding loop that only ships changes it can prove. A persistent planner
directs disposable developer sessions, and every step is verified by deterministic checks
*outside* the model before it is committed.

Built on [Claude Code](https://docs.claude.com/en/docs/claude-code)'s headless mode.

<p align="center"><img src="assets/loopd_no_bg.png" width="360" alt="loopd"></p>

## Why loopd?

Coding agents are good at writing code and bad at knowing when they are done. Left
unattended, one will declare success on work that doesn't build, quietly weaken a test to
make it pass, or drift off the task. So you supervise it: read the plan, hand it steps, run
the tests yourself, review each diff, and decide what is actually finished.

loopd automates that supervision — without trusting the model to grade its own work.

- A **planner** breaks the task into small steps and reviews every result.
- A **developer** implements one step at a time.
- A **verification layer** — ordinary shell commands, run by the orchestrator, not the
  agent — decides whether a step actually passed.

A model can *propose* that something is done. It cannot *declare* it done. When the step's
checks are red, "accept" isn't even an option the planner is allowed to choose. The payoff
is an agent you can start and walk away from: it runs to completion, commits one reviewable
change per step, and stops with a clear report if it can't succeed — instead of confidently
handing back broken work.

## Architecture

```
your interactive Claude Code session
        │  /handoff  (or --seed-session <id> fork)
        ▼
.agentic/brief.md
        │
        ▼
Execution Forecast  →  cost/runtime/steps estimate  →  raise budget · run constrained · abort
        │
        ▼
┌─ Planner · persistent session · resumed every turn ────────────────────────┐
│  plan → author dev prompt → review handover → directive:                   │
│  accept | reject+feedback | replan | descope | task_complete | abort       │
└──────┬───────────────────────────────────────────────▲─────────────────────┘
       │ next_prompt (verbatim)                        │ handover packet:
       ▼                                               │  dev summary
┌─ Developer · disposable session · sandboxed ──────┐  │  + real git diff
│  implement → inner retry loop vs gates (no        │──┘  + gate transcript
│  planner turn until green or attempts exhausted)  │     + integrity flags
└───────────────────────────────────────────────────┘
       Orchestrator (Python) holds the rules: gates run HERE · accept needs green gates
       + a real diff + evidence · task_complete ⇒ final verify + regression sweep in a
       PRISTINE checkout · budget / attempt / replan caps · resumable state
```

- **Persistent planner.** One long-lived session that plans, writes every developer
  prompt, reviews every result, and decides what happens next.
- **Disposable developer sessions.** Each step is implemented by a fresh session whose only
  authority is to change code.
- **Deterministic verification.** A step's checks are plain commands run by the
  orchestrator. Exit 0, or it didn't happen.
- **The orchestrator holds the rules.** Budgets, retries, commits, and the "is it done?"
  decision live in code, never in a prompt.

## Design philosophy

- **Models propose; the harness disposes.** No agent can mark its own work complete.
- **Verification is deterministic and external.** Pass/fail is decided by commands with
  exit codes, not by a model's opinion.
- **"Done" means reproducibly verified.** A task is complete only after its final checks —
  and every accepted step's checks — pass in a clean, from-scratch checkout.
- **Every step is a reviewable commit.** The git log is the audit trail; each accepted step
  is one commit on an isolated run branch.
- **Bounded autonomy.** Budget, attempt, and replan caps mean failures re-plan or stop with
  a report — they don't hang or burn money silently.
- **Resumable.** State is durable; a crash or a budget stop continues with one flag.

## Key features

- Plan → implement → **verify** → commit loop that runs unattended to completion.
- **Execution Forecast** — before it builds, one cheap model call sizes the work and a
  deterministic, self-calibrating estimator predicts cost, runtime, steps, and risk, then asks
  whether to raise the budget or run constrained. It grades itself against actuals after every
  run and gets truer over time.
- A planner that stays in the loop: it reviews each result and re-plans on failure.
- **Failure Analysis** — when loopd genuinely can't finish, it explains the blocker like a
  senior engineer (what happened · why · what it'd do · other options) and continues from
  your one-click choice, in the CLI or the dashboard.
- **GitHub, if you want it** — build straight from an issue (`loopd #142`) and open a pull
  request with a written handover when a run succeeds (`loopd pr`). Uses your existing `gh`
  CLI login — loopd never touches your tokens — and is entirely optional.
- **Engineering memory** — a structured `.agentic/memory.md` (architecture decisions, past
  failures, TODOs) the planner reads every run and updates automatically.
- Deterministic gates the model can't bypass, plus built-in probes for real-world checks
  (HTTP, TCP ports, `docker build`, env files, "does the server actually boot").
- One git commit per accepted step on a throwaway run branch — your main branch is untouched.
- Per-run USD budget with mid-run enforcement; stops are resumable.
- Seed a run from an interactive Claude Code session (`/handoff`) or a spec file.
- Standard library only — no Python dependencies to install.

## Quick start

Requirements: **Python 3.10+**, **git**, and the **Claude Code CLI**
(`npm install -g @anthropic-ai/claude-code`).

```bash
git clone https://github.com/ruchirk22/loopd
cd loopd
cp .env.example .env        # then set ANTHROPIC_API_KEY (or CLAUDE_CODE_OAUTH_TOKEN)
ln -s "$(pwd)/loopd" /usr/local/bin/loopd     # put the `loopd` command on your PATH

cd ../my-service            # the current directory is your project
loopd "Add a /health endpoint that returns {\"status\":\"ok\"} plus a passing test"
```

The current directory is the project — you never paste a path. loopd forecasts the work,
takes one decision (the budget), builds it, verifies it in a clean checkout, and hands you a
committed change. Your token is read from `.env` automatically — no `export` needed.

## Basic usage

```bash
loopd                       # the workspace home: status, history, "what do you want to build?"
loopd "add rate limiting"   # build something
loopd spec.md               # build from a markdown spec
loopd resume                # continue a paused run (e.g. after a budget stop)
loopd status                # how the last run went
loopd ui                    # open the live dashboard in a browser
```

Every command lives in the [CLI reference](docs/cli.md). Ctrl-C is always safe — nothing is
lost, and `loopd resume` picks up exactly where it stopped.

**Continuing work you've already scoped.** If you explored the task in an interactive
Claude Code session, hand that context over instead of retyping it. Install the `/handoff`
command (`cp commands/handoff.md <repo>/.claude/commands/`), run `/handoff` in that session
to write `.agentic/brief.md`, review it, then launch — loopd picks the brief up
automatically:

```bash
cd ../my-app && loopd --budget 25
```

For real work, run inside the container so the developer's file access is confined to it
(see [docs/security.md](docs/security.md)):

```bash
docker build -t loopd .
docker run --rm --env-file .env -v "$(pwd)/../my-app:/work" loopd --budget 25
```

Prefer a browser? `loopd ui` opens a local dashboard to launch and watch runs live (see
[docs/usage.md](docs/usage.md#5-mission-control--the-dashboard-loopd-ui)).

## How a run works

1. **Plan.** The planner reads the repo and produces an ordered set of steps, each with
   acceptance criteria and `verify` commands (real checks — tests, builds, linters, probes).
2. **Dispatch.** The planner writes the developer's instructions for the next step.
3. **Implement + verify.** The developer makes the change; the orchestrator runs the step's
   `verify` commands. Red gates send the developer back with the transcript — up to a retry
   cap — without spending a planner turn.
4. **Review.** The planner sees a handover packet (the developer's summary, the real git
   diff, the gate transcript) and decides: **accept** (commit and continue), **reject**
   (send feedback), **replan**, **descope**, or **abort**. Accept is only offered when the
   gates are green.
5. **Finalize.** When no steps remain, the task's final checks *and* every accepted step's
   checks are replayed in a **pristine checkout**. Only then is the run complete.

Every run writes a human-readable `.agentic/report.md` (outcome, per-step status, cost,
time, commits) on completion *or* failure. That plus plan state, the event log, and the
handover packets all live under `<repo>/.agentic/`, and work is committed step-by-step on
an isolated `agentic/run-<timestamp>` branch.

Exit codes: `0` verified done · `1` stopped with a report · `2` setup/plan problem ·
`3` budget exceeded (resumable). A stopped run always leaves `.agentic/escalation.json`.

## Configuration

Copy `.env.example` to `.env` and set your token; that's the only required configuration.
Common knobs (all optional, via `.env` or environment):

| Variable | Default | Meaning |
|---|---|---|
| `PM_MODEL`, `DEV_MODEL` | `claude-opus-4-8` | models for the planner and developer |
| `BUDGET_USD` | `25` | per-run spend cap (also `--budget`) |
| `MAX_ATTEMPTS_PER_STEP` | `3` | developer↔gate retries per step |
| `MAX_REPLANS` | `3` | plan revisions before the run stops |

The full list of variables, CLI flags, and seeding options is in
[docs/configuration.md](docs/configuration.md).

## Documentation

- [docs/usage.md](docs/usage.md) — how to run loopd day to day.
- [docs/architecture.md](docs/architecture.md) — how the loop, directives, and verification
  fit together.
- [docs/configuration.md](docs/configuration.md) — every setting, flag, and seeding mode.
- [docs/security.md](docs/security.md) — the sandbox model and what the guarantees do and
  don't cover.
