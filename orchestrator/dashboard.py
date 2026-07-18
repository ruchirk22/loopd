"""loopd mission-control dashboard: launch runs and watch the runtime plan, execute, verify
and decide — live, from the `.agentic/` data the loop writes.

Stdlib only (http.server). LOCAL TOOL: it spawns processes and reads paths you give it, so
it binds to 127.0.0.1 by default. Do not expose it to a network.

    python3 dashboard.py --repo ../my-app          # default target repo, opens on :8787
    python3 dashboard.py --port 9000

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
RUN_PY = REPO_ROOT / "run.py"
ASSETS = REPO_ROOT / "assets"

sys.path.insert(0, str(REPO_ROOT))
from orchestrator.env import load_dotenv  # noqa: E402


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
           "events": [], "timeline": []}
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
    })
    return out


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


def build_run_command(repo, budget, mode: str, constrained: bool = False) -> list:
    """The `run.py` invocation for a launch. Pure, so it's testable without spawning."""
    cmd = [sys.executable, str(RUN_PY), "--repo", str(repo), "--budget", str(budget)]
    cmd.append("--resume-run" if mode == "resume" else "--fresh")
    if constrained:
        cmd.append("--constrained")
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

    def launch(self, repo, task, budget, pm_model, dev_model, mode, constrained=False) -> dict:
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
        cmd = build_run_command(repo, budget, mode, constrained=constrained)
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
                result = manager.launch(
                    repo=repo, task=body.get("task", ""),
                    budget=body.get("budget", default_budget),
                    pm_model=(body.get("pm_model") or "").strip(),
                    dev_model=(body.get("dev_model") or "").strip(),
                    mode=body.get("mode", "new"),
                    constrained=bool(body.get("constrained")))
                self._json(result, 200 if result.get("ok") else 409)
            elif u.path == "/api/forecast":
                self._json(compute_forecast(repo, body.get("task", ""),
                                            body.get("budget", default_budget)))
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
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>loopd — mission control</title>
<link rel="icon" href="/assets/loopd.svg">
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');
:root{
  --bg:#0B0D12; --card:#11131A; --card2:#0d0f15; --line:rgba(255,255,255,.06); --line2:rgba(255,255,255,.10);
  --fg:#F5F5F5; --mut:#9CA3AF; --mut2:#6b7280;
  --acc:#6E7CFF; --acc2:#9D7CFF; --ok:#22C55E; --warn:#FACC15; --bad:#F87171;
  --grad:linear-gradient(135deg,#6E7CFF,#9D7CFF);
  --r:16px; --r2:12px; --r3:8px; --glow:0 0 0 1px rgba(110,124,255,.35), 0 0 24px rgba(110,124,255,.18);
  --font:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  --mono:'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
*{box-sizing:border-box;} html,body{height:100%;}
body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--font);font-size:14px;line-height:1.55;
  -webkit-font-smoothing:antialiased;letter-spacing:-.01em;}
