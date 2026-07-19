# Changelog

All notable changes to loopd are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and loopd uses
[semantic versioning](https://semver.org/).

## [0.1.0] — 2026-07-19

The first public release. loopd is an autonomous engineering runtime on Claude Code that
only ships changes it can prove.

### Added
- **Execution loop** — a persistent planner directs disposable developer sessions;
  deterministic shell gates decide what's actually done; one reviewable commit per accepted
  step on an isolated run branch; final verification replays every check in a clean checkout.
- **`loopd` CLI** — one hero command (`loopd "<task>"`), a project workspace model, and
  ambient verbs (`status`, `plan`, `report`, `logs`, `memory`, `projects`, `resume`, `ui`, …).
- **Mission Control dashboard** (`loopd ui`) — a calm, monochrome browser view: Projects,
  the live Project screen, and a Completion Report, with a "needs you" state for blockers.
- **Execution Forecast** — a cheap pre-run estimate of cost, runtime, steps, and risk, with a
  single budget decision; self-calibrates against actuals over time.
- **Failure Analysis** — when a run can't finish, loopd explains the blocker (what happened ·
  why · what it'd do · other options) and continues from your one-click choice.
- **Engineering memory** — durable per-project knowledge (decisions, past failures, TODOs)
  that the planner reads each run and updates.
- **GitHub integration** (optional, via the `gh` CLI) — build from an issue (`loopd #142`)
  and open a pull request with a written handover (`loopd pr`). loopd never handles tokens.
- **Budget & wall-clock caps**, **resumable/crash-safe state**, and **reports** on every
  terminal outcome.
- **pip packaging** — `pip install loopd`; stdlib-only, no dependencies.

[0.1.0]: https://github.com/ruchirk22/loopd/releases/tag/v0.1.0
