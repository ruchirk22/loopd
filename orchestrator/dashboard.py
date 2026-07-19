"""loopd mission-control dashboard: launch runs and watch the runtime plan, execute, verify
and decide — live, from the `.agentic/` data the loop writes.

Stdlib only (http.server). LOCAL TOOL: it spawns processes and reads paths you give it, so
it binds to 127.0.0.1 by default. Do not expose it to a network.

    loopd ui                                       # the usual way in
    python3 dashboard.py --repo ../my-app --port 9000   # equivalent, low-level entry

Everything shown is real, recorded data — no fabricated ETAs or token counters.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
# The engine runs as a module (`python -m orchestrator.run`) so it works both from a source
# checkout and when pip-installed. Assets ship inside the package.
ENGINE_CMD = [sys.executable, "-m", "orchestrator.run"]
ASSETS = Path(__file__).resolve().parent / "assets"

sys.path.insert(0, str(REPO_ROOT))
from orchestrator.env import load_dotenv  # noqa: E402
from orchestrator import workspace  # noqa: E402
from orchestrator import github  # noqa: E402


# ---------------------------------------------------------------- data

def _read_events(path: Path, cap: int = 2000) -> list:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(errors="replace").splitlines()[-cap:]
    except OSError:
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _active_node(events, running, finished):
    """Infer which runtime phase is active from the most recent event — planner, developer,
    verification, or decision. Honest inference, not a guess about the future."""
    if finished:
        return "done"
    if not running:
        return None
    for e in reversed(events):
        ev = e.get("event", "")
        label = str(e.get("label", ""))
        if ev == "gates":
            return "verification"
        if ev == "dev_error":
            return "developer"
        if ev in ("step_committed", "replanned", "step_rejected", "step_descoped",
                  "step_adopted_head"):
            return "decision"
        if ev == "pm_turn":
            if label.startswith("dispatch"):
                return "developer"
            if label.startswith("review"):
                return "review"
            if label in ("finalize", "corrective"):
                return "decision"
            if label == "plan" or e.get("verdict") == "plan":
                return "planner"
        if ev in ("run_started", "run_resumed", "pre_run_snapshot"):
            return "planner"
    return "planner"


_KIND = {  # event -> (kind, human text builder)
    "run_started": ("info", lambda e: "Run started"),
    "run_resumed": ("info", lambda e: "Run resumed"),
    "pre_run_snapshot": ("info", lambda e: "Snapshotted pre-run changes"),
    "pm_checkpoint": ("info", lambda e: "PM checkpoint"),
    "gates": ("gate", lambda e: "Gates passed" if e.get("passed") else "Gates failed"),
    "dev_error": ("warn", lambda e: f"Developer error · step {e.get('step','?')}"),
    "step_committed": ("ok", lambda e: f"Step {e.get('step','?')} accepted · {str(e.get('sha',''))[:9]}"),
    "step_rejected": ("warn", lambda e: f"Review rejected step {e.get('step','?')}"),
    "step_descoped": ("warn", lambda e: f"Step {e.get('step','?')} descoped"),
    "step_adopted_head": ("ok", lambda e: f"Step {e.get('step','?')} adopted commit"),
    "replanned": ("replan", lambda e: "Plan revised"),
    "escalation": ("bad", lambda e: f"Stopped: {e.get('reason','')}"),
    "budget_exceeded": ("bad", lambda e: "Budget exceeded"),
    "run_finished": ("ok", lambda e: "Run complete"),
}


def _timeline(events, n=48):
    out = []
    for e in events:
        ev = e.get("event", "")
        if ev == "pm_turn":
            label = str(e.get("label", ""))
            verdict = e.get("verdict", "")
            if label.startswith("dispatch"):
                out.append((e.get("ts"), "arrow", f"Dispatched step {label.split(':',1)[-1]}"))
            elif label == "plan" or verdict == "plan":
                out.append((e.get("ts"), "info", "Plan created"))
            elif label.startswith("review"):
                vk = {"accept": ("ok", "accepted"), "reject": ("warn", "rejected"),
                      "replan": ("replan", "replan"), "descope": ("warn", "descoped"),
                      "abort": ("bad", "aborted")}.get(verdict, ("dot", verdict or "reviewed"))
                out.append((e.get("ts"), vk[0], f"Review: {vk[1]} · step {label.split(':',1)[-1]}"))
            elif label == "finalize":
                out.append((e.get("ts"), "info", f"Finalize: {verdict}"))
            continue
        spec = _KIND.get(ev)
        if spec:
            out.append((e.get("ts"), spec[0], spec[1](e)))
    out = out[-n:]
    return [{"ts": ts, "kind": k, "text": t} for ts, k, t in out]


def _gate_stats(events):
    g = [e for e in events if e.get("event") == "gates"]
    passed = sum(1 for e in g if e.get("passed"))
    return passed, len(g)


def snapshot(repo, running: bool = False) -> dict:
    repo = Path(repo).expanduser().resolve()
    ad = repo / ".agentic"
    state_path = ad / "state.json"
    out = {"repo": str(repo), "exists": state_path.exists(), "running": running,
           "events": [], "timeline": [],
           # Workspace framing is available even before the first run, so the Project screen's
           # empty state can still show what loopd knows about this project.
           "name": repo.name, "health": workspace.health(repo),
           "memory_count": _memory_count(repo), "forecast_accuracy": _forecast_accuracy(repo),
           "runs": _project_runs(repo), "has_memory": (ad / "memory.md").is_file()}
    if not state_path.exists():
        return out
    try:
        st = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        out["error"] = "state.json is unreadable"
        return out
    plan = st.get("plan") or {}
    steps = plan.get("steps", []) if plan else []
    done = sum(1 for s in steps if s.get("status") == "done")
    skipped = sum(1 for s in steps if s.get("status") == "skipped")
    current = next((s for s in steps if s.get("status") in ("in_progress", "pending")), None)
    cur_idx = (steps.index(current) + 1) if current in steps and current else (done + skipped)
    events = _read_events(ad / "log.jsonl")
    finished = st.get("finished", False)
    started = st.get("started")
    gate_pass, gate_total = _gate_stats(events)

    out.update({
        "task": st.get("task", ""),
        "branch": st.get("branch", ""),
        "finished": finished,
        "total_cost_usd": st.get("total_cost_usd", 0.0),
        "budget_usd": st.get("budget_usd"),
        "pm_model": st.get("pm_model", ""),
        "dev_model": st.get("dev_model", ""),
        "replans_used": st.get("replans_used", 0),
        "plan_summary": plan.get("summary", ""),
        "elapsed_s": (time.time() - started) if started else None,
        "active_node": _active_node(events, running, finished),
        "step_index": cur_idx,
        "steps": [{
            "id": s.get("id"), "goal": s.get("goal"), "status": s.get("status"),
            "attempts": s.get("attempts", 0), "rejections": s.get("rejections", 0),
            "cost_usd": s.get("cost_usd", 0.0), "commit": (s.get("commit_sha") or "")[:9],
            "skip_reason": s.get("skip_reason", ""),
            "verify_count": len(s.get("verify", []) or []),
        } for s in steps],
        "counts": {"done": done, "skipped": skipped, "total": len(steps)},
        "current_step": ({"id": current.get("id"), "goal": current.get("goal")}
                         if current else None),
        "metrics": {
            "accepted": done, "rejected": sum(s.get("rejections", 0) for s in steps),
            "replans": st.get("replans_used", 0),
            "attempts": sum(s.get("attempts", 0) for s in steps),
            "gate_pass": gate_pass, "gate_total": gate_total,
        },
        "timeline": _timeline(events),
        "has_report": (ad / "report.md").is_file(),
        "has_escalation": (ad / "escalation.json").is_file(),
        "has_memory": (ad / "memory.md").is_file(),
        "forecast": st.get("forecast"),  # predicted (+ 'actual' once the run ends)
        # Workspace framing — the project has an identity, not just a run.
        "name": repo.name,
        "health": workspace.health(repo),
        "memory_count": _memory_count(repo),
        "forecast_accuracy": _forecast_accuracy(repo),
        "runs": _project_runs(repo),
        "escalation": _read_escalation(ad),
        "analysis": _read_analysis(ad),   # Failure Analysis for the 'needs you' state
    })
    return out


def _read_analysis(ad: Path):
    p = ad / "analysis.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _memory_count(repo) -> int:
    from orchestrator import memory as _m
    try:
        return sum(len(v) for v in _m.load(repo).values())
    except Exception:
        return 0


def _forecast_accuracy(repo):
    from orchestrator import forecast as _f
    try:
        return _f.ForecastHistory(repo).accuracy()
    except Exception:
        return None


def _project_runs(repo) -> int:
    try:
        e = next((p for p in workspace._load()["projects"]
                  if p.get("path") == str(Path(repo).expanduser().resolve())), None)
        return int(e.get("runs", 0)) if e else 0
    except Exception:
        return 0


def _read_escalation(ad: Path):
    p = ad / "escalation.json"
    if not p.is_file():
        return None
    try:
        e = json.loads(p.read_text())
        return {"reason": e.get("reason", ""), "detail": (e.get("detail") or "")[:1200],
                "pm_reasoning": (e.get("pm_reasoning") or "")[:1200], "step": e.get("step", "")}
    except (OSError, json.JSONDecodeError):
        return None


def step_detail(repo, step_id) -> dict:
    repo = Path(repo).expanduser().resolve()
    ad = repo / ".agentic"
    out = {"found": False, "id": step_id}
    try:
        st = json.loads((ad / "state.json").read_text())
    except (OSError, json.JSONDecodeError):
        return out
    steps = (st.get("plan") or {}).get("steps", [])
    step = next((s for s in steps if str(s.get("id")) == str(step_id)), None)
    if not step:
        return out
    handovers = sorted((ad / "handovers").glob(f"step-{step_id}-attempt-*.md")) \
        if (ad / "handovers").is_dir() else []
    latest = ""
    if handovers:
        try:
            latest = handovers[-1].read_text(errors="replace")
        except OSError:
            latest = ""
    out.update({
        "found": True,
        "step": {
            "id": step.get("id"), "goal": step.get("goal"), "status": step.get("status"),
            "details": step.get("details", ""),
            "acceptance_criteria": step.get("acceptance_criteria", []),
            "verify": step.get("verify", []),
            "attempts": step.get("attempts", 0), "rejections": step.get("rejections", 0),
            "cost_usd": step.get("cost_usd", 0.0), "commit_sha": step.get("commit_sha", ""),
            "skip_reason": step.get("skip_reason", ""), "dev_summary": step.get("dev_summary", ""),
        },
        "handover": latest,
        "handover_count": len(handovers),
    })
    return out


def build_run_command(repo, budget, mode: str, constrained: bool = False,
                      option: str = "") -> list:
    """The engine invocation for a launch. Pure, so it's testable without spawning."""
    cmd = [*ENGINE_CMD, "--repo", str(repo), "--budget", str(budget)]
    cmd.append("--resume-run" if mode == "resume" else "--fresh")
    if constrained:
        cmd.append("--constrained")
    if mode == "resume" and option:   # the failure-analysis option the owner picked
        cmd += ["--option", option]
    return cmd