::selection{background:rgba(110,124,255,.3);}
.mono{font-family:var(--mono);}
::-webkit-scrollbar{width:10px;height:10px;} ::-webkit-scrollbar-thumb{background:#1c1f27;border-radius:10px;border:2px solid var(--bg);}

/* top bar */
.top{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:14px;padding:12px 20px;
  border-bottom:1px solid var(--line);background:rgba(9,9,11,.72);backdrop-filter:blur(14px);}
.brand{display:flex;align-items:center;}
.brand img{height:26px;width:100px;object-fit:cover;object-position:center;border-radius:6px;}
.tags{display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
.tag{display:inline-flex;align-items:center;gap:7px;font-size:12px;color:var(--mut);
  border:1px solid var(--line);border-radius:999px;padding:5px 11px;background:var(--card);max-width:320px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.tag b{color:var(--fg);font-weight:500;}
.tag.mono{font-family:var(--mono);font-size:11.5px;}
.top .spacer{flex:1;}
.live{display:inline-flex;align-items:center;gap:7px;font-size:12px;color:var(--mut);}
.live .d{width:7px;height:7px;border-radius:50%;background:var(--mut2);}
.live.on .d{background:var(--ok);box-shadow:0 0 0 3px rgba(74,222,128,.16);animation:pulse 1.6s infinite;}
.btn{font-family:var(--font);font-size:13px;font-weight:600;border-radius:9px;padding:7px 13px;cursor:pointer;
  border:1px solid var(--line2);background:#15171f;color:var(--fg);transition:.15s;}
.btn:hover{border-color:rgba(255,255,255,.2);} .btn:active{transform:translateY(1px);}
.btn.primary{background:linear-gradient(135deg,var(--acc),var(--acc2));border:0;box-shadow:var(--glow);}
.btn.primary:hover{filter:brightness(1.08);}
.btn.ghost{background:transparent;} .btn:disabled{opacity:.4;cursor:not-allowed;}
.btn.danger{color:#ffd9d9;border-color:rgba(248,113,113,.3);background:rgba(248,113,113,.08);}

/* layout */
.wrap{max-width:1360px;margin:0 auto;padding:24px 20px 60px;}
.grid{display:grid;grid-template-columns:1fr 340px;gap:20px;align-items:start;}
@media(max-width:1000px){.grid{grid-template-columns:1fr;}}
.col{display:flex;flex-direction:column;gap:20px;min-width:0;}
.card{background:linear-gradient(180deg,var(--card),var(--card2));border:1px solid var(--line);
  border-radius:var(--r);padding:20px;animation:rise .4s ease both;}
.card h3{margin:0 0 16px;font-size:11px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;color:var(--mut2);}
@keyframes rise{from{opacity:0;transform:translateY(6px);}to{opacity:1;transform:none;}}
@keyframes pulse{0%,100%{opacity:1;}50%{opacity:.4;}}

/* hero */
.hero{padding:26px;}
.hero .state{display:flex;align-items:center;gap:12px;}
.hero .badge{font-size:12px;font-weight:600;padding:5px 12px;border-radius:999px;border:1px solid var(--line2);
  color:var(--mut);letter-spacing:.02em;}
.hero .badge.run{color:var(--acc);border-color:rgba(110,124,255,.4);background:rgba(110,124,255,.08);}
.hero .badge.ok{color:var(--ok);border-color:rgba(74,222,128,.35);background:rgba(74,222,128,.07);}
.hero .badge.bad{color:var(--bad);border-color:rgba(248,113,113,.35);background:rgba(248,113,113,.07);}
.hero .phase{font-size:13px;color:var(--mut);}
.hero .action{margin:14px 0 4px;font-size:26px;font-weight:600;letter-spacing:-.02em;min-height:32px;}
.hero .sub{color:var(--mut);font-size:13.5px;}
.progress{height:8px;border-radius:999px;background:#0a0b10;border:1px solid var(--line);overflow:hidden;margin:20px 0 18px;}
.progress>span{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--acc),var(--acc2));
  border-radius:999px;transition:width .5s cubic-bezier(.4,0,.2,1);}
.herostats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;}
@media(max-width:640px){.herostats{grid-template-columns:repeat(2,1fr);}}
.hstat .k{font-size:11px;color:var(--mut2);letter-spacing:.04em;}
.hstat .v{font-size:19px;font-weight:600;margin-top:3px;letter-spacing:-.02em;}
.hstat .v.mono{font-size:14px;color:var(--mut);}

/* orchestration flow */
.graph{display:flex;align-items:center;gap:0;position:relative;padding-bottom:24px;overflow-x:auto;}
.node{flex:1;min-width:118px;padding:14px;border:1px solid var(--line);border-radius:12px;background:var(--card2);
  transition:.35s;}
.node .top{display:flex;align-items:center;gap:8px;}
.node .ic{width:16px;height:16px;color:var(--mut2);flex:none;}
.node .nm{font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--mut);}
.node .sub{font-size:11px;color:var(--mut2);margin-top:7px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.node.active{border-color:rgba(110,124,255,.5);box-shadow:var(--glow);background:rgba(110,124,255,.06);}
.node.active .nm{color:var(--fg);} .node.active .ic{color:var(--acc);animation:pulse 1.6s infinite;}
.node.done{border-color:rgba(34,197,94,.22);} .node.done .ic{color:var(--ok);}
.edge{width:24px;height:0;border-top:1.5px dashed var(--line2);flex:none;}
.retline{position:absolute;left:9%;right:9%;bottom:6px;height:14px;border:1.5px dashed var(--line2);
  border-top:0;border-radius:0 0 12px 12px;opacity:.6;}
@media(max-width:720px){.graph{flex-direction:column;align-items:stretch;padding-bottom:0;}
  .edge{width:0;height:16px;border-top:0;border-left:1.5px dashed var(--line2);margin-left:24px;}
  .retline{display:none;}}

/* plan cards */
.steps{display:flex;flex-direction:column;gap:10px;}
.step{border:1px solid var(--line);border-radius:12px;padding:14px 16px;background:var(--card2);cursor:pointer;
  transition:.15s;display:flex;align-items:center;gap:14px;animation:rise .35s ease both;}
.step:hover{border-color:var(--line2);transform:translateX(2px);}
.step .num{font-family:var(--mono);font-size:12px;color:var(--mut2);min-width:20px;}
.step .body{flex:1;min-width:0;}
.step .title{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.step .meta{font-size:12px;color:var(--mut2);margin-top:3px;display:flex;gap:14px;flex-wrap:wrap;}
.step .meta .mono{color:var(--mut);}
.sbadge{font-size:11px;font-weight:600;padding:3px 10px;border-radius:999px;border:1px solid var(--line2);
  color:var(--mut);white-space:nowrap;display:inline-flex;align-items:center;gap:6px;}
.sbadge::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor;}
.s-done,.s-completed{color:var(--ok);border-color:rgba(74,222,128,.3);}
.s-in_progress,.s-running{color:var(--acc);border-color:rgba(110,124,255,.35);}
.s-rejected{color:var(--warn);border-color:rgba(250,204,21,.3);}
.s-pending{color:var(--mut2);} .s-skipped{color:var(--warn);border-color:rgba(250,204,21,.3);}

/* timeline */
.tl{display:flex;flex-direction:column;gap:0;position:relative;max-height:420px;overflow:auto;}
.tl .row{display:flex;gap:12px;padding:5px 0;font-size:12.5px;animation:rise .3s ease both;}
.tl .rail{display:flex;flex-direction:column;align-items:center;}
.tl .d{width:9px;height:9px;border-radius:50%;margin-top:5px;background:var(--mut2);flex:none;}
.tl .row:not(:last-child) .rail::after{content:"";width:1px;flex:1;background:var(--line);margin-top:3px;}
.tl .d.ok{background:var(--ok);} .tl .d.warn{background:var(--warn);} .tl .d.bad{background:var(--bad);}
.tl .d.replan{background:var(--acc2);} .tl .d.arrow,.tl .d.info,.tl .d.gate{background:var(--acc);}
.tl .txt{color:var(--fg);} .tl .t{color:var(--mut2);font-family:var(--mono);font-size:11px;}

/* metrics */
.mgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px;}
.mstat{border:1px solid var(--line);border-radius:10px;padding:12px 13px;background:var(--card2);}
.mstat .k{font-size:10.5px;color:var(--mut2);letter-spacing:.04em;text-transform:uppercase;}
.mstat .v{font-size:18px;font-weight:600;margin-top:4px;letter-spacing:-.02em;}

/* console */
.term{background:#08090c;border:1px solid var(--line);border-radius:12px;overflow:hidden;}
.term .bar{display:flex;align-items:center;gap:7px;padding:9px 13px;border-bottom:1px solid var(--line);}
.term .bar i{width:11px;height:11px;border-radius:50%;background:#2a2d36;display:inline-block;}
.term .bar .ttl{color:var(--mut2);font-size:12px;margin-left:6px;font-family:var(--mono);}
.term pre{margin:0;padding:14px;max-height:340px;overflow:auto;font-family:var(--mono);font-size:12px;
  line-height:1.6;color:#c9d1d9;white-space:pre-wrap;word-break:break-word;}

/* report */
pre.report{margin:0;background:#08090c;border:1px solid var(--line);border-radius:12px;padding:16px;
  max-height:420px;overflow:auto;font-family:var(--mono);font-size:12px;line-height:1.6;color:var(--mut2);white-space:pre-wrap;}

/* empty */
.empty{text-align:center;color:var(--mut2);padding:60px 20px;}
.empty .big{font-size:40px;opacity:.4;margin-bottom:14px;}
.empty .t{font-size:16px;color:var(--mut);font-weight:500;margin-bottom:6px;}
.empty .btn{margin-top:18px;}

/* modal + drawer */
.scrim{position:fixed;inset:0;background:rgba(5,5,7,.6);backdrop-filter:blur(3px);opacity:0;pointer-events:none;
  transition:.2s;z-index:40;}
.scrim.show{opacity:1;pointer-events:auto;}
.modal{position:fixed;z-index:50;left:50%;top:50%;transform:translate(-50%,-46%);width:min(560px,92vw);
  background:var(--card);border:1px solid var(--line2);border-radius:16px;padding:24px;opacity:0;pointer-events:none;
  transition:.22s cubic-bezier(.4,0,.2,1);box-shadow:0 30px 80px rgba(0,0,0,.5);}
.modal.show{opacity:1;pointer-events:auto;transform:translate(-50%,-50%);}
.modal h2{margin:0 0 4px;font-size:18px;font-weight:700;letter-spacing:-.02em;}
.modal .lead{color:var(--mut);font-size:13px;margin-bottom:8px;}
.drawer{position:fixed;z-index:50;top:0;right:0;height:100%;width:min(680px,94vw);background:var(--card);
  border-left:1px solid var(--line2);transform:translateX(102%);transition:.28s cubic-bezier(.4,0,.2,1);
  overflow:auto;box-shadow:-30px 0 80px rgba(0,0,0,.5);}
.drawer.show{transform:none;}
.drawer .dh{position:sticky;top:0;background:rgba(17,19,26,.92);backdrop-filter:blur(8px);
  padding:18px 22px;border-bottom:1px solid var(--line);display:flex;align-items:flex-start;gap:12px;}
.drawer .dh h2{margin:0;font-size:16px;font-weight:600;letter-spacing:-.01em;}
.drawer .dh .x{margin-left:auto;cursor:pointer;color:var(--mut);font-size:20px;line-height:1;background:none;border:0;}
.drawer .db{padding:20px 22px;display:flex;flex-direction:column;gap:20px;}
.sec .lab{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--mut2);margin-bottom:8px;font-weight:600;}
.sec pre,.sec ul{margin:0;} .sec ul{padding-left:18px;color:var(--mut);}
.sec pre{background:#08090c;border:1px solid var(--line);border-radius:10px;padding:14px;overflow:auto;max-height:360px;
  font-family:var(--mono);font-size:12px;line-height:1.6;color:#c9d1d9;white-space:pre-wrap;word-break:break-word;}

label{display:block;font-size:12px;color:var(--mut);margin:14px 0 6px;font-weight:500;}
label:first-of-type{margin-top:0;}
input,textarea{width:100%;background:#0b0c11;color:var(--fg);border:1px solid var(--line2);border-radius:10px;
  padding:10px 12px;font-family:var(--font);font-size:13.5px;transition:.15s;}
input::placeholder,textarea::placeholder{color:#565b66;}
input:focus,textarea:focus{outline:none;border-color:var(--acc);box-shadow:0 0 0 3px rgba(110,124,255,.16);}
textarea{min-height:150px;resize:vertical;font-family:var(--mono);font-size:12.5px;}
.frow{display:flex;gap:10px;} .frow>*{flex:1;}
.msg{font-size:13px;margin-top:12px;min-height:18px;} .msg.err{color:var(--bad);} .msg.ok{color:var(--ok);}
.hint{color:var(--mut2);font-weight:400;}
/* Execution forecast */
.fc-card{margin-top:16px;border:1px solid var(--line2);border-radius:var(--r2);background:var(--card2);
  padding:16px;box-shadow:var(--glow);}
.fc-h{font-size:11px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;
  background:var(--grad);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:12px;}
.fc-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;}
.fc-stat{border:1px solid var(--line);border-radius:10px;padding:10px 11px;background:var(--card);}
.fc-stat .k{font-size:10px;color:var(--mut2);letter-spacing:.04em;text-transform:uppercase;}
.fc-stat .v{font-size:17px;font-weight:600;margin-top:3px;letter-spacing:-.02em;}
.fc-gap{margin-top:12px;font-size:12.5px;padding:10px 12px;border-radius:10px;border:1px solid var(--line);}
.fc-gap.bad{color:#fecaca;border-color:rgba(248,113,113,.35);background:rgba(248,113,113,.07);}
.fc-gap.ok{color:#bbf7d0;border-color:rgba(34,197,94,.30);background:rgba(34,197,94,.07);}
.fc-note{color:var(--mut2);font-size:11.5px;margin-top:6px;}
.fc-analyzing{font-family:var(--mono);color:var(--acc);font-size:13px;letter-spacing:.08em;}
.fc-acc{margin-top:12px;font-size:13px;color:var(--mut);} .fc-acc b{color:var(--fg);font-size:16px;}
.btn.sm{padding:5px 11px;font-size:12px;margin-top:8px;}
.chk{display:flex;align-items:center;gap:8px;margin-top:14px;font-size:12.5px;color:var(--mut);font-weight:400;}
.chk input{width:auto;}
</style></head>
<body>
<div class="top">
  <div class="brand"><img src="/assets/loopd.svg" alt="loopd"></div>
  <div class="tags">
    <span class="tag mono" id="t-repo" title="">—</span>
    <span class="tag mono" id="t-branch">—</span>
    <span class="tag" id="t-budget">—</span>
  </div>
  <span class="spacer"></span>
  <span class="live" id="live"><span class="d"></span><span id="live-t">idle</span></span>
  <button class="btn danger" id="stop" disabled>Stop</button>
  <button class="btn ghost" id="resume">Resume</button>
  <button class="btn primary" id="newrun">New run</button>
</div>

<div class="wrap">
  <div id="app"></div>
</div>

<!-- New Run modal -->
<div class="scrim" id="scrim"></div>
<div class="modal" id="modal">
  <h2>New run</h2>
  <div class="lead">loopd will plan, build, verify and commit — step by step.</div>
  <label>Target repo</label>
  <input id="f-repo" placeholder="../my-app">
  <label>Task / brief <span class="hint">— long tasks welcome (your @file)</span></label>
  <textarea id="f-task" placeholder="What to build: objective, constraints, definition of done…"></textarea>
  <div class="frow">
    <div><label>Budget ($)</label><input id="f-budget" type="number" min="1" step="1"></div>
    <div><label>PM model</label><input id="f-pm" placeholder="default"></div>
    <div><label>Dev model</label><input id="f-dev" placeholder="default"></div>
  </div>
  <label class="chk"><input type="checkbox" id="f-constrained"> Constrained mode — prioritize critical work, defer polish</label>
  <div id="f-forecast"></div>
  <div class="frow" style="margin-top:18px;">
    <button class="btn primary" id="f-start">Start run</button>
    <button class="btn ghost" id="f-estimate">Estimate first</button>
    <button class="btn ghost" id="f-cancel">Cancel</button>
  </div>
  <div class="msg" id="f-msg"></div>
</div>

<!-- Step drawer -->
<div class="drawer" id="drawer">
  <div class="dh"><h2 id="d-title">Step</h2><button class="x" id="d-close">✕</button></div>
  <div class="db" id="d-body"></div>
</div>

<script>
const $ = s => document.querySelector(s);
const el = (t,c,h) => { const e=document.createElement(t); if(c)e.className=c; if(h!=null)e.innerHTML=h; return e; };
const esc = s => (s==null?"":String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const money = n => "$"+(Number(n)||0).toFixed(2);
let CFG={default_repo:"",default_budget:25}, REPO="", DASH_STATE=null;

function setHTML(node,html){ if(node && node.dataset.sig!==html){ node.dataset.sig=html; node.innerHTML=html; } }

async function init(){
  try{ CFG=await (await fetch("/api/config")).json(); }catch(e){}
  REPO=CFG.default_repo||"";
  $("#f-repo").value=REPO; $("#f-budget").value=CFG.default_budget||25;
  $("#newrun").onclick=()=>openModal();
  $("#f-cancel").onclick=closeModal; $("#scrim").onclick=closeModal;
  $("#f-start").onclick=()=>launch("new");
  $("#f-estimate").onclick=estimate;
  $("#resume").onclick=()=>launch("resume");
  $("#stop").onclick=stopRun;
  $("#d-close").onclick=()=>$("#drawer").classList.remove("show");
  document.addEventListener("keydown",e=>{ if(e.key==="Escape"){closeModal();$("#drawer").classList.remove("show");} });
  tick(); setInterval(tick,1500);
  if(!REPO) openModal();
}
function openModal(){ $("#scrim").classList.add("show"); $("#modal").classList.add("show"); }
function closeModal(){ $("#scrim").classList.remove("show"); $("#modal").classList.remove("show"); }

async function launch(mode){
  const msg=$("#f-msg"); const repo=(mode==="resume"?REPO:$("#f-repo").value.trim());
  if(!repo){ msg.textContent="Enter a repo path."; msg.className="msg err"; return; }
  msg.textContent="Launching…"; msg.className="msg";
  try{
    const r=await fetch("/api/run",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({repo,task:$("#f-task").value,budget:$("#f-budget").value,
        pm_model:$("#f-pm").value,dev_model:$("#f-dev").value,mode,
        constrained:$("#f-constrained").checked})});
    const d=await r.json();
    if(d.ok){ REPO=repo; msg.textContent="Started ("+d.mode+")"; msg.className="msg ok"; setTimeout(closeModal,700); }
    else{ msg.textContent="✗ "+(d.error||"failed"); msg.className="msg err"; }
  }catch(e){ msg.textContent="✗ "+e; msg.className="msg err"; }
  tick();
}

async function estimate(){
  const msg=$("#f-msg"), fc=$("#f-forecast"); const repo=$("#f-repo").value.trim();
  if(!repo){ msg.textContent="Enter a repo path."; msg.className="msg err"; return; }
  msg.textContent="Analyzing task…"; msg.className="msg";
  fc.innerHTML='<div class="fc-card"><div class="fc-analyzing">████████████ analyzing…</div></div>';
  try{
    const r=await fetch("/api/forecast",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({repo,task:$("#f-task").value,budget:$("#f-budget").value})});
    const d=await r.json();
    if(d.ok){ fc.innerHTML=forecastCardHTML(d.forecast); msg.textContent=""; }
    else{ fc.innerHTML=""; msg.textContent="✗ "+(d.error||"forecast failed"); msg.className="msg err"; }
  }catch(e){ fc.innerHTML=""; msg.textContent="✗ "+e; msg.className="msg err"; }
}
function useRecommended(b){ $("#f-budget").value=b; $("#f-constrained").checked=false; }
function fmtMin(m){ m=Number(m)||0; if(m<1)return Math.round(m*60)+" sec"; if(m<90)return Math.round(m)+" min"; const h=(m/60)|0; return h+"h "+Math.round(m%60)+"m"; }
function riskCls(r){ r=(r||"").toLowerCase(); return r==="high"?"bad":r==="low"?"ok":"warn"; }
function forecastCardHTML(f){
  const gap=Number(f.budget_gap_usd)||0, short=gap>0;
  return `<div class="fc-card"><div class="fc-h">Execution Forecast</div>
    <div class="fc-grid">
      <div class="fc-stat"><div class="k">Est. cost</div><div class="v">${money(f.estimated_cost_usd)}</div></div>
      <div class="fc-stat"><div class="k">Runtime</div><div class="v">${fmtMin(f.estimated_runtime_min)}</div></div>
      <div class="fc-stat"><div class="k">Steps</div><div class="v">${f.estimated_steps}</div></div>
      <div class="fc-stat"><div class="k">Confidence</div><div class="v">${f.confidence}%</div></div>
      <div class="fc-stat"><div class="k">Risk</div><div class="v"><span class="badge ${riskCls(f.risk)}">${esc(f.risk)}</span></div></div>
      <div class="fc-stat"><div class="k">Budget</div><div class="v">${money(f.budget_usd)}</div></div>
    </div>`+
    (short
      ? `<div class="fc-gap bad">Short by ${money(gap)} — recommended ${money(f.recommended_budget_usd)} for retry headroom.
          <button class="btn ghost sm" onclick="useRecommended(${f.recommended_budget_usd})">Use ${money(f.recommended_budget_usd)}</button>
          <div class="fc-note">Or Start anyway to run in constrained mode (core work first; may stop before every criterion).</div></div>`
      : `<div class="fc-gap ok">Budget covers the estimate (${money(-gap)} headroom).</div>`)+
    (f.calibration_samples?`<div class="fc-note">Calibrated on ${f.calibration_samples} prior run(s) in this repo.</div>`:"")+
  `</div>`;
}
function accuracyPct(p,a){
  let parts=[]; for(const [pk,ak] of [["estimated_cost_usd","cost_usd"],["estimated_runtime_min","runtime_min"]]){
    const P=p[pk],A=a[ak]; if(P==null||A==null)continue; const hi=Math.max(Math.abs(P),Math.abs(A));
    parts.push(hi<=0?100:Math.max(0,Math.min(100,100*(1-Math.abs(A-P)/hi)))); }
  return parts.length?Math.round(parts.reduce((x,y)=>x+y,0)/parts.length*10)/10:0;
}
function forecastPanel(s){
  const f=s.forecast; if(!f) return "";
  const a=f.actual;
  if(a){
    const acc=accuracyPct(f,a);
    return `<div class="card"><h3>Forecast vs actual</h3><div class="mgrid">
      <div class="mstat"><div class="k">Cost · predicted</div><div class="v">${money(f.estimated_cost_usd)}</div></div>
      <div class="mstat"><div class="k">Cost · actual</div><div class="v">${money(a.cost_usd)}</div></div>
      <div class="mstat"><div class="k">Runtime · predicted</div><div class="v">${fmtMin(f.estimated_runtime_min)}</div></div>
      <div class="mstat"><div class="k">Runtime · actual</div><div class="v">${fmtMin(a.runtime_min)}</div></div>
      <div class="mstat"><div class="k">Steps · predicted</div><div class="v">${f.estimated_steps}</div></div>
      <div class="mstat"><div class="k">Steps · actual</div><div class="v">${a.steps_done}/${a.steps_total}</div></div>
    </div><div class="fc-acc">Prediction accuracy <b>${acc}%</b>
      <div class="progress" style="margin:8px 0 0;"><span style="width:${acc}%"></span></div></div></div>`;
  }
  return `<div class="card"><h3>Execution forecast</h3><div class="mgrid">
    <div class="mstat"><div class="k">Est. cost</div><div class="v">${money(f.estimated_cost_usd)}</div></div>
    <div class="mstat"><div class="k">Runtime</div><div class="v">${fmtMin(f.estimated_runtime_min)}</div></div>
    <div class="mstat"><div class="k">Steps</div><div class="v">${f.estimated_steps}</div></div>
    <div class="mstat"><div class="k">Confidence</div><div class="v">${f.confidence}%</div></div>
    <div class="mstat"><div class="k">Risk</div><div class="v">${esc(f.risk)}</div></div>
    <div class="mstat"><div class="k">Budget</div><div class="v">${money(f.chosen_budget_usd||f.budget_usd)}</div></div>
  </div>${f.constrained?'<div class="fc-note">⚠ constrained mode — core work prioritized</div>':''}</div>`;
}
async function stopRun(){
  if(!REPO) return;
  await fetch("/api/stop",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({repo:REPO})});
  tick();
}

function dur(s){ if(s==null)return"—"; s=Math.floor(s); const h=(s/3600)|0,m=((s%3600)/60)|0,x=s%60;
  return h?`${h}h ${m}m`:m?`${m}m ${x}s`:`${x}s`; }

async function tick(){
  if(!REPO){ renderEmpty("Enter a repo path to begin."); return; }
  let s; try{ s=await (await fetch("/api/state?repo="+encodeURIComponent(REPO))).json(); }catch(e){ return; }
  DASH_STATE=s;
  renderTop(s); renderApp(s);
  try{ const c=await (await fetch("/api/console?repo="+encodeURIComponent(REPO))).json(); renderConsole(c.log||""); }catch(e){}
}

function renderTop(s){
  const rt=$("#t-repo"); rt.textContent="repo "+shortpath(s.repo||REPO); rt.title=s.repo||REPO;
  $("#t-branch").textContent="⎇ "+(s.branch||"—");
  $("#t-budget").innerHTML = s.budget_usd!=null ? ("<b>"+money(s.total_cost_usd)+"</b> / $"+Number(s.budget_usd).toFixed(0)) : "—";
  const live=$("#live");
  live.className="live"+(s.running?" on":"");
  $("#live-t").textContent = s.running?"running":(s.finished?"complete":(s.has_escalation?"stopped":"idle"));
  $("#stop").disabled=!s.running; $("#resume").disabled=s.running;
}
function shortpath(p){ const a=(p||"").split("/"); return a.slice(-2).join("/")||p; }

function renderEmpty(t){
  setHTML($("#app"), `<div class="card"><div class="empty"><div class="big">◍</div>
    <div class="t">No run yet</div><div>${esc(t)}</div>
    <button class="btn primary" onclick="openModal()">Start a run</button></div></div>`);
}

function renderApp(s){
  if(!s.exists){ renderEmpty("Start a run to watch loopd work."); return; }
  const c=s.counts||{done:0,skipped:0,total:0};
  const pct=c.total?Math.round(100*(c.done+c.skipped)/c.total):0;
  let stateCls="", stateTxt="Idle", phase="";
  if(s.running){ stateCls="run"; stateTxt="Running"; phase=phaseLabel(s.active_node); }
  else if(s.finished){ stateCls="ok"; stateTxt="Complete"; phase="All steps verified"; }
  else if(s.has_escalation){ stateCls="bad"; stateTxt="Stopped"; phase="See report"; }
  const action = s.current_step ? esc(s.current_step.goal)
    : (s.finished?"Run complete":(s.plan_summary?esc(s.plan_summary).slice(0,120):"Awaiting plan"));
  const stepline = c.total?`Step ${Math.min(s.step_index||c.done+c.skipped,c.total)} of ${c.total}`:"Planning";
  const m=s.metrics||{};
  const gaterate=m.gate_total?Math.round(100*m.gate_pass/m.gate_total)+"%":"—";

  const hero = `<div class="card hero">
    <div class="state"><span class="badge ${stateCls}">${stateTxt}</span><span class="phase">${esc(phase)}</span></div>
    <div class="action">${action}</div>
    <div class="sub">${stepline}${s.current_step?` · current: ${esc(s.current_step.id)}`:""}</div>
    <div class="progress"><span style="width:${pct}%"></span></div>
    <div class="herostats">
      <div class="hstat"><div class="k">ELAPSED</div><div class="v">${dur(s.elapsed_s)}</div></div>
      <div class="hstat"><div class="k">COST</div><div class="v">${money(s.total_cost_usd)}</div></div>
      <div class="hstat"><div class="k">RETRIES</div><div class="v">${m.rejected||0} rej · ${m.replans||0} replan</div></div>
      <div class="hstat"><div class="k">MODEL</div><div class="v mono">${esc(s.dev_model||s.pm_model||"—")}</div></div>
    </div></div>`;

  const nodes=["planner","developer","verification","review","decision"];
  const order={planner:0,developer:1,verification:2,review:3,decision:4};
  const ai=order[s.active_node];
  const graph = `<div class="card"><h3>Orchestration</h3><div class="graph">`+
    nodes.map((n,i)=>{
      let cls="node"; if(s.active_node===n)cls+=" active"; else if(s.running&&ai!=null&&i<ai)cls+=" done";
      return `<div class="${cls}"><div class="top">${nodeSvg(n)}<span class="nm">${nodeLabel(n)}</span></div>`
        + `<div class="sub">${esc(nodeSub(n,s))}</div></div>`
        + (i<nodes.length-1?`<div class="edge"></div>`:"");
    }).join("")+`<div class="retline"></div></div></div>`;

  const steps = (s.steps&&s.steps.length)
    ? `<div class="card"><h3>Plan · ${c.done}/${c.total} accepted</h3><div class="steps">`+
      s.steps.map((st,i)=>`<div class="step" onclick="openStep('${esc(st.id)}')">
        <span class="num">${String(i+1).padStart(2,'0')}</span>
        <div class="body"><div class="title">${esc(st.goal||st.id)}</div>
          <div class="meta"><span>${st.attempts} attempt${st.attempts===1?'':'s'}</span>
            <span>${money(st.cost_usd)}</span>
            ${st.commit?`<span class="mono">${esc(st.commit)}</span>`:``}
            ${st.verify_count?`<span>${st.verify_count} check${st.verify_count===1?'':'s'}</span>`:``}
            ${st.skip_reason?`<span>· ${esc(st.skip_reason).slice(0,60)}</span>`:``}</div></div>
        <span class="sbadge s-${st.status}">${st.status}</span></div>`).join("")+`</div></div>`
    : `<div class="card"><h3>Plan</h3><div class="empty" style="padding:30px;">Waiting for the planner…</div></div>`;

  const report = s.has_report ? `<div class="card"><h3>Report</h3><pre class="report" id="report">loading…</pre></div>` : "";

  setHTML($("#app"), `<div class="grid">
    <div class="col">${hero}${graph}${steps}
      <div class="card"><h3>Console</h3><div class="term"><div class="bar"><i></i><i></i><i></i>
        <span class="ttl">run.py — ${esc(shortpath(s.repo))}</span></div><pre id="console">—</pre></div></div>
      ${report}</div>
    <div class="col">
      <div class="card"><h3>Metrics</h3><div class="mgrid">
        <div class="mstat"><div class="k">Budget</div><div class="v">${money(s.total_cost_usd)}</div></div>
        <div class="mstat"><div class="k">Runtime</div><div class="v">${dur(s.elapsed_s)}</div></div>
        <div class="mstat"><div class="k">Accepted</div><div class="v">${m.accepted||0}</div></div>
        <div class="mstat"><div class="k">Rejected</div><div class="v">${m.rejected||0}</div></div>
        <div class="mstat"><div class="k">Replans</div><div class="v">${m.replans||0}</div></div>
        <div class="mstat"><div class="k">Gate pass</div><div class="v">${gaterate}</div></div>
      </div></div>
      ${forecastPanel(s)}
      <div class="card"><h3>Timeline</h3><div class="tl" id="tl"></div></div>
      ${s.has_memory?`<div class="card"><h3>Project memory</h3><pre class="report" id="memory">loading…</pre></div>`:""}
    </div></div>`);

  renderTimeline(s.timeline||[]);
  if(s.has_report) loadReport();
  if(s.has_memory) loadMemory();
}

function nodeLabel(n){ return {planner:"Plan",developer:"Developer",verification:"Verification",review:"Review",decision:"Decision"}[n]||"—"; }
function phaseLabel(n){ return {planner:"Planning",developer:"Developing",verification:"Verifying",review:"Reviewing evidence",decision:"Deciding",done:"Done"}[n]||"Working"; }
function nodeSub(n,s){ return {planner:"PM"+(s.pm_model?" · "+s.pm_model:""),developer:"Implement",
  verification:"Deterministic gates",review:"Evidence check",decision:"Accept / Reject / Replan"}[n]||""; }
const _SVG={
  planner:'<path d="M7 3h7l4 4v14H7z"/><path d="M14 3v4h4"/><path d="M10 12h5M10 16h4"/>',
  developer:'<path d="M9 8l-4 4 4 4"/><path d="M15 8l4 4-4 4"/>',
  verification:'<circle cx="12" cy="12" r="9"/><path d="M8.5 12.5l2.4 2.4 4.6-5.2"/>',
  review:'<path d="M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12z"/><circle cx="12" cy="12" r="2.4"/>',
  decision:'<circle cx="6" cy="6" r="2.2"/><circle cx="6" cy="18" r="2.2"/><circle cx="17" cy="9" r="2.2"/><path d="M6 8.2v7.6M6 12h6a5 5 0 0 0 4.7-3.4"/>',
};
function nodeSvg(n){ return '<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'+(_SVG[n]||'')+'</svg>'; }

function renderTimeline(tl){
  const node=$("#tl"); if(!node) return;
  if(!tl.length){ setHTML(node,`<div style="color:var(--mut2);padding:8px 0;">No events yet.</div>`); return; }
  const html = tl.slice().reverse().map(e=>{
    const t=e.ts?new Date(e.ts*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'}):"";
    return `<div class="row"><div class="rail"><span class="d ${esc(e.kind)}"></span></div>
      <div><div class="txt">${esc(e.text)}</div><div class="t">${t}</div></div></div>`;
  }).join("");
  setHTML(node,html);
}

function renderConsole(log){
  const pre=$("#console"); if(!pre) return;
  const atBottom = pre.scrollTop+pre.clientHeight >= pre.scrollHeight-30;
  if(pre.dataset.sig!==log){ pre.dataset.sig=log; pre.textContent=log||"—"; if(atBottom) pre.scrollTop=pre.scrollHeight; }
}
async function loadReport(){
  const pre=$("#report"); if(!pre) return;
  try{ const r=await (await fetch("/api/report?repo="+encodeURIComponent(REPO))).json();
    if(pre.dataset.sig!==r.report){ pre.dataset.sig=r.report; pre.textContent=r.report||""; } }catch(e){}
}
async function loadMemory(){
  const pre=$("#memory"); if(!pre) return;
  try{ const r=await (await fetch("/api/memory?repo="+encodeURIComponent(REPO))).json();
    if(pre.dataset.sig!==r.memory){ pre.dataset.sig=r.memory; pre.textContent=r.memory||"(empty)"; } }catch(e){}
}

async function openStep(id){
  const dr=$("#drawer"); $("#d-title").textContent="Step "+id;
  $("#d-body").innerHTML=`<div style="color:var(--mut);">Loading…</div>`; dr.classList.add("show");
  let d; try{ d=await (await fetch(`/api/step?repo=${encodeURIComponent(REPO)}&id=${encodeURIComponent(id)}`)).json(); }
  catch(e){ $("#d-body").innerHTML=`<div class="err">Failed to load.</div>`; return; }
  if(!d.found){ $("#d-body").innerHTML=`<div style="color:var(--mut);">No detail recorded.</div>`; return; }
  const st=d.step;
  $("#d-title").innerHTML=`${esc(st.goal||st.id)} <span class="sbadge s-${st.status}" style="margin-left:8px;">${st.status}</span>`;
  const sec=(lab,inner)=>`<div class="sec"><div class="lab">${lab}</div>${inner}</div>`;
  const list=a=>a&&a.length?`<ul>${a.map(x=>`<li>${esc(x)}</li>`).join("")}</ul>`:`<div style="color:var(--mut2);">—</div>`;
  let html="";
  html+=sec("Overview",`<div style="color:var(--mut);font-size:13px;line-height:1.7;">
     Attempts ${st.attempts} · Rejections ${st.rejections} · Cost ${money(st.cost_usd)}
     ${st.commit_sha?` · commit <span class="mono">${esc(st.commit_sha.slice(0,9))}</span>`:``}
     ${st.skip_reason?`<br>Descoped: ${esc(st.skip_reason)}`:``}</div>`);
  if(st.details) html+=sec("Details",`<div style="color:var(--mut);">${esc(st.details)}</div>`);
  html+=sec("Acceptance criteria",list(st.acceptance_criteria));
  html+=sec("Verify commands",list(st.verify));
  if(st.dev_summary) html+=sec("Developer summary",`<pre>${esc(st.dev_summary)}</pre>`);
  html+=sec("Handover packet"+(d.handover_count>1?` · latest of ${d.handover_count}`:""),
     d.handover?`<pre>${esc(d.handover)}</pre>`:`<div style="color:var(--mut2);">No handover recorded (step not yet reviewed).</div>`);
  $("#d-body").innerHTML=html;
}
init();
</script>
</body></html>
"""


if __name__ == "__main__":
    sys.exit(main())
