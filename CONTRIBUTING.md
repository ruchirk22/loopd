# Contributing to loopd

Thanks for being here. loopd is open source (MIT) because people are literally giving it
permission to modify their codebases — you should be able to read exactly what it does. That
same openness applies to contributions.

## The principles (please keep these intact)

These are the load-bearing ideas; PRs that break them will be hard to accept:

- **Verification is deterministic and external.** Pass/fail is decided by shell commands with
  exit codes, never a model's opinion. Don't move that judgment into a prompt.
- **The engine is agnostic.** `loop.py` and the run pipeline know nothing about GitHub,
  forecasting, or failure-analysis specifics — those live at the surface (`cli.py`,
  `dashboard.py`) or in their own modules. Keep it that way.
- **Stdlib only.** loopd has zero Python dependencies and we'd like to keep it that way.
  If you think you need a dependency, open an issue first.
- **Reuse existing auth.** loopd never stores tokens — it rides Claude Code (`claude`) and,
  optionally, the GitHub CLI (`gh`). Don't add credential handling.

## Getting set up

```bash
git clone https://github.com/ruchirk22/loopd
cd loopd
pip install -e .
python -m unittest discover -s tests    # all tests: stdlib only, no network, no API calls
```

Run loopd from the checkout with `./loopd …`, `python -m orchestrator.run …`, or the
installed `loopd` command.

## Making a change

1. **Open an issue** for anything non-trivial so we can agree on direction first.
2. **Keep PRs small and focused** — one concern per PR.
3. **Add tests.** The suite is fast and hermetic; new behavior needs coverage.
4. **Keep docs in sync.** If you change a flag, command, or behavior, update the relevant
   doc in the same PR (README / `docs/`).
5. **Match the surrounding style** — comments explain *why*, not *what*; prose over cleverness.

Run the suite before pushing; CI runs it on Python 3.10–3.13 and builds the package.

## Reporting bugs & security

- Bugs / ideas → [Issues](https://github.com/ruchirk22/loopd/issues).
- Security vulnerabilities → **please do not open a public issue**; see
  [docs/security.md](docs/security.md) for responsible disclosure.

## Being decent

Be kind and assume good faith. Harassment or hostility isn't welcome. Maintainers may edit,
label, or close contributions to keep the project healthy.