def compute_forecast(repo, task, budget) -> dict:
    """Pre-run Execution Forecast preview for the dashboard (one cheap model call, in-process).
    Reads the task text or an existing .agentic/brief.md; does NOT touch run state. Returns
    {'ok':True,'forecast':{...}} or {'ok':False,'error':...}."""
    from orchestrator import forecast
    from orchestrator.config import Config
    try:
        repo = Path(repo).expanduser().resolve()
        brief = (task or "").strip()
        if brief and github.parse_issue_ref(brief):   # an issue link → estimate from the issue
            iss = github.fetch_issue(repo, brief)
            brief = github.issue_to_brief(iss) if iss else ""
        if not brief:
            bf = repo / ".agentic" / "brief.md"
            brief = bf.read_text(errors="replace") if bf.is_file() else ""
        if not brief:
            return {"ok": False, "error": "enter a task (or write a brief via /handoff first)"}
        cfg = Config(repo=repo)
        cfg.forecast_enabled = True
        try:
            cfg.budget_usd = float(budget)
        except (TypeError, ValueError):
            pass
        fc = forecast.run_forecast(cfg, brief, cfg.budget_usd, ledger=None)
    except Exception as e:  # a preview must never 500 the dashboard
        return {"ok": False, "error": f"forecast failed: {e}"}
    if fc is None:
        return {"ok": False, "error": "forecast unavailable (estimate call failed)"}
    return {"ok": True, "forecast": fc.to_dict()}


# ---------------------------------------------------------------- process control

class RunManager:
    def __init__(self) -> None:
        self._procs: dict = {}
        self._lock = threading.Lock()

    def is_running(self, repo) -> bool:
        with self._lock:
            p = self._procs.get(str(Path(repo).expanduser().resolve()))
            return p is not None and p.poll() is None

    def launch(self, repo, task, budget, pm_model, dev_model, mode, constrained=False,
               option="") -> dict:
        repo = Path(repo).expanduser().resolve()
        if self.is_running(repo):
            return {"ok": False, "error": "a run is already active for this repo"}
        repo.mkdir(parents=True, exist_ok=True)
        ad = repo / ".agentic"
        ad.mkdir(parents=True, exist_ok=True)
        if mode == "new":
            if task and task.strip():
                (ad / "brief.md").write_text(task)
            elif not (ad / "brief.md").is_file():
                return {"ok": False, "error": "provide a task (or write a brief via /handoff first)"}
        try:
            budget = float(budget)
        except (TypeError, ValueError):
            return {"ok": False, "error": "budget must be a number"}
        cmd = build_run_command(repo, budget, mode, constrained=constrained, option=option)
        env = dict(os.environ)
        if pm_model:
            env["PM_MODEL"] = pm_model
        if dev_model:
            env["DEV_MODEL"] = dev_model
        try:
            logf = open(ad / "dashboard-run.log", "w")
            proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT), env=env, stdout=logf,
                                    stderr=subprocess.STDOUT, text=True, start_new_session=True)
        except OSError as e:
            return {"ok": False, "error": f"could not launch: {e}"}
        with self._lock:
            self._procs[str(repo)] = proc
        return {"ok": True, "pid": proc.pid, "mode": mode}

    def stop(self, repo) -> dict:
        key = str(Path(repo).expanduser().resolve())
        with self._lock:
            p = self._procs.get(key)
        if not p or p.poll() is not None:
            return {"ok": False, "error": "no active run for this repo"}
        try:
            # SIGINT so run.py exits like Ctrl-C — state is saved and the run is resumable.
            os.killpg(p.pid, signal.SIGINT)
        except (ProcessLookupError, PermissionError, OSError) as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    def console(self, repo, n: int = 400) -> str:
        path = Path(repo).expanduser().resolve() / ".agentic" / "dashboard-run.log"
        if not path.is_file():
            return ""
        try:
            return "\n".join(path.read_text(errors="replace").splitlines()[-n:])
        except OSError:
            return ""


# ---------------------------------------------------------------- HTTP

def _projects_list(manager) -> list:
    """The Projects screen: recent workspaces with their status, for the home grid."""
    out = []
    for e in workspace.recent(limit=24):
        repo = e["path"]
        rs = workspace.run_state(repo)
        running = manager.is_running(repo)
        if running:
            status = "working"
        elif rs.get("exists") and rs.get("paused"):
            status = "paused"
        elif rs.get("exists") and rs.get("finished"):
            status = "done"
        else:
            status = "idle"
        out.append({
            "name": e.get("name", Path(repo).name), "path": repo, "status": status,
            "runs": e.get("runs", 0), "last_code": e.get("last_code"),
            "task": rs.get("task", ""), "steps_done": rs.get("steps_done", 0),
            "steps_total": rs.get("steps_total", 0), "cost_usd": rs.get("cost_usd", 0.0),
            "health": workspace.health(repo),
            "forecast_accuracy": _forecast_accuracy(repo),
        })
    return out


def _github_info(repo) -> dict:
    """Lightweight enrichment for the Repository card — repo slug + this branch's PR, if any."""
    av = github.available()
    if not av["ok"]:
        return {"available": False, "hint": av.get("hint", "")}
    branch = github.current_branch(repo)
    return {"available": True, "repo": github.repo_meta(repo), "branch": branch,
            "pr": github.pr_status(repo, branch) if branch else None}


