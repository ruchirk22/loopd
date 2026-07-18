# Usage

How to actually run loopd day to day. For the reference list of every flag and variable see
[configuration.md](configuration.md); for what happens under the hood see
[architecture.md](architecture.md).

## 1. Setup (once)

You need **Python 3.10+**, **git**, and the **Claude Code CLI**:

```bash
npm install -g @anthropic-ai/claude-code
git clone https://github.com/ruchirk22/loopd && cd loopd
cp .env.example .env
```

Open `.env` and uncomment **exactly one** auth line:

- `ANTHROPIC_API_KEY=sk-ant-...` — billed as API usage, or
- `CLAUDE_CODE_OAUTH_TOKEN=...` — from `claude setup-token`, to use a Claude Pro/Max plan.

> If both are set the API key wins, so leave the unused one commented — an unfilled
> `sk-ant-...` placeholder will break auth. `.env` is git-ignored; loopd loads it
> automatically, so you never `export` anything.

Sanity check: `claude -p "say hi"` should reply, not error.

## 2. Give loopd a task

There are four ways to hand over what you want built, in rough order of increasing context.

**A. Inline** — a one-liner:

```bash
python3 run.py "Add a /health endpoint returning {\"status\":\"ok\"} plus a passing test" \
  --repo ../my-service
```

**B. A spec file** — for anything longer than a sentence, write it in a file and pass `@`:

```bash
python3 run.py @spec.md --repo ../my-service
```

**C. `/handoff` (recommended for real work).** If you already explored the task in an
interactive Claude Code session, hand that context over instead of retyping it. Install the
command once, run it in your session, review the brief it writes, then launch:

```bash
cp commands/handoff.md ../my-app/.claude/commands/   # once per repo (or into ~/.claude/commands/)
# in an interactive Claude Code session opened on ../my-app, type:  /handoff
# → it writes ../my-app/.agentic/brief.md ; review/edit it, then:
python3 run.py --repo ../my-app --budget 25          # brief is picked up automatically
```

**D. `--seed-session <id>`.** Fork a live interactive session headlessly (the original is
untouched) and let the fork distill the brief itself — highest fidelity. Must run from the
same directory the session was opened in:

```bash
python3 run.py --seed-session <session-id> --repo ../my-app
# find the id via `claude --resume` (picker) or: ls -t ~/.claude/projects/<slug>/*.jsonl | head -1
```

`--repo` is always required. Point it at an **empty directory** to start a project from
scratch (loopd runs `git init` for you) or at an **existing repo** to build on.

Precedence when several sources are present: `--brief` and `--seed-session` always win.
Otherwise, on a fresh run inline/`@spec` task text is authoritative and (re)writes
`.agentic/brief.md` — so a leftover brief from a previous task can't silently override a new
one. On `--resume-run` the brief written at the start of that run is kept as-is.

## 3. The Execution Forecast

Before it starts building, loopd estimates the job — the way a senior engineer would size a
ticket before touching the keyboard. One **cheap** model call sizes the *engineering work*
(steps, complexity, risk, expected retries/replans, verification needs, confidence); a
**deterministic** estimator then turns that into a predicted cost, runtime, and a recommended
budget. The model never invents dollars or minutes — those come from named coefficients in
`orchestrator/forecast.py`, so the numbers are reproducible, not hallucinated.

```
Analyzing task…

  ┌─────────────────────────────────────────────┐
  │              EXECUTION FORECAST              │
  └─────────────────────────────────────────────┘

  Estimated Cost     $28.86
  Estimated Runtime  71 min
  Estimated Steps    11
  Confidence         83%   ██████████████████░░░░
  Risk               Medium

  Current Budget     $25.00
  Budget Gap        +$3.86  (short — see options below)
  Recommended        $40.00  (room for retries)
```

If the estimate exceeds your budget, loopd asks what to do (on a terminal):

```
  Increase budget to $40.00?   [Y] raise  ·  [C] continue anyway  ·  [E] edit budget  ·  [A] abort
```

- **Raise** (the default) runs at the recommended budget — enough headroom for retries.
- **Continue anyway** runs in **constrained mode**: the planner prioritizes the critical
  acceptance criteria, defers polish and optional refactors, and finishes the highest-value
  work first — so a budget stop still leaves a coherent result. It may stop before *every*
  criterion is met.
- **Edit** sets your own budget; **Abort** stops before spending.

Flags let you skip the prompt (CI, Docker, the dashboard):

