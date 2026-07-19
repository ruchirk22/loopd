# Installing loopd

One job: get `loopd` working. Should take about a minute.

## 1. Prerequisites

- **Python 3.10+**
- **git**
- **[Claude Code CLI](https://docs.claude.com/en/docs/claude-code)** — loopd drives it:
  ```bash
  npm install -g @anthropic-ai/claude-code
  ```
- *(optional)* **[GitHub CLI](https://cli.github.com)** (`gh`) — only if you want loopd to build
  from issues and open pull requests.

## 2. Install

```bash
pip install loopd
```

That's the whole install. loopd has **no Python dependencies** of its own.

## 3. Authenticate (nothing to configure)

loopd reuses **Claude Code's** login — it never asks for, handles, or stores API keys. If
you can run `claude`, loopd just works. If you're not signed in yet:

```bash
claude login
```

For CI or headless use, loopd will also honor an `ANTHROPIC_API_KEY` or
`CLAUDE_CODE_OAUTH_TOKEN` in the environment — but you never have to set one for normal use.

## 4. First run

```bash
cd your-project
loopd
```

The first launch walks you through a quick, one-time setup (it checks git, Claude Code, and
GitHub, and picks where loopd keeps its data). Then just tell it what to build:

```bash
loopd "add a /health endpoint with a passing test"
```

Prefer a browser? `loopd ui` opens the dashboard.

## From source (for contributors)

```bash
git clone https://github.com/ruchirk22/loopd
cd loopd
pip install -e .          # or run from the checkout: ./loopd …  /  python -m orchestrator.run …
python -m unittest discover -s tests   # the test suite (stdlib only, no network)
```

See [CONTRIBUTING.md](CONTRIBUTING.md) to hack on loopd, and the
[docs](README.md#documentation) for everything else.
