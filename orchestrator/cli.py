"""The `loopd` command — the whole product, from the terminal.

One hero command (`loopd "what I want built"`), a workspace home (`loopd`), and a small set
of ambient verbs (status, plan, logs, report, memory, projects, resume, ui, new, clone). The
current directory is always the workspace; the user should almost never type a path.

This module is the experience layer. It speaks in outcomes and reassurance ("I've got it
from here"), takes responsibility instead of asking permission, and delegates the actual
engineering to the orchestrator engine (loop.run) underneath. See docs/cli.md for the full
command reference.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from . import analysis, github, loop, program, workspace
from . import forecast as _forecast
from .config import Config
from .env import load_dotenv

# Single source of truth is pyproject; read the installed package metadata so `loopd version`
# never drifts from the released version. Falls back for a source checkout that isn't installed.
try:
    from importlib.metadata import PackageNotFoundError, version as _pkg_version
    try:
        __version__ = _pkg_version("loopd")
    except PackageNotFoundError:
        __version__ = "0.2.0"
except Exception:
    __version__ = "0.2.0"

SUBCOMMANDS = {
    "ui", "status", "plan", "logs", "report", "memory", "projects", "history",
    "resume", "new", "build", "clone", "pr", "config", "help", "version",
}

# --------------------------------------------------------------- voice

_TTY = sys.stdout.isatty()


def _c(s: str, code: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _TTY else s


def _b(s: str) -> str:   return _c(s, "1")          # bold
def _dim(s: str) -> str: return _c(s, "2")          # muted
def _acc(s: str) -> str: return _c(s, "38;5;99")    # loopd accent
def _ok(s: str) -> str:  return _c(s, "38;5;42")    # green
def _warn(s: str) -> str: return _c(s, "38;5;214")  # amber


def say(msg: str = "") -> None:
    print(msg)


def _prompt(q: str) -> Optional[str]:
    """Ask a question only when there's a human at the keyboard; otherwise return None."""
    if not sys.stdin.isatty():
        return None
    try:
        return input(q).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


# --------------------------------------------------------------- environment / onboarding

def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _require_claude() -> bool:
    """loopd drives Claude Code; a build can't start without it. Friendly guidance, not a crash."""
    if _have("claude"):
        return True
    say(_warn("  loopd needs Claude Code to run.") + _dim("  It reuses your Claude login — no keys."))
    say(_dim("  Install: ") + "npm install -g @anthropic-ai/claude-code" + _dim("   then  ") + "claude login")
    return False


def _first_run_wizard() -> None:
    """One-time, friendly setup. Detects the environment, offers to connect GitHub, and gets
    out of the way. Auth is never asked for here — loopd rides your Claude Code login."""
    say()
    say(_b("  Welcome to loopd."))
    say(_dim("  Let's get you ready."))
    say()
    (say(_ok("  ✓ ") + "Git detected") if _have("git")
     else say(_warn("  ✗ ") + "Git not found — please install git"))
    (say(_ok("  ✓ ") + "Claude Code detected")
     if _have("claude") else
     say(_warn("  ✗ ") + "Claude Code not found  " + _dim("(npm install -g @anthropic-ai/claude-code, then `claude login`)")))
    gh_ready = github.available()["ok"]
    say(_ok("  ✓ ") + "GitHub connected" if gh_ready
        else _dim("  ○ ") + "GitHub not connected  " + _dim("(optional)"))
    say()

    if not gh_ready and _have("gh"):
        ans = _prompt("  Connect GitHub now? [Y/n] > ")
        if ans is None or ans.strip().lower() not in ("n", "no"):
            say(_dim("  Opening GitHub sign-in…"))
            try:
                subprocess.run(["gh", "auth", "login"])
            except (OSError, subprocess.SubprocessError):
                pass
            if github.available()["ok"]:
                say(_ok("  ✓ ") + "Connected")

    where = str(workspace.home())
    ans = _prompt(f"  Where should loopd keep its data?  [{where}]  > ")
    if ans and ans.strip() and str(Path(ans.strip()).expanduser()) != where:
        say(_dim("  To use that, set ") + f"LOOPD_HOME={ans.strip()}" + _dim(" in your shell — using ")
            + where + _dim(" for now."))

    workspace.mark_configured()
    say()
    say(_ok("  Done.") + _dim("  What do you want to build?"))
    say()