```bash
python3 run.py "Build OAuth" --repo ../svc --forecast-only   # just estimate, don't run
python3 run.py "Build OAuth" --repo ../svc --forecast-only --json   # machine-readable
python3 run.py "Build OAuth" --repo ../svc --yes             # accept the recommended budget
python3 run.py "Build OAuth" --repo ../svc --force           # proceed at the current budget (constrained if short)
python3 run.py "Build OAuth" --repo ../svc --constrained     # force constrained planning
python3 run.py "Build OAuth" --repo ../svc --no-forecast     # skip the estimate entirely
```

Non-interactively (no TTY) and with no flag, loopd proceeds at the current budget in
constrained mode — it never silently raises your budget and never blocks automation.

**It learns.** Every finished run for which a forecast was produced appends a
predicted-vs-actual record to `.agentic/forecasts.jsonl` (which survives `--fresh`, like
project memory). The estimator
folds a calibration factor from that history back into future estimates, so its numbers get
truer for *your* project over time. After a run you see how it did:

```
  ┌─────────────────────────────────────────────┐
  │           EXECUTION FORECAST · ACTUAL        │
  └─────────────────────────────────────────────┘

                      Predicted     Actual
  Cost                $28.86        $25.97
  Runtime             71 min        78 min
  Steps               11            10

  Prediction Accuracy   90.5%   ████████████████████░░
```