def _open_pr_api(repo) -> dict:
    from orchestrator.config import Config
    from orchestrator import memory
    av = github.available()
    if not av["ok"]:
        return {"ok": False, "error": av.get("hint", "GitHub isn't connected")}
    if not github.has_remote(repo):
        return {"ok": False, "error": "this project has no git remote"}
    try:
        decisions = memory.load(repo).get(memory.DECISIONS, [])[-6:]
    except Exception:
        decisions = []
    payload = github.assemble_pr(repo, decisions=decisions)
    if not payload:
        return {"ok": False, "error": "no completed run to open a PR from"}
    cfg = Config(repo=repo)
    return github.open_pr(repo, payload["branch"], cfg.github_pr_base or payload["base"],
                          payload["title"], payload["body"], draft=cfg.github_pr_draft)


def _make_handler(manager: RunManager, default_repo: str, default_budget: float):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj))

        def _serve_asset(self, name):
            # only files directly inside assets/, no traversal
            safe = Path(name).name
            path = ASSETS / safe
            if not path.is_file():
                self._json({"error": "not found"}, 404)
                return
            ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            try:
                self._send(200, path.read_bytes(), ctype)
            except OSError:
                self._json({"error": "unreadable"}, 500)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            repo = (q.get("repo", [default_repo]) or [default_repo])[0]
            if u.path == "/":
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif u.path.startswith("/assets/"):
                self._serve_asset(u.path[len("/assets/"):])
            elif u.path == "/api/config":
                self._json({"default_repo": default_repo or "", "default_budget": default_budget})
            elif u.path == "/api/projects":
                self._json({"projects": _projects_list(manager)})
            elif u.path == "/api/state":
                self._json(snapshot(repo, running=manager.is_running(repo)) if repo
                           else {"exists": False, "events": [], "timeline": [], "repo": ""})
            elif u.path == "/api/console":
                self._json({"log": manager.console(repo)})
            elif u.path == "/api/report":
                p = Path(repo).expanduser().resolve() / ".agentic" / "report.md"
                self._json({"report": p.read_text(errors="replace") if p.is_file() else ""})
            elif u.path == "/api/memory":
                p = Path(repo).expanduser().resolve() / ".agentic" / "memory.md"
                self._json({"memory": p.read_text(errors="replace") if p.is_file() else ""})
            elif u.path == "/api/step":
                self._json(step_detail(repo, (q.get("id", [""]) or [""])[0]))
            elif u.path == "/api/github":
                self._json(_github_info(repo))
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            u = urlparse(self.path)
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._json({"ok": False, "error": "bad request body"}, 400)
                return
            repo = (body.get("repo") or default_repo or "").strip()
            if not repo:
                self._json({"ok": False, "error": "repo is required"}, 400)
                return
            if u.path == "/api/run":
                task = body.get("task", "")
                if body.get("mode", "new") == "new" and github.parse_issue_ref((task or "").strip()):
                    iss = github.fetch_issue(repo, task.strip())
                    if not iss:
                        self._json({"ok": False, "error": "couldn't read that issue"}, 409)
                        return
                    github.write_issue_context(repo, iss)  # brief.md drives the run
                    task = ""
                result = manager.launch(
                    repo=repo, task=task,
                    budget=body.get("budget", default_budget),
                    pm_model=(body.get("pm_model") or "").strip(),
                    dev_model=(body.get("dev_model") or "").strip(),
                    mode=body.get("mode", "new"),
                    constrained=bool(body.get("constrained")),
                    option=(body.get("option") or "").strip())
                self._json(result, 200 if result.get("ok") else 409)
            elif u.path == "/api/forecast":
                self._json(compute_forecast(repo, body.get("task", ""),
                                            body.get("budget", default_budget)))
            elif u.path == "/api/pr":
                self._json(_open_pr_api(repo))
            elif u.path == "/api/stop":
                result = manager.stop(repo)
                self._json(result, 200 if result.get("ok") else 409)
            else:
                self._json({"error": "not found"}, 404)

    return Handler


def serve(host: str, port: int, default_repo: str, default_budget: float) -> None:
    manager = RunManager()
    httpd = ThreadingHTTPServer((host, port), _make_handler(manager, default_repo, default_budget))
    print(f"loopd dashboard on http://{host}:{port}  (local only — do not expose)")
    if default_repo:
        print(f"default repo: {Path(default_repo).expanduser().resolve()}")
    print("Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()


def main(argv=None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description="loopd mission-control dashboard (local).")
    ap.add_argument("--repo", default="", help="default target repo for the launch form")
    ap.add_argument("--budget", type=float, default=float(os.environ.get("BUDGET_USD", "25")))
    ap.add_argument("--host", default="127.0.0.1", help="bind host (keep it local)")
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args(argv)
    serve(args.host, args.port, args.repo, args.budget)
    return 0


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>loopd</title>
<link rel="icon" href="/assets/loopd.svg">
<style>
/* System font stack only — the dashboard is a local tool and must render offline; the
   font-family declarations below fall back cleanly to the OS UI + monospace fonts. */
:root{
  --bg:#0b0b0c; --panel:#141416; --panel-2:#101012; --raise:#17171a;
  --line:rgba(255,255,255,.07); --line-2:rgba(255,255,255,.12);
  --fg:#eaeaea; --fg-strong:#fbfbfb; --mut:#9b9ba0; --faint:#66666b;
  --attention:#d6b16a; --good:#8fb28c;
  --r:14px; --r2:10px; --r3:7px;
  --font:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  --mono:'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
  --ease:cubic-bezier(.2,.6,.2,1);
}
*{box-sizing:border-box;} html,body{height:100%;}
body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--font);font-size:14px;
  line-height:1.55;letter-spacing:-.006em;-webkit-font-smoothing:antialiased;}
::selection{background:rgba(255,255,255,.14);}
::-webkit-scrollbar{width:9px;height:9px;} ::-webkit-scrollbar-thumb{background:var(--line-2);border-radius:9px;}
a{color:inherit;}
button{font-family:inherit;font-size:13px;color:var(--fg);background:var(--raise);border:1px solid var(--line-2);
  border-radius:var(--r3);padding:8px 14px;cursor:pointer;transition:.16s var(--ease);letter-spacing:-.01em;}