def _maybe_onboard() -> None:
    if workspace.is_configured() or not sys.stdin.isatty():
        return
    try:
        _first_run_wizard()
    except (EOFError, KeyboardInterrupt):
        workspace.mark_configured()  # never nag twice


# --------------------------------------------------------------- source detection

_ISSUE = re.compile(r"^#\d+$")


def _is_issue(s: str) -> bool:
    return bool(_ISSUE.match(s)) or (s.startswith("http") and "/issues/" in s)


def _is_repo_url(s: str) -> bool:
    if "/issues/" in s:
        return False
    return (s.startswith(("http://", "https://", "git@", "ssh://"))
            or s.startswith("github.com/") or s.endswith(".git"))


def _clone_name(url: str) -> str:
    tail = url.rstrip("/").split("/")[-1]
    return tail[:-4] if tail.endswith(".git") else tail


# --------------------------------------------------------------- config

def _add_run_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", help="work in this repo instead of the current directory")
    p.add_argument("--budget", type=float, default=None, help="spend cap for this run (USD)")
    p.add_argument("--brief", help="seed from a brief/spec file")
    p.add_argument("--seed-session", dest="seed_session", help="seed from an interactive session id")
    p.add_argument("--final-verify", action="append", default=[], dest="final_verify",
                   help="extra whole-project check (repeatable)")
    p.add_argument("--resume", action="store_true", help="continue the paused run here")
    p.add_argument("--fresh", action="store_true", help="archive prior state and start over")
    p.add_argument("-y", "--yes", action="store_true", dest="yes",
                   help="accept the recommended budget without asking")
    p.add_argument("--force", action="store_true", help="proceed at the current budget (constrained if short)")
    p.add_argument("--constrained", action="store_true", help="prioritize critical work, defer polish")
    p.add_argument("--no-forecast", action="store_true", dest="no_forecast", help="skip the pre-run estimate")
    p.add_argument("--forecast-only", action="store_true", dest="forecast_only",
                   help="show the estimate and exit without building")
    p.add_argument("--json", action="store_true", help="machine-readable output where applicable")
    p.add_argument("-q", "--quiet", action="store_true", dest="quiet", help="less chatter")
    p.add_argument("--pr", action="store_true", help="open a pull request after a successful run")
    p.add_argument("--no-pr", action="store_true", dest="no_pr", help="don't offer a pull request")


def _build_cfg(repo, args) -> Config:
    cfg = Config(
        repo=Path(repo),
        brief_path=Path(args.brief) if getattr(args, "brief", None) else None,
        seed_session=getattr(args, "seed_session", None),
        final_verify_extra=list(getattr(args, "final_verify", []) or []),
        budget_explicit=getattr(args, "budget", None) is not None,
        no_forecast=getattr(args, "no_forecast", False),
        assume_yes=getattr(args, "yes", False),
        force=getattr(args, "force", False),
        forecast_only=getattr(args, "forecast_only", False),
        constrained=getattr(args, "constrained", False),
    )
    if getattr(args, "no_forecast", False):
        cfg.forecast_enabled = False
    if getattr(args, "budget", None) is not None:
        cfg.budget_usd = args.budget
    return cfg


# --------------------------------------------------------------- reassurance / bookends

def _reassure() -> None:
    say()
    say(_b("  I've got it from here."))
    say(_dim("  Check back any time with ") + "`loopd status`" + _dim(" — or watch live with ") + "`loopd ui`.")
    say(_dim("  Close your laptop or press Ctrl-C whenever you like. Nothing is lost."))
    say()


def _fmt_health(h: dict) -> str:
    if not h.get("is_repo"):
        return "new folder"
    if h.get("dirty"):
        n = h["dirty_count"]
        return f"{h['branch']} · {n} uncommitted"
    return f"{h['branch']} · clean"


