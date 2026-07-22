# FAQ

The questions people ask before trusting a tool with their codebase.

### What does loopd actually do?
It turns an objective into finished, verified, committed code. A planner breaks the work into
steps and reviews each result; a developer implements one step at a time; and a verification
layer — ordinary shell commands run by loopd, not the model — decides whether each step
passed. See [architecture.md](architecture.md).

### Does it send my code anywhere?
Only to Claude, through the **Claude Code CLI** you already installed and logged into — the
same place your interactive Claude Code sessions go. loopd adds no telemetry, no analytics,
and no other network calls. It's open source, so you can verify that.

### Does it store my API keys or tokens?
No. loopd reuses **Claude Code's** authentication and, optionally, the **GitHub CLI** (`gh`).
It never asks for, handles, or stores a key or token. (For CI it will honor an env credential
if you set one, but that's your choice.)

### How is the budget enforced?
There's a per-run USD cap, checked after every model call in `ledger.py`. When it's hit, the
run stops cleanly and is resumable — nothing is lost. loopd also *forecasts* the cost before
starting so you decide up front.

### Can I audit the verification layer?
Yes — it's `gates.py` and `probe.py`, and it's the whole point: a step is "done" only when its
`verify` commands exit 0, run by loopd outside the model. A step with no checks can't be
accepted. Acceptance also requires evidence quoted from the real diff/transcript.

### How do I know if I can trust a delivery?
Every run ends with a **Delivery Confidence** score (0–100, banded) — a deterministic, no-model-call
answer scored from ground truth: which acceptance criteria are backed by cited evidence, how much
scope was delivered, whether the passing gates prove *behavior* (not just units), whether the
pristine-checkout replay passed, churn, and integrity. The **High band (default 75%) is the bar**.
It's in `report.md`, the dashboard, the PR body, and `.agentic/confidence.json`; disable with
`CONFIDENCE_ENABLED=0`. Right after planning, loopd also shows the plan's confidence *ceiling* so an
under-verified plan is caught before the budget is spent. See
[usage](usage.md#delivery-confidence).

### Does it touch my main branch?
No. Each run works on an isolated `agentic/run-<timestamp>` branch with one commit per accepted
step. Uncommitted changes are left alone.

### Do I need GitHub?
No. GitHub is an optional enhancement. Everything works without it; when `gh` isn't connected,
loopd says so and carries on.

### Is it safe to let it run unattended?
The developer runs with permissions bypassed so it can work without prompts — which is why you
should run it inside the sandbox/container for anything you care about. See
[security.md](security.md) for the full model and its limits.

### What happens when it gets stuck?
It doesn't just stop — it explains the blocker (what happened, why, what it'd do, other
options) and waits for one decision from you. See Failure Analysis in
[usage.md](usage.md).

### Which models does it use?
Opus 4.8 for the planner and developer by default (configurable), and a cheap Haiku call for
the forecast. Set `PM_MODEL` / `DEV_MODEL` in `.env` to change them.