button:hover{background:#1e1e22;border-color:rgba(255,255,255,.2);}
button.primary{background:var(--fg-strong);color:#0a0a0a;border-color:var(--fg-strong);font-weight:560;}
button.primary:hover{background:#fff;}
button.ghost{background:transparent;} button:disabled{opacity:.4;cursor:not-allowed;}
input,textarea{width:100%;background:var(--panel-2);color:var(--fg);border:1px solid var(--line-2);
  border-radius:var(--r2);padding:11px 13px;font-family:var(--font);font-size:13.5px;transition:.16s var(--ease);}
input:focus,textarea:focus{outline:none;border-color:rgba(255,255,255,.34);background:#0c0c0e;}
input::placeholder,textarea::placeholder{color:var(--faint);}
textarea{resize:vertical;min-height:92px;line-height:1.6;}

/* top bar */
.top{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:14px;height:56px;padding:0 22px;
  background:rgba(11,11,12,.82);backdrop-filter:blur(14px);border-bottom:1px solid var(--line);}
.brand{display:flex;align-items:center;gap:10px;cursor:pointer;}
.logo{height:26px;filter:grayscale(1) brightness(1.7) contrast(.82);opacity:.94;}
.crumb{color:var(--mut);font-size:13px;display:flex;align-items:center;gap:8px;}
.crumb b{color:var(--fg);font-weight:560;}
.crumb .sep{color:var(--faint);}
.spacer{flex:1;}
.dot{display:inline-flex;align-items:center;gap:8px;color:var(--mut);font-size:12.5px;}
.dot .d{width:8px;height:8px;border-radius:50%;background:var(--faint);}
.dot.working .d{background:var(--fg-strong);animation:breathe 2.2s var(--ease) infinite;}
.dot.paused .d{background:transparent;border:1.5px solid var(--attention);}
.dot.needs .d{background:var(--attention);animation:breathe 1.6s var(--ease) infinite;}
.dot.done .d{background:var(--good);}
@keyframes breathe{0%,100%{opacity:1;} 50%{opacity:.28;}}
.iconbtn{background:transparent;border:none;color:var(--mut);padding:6px;font-size:15px;}
.iconbtn:hover{color:var(--fg);background:transparent;}

.wrap{max-width:1080px;margin:0 auto;padding:30px 22px 80px;}
.screen{animation:rise .28s var(--ease);}
@keyframes rise{from{opacity:0;transform:translateY(6px);} to{opacity:1;transform:none;}}
@keyframes fade{from{opacity:0;} to{opacity:1;}}

h1.page{font-size:15px;font-weight:560;color:var(--mut);letter-spacing:.02em;margin:0 0 20px;}

/* cards */
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:18px 20px;
  animation:fade .3s var(--ease);}
.card+.card{margin-top:14px;}
.card h3{margin:0 0 14px;font-size:11px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;color:var(--faint);}

/* projects grid */
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;}
.proj{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:18px 20px;
  cursor:pointer;transition:.18s var(--ease);animation:fade .3s var(--ease);}
.proj:hover{border-color:var(--line-2);transform:translateY(-1px);background:#161618;}
.proj .ph{display:flex;align-items:center;justify-content:space-between;gap:10px;}
.proj .nm{font-size:15.5px;font-weight:560;color:var(--fg-strong);}
.proj .task{color:var(--mut);margin:9px 0 12px;font-size:13.5px;min-height:19px;}
.proj .meta{color:var(--faint);font-size:12px;display:flex;gap:14px;flex-wrap:wrap;}
.proj.new{border-style:dashed;display:flex;flex-direction:column;justify-content:center;align-items:center;
  text-align:center;color:var(--mut);min-height:118px;gap:6px;}
.proj.new .plus{font-size:20px;color:var(--fg);} .proj.new:hover{border-color:rgba(255,255,255,.24);}
.chip{font-size:11px;font-weight:520;padding:3px 9px;border-radius:999px;border:1px solid var(--line-2);
  color:var(--mut);display:inline-flex;align-items:center;gap:6px;white-space:nowrap;}
.chip .d{width:6px;height:6px;border-radius:50%;background:var(--faint);}
.chip.working .d{background:var(--fg-strong);animation:breathe 2.2s var(--ease) infinite;}
.chip.paused{color:var(--attention);border-color:rgba(214,177,106,.34);} .chip.paused .d{background:var(--attention);}
.chip.done{color:var(--good);border-color:rgba(143,178,140,.3);} .chip.done .d{background:var(--good);}

/* project layout */
.cols{display:grid;grid-template-columns:1fr 320px;gap:16px;align-items:start;}
@media(max-width:860px){.cols{grid-template-columns:1fr;} .grid{grid-template-columns:1fr;}}

/* hero */
.hero{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:22px 24px;
  animation:fade .3s var(--ease);}
.hero.attn{border-color:rgba(214,177,106,.38);box-shadow:inset 3px 0 0 var(--attention);}
.hero.done{box-shadow:inset 3px 0 0 var(--good);}
.hero .state{display:flex;align-items:center;justify-content:space-between;color:var(--mut);font-size:12.5px;}
.hero .headline{font-size:22px;font-weight:560;color:var(--fg-strong);margin:14px 0 4px;letter-spacing:-.02em;
  line-height:1.25;}
.hero .headline.sm{font-size:19px;}
.hero .sub{color:var(--mut);font-size:13.5px;}
.hero .quote{color:var(--faint);font-size:12.5px;margin-top:16px;font-style:normal;}
.bar{height:6px;border-radius:999px;background:#0a0a0b;border:1px solid var(--line);overflow:hidden;margin:18px 0 10px;position:relative;}
.bar > span{display:block;height:100%;background:var(--fg-strong);border-radius:999px;transition:width .6s var(--ease);}
.bar.thin{height:5px;margin:8px 0;}
.bar .fmark{position:absolute;top:-3px;bottom:-3px;width:1px;background:var(--attention);opacity:.7;}
.metaline{display:flex;gap:16px;color:var(--faint);font-size:12px;}

/* plan */
.plan{margin-top:16px;}
.planhdr{display:flex;justify-content:space-between;align-items:baseline;margin:0 0 4px;}
.planhdr h3{margin:0;} .planhdr .cnt{color:var(--faint);font-size:12px;}
.steps{display:flex;flex-direction:column;}
.step{display:flex;align-items:center;gap:12px;padding:11px 8px;border-radius:var(--r3);cursor:pointer;
  transition:.14s var(--ease);border-bottom:1px solid var(--line);}
.step:last-child{border-bottom:none;} .step:hover{background:var(--raise);}
.step .mk{width:16px;text-align:center;color:var(--faint);font-size:13px;flex-shrink:0;}
.step.done .mk{color:var(--good);} .step.in_progress .mk{color:var(--fg-strong);}
.step.needs .mk{color:var(--attention);}
.step .g{flex:1;color:var(--fg);font-size:13.5px;}
.step.pending .g{color:var(--mut);}
.step .rt{color:var(--faint);font-size:12px;font-family:var(--mono);}
.step.in_progress .rt{color:var(--mut);}
.pulse{animation:breathe 1.8s var(--ease) infinite;}

/* rail */
.rc{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);overflow:hidden;animation:fade .3s var(--ease);}
.rc+.rc{margin-top:12px;}
.rc .rh{display:flex;justify-content:space-between;align-items:center;padding:14px 16px;cursor:pointer;user-select:none;}
.rc .rh .lab{font-size:11px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;color:var(--faint);}
.rc .rh .val{color:var(--fg);font-size:12.5px;}
.rc .rh .car{color:var(--faint);transition:.2s var(--ease);}
.rc.open .rh .car{transform:rotate(180deg);}
.rc .body{max-height:0;overflow:hidden;transition:max-height .28s var(--ease);}
.rc.open .body{max-height:420px;}
.rc .body .inner{padding:0 16px 15px;color:var(--mut);font-size:12.5px;border-top:1px solid var(--line);padding-top:13px;}
.kv{display:flex;justify-content:space-between;padding:4px 0;} .kv .k{color:var(--faint);} .kv .v{color:var(--fg);font-family:var(--mono);}
.mlist{margin:0;padding-left:16px;} .mlist li{margin:4px 0;color:var(--mut);}

/* report tiles */
.tiles{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px;}
@media(max-width:860px){.tiles{grid-template-columns:1fr;}}
.va{display:flex;justify-content:space-between;padding:6px 0;font-size:13px;border-bottom:1px solid var(--line);}
.va:last-child{border-bottom:none;} .va .k{color:var(--faint);} .va .v{font-family:var(--mono);}
.va .v .arw{color:var(--faint);margin:0 6px;}
.verified{color:var(--good);font-size:12.5px;display:flex;gap:16px;flex-wrap:wrap;margin-top:6px;}
/* failure analysis — the "needs you" beats, matching the CLI */
.fa{margin-top:16px;}
.fa-beat{font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--faint);margin:16px 0 5px;}
.fa-beat:first-child{margin-top:0;}
.fa-txt{color:var(--fg);font-size:14px;line-height:1.6;}
.fa-conf{color:var(--faint);font-weight:400;text-transform:none;letter-spacing:0;}
.opts{display:flex;flex-direction:column;gap:8px;margin-top:8px;}
.opt{display:block;border:1px solid var(--line);border-radius:var(--r2);padding:11px 13px;cursor:pointer;transition:.14s var(--ease);}
.opt:hover{border-color:var(--line-2);background:var(--raise);}
.opt.rec{border-color:rgba(214,177,106,.32);}
.opt input{width:auto;margin-right:9px;vertical-align:middle;accent-color:var(--attention);}
.opt .ol{font-size:13.5px;color:var(--fg);}
.opt .rtag{font-size:10px;color:var(--attention);border:1px solid rgba(214,177,106,.34);border-radius:999px;padding:1px 7px;margin-left:7px;letter-spacing:.02em;}
.opt .od{display:block;color:var(--mut);font-size:12.5px;margin:5px 0 0 25px;}

/* activity + toggles */
.toggle{color:var(--faint);font-size:12px;cursor:pointer;user-select:none;margin-top:12px;display:inline-flex;gap:6px;align-items:center;}
.toggle:hover{color:var(--mut);}
pre.mono{font-family:var(--mono);font-size:12px;line-height:1.65;color:#c9c9cf;white-space:pre-wrap;word-break:break-word;
  background:var(--panel-2);border:1px solid var(--line);border-radius:var(--r2);padding:14px;max-height:340px;overflow:auto;margin:12px 0 0;}
.tl{display:flex;flex-direction:column;gap:2px;}
.tl .ev{display:flex;gap:10px;padding:4px 0;color:var(--mut);font-size:12.5px;}
.tl .ev .t{color:var(--faint);font-family:var(--mono);font-size:11px;min-width:52px;}

/* actions row */
.actions{display:flex;gap:10px;margin-top:22px;align-items:center;}
.actions .sp{flex:1;}

/* drawer */
.drawer{position:fixed;top:0;right:0;height:100%;width:440px;max-width:92vw;background:var(--panel);
  border-left:1px solid var(--line-2);transform:translateX(100%);transition:transform .3s var(--ease);z-index:40;
  display:flex;flex-direction:column;box-shadow:-30px 0 60px rgba(0,0,0,.4);}
.drawer.show{transform:none;}
.drawer .dh{display:flex;justify-content:space-between;align-items:center;padding:18px 20px;border-bottom:1px solid var(--line);}
.drawer .dh h2{margin:0;font-size:15px;font-weight:560;}
.drawer .db{padding:20px;overflow:auto;}
.drawer .x{background:transparent;border:none;color:var(--mut);font-size:15px;padding:4px 8px;}
.sect{margin:0 0 18px;} .sect .lab{font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--faint);margin-bottom:7px;}
.sect ul{margin:0;padding-left:16px;} .expand{cursor:pointer;color:var(--faint);font-size:12.5px;} .expand:hover{color:var(--mut);}

/* modal */
.scrim{position:fixed;inset:0;background:rgba(6,6,7,.66);backdrop-filter:blur(3px);opacity:0;pointer-events:none;
  transition:opacity .2s var(--ease);z-index:50;}
.scrim.show{opacity:1;pointer-events:auto;}
.modal{position:fixed;left:50%;top:50%;transform:translate(-50%,-46%) scale(.98);opacity:0;pointer-events:none;
  width:460px;max-width:92vw;background:var(--panel);border:1px solid var(--line-2);border-radius:var(--r);
  padding:24px;z-index:51;transition:.22s var(--ease);}
.modal.show{transform:translate(-50%,-50%);opacity:1;pointer-events:auto;}
.modal h2{margin:0 0 4px;font-size:16px;font-weight:600;} .modal .lead{color:var(--mut);font-size:13px;margin-bottom:18px;}
.modal label{display:block;font-size:12px;color:var(--mut);margin:14px 0 6px;}
.frow{display:flex;gap:10px;margin-top:20px;flex-wrap:wrap;}
.msg{font-size:12.5px;color:var(--mut);min-height:16px;margin-top:12px;} .msg.err{color:var(--attention);}

/* forecast card in modal */
.fc .row{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--line);font-size:14px;}
.fc .row:last-child{border-bottom:none;} .fc .row .k{color:var(--mut);} .fc .row .v{font-family:var(--mono);color:var(--fg-strong);}
.fc .note{margin:16px 0 4px;color:var(--mut);font-size:13px;}
.empty{color:var(--faint);text-align:center;padding:46px 20px;}
.center{max-width:560px;margin:8vh auto 0;text-align:center;}
.center .big{font-size:17px;color:var(--mut);margin-bottom:22px;}
</style>
</head>
<body>
<div class="top">
  <div class="brand" onclick="showProjects()"><img class="logo" src="/assets/loopd_no_bg.png" alt="loopd"></div>
  <div class="crumb" id="crumb"></div>
  <div class="spacer"></div>
  <div class="dot" id="dot"><span class="d"></span><span id="dotlab">all calm</span></div>
  <button class="iconbtn" onclick="openSettings()" title="Settings">&#9881;</button>