def _open_line(repo) -> None:
    """A one-line 'you're back in a workspace' note — only for projects with history."""
    s = workspace.summary(repo)
    if not s["runs"]:
        return
    bits = [f"{s['runs']} run" + ("s" if s["runs"] != 1 else "")]
    if s["forecast_accuracy"] is not None:
        bits.append(f"forecasts ~{s['forecast_accuracy']:.0f}% accurate")
    if s["memory_count"]:
        bits.append(f"remembers {s['memory_count']}")
    say(_dim("Opening ") + _b(s["name"]) + _dim("  ·  " + "  ·  ".join(bits)))


def _close_line(repo, code: int) -> None:
    st = workspace.run_state(repo)
    cost = st.get("cost_usd", 0.0) if st.get("exists") else 0.0
    workspace.record_run(repo, code, cost)
    if code == 0:
        s = workspace.summary(repo)
        say(_dim(f"Added to {s['name']}'s history — {s['runs']} run"
                 + ("s" if s["runs"] != 1 else "") + f" · ${s['lifetime_cost_usd']:.2f} lifetime."))
    elif code != 3:  # budget stops already print their own resume hint
        say(_dim("Stopped — see ") + "`loopd report`" + _dim(" for the details."))


# --------------------------------------------------------------- the hero command

def cmd_run(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog="loopd", add_help=True,
                                description="Build something. Run this inside your project.")
    p.add_argument("words", nargs="*", help='what to build, e.g. loopd "add a /health endpoint"')
    _add_run_flags(p)
    args = p.parse_args(argv)

    words = args.words
    repo = Path(args.repo).expanduser().resolve() if args.repo else Path.cwd()
    task: Optional[str] = None
    head = words[0] if words else None
    from_issue = False

    if head is not None:
        if _is_issue(head):
            if not _resolve_issue(repo, head):   # fetches the issue → writes the brief
                return 2
            from_issue = True
        elif _is_repo_url(head):
            return cmd_clone([head] + words[1:] + _passthrough(args))
        else:
            maybe = Path(head).expanduser()
            if len(words) == 1 and maybe.is_file():
                args.brief = str(maybe)          # a spec file drives the run
            else:
                task = " ".join(words)

    if (task is None and not args.brief and not args.seed_session and not args.resume
            and not from_issue):
        return cmd_home([])                  # nothing to build → the workspace home

    if not _require_claude():
        return 2

    cfg = _build_cfg(repo, args)
    if from_issue and not cfg.brief_path:
        cfg.brief_path = cfg.state_dir / "brief.md"   # the issue brief drives the run
    workspace.register(repo)
    _open_line(repo)

    if args.forecast_only:
        return _forecast_only_flow(task, cfg, args.json)

    reassure = not args.quiet
    code = loop.run(task, cfg, resume=args.resume, fresh=args.fresh,
                    on_start=(_reassure if reassure else None))
    _close_line(repo, code)
    if code == 0:
        _offer_pr(repo, cfg, args)
    return code


# --------------------------------------------------------------- GitHub (surface only)

def _resolve_issue(repo, ref: str) -> bool:
    av = github.available()
    if not av["ok"]:
        say(_warn("  GitHub isn't connected yet.") + _dim("  " + av["hint"]))
        return False
    issue = github.fetch_issue(repo, ref)
    if not issue:
        say(_warn("  I couldn't read that issue.") + _dim(" Check the number/URL and your access."))
        return False
    github.write_issue_context(repo, issue)
    say(_dim("Building from issue ") + _b(f"#{issue['number']}") + _dim(" — ") + issue["title"])
    return True


def _recent_decisions(repo) -> List[str]:
    from . import memory
    try:
        return memory.load(repo).get(memory.DECISIONS, [])[-6:]
    except Exception:
        return []


