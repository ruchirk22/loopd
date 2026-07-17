# Security & the sandbox

## The developer runs with permissions bypassed

To run unattended, the developer session uses `--permission-mode bypassPermissions` — it
edits files and runs commands without approval prompts. That is only acceptable when its
blast radius is contained.

**Use the container for real work.** The shipped `Dockerfile` runs as a non-root user
(Claude Code refuses `bypassPermissions` as root), with git identity preconfigured:

```bash
docker build -t loopd .
docker run --rm --env-file .env -v "$(pwd)/../my-app:/work" loopd --budget 25
```

Running `python3 run.py` directly is fine for throwaway directories you don't mind the
agent modifying; anything you care about should go through the container or a disposable
git worktree.

## What the orchestrator hardens

- **Gate commands run with `shell=True`** and come from the planner. Fine inside the
  sandbox; never run an untrusted plan on your host. Each gate runs in its own process
  group and is group-killed on timeout, so a check can't leak background processes.
- **Git runs with hooks disabled**, so a developer-planted `.git/hooks` script can't
  execute under the orchestrator (e.g. during the pristine-checkout verification).
- **Run isolation.** Each run works on its own `agentic/run-<timestamp>` branch with a
  commit per accepted step; your main branch is never touched.
- **Budget** is enforced after every model call (a timed-out call is charged an estimate,
  not $0), and a budget stop is resumable.

## The trust boundary (what the guarantees do and don't cover)

The verification spine is hardened, but the planner and developer are the *same model*, so
this is defense against sloppiness and drift — not a sandbox against a malicious planner.

- **Gate authorship is trusted.** The orchestrator rejects trivially-true `verify` commands
  and flags edits to gate-defining files, but a determined planner could still author a
  weak-but-non-trivial check. Spot-check `verify` lists on early runs; the final
  pristine-checkout verification plus the regression sweep is the backstop.
- **`.agentic/` is writable by the developer** (it lives inside the target repo for
  observability). During a run its state is held in memory and rewritten; a resume validates
  the state's schema, not its authenticity. If that matters to your threat model, run each
  step in a fresh container.
- **Evidence proves the planner cited real output, not that the code is correct.** Accept
  requires quotes grounded in the real diff / gate transcript, with every criterion covered
  — that stops blind rubber-stamping, but it can't prove a criterion is *semantically* met.
  Deterministic gates remain the arbiter of "done".

## Cost

The default models are capable and not free. The budget cap (`BUDGET_USD` / `--budget`) is
checked after every call and stops are resumable — start conservative and raise
deliberately. Use the [benchmarks](../benchmarks/README.md) to measure cost per task before
scaling up.
