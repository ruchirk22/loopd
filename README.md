# agentic-loop

A **self-hosted PM + Developer agentic loop** built on Claude Code's headless mode —
the *PM-Sovereign* design: ONE persistent PM session (Opus 4.8) plans, authors every
developer prompt verbatim, reviews every handover, and decides what happens next;
a Python orchestrator relays its directives under hard rails it alone controls.

```
your interactive Claude Code session
        │  /handoff  (or --seed-session <id> fork)
        ▼
.agentic/brief.md
        │
        ▼
┌─ PM session · claude-opus-4-8 · resumed every turn ────────────────────────┐
│  plan → author dev prompt → review handover → directive:                   │
│  accept | reject+feedback | replan | descope | task_complete | abort       │
└──────┬───────────────────────────────────────────────▲─────────────────────┘
       │ next_prompt (verbatim)                        │ handover packet:
       ▼                                               │  dev summary (schema'd)
┌─ Dev session · opus-4-8 · bypassPermissions ──────┐  │  + real git diff
│  implement → inner retry loop vs gates (no PM     │──┘  + gate transcript
│  turn until green or attempts exhausted)          │     + integrity flags
└───────────────────────────────────────────────────┘
       Python rails: gates run HERE · accept needs green gates + real diff +
       quoted evidence · task_complete ⇒ final verify + regression sweep in a
       PRISTINE worktree · budget/attempt/rejection/replan caps · resumable state
```

The PM's opinion alone can never mark anything done: when gates are red, `accept` is
not even in its response schema. And the gates alone never end the run: the PM judges
green-gated work against the acceptance criteria and can reject, replan, or descope.

## Layout

```
orchestrator/
  config.py     knobs: models (Opus 4.8 both), caps, budget, paths
  claude_cli.py subprocess wrapper around `claude -p --output-format json`
  pm.py         the PM session: directive schemas (dynamic!), payloads, evidence checks
  plan.py       the living plan: PM-authored mutations, validation, trivial-gate denylist
  developer.py  dev calls with schema-forced structured summaries
  gates.py      deterministic verification (setup/check/teardown; empty list = FAIL)
  handover.py   the handover packet: diff + gate transcript + integrity flags
  probe.py      deterministic probes: http, port, docker-build, env-file, proc-up
  ledger.py     durable resumable state, budget kill switch, git commits + worktrees
  seed.py       context seeding: brief.md from /handoff, --brief, --seed-session, or task
  loop.py       the control plane enforcing all of the above
prompts/        pm_system.md · dev_system.md
commands/       handoff.md — the /handoff slash command for your interactive sessions
run.py          entrypoint
Dockerfile      the sandbox (non-root — required for bypassPermissions)
tests/          unit + scripted end-to-end tests (stdlib unittest, no network)
```

## Prerequisites

- **Python 3.10+** (standard library only — no pip installs).
- **git** on PATH.
- **Claude Code CLI**: `npm install -g @anthropic-ai/claude-code` (verified on 2.1.205).
- Auth: `export ANTHROPIC_API_KEY=sk-ant-...` (billed as API usage), or
  `claude setup-token` → `export CLAUDE_CODE_OAUTH_TOKEN=...` to use a Pro/Max plan.

## Seeding: getting your context into the PM

You usually already explored the task in an interactive Claude Code session. Hand that
context over — don't retype it:

1. **`/handoff` (recommended).** Copy `commands/handoff.md` into the target repo's
   `.claude/commands/` (or `~/.claude/commands/`). In your interactive session type
   `/handoff` — it writes `.agentic/brief.md` (objective, verified repo facts, decisions
   with rationale, environment, gotchas, out-of-scope, definition of done). Review it,
   then launch the loop; the brief is picked up automatically.
2. **`--seed-session <session-id>`.** Forks your interactive session headlessly
   (`--fork-session`, the original is untouched) and has the fork distill the brief
   itself. Full conversation fidelity. Must run from the same directory the session was
   opened in; find ids via `claude --resume` (picker) or
   `ls -t ~/.claude/projects/<dir-slug>/*.jsonl`.
3. **`--brief file.md`** or a plain task string — for specs you already have.