def _offer_pr(repo, cfg: Config, args) -> None:
    """After a successful run, offer a PR — never automatic, always one confirmation
    (unless --pr / GITHUB_AUTO_PR explicitly opts in)."""
    if getattr(args, "no_pr", False) or not cfg.github_enabled:
        return
    if not github.available()["ok"] or not github.has_remote(repo):
        return
    auto = getattr(args, "pr", False) or cfg.github_auto_pr
    if not auto:
        if not sys.stdin.isatty():
            say(_dim("  Open a PR when you're ready with ") + "`loopd pr`" + _dim("."))
            return
        ans = _prompt("  Open a pull request? [Y/n] > ")
        if ans is not None and ans.strip().lower() in ("n", "no"):
            return
    _open_pr(repo, cfg)


def _open_pr(repo, cfg: Config) -> None:
    payload = github.assemble_pr(repo, decisions=_recent_decisions(repo))
    if not payload:
        say(_dim("  There's no completed run here to open a PR from."))
        return
    say(_dim("  Opening a pull request…"))
    r = github.open_pr(repo, payload["branch"], cfg.github_pr_base or payload["base"],
                       payload["title"], payload["body"], draft=cfg.github_pr_draft)
    if r.get("ok"):
        say(_ok("  ✓ ") + ("PR already open: " if r.get("existing") else "Pull request opened: ")
            + r["url"])
    else:
        say(_warn("  Couldn't open the PR: ") + _dim(r.get("error", "")))


def cmd_pr(argv: List[str]) -> int:
    repo = _repo_arg(argv)
    cfg = Config(repo=repo)
    av = github.available()
    if not av["ok"]:
        say(_warn("  GitHub isn't connected.") + _dim("  " + av["hint"]))
        return 2
    if not github.has_remote(repo):
        say(_dim("  This project has no git remote to open a pull request against."))
        return 2
    _open_pr(repo, cfg)
    return 0


def _passthrough(args) -> List[str]:
    """Re-emit the run flags so clone-then-run carries them through."""
    out: List[str] = []
    if args.budget is not None: out += ["--budget", str(args.budget)]
    if args.yes: out += ["--yes"]
    if args.force: out += ["--force"]
    if args.constrained: out += ["--constrained"]
    if args.no_forecast: out += ["--no-forecast"]
    if args.quiet: out += ["--quiet"]
    return out


def _forecast_only_flow(task, cfg: Config, as_json: bool) -> int:
    import json as _json
    cfg.forecast_enabled = True
    brief = _forecast.resolve_brief(cfg, task)
    if brief is None:
        say("Nothing to estimate — tell me what to build, or point me at a spec file.")
        return 2
    fc = _forecast.run_forecast(cfg, brief, cfg.budget_usd, ledger=None)
    if fc is None:
        say("I couldn't produce an estimate just now.")
        return 2
    say(_json.dumps(fc.to_dict(), indent=2) if as_json else _forecast.render_card(fc))
    return 0


# --------------------------------------------------------------- workspace home & picker

def cmd_home(argv: List[str]) -> int:
    repo = Path.cwd()
    s = workspace.summary(repo)
    rs = s["run_state"]
    is_project = s["health"]["is_repo"] or (repo / ".agentic").exists()

    # A paused run always leads.
    if rs.get("exists") and rs.get("paused"):
        workspace.register(repo)
        say(_b(f"  {s['name']}") + _dim(f"  ·  paused run"))
        say(f"  “{rs['task']}” — {rs['steps_done']} of {rs['steps_total']} done, "
            f"${rs['cost_usd']:.2f} spent.")
        ans = _prompt("  Resume it? [Y] · or start something new [n]  > ")
        if ans is None:
            say(_dim("  Resume with ") + "`loopd resume`" + _dim(", or ")
                + 'loopd "<something new>"' + _dim("."))
            return 0
        if ans.lower() in ("", "y", "yes"):
            return cmd_resume([])
        newtask = _prompt("  What do you want to build?  > ")
        return cmd_run([newtask]) if newtask else 0

    if is_project:
        _print_workspace_header(s)
        newtask = _prompt("  What do you want to build?  > ")
        return cmd_run([newtask]) if newtask else 0

    return _picker()


