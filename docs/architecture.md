# Architecture

loopd drives two headless [Claude Code](https://docs.claude.com/en/docs/claude-code)
sessions from a Python control plane. The design is *PM-Sovereign*: a persistent planner
owns judgment; a disposable developer owns implementation; and the orchestrator owns the
rules neither agent can override.

## Components

| Module | Responsibility |
|---|---|
| `orchestrator/pm.py` | The persistent planner session. Plans, authors developer prompts, reviews handovers, emits schema-validated directives. |
| `orchestrator/developer.py` | Runs one step in a developer session and returns a structured summary. |
| `orchestrator/gates.py` | Runs a step's `verify` commands (each in its own process group). Exit 0 = pass; an empty list is a failure. |
| `orchestrator/probe.py` | Deterministic checks the planner composes into `verify`: `http`, `port`, `docker-build`, `env-file`, `proc-up`. |
| `orchestrator/plan.py` | The living plan: planner-authored mutations, validation, and a trivial-command screen. |
| `orchestrator/handover.py` | Builds the review packet (developer summary + real git diff + gate transcript + integrity flags). |
| `orchestrator/ledger.py` | Durable state, per-step git commits, run branch, budget enforcement, resume. |
| `orchestrator/seed.py` | Turns `/handoff`, `--brief`, `--seed-session`, or a task string into `.agentic/brief.md`. |
| `orchestrator/forecast.py` | Execution Forecast: one cheap model call sizes the work; a deterministic, calibrated estimator turns it into predicted cost/runtime/steps and a recommended budget. Learns from `.agentic/forecasts.jsonl`. |
| `orchestrator/architecture.py` | Architecture spine: binding per-project decisions (data model, contracts, tenancy/isolation strategy, invariants) the Architect proposes and every planner turn honors. Stored in `.agentic/architecture.md`. |
| `orchestrator/program.py` | Program orchestration (`loopd build`): decomposes a PRD into ordered epics and runs each as a full, governed loop, sharing the spine + memory. Resumable via `.agentic/program.json`. |
| `orchestrator/reporter.py` | The run's terminal surface: a live status line on a TTY, plain milestone lines off one, and the end-of-run handover. |
| `orchestrator/analysis.py` | Failure Analysis: turns a stop into a grounded explanation (summary, root cause, ranked options, recommendation) from the PM's abort directive, or a deterministic fallback. Persisted to `.agentic/analysis.json`; rendered identically by CLI and dashboard. |
| `orchestrator/github.py` | Optional GitHub enhancement via the `gh` CLI (never handles tokens): issues in (`gh issue view` → brief), PRs out (`gh pr create` with a handover body). Called only from the CLI/dashboard surface — the engine stays GitHub-agnostic. |
| `orchestrator/loop.py` | The control plane that ties it together and enforces every rule. |
| `orchestrator/memory.py` | Engineering memory: `.agentic/memory.md` the planner reads each run and updates at the end. |
| `orchestrator/dashboard.py` | Optional local web UI (stdlib `http.server`) to launch and watch runs; reads the same `.agentic/` files. |

## The run lifecycle

```
brief ─▶ architecture spine ─▶ forecast ─▶ user decision ─▶ plan ─▶ [ dispatch ─▶ developer ⇄
         (binding decisions,      (raise / constrain / abort)          gates (inner retries) ─▶
          owner-approved)                                              handover ─▶ review ] ─▶
                                            accept · reject · replan · descope   finalize ─▶ grade
                                                    · abort ◀──────────────────┘
```

The architecture spine and forecast run once, after the brief exists and before planning. On
`--resume-run` they're skipped — the saved spine (`.agentic/architecture.md`) and forecast (and
any constrained-mode choice) are honored instead.

**At program scale**, `loopd build <prd>` sits one level up: it decomposes the PRD into ordered
epics and runs the loop above *per epic*, with a governed owner checkpoint at each epic boundary,
sharing the spine and memory so the epics cohere. Progress is tracked in `.agentic/program.json`
(resumable with `loopd build --resume`).
At every terminal outcome the run is *graded*: actuals are diffed against the forecast and the
record is appended to `.agentic/forecasts.jsonl` to calibrate future estimates.

1. **Plan.** The planner (read-only tools), seeded with the brief and the project's
   engineering memory (`.agentic/memory.md`), produces steps, each with `acceptance_criteria`
   and `verify` commands. The plan is validated: unique ids, non-empty verify, and no
   trivially-true checks (`true`, bare `echo`, `ls`, `pytest || true`, …).
2. **Dispatch.** The planner writes the developer's prompt verbatim for the next step.
3. **Inner loop.** The developer implements; the orchestrator runs the step's gates. A red
   gate resumes the *same* developer session with the transcript, up to
   `MAX_ATTEMPTS_PER_STEP` — no planner turn is spent on gate churn.
4. **Review.** The planner receives a handover packet and returns a directive (below).
5. **Finalize.** With no steps left, the planner's `final_verify` commands *and* every
   accepted step's `verify` commands are replayed in a fresh `git worktree` checkout of
   HEAD. Both must pass for exit 0.

## Directives

Each planner turn returns one validated directive:

| Verdict | Effect |
|---|---|
| `accept` | Commit the step and continue. Offered **only when gates are green**; refused for a no-op diff. |
| `reject` | Send feedback to the developer and retry the step. |
| `replan` | Mutate the remaining plan (add / update / remove / reorder steps). |
| `descope` | Skip a step, recording the impact. |
| `task_complete` | Request finalization (triggers the pristine-checkout verification). |
| `abort` | Stop; the run can't continue. |

The verdict enum is built **dynamically per turn** — when the gates are red, `accept` and
`task_complete` are not in the schema the planner is allowed to answer with. A model
therefore cannot mark work done over a failing check; the impossibility is structural, not
a prompt instruction.

## The handover packet

Review is grounded in ground truth the agents can't fabricate:

- the developer's structured summary (self-reported),
- the **real `git diff`** since the last accepted commit,
- the **gate transcript** produced by the orchestrator,
- **integrity flags** — raised when the diff is a no-op, touches test files, or touches the
  files/config a gate command runs. Flagged accepts require an explicit acknowledgement.

## State, commits, and resume

- All state lives under `<repo>/.agentic/`: `state.json` (atomic writes), `log.jsonl`
  (event stream), `handovers/`, `escalation.json` (on failure), `report.md` (a
  human-readable end-of-run summary written on every outcome), `memory.md`
  (engineering memory), `architecture.md` (the binding architecture spine), `forecasts.jsonl`
  (predicted-vs-actual history), and `analysis.json` (the current Failure Analysis, cleared when
  the blocker is resolved). `memory.md`, `architecture.md`, and `forecasts.jsonl` persist across
  `--fresh`. It is excluded from the target repo's history.
- Each run works on an isolated `agentic/run-<timestamp>` branch, with one commit per
  accepted step — your main branch is never touched.
- `--resume-run` reloads `state.json` and continues at the first unfinished step. Budget
  stops and interrupts are resumable; a crash between committing and recording is
  reconciled on resume.

## Context management

The planner is one session resumed every turn. To keep its context bounded over long runs,
loopd periodically asks it for a structured checkpoint (decisions, open risks, remaining
plan) and reseeds a fresh planner session from the brief + checkpoint + a ledger digest.
The same path powers crash recovery.