## Run it

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python run.py --repo ../my-app --budget 25            # picks up .agentic/brief.md
python run.py "Add a /health endpoint with a passing test" --repo ../my-service
python run.py --resume-run --repo ../my-app           # continue an interrupted run
```

Exit codes: `0` verified done · `1` aborted (see `.agentic/escalation.json`) ·
`2` setup/plan failure · `3` budget exceeded (raise `--budget`, then `--resume-run`).

Because the developer runs with `--permission-mode bypassPermissions`, prefer the
container for anything real:

```bash
docker build -t agentic-loop .
docker run --rm -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \
  -v "$(pwd)/../my-app:/work" agentic-loop --budget 25
```

The container runs as a non-root user (Claude Code refuses `bypassPermissions` as
root) with git identity preconfigured. The mounted repo must be writable by uid 1001.

## How a step flows

1. **Dispatch.** The PM authors the developer's instructions verbatim (fresh session,
   or deliberately resuming the step's previous one).
2. **Inner loop.** The developer implements; the orchestrator runs the step's frozen
   `verify` commands. Red gates resume the same dev session with the transcript — up
   to `MAX_ATTEMPTS_PER_STEP` — without spending a PM turn.
3. **Handover.** The PM reviews the packet: the dev's structured summary, the real
   diff, the orchestrator's gate transcript, and integrity flags (no-op diff, tests
   touched, gate-target files touched — each must be addressed explicitly).
4. **Directive.** `accept` (commit — refused for no-op diffs or unverifiable evidence),
   `reject` (feedback to the same dev session, capped), `replan` (plan mutations,
   validated: unique ids, no unverifiable or trivially-true steps, done steps
   immutable), `descope` (skip with recorded impact), or `abort`.
5. **Finalize.** When no steps remain, `task_complete` runs the PM's `final_verify`
   (plus any `--final-verify` you demand) AND replays every accepted step's gates in a
   **pristine worktree** — a clean checkout of HEAD — so "done" means reproducible.
6. **Checkpoint.** Every ~8 reviews (or on handover-byte pressure) the PM writes a
   structured checkpoint and a fresh PM session takes over, seeded from
   brief + checkpoint + ledger digest. The same path powers `--resume-run` and
   recovery from lost sessions — resume is not a special case.

State (`state.json`, atomic writes), the event log (`log.jsonl`), handover packets,
discarded-work diffs, and escalation reports all live under `<repo>/.agentic/`, which
is excluded from the target repo's history. Each run works on its own git branch
(`agentic/run-<ts>`, `USE_RUN_BRANCH=0` to disable) with a commit per accepted step.

## Verification cookbook (deployment-shaped tasks)

For "does it actually deploy/boot/serve" checks, the PM composes deterministic probes
instead of fragile one-liners — see `orchestrator/probe.py`:

```bash
python3 -m orchestrator.probe http --url http://localhost:8080/health --expect-status 200 --expect-body ok
python3 -m orchestrator.probe docker-build --path . --tag check
python3 -m orchestrator.probe env-file --path .env.production --requires DATABASE_URL,GCS_BUCKET
python3 -m orchestrator.probe proc-up --start "npm run preview -- --port 4173" \
    --ready-port 4173 --then "python3 -m orchestrator.probe http --url http://localhost:4173 --expect-status 200"
```

Container-build or emulator gates inside the Docker sandbox need the docker CLI /
gcloud SDK added to the image and (for docker) a socket mount — a real blast-radius
tradeoff; keep it opt-in and never on a host holding real credentials.

## Configuration

All via env vars (see `.env.example`): `PM_MODEL` / `DEV_MODEL` (default
`claude-opus-4-8` — pinned, not the floating alias), `BUDGET_USD`,
`MAX_ATTEMPTS_PER_STEP`, `MAX_REJECTIONS_PER_STEP`, `MAX_REPLANS`,
`CHECKPOINT_EVERY_REVIEWS`, `HANDOVER_BYTES_CAP`, `MAX_WALL_CLOCK_MIN`, and more.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

No network, no Claude CLI needed: the end-to-end test drives the whole loop with a
scripted fake PM/dev.

## Safety notes

- **Sandbox is mandatory for real work.** `bypassPermissions` skips approval prompts;
  that is only acceptable inside the container / a throwaway worktree.
- **Gate commands come from an LLM** and run with `shell=True`. Fine inside the
  sandbox; never run an untrusted plan on your host.
- **Money:** Opus 4.8 on both agents is powerful and not cheap. The budget is checked
  after *every* CLI call (planning and seeding included) and a budget stop is
  resumable — start conservative and raise deliberately.