def _print_workspace_header(s: dict) -> None:
    say(_b(f"  {s['name']}") + _dim(f"  ·  {_fmt_health(s['health'])}"))
    line = []
    if s["runs"]:
        line.append(f"{s['runs']} run" + ("s" if s["runs"] != 1 else ""))
        line.append(f"${s['lifetime_cost_usd']:.2f} lifetime")
    if s["forecast_accuracy"] is not None:
        line.append(f"forecasts ~{s['forecast_accuracy']:.0f}% accurate")
    if s["memory_count"]:
        line.append(f"remembers {s['memory_count']} thing" + ("s" if s["memory_count"] != 1 else ""))
    if line:
        say(_dim("  " + "  ·  ".join(line)))
    say()


def _picker() -> int:
    recents = workspace.recent()
    if not sys.stdin.isatty():
        say(_b("loopd") + _dim(" — no project here yet."))
        if recents:
            say(_dim("Recent projects:"))
            for e in recents:
                say(f"  · {e['name']}  ({e['path']})")
        say(_dim('Start one with ') + 'loopd "<what to build>"' + _dim(", or ")
            + "loopd clone <url>" + _dim(", or ") + 'loopd new "<idea>"' + _dim("."))
        return 0

    say(_b("  Where should I work?"))
    say("  1  Here — set up this folder as a project")
    say("  2  Clone from a URL")
    say("  3  Create something new")
    for i, e in enumerate(recents, start=4):
        say(f"  {i}  {e['name']}  " + _dim(f"({_fmt_health(workspace.health(e['path']))})"))
    choice = _prompt("  > ")
    if not choice:
        return 0
    if choice == "1":
        t = _prompt("  What do you want to build here?  > ")
        return cmd_run([t]) if t else 0
    if choice == "2":
        url = _prompt("  Repo URL  > ")
        return cmd_clone([url]) if url else 0
    if choice == "3":
        idea = _prompt("  Describe the new project  > ")
        return cmd_new([idea]) if idea else 0
    if choice.isdigit() and 4 <= int(choice) < 4 + len(recents):
        e = recents[int(choice) - 4]
        t = _prompt(f"  What do you want to build in {e['name']}?  > ")
        return cmd_run([t, "--repo", e["path"]]) if t else 0
    say(_dim("  Didn't recognize that."))
    return 0


# --------------------------------------------------------------- ambient verbs

def cmd_ui(argv: List[str]) -> int:
    from . import dashboard
    p = argparse.ArgumentParser(prog="loopd ui", description="Open the loopd dashboard.")
    p.add_argument("--repo", default=str(Path.cwd()))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--budget", type=float, default=None)
    args = p.parse_args(argv)
    budget = args.budget if args.budget is not None else Config(repo=Path(args.repo)).budget_usd
    name = Path(args.repo).expanduser().resolve().name
    say(_acc("  loopd ui") + _dim(f"  ·  {name}  ·  ") + f"http://{args.host}:{args.port}"
        + _dim("  (local only)"))
    say(_dim("  Ctrl-C to close."))
    try:
        dashboard.serve(args.host, args.port, args.repo, budget)
    except KeyboardInterrupt:
        say(_dim("\n  Closed."))
    return 0


def cmd_status(argv: List[str]) -> int:
    repo = _repo_arg(argv)
    s = workspace.summary(repo)
    rs = s["run_state"]
    if not rs.get("exists"):
        say(_dim(f"No runs yet in {s['name']}. Start one with ") + 'loopd "<what to build>"' + _dim("."))
        return 0
    _print_workspace_header(s)
    if rs.get("finished"):
        say(_ok("  ✓ Last run complete") + _dim(f"  ·  {rs['steps_done']}/{rs['steps_total']} steps · ${rs['cost_usd']:.2f}"))
        say(_dim("  Full write-up: ") + "`loopd report`")
    else:
        fa = analysis.load(repo)
        if fa:
            say(analysis.render(fa))   # the blocker, explained — same content the dashboard shows
        else:
            say(_warn("  ▸ Run in progress / paused") + _dim(f"  ·  “{rs['task']}”"))
            say(_dim(f"    {rs['steps_done']} of {rs['steps_total']} done · ${rs['cost_usd']:.2f} spent"))
            say(_dim("  Resume with ") + "`loopd resume`" + _dim("  ·  watch with ") + "`loopd ui`")
    return 0


