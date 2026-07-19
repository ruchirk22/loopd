# Changelog

All notable changes to loopd are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and loopd uses
[semantic versioning](https://semver.org/).

## [0.1.3] — 2026-07-20

### Changed
- README: replaced the static logo with an animated logo GIF as the landing hero (renders on
  GitHub and PyPI).

## [0.1.2] — 2026-07-19

### Fixed
- Dashboard: eliminated the periodic full-panel re-render that made the screen flicker every
  poll. The panel now re-renders only on real changes (step/phase/status); the elapsed and
  cost fields update in place, and a finished run's elapsed is frozen at its true duration.

### Changed
- Dashboard: larger, more legible type and components throughout, and a bigger full-color
  header logo.

## [0.1.1] — 2026-07-19

A polish release: documentation, packaging, and repository quality. No runtime behavior
changes — the engine is identical to 0.1.0.

### Changed
- Re-based all documentation on the `loopd` command (the guides previously showed the
  low-level `python -m orchestrator.run` engine form), converged every surface on a single
  product description, de-duplicated overlapping docs, and rewrote the README landing page.
- loopd's own baseline/snapshot commits in target repos are now authored as `loopd`.

### Added
- `SECURITY.md` (private vulnerability reporting), `CODE_OF_CONDUCT.md`, a `docs/` index, a
  `github-actions` Dependabot config, Python 3.10–3.13 trove classifiers, and `twine check`
  plus least-privilege permissions in CI.

### Removed
- Redundant root entry shims (`run.py`, `dashboard.py`, `loopd`) and an unused logo asset;
  fixed a broken `COPY` line in the Dockerfile.

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

[0.1.2]: https://github.com/ruchirk22/loopd/releases/tag/v0.1.2
[0.1.1]: https://github.com/ruchirk22/loopd/releases/tag/v0.1.1
[0.1.0]: https://github.com/ruchirk22/loopd/releases/tag/v0.1.0