</div>

<div class="wrap"><div id="app"></div></div>

<div class="scrim" id="scrim" onclick="closeModal()"></div>
<div class="modal" id="modal"></div>

<div class="drawer" id="drawer">
  <div class="dh"><h2 id="d-title">Step</h2><button class="x" onclick="closeDrawer()">&#10005;</button></div>
  <div class="db" id="d-body"></div>
</div>

<script>
const $=s=>document.querySelector(s);
const esc=s=>(s==null?"":String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const money=n=>"$"+(Number(n)||0).toFixed(2);
function fmin(m){m=Number(m)||0; if(m<1)return Math.round(m*60)+" sec"; if(m<90)return Math.round(m)+" min"; const h=(m/60)|0;return h+"h "+Math.round(m%60)+"m";}
function dur(s){if(s==null)return"—"; s=Math.floor(s); const h=(s/3600)|0,m=((s%3600)/60)|0,x=s%60; return h?`${h}h ${m}m`:m?`${m}m ${x}s`:`${x}s`;}
function setHTML(node,html){if(node&&node.dataset.sig!==html){node.dataset.sig=html;node.innerHTML=html;}}

let CFG={default_repo:"",default_budget:25}, REPO="", VIEW="projects", STATE=null, DRAWER=null, PENDING=null;
const OPEN={}; // open state of rail accordions

async function jget(u){try{return await (await fetch(u)).json();}catch(e){return null;}}
async function jpost(u,b){try{const r=await fetch(u,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)});return {ok:r.ok,data:await r.json()};}catch(e){return {ok:false,data:{error:String(e)}};}}

async function init(){
  CFG=await jget("/api/config")||CFG;
  if(CFG.default_repo){openProject(CFG.default_repo);}
  else{showProjects();}
  setInterval(loop,1600);
}
function loop(){ if(VIEW==="projects") renderProjects(); else if(VIEW==="project") tick(); }

/* ---------------- top bar ---------------- */
function setDot(kind,label){const d=$("#dot");d.className="dot"+(kind?" "+kind:"");$("#dotlab").textContent=label;}
function setCrumb(name){ $("#crumb").innerHTML = name?`<span class="sep">&rsaquo;</span> <b>${esc(name)}</b>`:""; }

/* ---------------- Projects ---------------- */
async function showProjects(){ VIEW="projects"; REPO=""; setCrumb(""); await renderProjects(); }
async function renderProjects(){
  const d=await jget("/api/projects"); const projs=(d&&d.projects)||[];
  const anyWork=projs.some(p=>p.status==="working");
  setDot(anyWork?"working":"", anyWork?"working":"all calm");
  const rank={working:0,paused:1,done:2,idle:3};
  projs.sort((a,b)=>(rank[a.status]??9)-(rank[b.status]??9));
  let cards=projs.map(p=>projCard(p)).join("");
  const newCard=`<div class="proj new" onclick="openNew()"><div class="plus">+</div><div>New project</div>
    <div style="font-size:12px;color:var(--faint)">folder &middot; repo &middot; an idea</div></div>`;
  let body;
  if(!projs.length){
    body=`<div class="center"><div class="big">What would you like to build today?</div>
      <button class="primary" onclick="openNew()">&nbsp;+&nbsp; Start your first project &nbsp;</button>
      <div style="margin-top:18px;color:var(--faint);font-size:12.5px">open a folder &middot; clone a repo &middot; describe an idea</div></div>`;
  }else{
    body=`<h1 class="page">Your projects</h1><div class="grid">${cards}${newCard}</div>`;
  }
  setHTML($("#app"), `<div class="screen">${body}</div>`);
}
function projCard(p){
  const st=p.status; const h=p.health||{};
  const chip=`<span class="chip ${st}"><span class="d"></span>${st}</span>`;
  const line=(st==="idle")
    ? `${p.runs||0} run(s)${p.forecast_accuracy!=null?` &middot; forecasts ~${Math.round(p.forecast_accuracy)}%`:""}`
    : `${p.steps_done||0} of ${p.steps_total||0} &middot; ${money(p.cost_usd)}`;
  const hh = h.is_repo ? `${esc(h.branch||"")}${h.dirty?` &middot; ${h.dirty_count} uncommitted`:" &middot; clean"}` : "new folder";
  return `<div class="proj" onclick="openProject('${encodeURIComponent(p.path)}')">
    <div class="ph"><span class="nm">${esc(p.name)}</span>${chip}</div>
    <div class="task">${p.task?("&ldquo;"+esc(p.task)+"&rdquo;"):"&nbsp;"}</div>
    <div class="meta"><span>${line}</span><span>${hh}</span></div></div>`;
}