def cmd_plan(argv: List[str]) -> int:
    import json as _json
    repo = _repo_arg(argv)
    sp = Path(repo) / ".agentic" / "state.json"
    if not sp.is_file():
        say(_dim("No plan yet — I plan once you give me a task."))
        return 0
    try:
        plan = (_json.loads(sp.read_text()).get("plan") or {})
    except (OSError, _json.JSONDecodeError):
        say(_dim("No readable plan yet."))
        return 0
    steps = plan.get("steps", [])
    if plan.get("summary"):
        say(_b("  " + plan["summary"]))
    if not steps:
        say(_dim("  (no steps yet)"))
        return 0
    marks = {"done": _ok("✓"), "skipped": _dim("–"), "in_progress": _acc("▸")}
    for i, st in enumerate(steps, 1):
        mark = marks.get(st.get("status"), _dim("·"))
        say(f"  {mark} {i:>2}. {st.get('goal', st.get('id',''))}")
    return 0


def cmd_logs(argv: List[str]) -> int:
    import json as _json
    repo = _repo_arg(argv)
    lp = Path(repo) / ".agentic" / "log.jsonl"
    if not lp.is_file():
        say(_dim("No activity logged yet."))
        return 0
    try:
        lines = lp.read_text(errors="replace").splitlines()[-40:]
    except OSError:
        return 0
    for line in lines:
        try:
            e = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        say(_dim(f"  {e.get('event','?')}") + (f"  {e.get('label','')}" if e.get("label") else ""))
    say(_dim("  (full detail with ") + "`loopd ui`" + _dim(")"))
    return 0


def cmd_report(argv: List[str]) -> int:
    repo = _repo_arg(argv)
    rp = Path(repo) / ".agentic" / "report.md"
    if not rp.is_file():
        say(_dim("No report yet — I write one at the end of every run."))
        return 0
    say(rp.read_text(errors="replace"))
    return 0


def cmd_memory(argv: List[str]) -> int:
    repo = _repo_arg(argv)
    mp = Path(repo) / ".agentic" / "memory.md"
    if not mp.is_file():
        say(_dim("I haven't learned anything durable about this project yet."))
        return 0
    say(mp.read_text(errors="replace"))
    return 0


def cmd_projects(argv: List[str]) -> int:
    recents = workspace.recent(limit=20)
    if not recents:
        say(_dim("No projects yet. Your first ") + 'loopd "<task>"' + _dim(" starts one."))
        return 0
    say(_b("  Recent projects"))
    for e in recents:
        h = workspace.health(e["path"])
        outcome = {0: _ok("last run passed"), None: _dim("no runs yet")}.get(
            e.get("last_code"), _warn("last run stopped"))
        say(f"  · {_b(e['name']):<24} " + _dim(f"{_fmt_health(h)}  ·  {e.get('runs',0)} run(s)  ·  ")
            + outcome)
    return 0


def cmd_resume(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog="loopd resume", add_help=True)
    p.add_argument("--repo")
    p.add_argument("-y", "--yes", "--fix", action="store_true", dest="yes",
                   help="apply the recommended option without asking")
    p.add_argument("--option", help="apply a specific option by id")
    p.add_argument("--budget", type=float, default=None)
    args = p.parse_args(argv)
    repo = Path(args.repo).expanduser().resolve() if args.repo else Path.cwd()
    if not _require_claude():
        return 2

    # If loopd stopped with a blocker, show the explanation and let the owner pick ONE option.
    # We never auto-apply — even the recommended path takes one explicit confirmation.
    fa = analysis.load(repo)
    choice = None
    if fa:
        say(analysis.render(fa))
        opt = None
        if args.option:
            opt = fa.option(args.option)
        elif args.yes:
            opt = fa.recommended
        elif sys.stdin.isatty():
            opt = _resume_pick(fa)
        # non-interactive with no flag → a plain resume (re-attempt), which is itself explicit.
        if opt is not None:
            if opt.kind == "abort":
                say(_dim("  Leaving it here — the work so far is committed."))
                return 0
            choice = analysis.resolve_choice(repo, option_id=opt.id)
            say(_dim("  Continuing: ") + opt.label)

    cfg = _build_cfg(repo, args)
    workspace.register(repo)
    _open_line(repo)
    code = loop.run(None, cfg, resume=True, on_start=_reassure, resume_choice=choice)
    _close_line(repo, code)
    return code


