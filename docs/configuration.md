# Configuration

The only required setting is your auth token. Everything else has a working default.

## Auth

Copy the template and fill it in — it's loaded automatically, no `export` needed:

```bash
cp .env.example .env
```

Set one of:

- `ANTHROPIC_API_KEY` — billed as API usage, or
- `CLAUDE_CODE_OAUTH_TOKEN` — from `claude setup-token`, to use a Claude Pro/Max plan.

`.env` is git-ignored. A real shell `export` overrides a value in `.env`.

## Seeding a run

How the planner gets its context (in order of fidelity):

| Mode | How |
|---|---|
| `/handoff` | In an interactive Claude Code session, run `/handoff` (install `commands/handoff.md` into the repo's `.claude/commands/`). It writes `.agentic/brief.md`, which loopd picks up automatically. |
| `--seed-session <id>` | Fork an interactive session headlessly (original untouched) and distill it into the brief. Must run from the session's directory. |
| `--brief <file>` | Use an existing brief/spec file. |
| task string / `@file` | A task described on the command line, or read from a file. |

## CLI flags (`run.py`)

| Flag | Meaning |
|---|---|
| `--repo <path>` | **Required.** The project the developer works in. |
| `--brief <path>` | Seed from a brief file. |
| `--seed-session <id>` | Seed by forking an interactive session. |
| `--resume-run` | Continue the interrupted run in `<repo>/.agentic/state.json`. |
| `--fresh` | Archive prior run state and start over. |
| `--budget <usd>` | Override `BUDGET_USD` for this run. |
| `--final-verify <cmd>` | Extra command required in final verification (repeatable). |

Exit codes: `0` verified done · `1` stopped with a report · `2` setup/plan failure ·
`3` budget exceeded (resumable). A non-zero stop writes `.agentic/escalation.json`.

## Environment variables

All optional; defaults shown.

### Models
| Variable | Default |
|---|---|
| `PM_MODEL` | `claude-opus-4-8` |
| `DEV_MODEL` | `claude-opus-4-8` |

### Loop control
| Variable | Default | Meaning |
|---|---|---|
| `BUDGET_USD` | `25` | per-run spend cap |
| `MAX_ATTEMPTS_PER_STEP` | `3` | developer↔gate retries per step |
| `MAX_REJECTIONS_PER_STEP` | `2` | planner rejections before it must replan/descope/abort |
| `MAX_REPLANS` | `3` | plan revisions (and failed finalizations) before stopping |
| `MAX_TURNS_PER_CALL` | `40` | agent turns per CLI call |
| `MAX_WALL_CLOCK_MIN` | `0` | wall-clock cap; `0` = none |
| `TIMEOUT_COST_USD` | `1.0` | budget charged when a CLI call times out |

### Context, handover, timeouts
| Variable | Default |
|---|---|
| `CHECKPOINT_EVERY_REVIEWS` | `8` |
| `HANDOVER_BYTES_CAP` | `150000` |
| `HANDOVER_DIFF_CAP` | `20000` |
| `GATE_LOG_TAIL` | `8000` |
| `CALL_TIMEOUT_S` | `3600` |
| `GATE_TIMEOUT_S` | `1800` |

### Tools & safety
| Variable | Default | Meaning |
|---|---|---|
| `DEV_ALLOWED_TOOLS` | `Read,Edit,Write,Bash,Glob,Grep` | developer tool allowlist |
| `PM_ALLOWED_TOOLS` | `Read,Glob,Grep` | planner is read-only |
| `DEV_PERMISSION_MODE` | `bypassPermissions` | only safe inside the sandbox — see [security.md](security.md) |
| `USE_RUN_BRANCH` | `1` | isolate each run on `agentic/run-<timestamp>` |

## Verification probes

The planner composes deterministic checks into `verify` commands (see
`python3 -m orchestrator.probe --help`):

```bash
python3 -m orchestrator.probe http --url http://localhost:8080/health --expect-status 200 --expect-body ok
python3 -m orchestrator.probe port --port 5432 --timeout 60
python3 -m orchestrator.probe docker-build --path . --tag check
python3 -m orchestrator.probe env-file --path .env.production --requires DATABASE_URL,GCS_BUCKET
python3 -m orchestrator.probe proc-up --start "npm run preview -- --port 4173" \
    --ready-port 4173 --then "python3 -m orchestrator.probe http --url http://localhost:4173 --expect-status 200"
```

A long-running check can carry its own timeout: `timeout=900;npm run build`.