/* ---------------- Project ---------------- */
function openProject(repo){ REPO=decodeURIComponent(repo); VIEW="project"; tick(); }
async function tick(){
  if(!REPO)return;
  const s=await jget("/api/state?repo="+encodeURIComponent(REPO)); if(!s)return;
  STATE=s; setCrumb(s.name||REPO.split("/").pop());
  renderProject(s);
}
function stateKind(s){
  if(!s.exists) return "empty";
  if(s.running) return "active";
  if(s.finished) return "report";
  return "needs"; // stopped/paused with work on disk
}
function renderProject(s){
  const k=stateKind(s);
  if(k==="active"){setDot("working","working");}
  else if(k==="needs"){setDot("needs","needs you");}
  else if(k==="report"){setDot("done","delivered");}
  else{setDot("", "ready");}
  const html = k==="empty"?viewEmpty(s): k==="active"?viewActive(s): k==="report"?viewReport(s):viewNeeds(s);
  setHTML($("#app"), `<div class="screen">${html}</div>`);
  if(k==="active"){ loadConsoleMaybe(); }
}

/* rail (shared) */
function rail(s){
  const f=s.forecast||{};
  const h=s.health||{};
  const acc=s.forecast_accuracy;
  const fc = f.estimated_cost_usd!=null
    ? `${money(f.estimated_cost_usd)} &middot; ${fmin(f.estimated_runtime_min)} &middot; ${f.confidence}%`
    : "—";
  const repoVal = h.is_repo?esc(h.branch||"repo"):"new";
  return `
  ${railCard("forecast","Forecast",fc,forecastBody(s))}
  ${railCard("repository","Repository",repoVal,repoBody(s))}
  ${railCard("memory","Memory",(s.memory_count||0)+" learned",memoryBody(s))}
  ${railCard("history","History",(s.runs||0)+" run(s)",historyBody(s,acc))}`;
}
function railCard(id,label,val,body){
  const open=OPEN[id]?" open":"";
  return `<div class="rc${open}" id="rc-${id}">
    <div class="rh" onclick="toggleRail('${id}')"><span class="lab">${label}</span>
      <span style="display:flex;gap:10px;align-items:center"><span class="val">${val}</span><span class="car">&#9662;</span></span></div>
    <div class="body"><div class="inner">${body}</div></div></div>`;
}
function toggleRail(id){OPEN[id]=!OPEN[id]; const el=$("#rc-"+id); if(el)el.classList.toggle("open",OPEN[id]);}
function forecastBody(s){const f=s.forecast||{}; if(f.estimated_cost_usd==null)return "No estimate for this run.";
  const a=f.actual;
  let r=`<div class="kv"><span class="k">estimated</span><span class="v">${money(f.estimated_cost_usd)}</span></div>
    <div class="kv"><span class="k">runtime</span><span class="v">${fmin(f.estimated_runtime_min)}</span></div>
    <div class="kv"><span class="k">confidence</span><span class="v">${f.confidence}%</span></div>
    <div class="kv"><span class="k">risk</span><span class="v">${esc(f.risk||"")}</span></div>`;
  if(a)r+=`<div class="kv"><span class="k">actual cost</span><span class="v">${money(a.cost_usd)}</span></div>`;
  return r;}
function repoBody(s){const h=s.health||{}; if(!h.is_repo)return "A fresh folder — I'll set up git when we start.";
  return `<div class="kv"><span class="k">branch</span><span class="v">${esc(h.branch||"?")}</span></div>
    <div class="kv"><span class="k">status</span><span class="v">${h.dirty?h.dirty_count+" uncommitted":"clean"}</span></div>
    <div id="ghinfo" style="margin-top:8px"><span class="expand" onclick="loadGithub()">check GitHub &rarr;</span></div>`;}
async function loadGithub(){const el=$("#ghinfo"); if(!el)return; el.innerHTML='<span class="fc-note">checking…</span>';
  const g=await jget("/api/github?repo="+encodeURIComponent(REPO));
  if(!g||!g.available){ el.innerHTML=`<span class="fc-note">${esc((g&&g.hint)||"GitHub not connected")}</span>`; return; }
  const slug=g.repo?g.repo.slug:"—";
  const pr=g.pr?`<div class="kv"><span class="k">pull request</span><span class="v"><a href="${esc(g.pr.url)}" target="_blank">#${g.pr.number} ${esc(g.pr.state.toLowerCase())}</a></span></div>`
    :`<div class="fc-note">no open PR for this branch</div>`;
  el.innerHTML=`<div class="kv"><span class="k">repo</span><span class="v">${esc(slug)}</span></div>${pr}`;}
async function openPr(){const el=$("#prinfo"); if(el)el.textContent="Opening…";
  const {data}=await jpost("/api/pr",{repo:REPO});
  if(data&&data.ok){ if(el)el.innerHTML=(data.existing?"Already open: ":"Opened: ")+`<a href="${esc(data.url)}" target="_blank">${esc(data.url)}</a>`; }
  else if(el){ el.textContent="Couldn't open PR — "+((data&&data.error)||"unknown"); }}
function memoryBody(s){ if(!s.has_memory)return "I'll note durable facts about this project as I learn them.";
  return `<span class="expand" onclick="loadMemory()">view what I've learned &rarr;</span><div id="memdump"></div>`;}
function historyBody(s,acc){ return `<div class="kv"><span class="k">runs</span><span class="v">${s.runs||0}</span></div>
  ${acc!=null?`<div class="kv"><span class="k">forecast accuracy</span><span class="v">~${Math.round(acc)}%</span></div>`:""}
  <div style="margin-top:8px"><span class="expand" onclick="loadReport()">open the last report &rarr;</span></div>`;}
async function loadMemory(){const d=await jget("/api/memory?repo="+encodeURIComponent(REPO)); const el=$("#memdump"); if(el&&d)el.innerHTML=`<pre class="mono">${esc(d.memory||"")}</pre>`;}

/* empty state */
function viewEmpty(s){
  return `<div class="cols"><div>
    <div class="hero">
      <div class="state"><span>Ready when you are.</span></div>
      <div class="headline sm">What would you like to build today?</div>
      <textarea id="obj" placeholder="Describe the objective — the definition of done, any constraints…"></textarea>
      <div class="actions"><button class="primary" onclick="delegate()">Delegate &rarr;</button>
        <span style="color:var(--faint);font-size:12px">or drop a spec, or paste an issue link</span></div>
      <div class="msg" id="msg"></div>
    </div></div>
    <div>${rail(s)}</div></div>`;
}

/* active state */
function viewActive(s){
  const c=s.counts||{done:0,skipped:0,total:0};
  const pct=c.total?Math.round(100*(c.done+c.skipped)/c.total):0;
  const cur=s.current_step; const node=nodeLabel(s.active_node);
  const head=cur?esc(cur.goal):(s.plan_summary?esc(s.plan_summary):"Starting the run…");
  const f=s.forecast||{}; const budget=(f.chosen_budget_usd||s.budget_usd||0);
  const spentPct=budget?Math.min(100,100*(s.total_cost_usd/budget)):0;
  const fmark=(budget&&f.estimated_cost_usd)?Math.min(100,100*(f.estimated_cost_usd/budget)):null;
  return `<div class="cols"><div>
    <div class="hero">
      <div class="state"><span>&#9679; Working</span><span>step ${Math.min(s.step_index||c.done,c.total)} of ${c.total}</span></div>
      <div class="headline">${head}</div>
      <div class="sub">${node?esc(node)+"…":""}</div>
      <div class="bar"><span style="width:${pct}%"></span></div>
      <div class="metaline"><span>elapsed ${dur(s.elapsed_s)}</span><span>${money(s.total_cost_usd)} of ${money(budget)}</span></div>
      <div class="quote">&ldquo;I've got it from here. Close this any time — nothing is lost.&rdquo;</div>
    </div>
    ${planCard(s)}
    <div class="toggle" onclick="toggleActivity()">&#9662; Activity — timeline &amp; console</div>
    <div id="activity"></div>
  </div><div>${rail(s)}</div></div>`;
}
function planCard(s){
  const c=s.counts||{}; const steps=s.steps||[];
  const rows=steps.map((st,i)=>{
    const cls=st.status; const mk={done:"&#10003;",in_progress:"&#9656;",skipped:"&ndash;",needs:"&#9650;"}[cls]||"&middot;";
    const rt=st.status==="in_progress"?`<span class="rt pulse">working…</span>`
      :(st.commit?`<span class="rt">${money(st.cost_usd)}</span>`:"");
    return `<div class="step ${cls}" onclick="openStep('${esc(st.id)}')">
      <span class="mk">${mk}</span><span class="g">${esc(st.goal||st.id)}</span>${rt}</div>`;
  }).join("");
  return `<div class="plan"><div class="planhdr"><h3>Plan</h3><span class="cnt">${c.done||0} / ${c.total||0}</span></div>
    <div class="steps">${rows||'<div class="empty">Planning the work…</div>'}</div></div>`;
}
function nodeLabel(n){return {planner:"Planning",developer:"Writing code",verification:"Verifying",review:"Reviewing the result",decision:"Deciding",done:"Done"}[n]||"";}

