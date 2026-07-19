# loopd CLI reference

The single source of truth for every `loopd` command. For a guided walkthrough see
[usage.md](usage.md); for the product thinking behind this experience see the CLI UX spec.

loopd is the execution layer around your coding agent: it **plans, forecasts, builds,
verifies, recovers, remembers, and delivers** engineering work. You mostly think about one
thing — *what you want built* — and run one command.

## Install the `loopd` command

loopd ships as a Python entrypoint (stdlib only — no pip install). Put it on your PATH:

```bash
# from the loopd checkout:
ln -s "$(pwd)/loopd" /usr/local/bin/loopd      # symlink (recommended)
# — or —
alias loopd="python3 /path/to/loopd/loopd"     # add to ~/.zshrc / ~/.bashrc
```

`python3 -m orchestrator …` and `./loopd …` are equivalent to `loopd …`.

## The mental model

- **The current directory is the project.** You almost never type or paste a path.
- **One hero command:** `loopd "<what to build>"`. Everything else is optional.
- **A project is a long-lived workspace** that accumulates run history, cost, forecast
  accuracy, engineering memory, and repository health. `loopd` (bare) is its home.
- **Ctrl-C is always safe.** Nothing is ever lost; `loopd resume` picks up exactly where it
  stopped.

## Build something

| Command | What it does |
|---|---|
| `loopd "<what to build>"` | Build it in the current project — forecast, decide, build, verify, deliver. |
| `loopd path/to/spec.md` | Build from a markdown spec file. |
| `loopd new "<idea>"` | Start a brand-new project from scratch in the current folder (sets up git). |
| `loopd clone <url> ["<task>"]` | Clone a repo, then optionally start building in it. |
| `loopd resume` | Continue the paused run. If loopd stopped with a blocker, it shows the diagnosis and lets you pick one option (`--yes` for the recommended, `--option <id>` for a specific one). |
| `loopd <github-issue>` | *(coming with GitHub Integration)* build straight from an issue. |

## Look in (all read-only, all optional)

| Command | What it does |
|---|---|
| `loopd` | The workspace home: this project's status, history, and "what do you want to build?" |
| `loopd status` | What's happening now, or how the last run went — including the blocker explanation if loopd is stuck. |
| `loopd plan` | The current plan as a checklist. |
| `loopd report` | The full write-up of the last run. |
| `loopd logs` | Recent activity. |
| `loopd memory` | What loopd has learned about this project across runs. |
| `loopd projects` | Your recent projects (aka `loopd history`). |
| `loopd ui` | Open the live dashboard in a browser. |
| `loopd config` | Show the effective settings and where things live. |
| `loopd help` | This command list. |
| `loopd version` | Print the loopd version. |

## Flags (on any build command)

| Flag | Effect |
|---|---|
| `--budget N` | Spend cap for this run, in USD (overrides the default). |
| `-y`, `--yes` | Accept the recommended budget without being asked. |
| `--force` | Proceed at the current budget (constrained if the estimate is short). |
| `--constrained` | Prioritize critical work and defer polish, regardless of budget. |
| `--no-forecast` | Skip the pre-run estimate and start immediately. |
| `--forecast-only` | Show the estimate and exit without building (`--json` for machine output). |
| `--resume` | Continue the paused run instead of starting fresh. |
| `--fresh` | Archive prior state and start over. |
| `--brief <file>` | Seed the run from a brief/spec file. |
| `--seed-session <id>` | Seed from an interactive Claude Code session. |
| `--final-verify "<cmd>"` | Add a whole-project check to final verification (repeatable). |
| `--repo <path>` | Work in another repo instead of the current directory (for scripts/CI). |
| `-q`, `--quiet` | Less chatter (drops the reassurance banner). |

## `loopd ui` flags

| Flag | Default | Effect |
|---|---|---|
| `--repo <path>` | current dir | Default project in the launch form. |
| `--host` | `127.0.0.1` | Bind host — keep it local. |
| `--port` | `8787` | Port. |
| `--budget` | `BUDGET_USD` or 25 | Default budget shown in the form. |

## Exit codes

`0` delivered · `1` stopped with a report (or declined at the forecast) · `2` setup / bad
input · `3` budget exhausted (resumable with `loopd resume`).

## Power-user one-liners

```bash
loopd "add OAuth with refresh tokens" --yes --budget 60      # decide nothing, just ship
loopd "tighten input validation" --forecast-only --json      # quote it for a script
loopd clone github.com/acme/api "add rate limiting" --yes    # url → working, one line
loopd resume                                                 # continue after a budget stop
```

## Environment & configuration

All defaults live in `.env` / environment variables ([configuration.md](configuration.md)).
The cross-project registry (recent projects) lives under `~/.loopd` (override with
`LOOPD_HOME`). Everything answerable by a flag has a sensible default, so a first run needs
no configuration at all.

> `run.py` remains the low-level engine entrypoint (`python3 run.py "<task>" --repo <path>`);
> `loopd` is the experience layer on top of it. New users should use `loopd`.
