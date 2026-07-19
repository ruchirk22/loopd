# Roadmap

Where loopd is headed after v0.1.0. This is a direction, not a promise — priorities shift
with what people actually need. Ideas and votes welcome in
[Discussions](https://github.com/ruchirk22/loopd/discussions).

## Shipped in v0.1.0
Execution loop · deterministic verification · Execution Forecast · Failure Analysis ·
engineering memory · GitHub integration · CLI + dashboard · budget/time caps · resumable state.

## Next
- **Packaging polish** — publish to PyPI, Homebrew formula, `loopd ui` auto-opening the browser.
- **Deeper GitHub** — respond to PR review comments, surface status checks, optional auto-merge,
  GitHub Enterprise support.
- **Smarter estimation & diagnosis** — regression-based forecasting fit on run history;
  similarity search over past runs to sharpen Failure Analysis.
- **Cross-run learning** — project-specific calibration and failure-pattern memory so loopd
  gets measurably better on a codebase over time.

## Later / exploring
- **Scale** — multiple concurrent runs and a fleet view.
- **More seams** — richer probes, custom verification recipes, team/shared workspaces.

Nothing here changes the core contract: loopd only ships changes it can prove.