The estimator is a swappable abstraction (`forecast.Estimator`): v1 is a configurable
weighted model; a future regression fit on `forecasts.jsonl` can drop in behind the same
interface without touching the execution pipeline. Tune it with `FORECAST_*` env vars
([configuration](configuration.md#execution-forecast)) or disable it with `--no-forecast` /
`FORECAST_ENABLED=0`.

## 4. Run it safely

The developer agent runs with permissions bypassed so it can work unattended. For anything
you care about, run inside the container so its file access is confined to it:

```bash
docker build -t loopd .
docker run --rm --env-file .env -v "$(pwd)/../my-app:/work" loopd --budget 25
```

Running `python3 run.py` directly is fine for a throwaway directory you don't mind the agent
editing. See [security.md](security.md) for the full sandbox model.

## 5. The web dashboard (browser UI)

Prefer a browser to the terminal? loopd ships a local dashboard that both **launches** runs
(a task box that takes long tasks — your `@file` as a textarea) and **watches** them live.

```bash
python3 dashboard.py --repo ../my-app        # opens on http://127.0.0.1:8787
```

From the page you can:

- **Start / resume / stop** — launch a new run (a task box that takes long tasks, your
  `@file`), resume an interrupted one, or stop the active run (state is saved, so it stays
  resumable) — all from the top bar.
- **Estimate first** — in the New Run modal, click *Estimate first* to see the Execution
  Forecast card (cost, runtime, steps, risk) before launching; a *Use $X* button applies the
  recommended budget. Tick *Constrained mode* to have the planner prioritize critical work.
  While a run is live and after it ends, the right column shows the forecast and, on
  completion, the predicted-vs-actual comparison with a prediction-accuracy score.
- **Watch it think** — a live status hero (elapsed, cost, retries, model, progress), an
  execution graph highlighting the active phase (planner → developer → verification →
  decision), the plan as step cards, a runtime timeline, the raw console, and the final
  report when it lands.
- **Inspect any step** — click a step card to open a drawer with its acceptance criteria,
  verify commands, developer summary, and the full handover packet (real diff + gate
  transcript).

It reads the same `.agentic/` files the CLI writes and refreshes about every 1.5s. It is a
**local tool** — it binds to `127.0.0.1` and spawns `run.py`, so don't expose it to a
network. Flags: `--repo`, `--budget`, `--host`, `--port` (see
[configuration.md](configuration.md#dashboard)).

## 6. Write a good brief (the highest-leverage thing you do)

loopd is only as good as what you ask for. Whether you write the brief by hand, via
`/handoff`, or in a `@spec.md`, cover:

- **Objective** — what must exist when this is done, in *testable* terms.
- **Constraints / decisions already made** — stack, conventions, "do not change X", with
  the rationale so the planner doesn't re-litigate them.
- **Environment** — target infra, emulators/local substitutes, and required secret/config
  **names** (never values).
- **Out of scope** — what it must not touch.
- **Definition of done** — a checklist of statements that must all hold, ideally ones a
  shell command could verify.

The `/handoff` command produces exactly this shape. The clearer the "definition of done",
the better the planner's `verify` commands — and those gates are what decide success.

## 7. Engineering memory

loopd keeps a small, structured record of what it has learned about your project at
`<repo>/.agentic/memory.md` — architecture decisions, past failures, and known TODOs:

```
## Architecture decisions
- Auth uses JWT
- No Redis (deployment restriction)
- Prefer Playwright over Cypress

## Past failures
- Docker image exceeded size limit

## Known TODOs
- Replace polling with websockets
```

Every run **reads** it (the planner honors the decisions, avoids the past failures, and
folds relevant TODOs into the plan) and **updates** it at the end — the planner records
durable knowledge on success, and a stopped run is recorded as a failure. It survives
`--fresh` (unlike a run's state) and is a plain file you can hand-edit or seed yourself.
Disable auto-updates with `UPDATE_MEMORY=0`.

## 8. While it runs

loopd prints live progress (`→ Step …`, `dev attempt …`, `gates: PASS`, `✓ accepted …`).
Everything is also written under `<repo>/.agentic/`:

| File | What it is |
|---|---|
| `state.json` | live plan + status + costs (atomic; the basis for resume) |
| `log.jsonl` | append-only event stream |
| `handovers/` | exactly what the planner reviewed each step |
| `report.md` | the end-of-run summary (written on success *and* failure) |
| `escalation.json` | why it stopped (only on a non-zero exit) |

You can **Ctrl-C** at any time — state is saved and the run is resumable.

## 9. After a run

Start with the report:

```bash
cat ../my-app/.agentic/report.md
```

It shows the outcome, per-step status/attempts/cost, the commits made, anything descoped,
and — on failure — why it stopped. Then inspect the work itself:

```bash
cd ../my-app
git log --oneline            # baseline + one commit per accepted step, on agentic/run-<ts>
git diff master              # everything the run changed
```

Each run works on its own `agentic/run-<timestamp>` branch, so your main branch is never
touched. To keep the work:

```bash
git checkout master && git merge agentic/run-<timestamp>
```

### Exit codes

| Code | Meaning | What to do |
|---|---|---|
| `0` | verified done | merge the run branch |
| `1` | stopped with a report | read `report.md` / `escalation.json`; fix the brief or repo, then `--resume-run` |
| `2` | setup / plan problem | fix the input (e.g. dirty tree, unusable brief) and re-run |
| `3` | budget exceeded | raise `--budget` and `--resume-run` — progress is kept |

## 10. Resume, retry, redo

```bash
python3 run.py --resume-run --repo ../my-app --budget 40   # continue where it stopped
python3 run.py --fresh      --repo ../my-app               # archive old state, start over
```

Budget stops and interrupts are always resumable — you never lose accepted steps.
A resume keeps the run's original budget unless you pass `--budget` again, so after a
budget stop (exit `3`) you must raise `--budget` to make headroom — resuming without it
just stops at the same cap.

## 11. Control cost

- Set a ceiling with `--budget` (or `BUDGET_USD` in `.env`); it's enforced after every model
  call, and a stop is resumable, so start conservative and raise deliberately.
- Choose models in `.env` (`PM_MODEL`, `DEV_MODEL`; default `claude-opus-4-8`). A cheaper
  model like `haiku` is fine for simpler tasks.
- The loop's overhead only pays off on multi-step work — for a trivial one-file change, a
  plain `claude -p` is cheaper.

## 12. Verify real-world projects

The planner composes deterministic checks into each step's `verify` commands, including
built-in probes for things unit tests can't cover:

```bash
python3 -m orchestrator.probe http --url http://localhost:8080/health --expect-status 200
python3 -m orchestrator.probe docker-build --path .
python3 -m orchestrator.probe env-file --path .env.production --requires DATABASE_URL,GCS_BUCKET
python3 -m orchestrator.probe proc-up --start "npm run preview -- --port 4173" \
    --ready-port 4173 --then "python3 -m orchestrator.probe http --url http://localhost:4173"
```

Full probe list and options: [configuration.md](configuration.md#verification-probes).

## 13. Troubleshooting

- **Everything fails instantly at $0 cost** → auth. Check `.env` has a working key/token and
  that a placeholder `sk-ant-...` isn't overriding your OAuth token.
- **"uncommitted changes … USE_RUN_BRANCH is off"** → commit/stash your work, or leave
  `USE_RUN_BRANCH=1` so loopd isolates the run on its own branch.
- **A step fails all attempts** → the planner will reject/replan/descope automatically; if
  the whole run stops, `report.md` and `escalation.json` say why. Often the fix is a clearer
  brief or a missing dependency in the target repo.
- **Model not available** → pick a model your account/plan supports via `PM_MODEL`/`DEV_MODEL`.

## See also

- [configuration.md](configuration.md) — every flag, env var, and seeding mode.
- [architecture.md](architecture.md) — how the loop and its guarantees work.
- [security.md](security.md) — the sandbox and the trust boundary.