/* needs-you / paused */
function viewNeeds(s){
  const c=s.counts||{}; const a=s.analysis;
  if(!a){  // no diagnosis (older run) — a calm, generic paused card
    const e=s.escalation||{};
    const why=e.reason==="budget_exceeded"?"I reached the budget for this run."
      :e.reason==="wall_clock_exceeded"?"I hit the time limit for this run."
      :(e.detail?esc(e.detail.split("\n")[0]):"The run stopped and is waiting for you.");
    return `<div class="cols"><div>
      <div class="hero attn"><div class="state"><span>&#9650; Paused</span><span>${c.done||0} of ${c.total||0} done</span></div>
        <div class="headline sm">${esc(why)}</div>
        <div class="actions"><button class="primary" onclick="applyFix(null,'')">Resume</button>
          <button class="ghost" onclick="loadReport()">See what happened</button></div></div>
      ${planCard(s)}<div id="activity"></div></div><div>${rail(s)}</div></div>`;
  }
  // the same four beats the CLI prints: what happened · why · what I'd do · other options
  const conf=a.confidence!=null?(a.confidence>=75?`~${a.confidence}% sure`:a.confidence>=45?`~${a.confidence}% sure — worth confirming`:"I'm not certain here"):"";
  const opts=(a.options||[]);
  const rec=opts.find(o=>o.recommended)||opts[0];
  const ordered=rec?[rec].concat(opts.filter(o=>o!==rec)):opts;
  const radios=ordered.map((o,i)=>`
    <label class="opt${o.recommended?" rec":""}">
      <input type="radio" name="fa-opt" value="${esc(o.id)}" data-kind="${esc(o.kind)}" ${i===0?"checked":""}>
      <span class="ol">${esc(o.label)}${o.recommended?' <span class="rtag">recommended</span>':""}</span>
      ${o.detail?`<span class="od">${esc(o.detail)}</span>`:""}
    </label>`).join("");
  return `<div class="cols"><div>
    <div class="hero attn">
      <div class="state"><span>&#9650; I need you for a moment</span><span>${a.step?("paused at step "+esc(a.step)):((c.done||0)+" of "+(c.total||0)+" done")}</span></div>
      <div class="fa">
        <div class="fa-beat">What happened</div><div class="fa-txt">${esc(a.summary||"")}</div>
        <div class="fa-beat">Why it happened${conf?` <span class="fa-conf">(${conf})</span>`:""}</div><div class="fa-txt">${esc(a.root_cause||"")}</div>
        <div class="fa-beat">What I'd do${opts.length>1?" · or choose another":""}</div>
        <div class="opts">${radios}</div>
      </div>
      <div class="actions"><button class="primary" onclick="applyFix()">Continue</button>
        <button class="ghost" onclick="loadReport()">See what happened</button></div>
    </div>
    ${planCard(s)}<div id="activity"></div>
  </div><div>${rail(s)}</div></div>`;
}
async function applyFix(forceOpt,forceKind){
  const s=STATE, a=(s&&s.analysis)||{};
  let oid=forceOpt, kind=forceKind;
  if(oid===undefined){ const sel=document.querySelector('input[name="fa-opt"]:checked'); oid=sel?sel.value:null; kind=sel?sel.dataset.kind:""; }
  if(kind==="abort"){ setHTML($("#app"),`<div class="screen center"><div class="big">Left as-is — the work so far is committed and safe.</div><button class="ghost" onclick="showProjects()">&larr; Projects</button></div>`); return; }
  let budget=(s&&s.budget_usd)||CFG.default_budget;
  if(a.reason==="budget_exceeded") budget=budget+15;   // a budget stop needs headroom to continue
  const body={repo:REPO,budget:budget,mode:"resume"};
  if(oid) body.option=oid;
  const {ok,data}=await jpost("/api/run",body);
  if(!ok||!data.ok){ alert((data&&data.error)||"couldn't resume"); return; }
  tick();
}

/* completion report */
function viewReport(s){
  const f=s.forecast||{}; const a=f.actual||{}; const c=s.counts||{};
  const task=(s.task||"").split("\n")[0];
  const acc=(f.estimated_cost_usd!=null&&a.cost_usd!=null)?accuracyPct(f,a):null;
  const vt=`<div class="tiles">
    <div class="card"><h3>Forecast vs actual</h3>
      ${vaRow("cost",money(f.estimated_cost_usd),money(a.cost_usd))}
      ${vaRow("time",fmin(f.estimated_runtime_min),fmin(a.runtime_min))}
      ${vaRow("steps",f.estimated_steps,(a.steps_done!=null?a.steps_done:c.done))}
      ${acc!=null?`<div class="va"><span class="k">accuracy</span><span class="v">${acc}%</span></div>`:""}
    </div>
    <div class="card"><h3>Pull request</h3>
      <div style="color:var(--mut);font-size:13px">Branch <span style="font-family:var(--mono);color:var(--fg)">${esc(s.branch||"")}</span> is ready to review.</div>
      <div class="actions" style="margin-top:12px"><button class="primary" onclick="openPr()">Open pull request</button></div>
      <div id="prinfo" class="fc-note" style="margin-top:10px"></div>
    </div>
    <div class="card"><h3>What I learned</h3>
      ${s.has_memory?`<div style="color:var(--good);font-size:12.5px">&#43; saved ${s.memory_count} note(s) to project memory</div>
        <span class="expand" onclick="loadMemory()">view &rarr;</span><div id="memdump"></div>`:'<div style="color:var(--faint)">Nothing new to remember this time.</div>'}
    </div>
    <div class="card"><h3>The work &middot; ${c.total||0} steps</h3>${workList(s)}</div>
  </div>`;
  return `<div class="screen">
    <div class="hero done">
      <div class="state"><span>&#10003; Delivered</span><span>${dur(s.elapsed_s)} &middot; ${money(s.total_cost_usd)}</span></div>
      <div class="headline">loopd finished the work.</div>
      <div class="sub">${task?esc(task):"See the summary below."}</div>
      <div class="verified">&#10003; every step's checks &nbsp;&nbsp; &#10003; full replay in a clean checkout</div>
    </div>
    ${vt}
    <div class="actions"><button class="primary" onclick="freshObjective()">Start another objective</button>
      <span class="sp"></span><button class="ghost" onclick="showProjects()">&larr; Projects</button></div>
  </div>`;
}
function vaRow(k,p,a){return `<div class="va"><span class="k">${k}</span><span class="v">${p}<span class="arw">&rarr;</span>${a}</span></div>`;}
function workList(s){const steps=(s.steps||[]);
  const rows=steps.slice(0,4).map(st=>`<div class="va"><span class="k">${esc(st.goal||st.id)}</span>
    <span class="v">${st.commit?esc(st.commit)+" ":""}${money(st.cost_usd)}</span></div>`).join("");
  const more=steps.length>4?`<div class="toggle" onclick="openStep('${esc(steps[4].id)}')">${steps.length-4} more &rarr;</div>`:"";
  return rows+more;}
function accuracyPct(p,a){let ps=[];for(const [pk,ak] of [["estimated_cost_usd","cost_usd"],["estimated_runtime_min","runtime_min"]]){const P=p[pk],A=a[ak];if(P==null||A==null)continue;const hi=Math.max(Math.abs(P),Math.abs(A));ps.push(hi<=0?100:Math.max(0,Math.min(100,100*(1-Math.abs(A-P)/hi))));}return ps.length?Math.round(ps.reduce((x,y)=>x+y,0)/ps.length*10)/10:0;}