def _resume_pick(fa):
    """Interactive one-confirmation pick. Enter accepts the recommended option."""
    ordered = [fa.recommended] + [o for o in fa.options if o is not fa.recommended]
    say("")
    for i, o in enumerate(ordered, 1):
        tag = _dim("  (recommended)") if o.recommended else ""
        say(f"  {i}  {o.label}{tag}")
    ans = _prompt("  Which? [1] > ")
    if ans is None:
        return fa.recommended
    ans = ans.strip()
    if ans == "":
        return fa.recommended
    if ans.isdigit() and 1 <= int(ans) <= len(ordered):
        return ordered[int(ans) - 1]
    return fa.recommended


def cmd_new(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog="loopd new")
    p.add_argument("words", nargs="*")
    _add_run_flags(p)
    args = p.parse_args(argv)
    repo = Path(args.repo).expanduser().resolve() if args.repo else Path.cwd()
    task = " ".join(args.words) if args.words else None
    if not task and not args.brief:
        say("Describe the new project, e.g. " + 'loopd new "a FastAPI TODO service on SQLite"')
        return 2
    if not _require_claude():
        return 2
    say(_acc("  New project") + _dim(f" in {repo.name}/ — I'll set up git and plan from scratch."))
    cfg = _build_cfg(repo, args)
    workspace.register(repo)
    code = loop.run(task, cfg, fresh=args.fresh, on_start=(None if args.quiet else _reassure))
    _close_line(repo, code)
    return code


def cmd_build(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog="loopd build",
                                description="Build a whole PRD as a sequence of governed epics.")
    p.add_argument("words", nargs="*", help="the PRD/spec text, @path, or a spec file")
    _add_run_flags(p)
    args = p.parse_args(argv)
    repo = Path(args.repo).expanduser().resolve() if args.repo else Path.cwd()
    task = None
    if args.words:
        head = args.words[0]
        if head.startswith("@"):
            task = Path(head[1:]).expanduser().read_text()
        elif len(args.words) == 1 and Path(head).expanduser().is_file():
            task = Path(head).expanduser().read_text()
        else:
            task = " ".join(args.words)
    have_state = ((repo / ".agentic" / "program.json").is_file()
                  or (repo / ".agentic" / "brief.md").is_file())
    if not task and not args.resume and not have_state:
        say('Describe the project or pass a spec, e.g. ' + _b('loopd build @prd.md'))
        return 2
    if not _require_claude():
        return 2
    cfg = _build_cfg(repo, args)
    workspace.register(repo)
    _open_line(repo)
    code = program.run_program(task, cfg, resume=args.resume)
    _close_line(repo, code)
    return code