/* ---------------- activity / console ---------------- */
let ACTIVITY=false;
function toggleActivity(){ACTIVITY=!ACTIVITY; renderActivity();}
async function renderActivity(){const el=$("#activity"); if(!el)return;
  if(!ACTIVITY){el.innerHTML="";return;}
  const tl=(STATE&&STATE.timeline)||[];
  const rows=tl.slice().reverse().map(e=>`<div class="ev"><span class="t">${tfmt(e.ts)}</span><span>${esc(e.text)}</span></div>`).join("");
  const con=await jget("/api/console?repo="+encodeURIComponent(REPO));
  el.innerHTML=`<div class="card"><h3>Timeline</h3><div class="tl">${rows||'<span style="color:var(--faint)">—</span>'}</div></div>
    <div class="card"><h3>Console</h3><pre class="mono">${esc((con&&con.log)||"—")}</pre></div>`;}
function loadConsoleMaybe(){ if(ACTIVITY) renderActivity(); }
function tfmt(ts){if(!ts)return "";const d=new Date(ts*1000);return d.toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"});}

/* ---------------- step drawer ---------------- */
async function openStep(id){const d=await jget("/api/step?repo="+encodeURIComponent(REPO)+"&id="+encodeURIComponent(id));
  if(!d||!d.found){return;} const st=d.step;
  const acc=(st.acceptance_criteria||[]).map(a=>`<li>${esc(a)}</li>`).join("");
  const verified=st.status==="done";
  $("#d-title").innerHTML=`Step ${esc(st.id)} &middot; <span style="color:var(--mut)">${esc(st.status)}</span>`;
  $("#d-body").innerHTML=`
    <div class="sect"><div class="lab">Goal</div><div>${esc(st.goal||"")}</div>${st.details?`<div style="color:var(--mut);margin-top:6px">${esc(st.details)}</div>`:""}</div>
    ${acc?`<div class="sect"><div class="lab">Acceptance</div><ul>${acc}</ul></div>`:""}
    <div class="sect"><div class="lab">Verification</div>
      <div style="color:${verified?'var(--good)':'var(--mut)'}">${verified?"&#10003; all checks passed":(st.verify?esc(st.verify.length+" check(s)"):"—")}</div></div>
    ${st.dev_summary?`<div class="sect"><div class="lab">What the developer did</div><div style="color:var(--mut)">${esc(st.dev_summary)}</div></div>`:""}
    ${d.handover?`<div class="sect"><div class="lab">Handover</div><span class="expand" onclick="this.nextElementSibling.style.display='block';this.style.display='none'">show the diff &amp; gate transcript &rarr;</span>
      <pre class="mono" style="display:none">${esc(d.handover)}</pre></div>`:""}
    ${st.commit_sha?`<div class="sect"><div class="lab">Commit</div><span style="font-family:var(--mono);color:var(--mut)">${esc(st.commit_sha.slice(0,12))}</span></div>`:""}`;
  $("#drawer").classList.add("show");
}
function closeDrawer(){$("#drawer").classList.remove("show");}

/* ---------------- report / memory loaders ---------------- */
async function loadReport(){const d=await jget("/api/report?repo="+encodeURIComponent(REPO));
  openModalHTML(`<h2>Run report</h2><div class="lead">The full write-up.</div>
    <pre class="mono" style="max-height:52vh">${esc((d&&d.report)||"No report yet.")}</pre>
    <div class="frow"><button class="ghost" onclick="closeModal()">Close</button></div>`);}

/* ---------------- delegate / forecast / run ---------------- */
async function delegate(){const t=$("#obj").value.trim(); const msg=$("#msg");
  if(!t){msg.textContent="Tell me what to build.";msg.className="msg err";return;}
  msg.textContent="Estimating…"; msg.className="msg";
  const {data}=await jpost("/api/forecast",{repo:REPO,task:t,budget:CFG.default_budget});
  if(!data||!data.ok){msg.textContent="Couldn't estimate — you can still start."; forecastModal(null,t); return;}
  msg.textContent=""; forecastModal(data.forecast,t);
}
function freshObjective(){ if(STATE) STATE.exists=false; renderProject(Object.assign({},STATE,{exists:false,finished:false,running:false})); }
function forecastModal(f,task){
  PENDING={task, budget: (f?f.budget_usd:CFG.default_budget), constrained:false};
  let body;
  if(f){
    const gap=Number(f.budget_gap_usd)||0, short=gap>0;
    body=`<h2>Execution Forecast</h2><div class="lead">My estimate before we start.</div>
    <div class="fc">
      <div class="row"><span class="k">Estimated cost</span><span class="v">${money(f.estimated_cost_usd)}</span></div>
      <div class="row"><span class="k">Estimated time</span><span class="v">${fmin(f.estimated_runtime_min)}</span></div>
      <div class="row"><span class="k">Steps</span><span class="v">${f.estimated_steps}</span></div>
      <div class="row"><span class="k">Confidence</span><span class="v">${f.confidence}%</span></div>
      <div class="row"><span class="k">Risk</span><span class="v">${esc(f.risk||"")}</span></div>
    </div>
    ${short?`<div class="note">Your budget ${money(f.budget_usd)} is short by ${money(gap)}. I'd set ${money(f.recommended_budget_usd)} for retry room.</div>
      <div class="frow"><button class="primary" onclick="startRun(${f.recommended_budget_usd},false)">Raise to ${money(f.recommended_budget_usd)}</button>
        <button onclick="startRun(${f.budget_usd},true)">Keep ${money(f.budget_usd)} &middot; focus on core</button></div>`
     :`<div class="note">Within your ${money(f.budget_usd)} budget.</div>
      <div class="frow"><button class="primary" onclick="startRun(${f.budget_usd},false)">Start</button></div>`}
    <div class="frow" style="margin-top:8px"><button class="ghost" onclick="closeModal()">Not now</button></div>`;
  }else{
    body=`<h2>Start the run?</h2><div class="lead">I couldn't produce an estimate, but I can still build it.</div>
    <div class="frow"><button class="primary" onclick="startRun(${CFG.default_budget},false)">Start</button>
      <button class="ghost" onclick="closeModal()">Not now</button></div>`;
  }
  openModalHTML(body);
}
async function startRun(budget,constrained){
  closeModal();
  const {ok,data}=await jpost("/api/run",{repo:REPO,task:PENDING.task,budget:budget,constrained:constrained,mode:"new"});
  if(!ok||!data.ok){ const m=$("#msg"); if(m){m.textContent="Couldn't start: "+((data&&data.error)||"unknown");m.className="msg err";} return; }
  tick();
}

/* ---------------- new project ---------------- */
function openNew(){
  const path=CFG.default_repo||"";
  openModalHTML(`<h2>New project</h2><div class="lead">Point me at a folder — the current directory is the default.</div>
    <label>Project folder</label><input id="np-path" value="${esc(path)}" placeholder="~/code/my-service">
    <div class="msg">Cloning a repo? Use <span style="font-family:var(--mono)">loopd clone &lt;url&gt;</span> in the terminal for now.</div>
    <div class="frow"><button class="primary" onclick="openNewGo()">Open</button><button class="ghost" onclick="closeModal()">Cancel</button></div>`);
}
function openNewGo(){const p=$("#np-path").value.trim(); if(!p)return; closeModal(); openProject(encodeURIComponent(p)); }

/* ---------------- settings / modal plumbing ---------------- */
function openSettings(){
  openModalHTML(`<h2>Settings</h2><div class="lead">loopd reads defaults from your .env.</div>
    <div class="fc">
      <div class="row"><span class="k">Default budget</span><span class="v">${money(CFG.default_budget)}</span></div>
      <div class="row"><span class="k">Dashboard</span><span class="v">local only</span></div>
    </div>
    <div class="frow">${REPO?`<button class="ghost" onclick="closeModal();showProjects()">&larr; All projects</button>`:""}
      <button class="primary" onclick="closeModal()">Done</button></div>`);
}
function openModalHTML(html){$("#modal").innerHTML=html; $("#scrim").classList.add("show"); $("#modal").classList.add("show");}
function closeModal(){$("#scrim").classList.remove("show"); $("#modal").classList.remove("show");}
document.addEventListener("keydown",e=>{if(e.key==="Escape"){closeModal();closeDrawer();}});

init();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    sys.exit(main())