def cmd_clone(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog="loopd clone")
    p.add_argument("url")
    p.add_argument("words", nargs="*")
    _add_run_flags(p)
    args = p.parse_args(argv)
    dest = Path.cwd() / _clone_name(args.url)
    if dest.exists():
        say(_warn(f"  {dest.name}/ already exists here.") + _dim(" Open it with ")
            + f'cd {dest.name} && loopd "<task>"')
        return 2
    say(_dim(f"  Cloning {_clone_name(args.url)}…"))
    r = subprocess.run(["git", "clone", "--", args.url, str(dest)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        say(_warn("  Clone failed: ") + (r.stderr.strip().splitlines() or ["unknown error"])[-1])
        return 1
    workspace.register(dest)
    say(_ok(f"  ✓ Cloned {dest.name}"))
    task = " ".join(args.words) if args.words else None
    if not task:
        say(_dim("  Build something in it with ") + f'cd {dest.name} && loopd "<task>"')
        return 0
    if not _require_claude():
        return 2
    cfg = _build_cfg(dest, args)
    _open_line(dest)
    code = loop.run(task, cfg, on_start=(None if args.quiet else _reassure))
    _close_line(dest, code)
    return code


def cmd_config(argv: List[str]) -> int:
    repo = Path.cwd()
    cfg = Config(repo=repo)
    say(_b("  loopd settings") + _dim("  (edit .env or pass flags to override)"))
    say(_dim("  default budget   ") + f"${cfg.budget_usd:.2f}")
    say(_dim("  forecast         ") + ("on" if cfg.forecast_enabled else "off")
        + _dim(f"  ·  model {cfg.forecast_model}"))
    say(_dim("  planner / developer  ") + f"{cfg.pm_model} / {cfg.dev_model}")
    say(_dim("  workspace store  ") + str(workspace.home()))
    return 0


def cmd_help(argv: Optional[List[str]] = None) -> int:
    say(_b("loopd") + _dim(" — an autonomous engineering runtime that only ships changes it can prove."))
    say(_dim("        It plans, forecasts, builds, verifies, recovers, remembers, and delivers work."))
    say()
    say(_b("  Build something"))
    say('  loopd "<what to build>"        ' + _dim("build it here (current directory is the project)"))
    say('  loopd path/to/spec.md          ' + _dim("build from a markdown spec"))
    say('  loopd build @prd.md            ' + _dim("break a whole PRD into governed epics and build them"))
    say("  loopd new \"<idea>\"             " + _dim("start a brand-new project from scratch"))
    say("  loopd clone <url> [\"<task>\"]   " + _dim("clone a repo and (optionally) start building"))
    say('  loopd #142                     ' + _dim("build straight from a GitHub issue"))
    say("  loopd resume                   " + _dim("continue the paused run in this project"))
    say("  loopd pr                       " + _dim("open a pull request for the last run"))
    say()
    say(_b("  Look in"))
    say("  loopd                          " + _dim("the workspace home for this project"))
    say("  loopd status                   " + _dim("what's happening / how the last run went"))
    say("  loopd plan                     " + _dim("the current plan as a checklist"))
    say("  loopd report                   " + _dim("the full write-up of the last run"))
    say("  loopd logs                     " + _dim("recent activity"))
    say("  loopd memory                   " + _dim("what loopd has learned about this project"))
    say("  loopd projects                 " + _dim("your recent projects"))
    say("  loopd ui                       " + _dim("open the live dashboard in a browser"))
    say()
    say(_b("  Handy flags") + _dim("  (on any build command)"))
    say("  --budget N   --yes   --force   --constrained   --no-forecast   --forecast-only   --quiet")
    say()
    say(_dim("  Full reference: docs/cli.md"))
    return 0


def cmd_version(argv: Optional[List[str]] = None) -> int:
    say(f"loopd {__version__}")
    return 0


# --------------------------------------------------------------- helpers

def _repo_arg(argv: List[str]):
    """Ambient verbs accept an optional --repo; default to the current directory."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--repo", default=None)
    known, _ = p.parse_known_args(argv)
    return Path(known.repo).expanduser().resolve() if known.repo else Path.cwd()


DISPATCH = {
    "ui": cmd_ui, "status": cmd_status, "plan": cmd_plan, "logs": cmd_logs,
    "report": cmd_report, "memory": cmd_memory, "projects": cmd_projects,
    "history": cmd_projects, "resume": cmd_resume, "new": cmd_new, "build": cmd_build,
    "clone": cmd_clone, "pr": cmd_pr, "config": cmd_config,
}


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    load_dotenv()
    try:
        if not argv:
            _maybe_onboard()
            return cmd_home([])
        head = argv[0]
        if head in ("-h", "--help", "help"):
            return cmd_help()
        if head in ("-V", "--version", "version"):
            return cmd_version()
        _maybe_onboard()
        if head in DISPATCH:
            return DISPATCH[head](argv[1:])
        return cmd_run(argv)
    except KeyboardInterrupt:
        say(_dim("\nPaused — nothing is lost. Pick up with ") + "`loopd resume`" + _dim("."))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
