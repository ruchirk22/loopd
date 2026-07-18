"""A tiny local web dashboard for loopd: launch runs (a task box, additive to `@file` for
long tasks) and watch them live — plan, budget, current step, event timeline, console —
by reading the `.agentic/` JSON the loop already writes.

Stdlib only (http.server). LOCAL TOOL: it spawns processes and reads paths you give it, so
it binds to 127.0.0.1 by default. Do not expose it to a network.

    python3 dashboard.py --repo ../my-app          # default target repo, opens on :8787
    python3 dashboard.py --port 9000
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_PY = REPO_ROOT / "run.py"

sys.path.insert(0, str(REPO_ROOT))
from orchestrator.env import load_dotenv  # noqa: E402


# ---------------------------------------------------------------- data

def _tail_events(path: Path, n: int) -> list:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def snapshot(repo, running: bool = False) -> dict:
    """Everything the UI needs about a repo's current/last run — read from `.agentic/`."""
    repo = Path(repo).expanduser().resolve()
    ad = repo / ".agentic"
    state_path = ad / "state.json"
    out = {"repo": str(repo), "exists": state_path.exists(), "running": running, "events": []}
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
    brief = ""
    if (ad / "brief.md").is_file():
        try:
            brief = (ad / "brief.md").read_text(errors="replace")[:4000]
        except OSError:
            brief = ""
    out.update({
        "task": st.get("task", ""),
        "brief": brief,
        "branch": st.get("branch", ""),
        "finished": st.get("finished", False),
        "total_cost_usd": st.get("total_cost_usd", 0.0),
        "budget_usd": st.get("budget_usd"),
        "replans_used": st.get("replans_used", 0),
        "plan_summary": plan.get("summary", ""),
        "steps": [{
            "id": s.get("id"), "goal": s.get("goal"), "status": s.get("status"),
            "attempts": s.get("attempts", 0), "rejections": s.get("rejections", 0),
            "cost_usd": s.get("cost_usd", 0.0), "commit": (s.get("commit_sha") or "")[:9],
            "skip_reason": s.get("skip_reason", ""),
        } for s in steps],
        "counts": {"done": done, "skipped": skipped, "total": len(steps)},
        "current_step": ({"id": current.get("id"), "goal": current.get("goal")}
                         if current else None),
        "events": _tail_events(ad / "log.jsonl", 60),
        "has_report": (ad / "report.md").is_file(),
        "has_escalation": (ad / "escalation.json").is_file(),
    })
    return out


def build_run_command(repo, budget, mode: str) -> list:
    """The `run.py` invocation for a launch. Pure, so it's testable without spawning."""
    cmd = [sys.executable, str(RUN_PY), "--repo", str(repo), "--budget", str(budget)]
    cmd.append("--resume-run" if mode == "resume" else "--fresh")
    return cmd


# ---------------------------------------------------------------- process control

class RunManager:
    def __init__(self) -> None:
        self._procs: dict = {}
        self._lock = threading.Lock()

    def is_running(self, repo) -> bool:
        with self._lock:
            p = self._procs.get(str(Path(repo).expanduser().resolve()))
            return p is not None and p.poll() is None

    def launch(self, repo, task, budget, pm_model, dev_model, mode) -> dict:
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
        cmd = build_run_command(repo, budget, mode)
        env = dict(os.environ)
        if pm_model:
            env["PM_MODEL"] = pm_model
        if dev_model:
            env["DEV_MODEL"] = dev_model
        try:
            logf = open(ad / "dashboard-run.log", "w")
            proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT), env=env,
                                    stdout=logf, stderr=subprocess.STDOUT, text=True)
        except OSError as e:
            return {"ok": False, "error": f"could not launch: {e}"}
        with self._lock:
            self._procs[str(repo)] = proc
        return {"ok": True, "pid": proc.pid, "mode": mode}

    def console(self, repo, n: int = 300) -> str:
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
        def log_message(self, *a):  # quiet
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

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            repo = (q.get("repo", [default_repo]) or [default_repo])[0]
            if u.path == "/":
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif u.path == "/api/config":
                self._json({"default_repo": default_repo or "", "default_budget": default_budget})
            elif u.path == "/api/state":
                if not repo:
                    self._json({"exists": False, "events": [], "repo": ""})
                    return
                self._json(snapshot(repo, running=manager.is_running(repo)))
            elif u.path == "/api/console":
                self._json({"log": manager.console(repo)})
            elif u.path == "/api/report":
                p = Path(repo).expanduser().resolve() / ".agentic" / "report.md"
                self._json({"report": p.read_text(errors="replace") if p.is_file() else ""})
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            u = urlparse(self.path)
            if u.path != "/api/run":
                self._json({"error": "not found"}, 404)
                return
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
            result = manager.launch(
                repo=repo, task=body.get("task", ""),
                budget=body.get("budget", default_budget),
                pm_model=(body.get("pm_model") or "").strip(),
                dev_model=(body.get("dev_model") or "").strip(),
                mode=body.get("mode", "new"),
            )
            self._json(result, 200 if result.get("ok") else 409)

    return Handler


def serve(host: str, port: int, default_repo: str, default_budget: float) -> None:
    manager = RunManager()
    httpd = ThreadingHTTPServer((host, port), _make_handler(manager, default_repo, default_budget))
    url = f"http://{host}:{port}"
    print(f"loopd dashboard on {url}  (local only — do not expose)")
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
    ap = argparse.ArgumentParser(description="loopd web dashboard (local).")
    ap.add_argument("--repo", default="", help="default target repo for the launch form")
    ap.add_argument("--budget", type=float, default=float(os.environ.get("BUDGET_USD", "25")))
    ap.add_argument("--host", default="127.0.0.1", help="bind host (keep it local)")
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args(argv)
    serve(args.host, args.port, args.repo, args.budget)
    return 0


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>loopd dashboard</title>
<style>
  :root { color-scheme: dark; --bg:#0d1117; --panel:#161b22; --line:#30363d; --fg:#e6edf3;
          --mut:#8b949e; --acc:#2f81f7; --ok:#3fb950; --warn:#d29922; --bad:#f85149; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
  header { padding:14px 20px; border-bottom:1px solid var(--line); display:flex; align-items:center; gap:12px; }
  header h1 { font-size:16px; margin:0; font-weight:600; letter-spacing:.3px; }
  header .sub { color:var(--mut); font-size:12px; }
  main { display:grid; grid-template-columns:360px 1fr; gap:16px; padding:16px; align-items:start; }
  @media (max-width:820px){ main{ grid-template-columns:1fr; } }
  .panel { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:16px; }
  .panel h2 { font-size:12px; text-transform:uppercase; letter-spacing:.6px; color:var(--mut); margin:0 0 12px; }
  label { display:block; font-size:12px; color:var(--mut); margin:10px 0 4px; }
  input, textarea, select { width:100%; background:#0d1117; color:var(--fg); border:1px solid var(--line);
          border-radius:6px; padding:8px; font:inherit; }
  textarea { min-height:150px; resize:vertical; font:12px/1.5 ui-monospace,Menlo,Consolas,monospace; }
  .row { display:flex; gap:8px; }
  .row > * { flex:1; }
  button { background:var(--acc); color:#fff; border:0; border-radius:6px; padding:9px 12px; font:inherit;
          font-weight:600; cursor:pointer; margin-top:12px; }
  button.secondary { background:#21262d; border:1px solid var(--line); }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .metrics { display:grid; grid-template-columns:repeat(4,1fr); gap:10px; margin-bottom:14px; }
  .metric { background:#0d1117; border:1px solid var(--line); border-radius:8px; padding:10px; }
  .metric .k { color:var(--mut); font-size:11px; text-transform:uppercase; letter-spacing:.4px; }
  .metric .v { font-size:18px; font-weight:600; margin-top:2px; }
  .bar { height:6px; background:#0d1117; border:1px solid var(--line); border-radius:4px; overflow:hidden; margin-top:6px; }
  .bar > span { display:block; height:100%; background:var(--acc); }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:7px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
  th { color:var(--mut); font-size:11px; text-transform:uppercase; letter-spacing:.4px; }
  .badge { font-size:11px; padding:2px 7px; border-radius:20px; border:1px solid var(--line); white-space:nowrap; }
  .b-done{ color:var(--ok); border-color:#238636; } .b-in_progress{ color:var(--acc); border-color:var(--acc); }
  .b-pending{ color:var(--mut); } .b-skipped{ color:var(--warn); border-color:#9e6a03; }
  .status { display:inline-flex; align-items:center; gap:7px; font-weight:600; }
  .dot { width:9px; height:9px; border-radius:50%; background:var(--mut); }
  .dot.run{ background:var(--acc); animation:pulse 1.2s infinite; } .dot.ok{ background:var(--ok); } .dot.bad{ background:var(--bad); }
  @keyframes pulse { 0%,100%{opacity:1;} 50%{opacity:.35;} }
  .timeline { max-height:220px; overflow:auto; font:12px/1.6 ui-monospace,Menlo,Consolas,monospace; }
  .timeline div { color:var(--mut); } .timeline b { color:var(--fg); font-weight:600; }
  pre.console, pre.report { background:#0d1117; border:1px solid var(--line); border-radius:8px; padding:12px;
          max-height:320px; overflow:auto; font:12px/1.5 ui-monospace,Menlo,Consolas,monospace; white-space:pre-wrap; }
  .muted { color:var(--mut); } .err { color:var(--bad); } .mt { margin-top:16px; }
  .stack { display:flex; flex-direction:column; gap:16px; }
</style></head>
<body>
<header><h1>loopd</h1><span class="sub">local run dashboard</span></header>
<main>
  <section class="panel">
    <h2>Start a run</h2>
    <label>Target repo (path)</label>
    <input id="repo" placeholder="../my-app">
    <label>Task / brief <span class="muted">(long tasks welcome — this is your @file)</span></label>
    <textarea id="task" placeholder="Describe what to build: objective, constraints, definition of done…"></textarea>
    <div class="row">
      <div><label>Budget ($)</label><input id="budget" type="number" step="1" min="1"></div>
      <div><label>PM model</label><input id="pm" placeholder="(default)"></div>
      <div><label>Dev model</label><input id="dev" placeholder="(default)"></div>
    </div>
    <button id="start">Start new run</button>
    <button id="resume" class="secondary">Resume interrupted run</button>
    <div id="launchmsg" class="mt"></div>
  </section>

  <section class="stack">
    <div class="panel">
      <h2>Run status</h2>
      <div id="statusline" class="status"><span class="dot"></span><span>Loading…</span></div>
      <div class="metrics mt">
        <div class="metric"><div class="k">Cost</div><div class="v" id="m-cost">–</div><div class="bar"><span id="m-costbar"></span></div></div>
        <div class="metric"><div class="k">Steps</div><div class="v" id="m-steps">–</div></div>
        <div class="metric"><div class="k">Replans</div><div class="v" id="m-replans">–</div></div>
        <div class="metric"><div class="k">Branch</div><div class="v" id="m-branch" style="font-size:12px;">–</div></div>
      </div>
      <div id="current" class="muted"></div>
    </div>

    <div class="panel">
      <h2>Plan</h2>
      <table><thead><tr><th>Step</th><th>Status</th><th>Att</th><th>Rej</th><th>Cost</th><th>Commit</th></tr></thead>
        <tbody id="steps"><tr><td colspan="6" class="muted">No run yet.</td></tr></tbody></table>
    </div>

    <div class="panel">
      <h2>Timeline</h2>
      <div id="timeline" class="timeline muted">—</div>
    </div>

    <div class="panel">
      <h2>Console</h2>
      <pre class="console" id="console">—</pre>
    </div>

    <div class="panel" id="reportpanel" style="display:none;">
      <h2>Report</h2>
      <pre class="report" id="report"></pre>
    </div>
  </section>
</main>

<script>
const $ = id => document.getElementById(id);
let CFG = { default_repo:"", default_budget:25 };

async function init(){
  try { CFG = await (await fetch("/api/config")).json(); } catch(e){}
  $("repo").value = CFG.default_repo || "";
  $("budget").value = CFG.default_budget || 25;
  tick(); setInterval(tick, 2000);
}

function repo(){ return $("repo").value.trim(); }

async function launch(mode){
  const msg = $("launchmsg"); msg.textContent = "Launching…"; msg.className = "mt muted";
  try{
    const r = await fetch("/api/run", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({repo:repo(), task:$("task").value, budget:$("budget").value,
        pm_model:$("pm").value, dev_model:$("dev").value, mode})});
    const d = await r.json();
    if(d.ok){ msg.textContent = "Started (pid "+d.pid+", "+d.mode+"). Watching…"; msg.className="mt"; }
    else { msg.textContent = "✗ "+(d.error||"failed"); msg.className="mt err"; }
  }catch(e){ msg.textContent="✗ "+e; msg.className="mt err"; }
  tick();
}
$("start").onclick = ()=>launch("new");
$("resume").onclick = ()=>launch("resume");

function badge(s){ return '<span class="badge b-'+s+'">'+s+'</span>'; }
function esc(s){ return (s==null?"":String(s)).replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

async function tick(){
  const rp = repo();
  if(!rp){ $("statusline").innerHTML='<span class="dot"></span><span class="muted">Enter a repo path to watch.</span>'; return; }
  let s;
  try { s = await (await fetch("/api/state?repo="+encodeURIComponent(rp))).json(); } catch(e){ return; }

  // status line
  let dot="dot", label="idle";
  if(s.running){ dot="dot run"; label="running"; }
  else if(s.finished){ dot="dot ok"; label="complete"; }
  else if(s.has_escalation){ dot="dot bad"; label="stopped"; }
  else if(!s.exists){ label="no run yet"; }
  $("statusline").innerHTML = '<span class="'+dot+'"></span><span>'+label+'</span>'
    + (s.task && s.task!=="(from brief)" ? ' <span class="muted">— '+esc(s.task).slice(0,120)+'</span>' : '');

  // metrics
  const cost = s.total_cost_usd||0, bud = s.budget_usd||0;
  $("m-cost").textContent = "$"+cost.toFixed(2)+(bud?(" / $"+bud.toFixed(0)):"");
  $("m-costbar").style.width = bud? Math.min(100,100*cost/bud)+"%" : "0%";
  const c = s.counts||{done:0,skipped:0,total:0};
  $("m-steps").textContent = c.total? (c.done+"/"+c.total+(c.skipped?(" ("+c.skipped+" skip)"):"")) : "–";
  $("m-replans").textContent = (s.replans_used!=null)? s.replans_used : "–";
  $("m-branch").textContent = s.branch || "–";
  $("current").textContent = s.current_step ? ("▶ current: "+s.current_step.id+" — "+s.current_step.goal) : "";

  // steps
  const tb = $("steps");
  if(s.steps && s.steps.length){
    tb.innerHTML = s.steps.map(st => '<tr><td>'+esc(st.id)+': '+esc(st.goal)+
      (st.skip_reason?'<br><span class="muted">'+esc(st.skip_reason)+'</span>':'')+'</td><td>'+badge(st.status)+
      '</td><td>'+st.attempts+'</td><td>'+st.rejections+'</td><td>$'+(st.cost_usd||0).toFixed(3)+
      '</td><td class="muted">'+(st.commit||'—')+'</td></tr>').join("");
  } else { tb.innerHTML = '<tr><td colspan="6" class="muted">No plan yet.</td></tr>'; }

  // timeline
  const ev = (s.events||[]).slice().reverse();
  $("timeline").innerHTML = ev.length ? ev.map(e=>{
    const t = e.ts? new Date(e.ts*1000).toLocaleTimeString() : "";
    return '<div><span class="muted">'+t+'</span> <b>'+esc(e.event||"")+'</b>'
      +(e.step?(' step '+esc(e.step)):'')+(e.verdict?(' → '+esc(e.verdict)):'')+'</div>';
  }).join("") : '<span class="muted">No events yet.</span>';

  // console
  try { const cl = await (await fetch("/api/console?repo="+encodeURIComponent(rp))).json();
        $("console").textContent = cl.log || "—"; } catch(e){}

  // report
  if(s.has_report){
    try { const rr = await (await fetch("/api/report?repo="+encodeURIComponent(rp))).json();
      $("report").textContent = rr.report || ""; $("reportpanel").style.display = rr.report? "block":"none"; } catch(e){}
  } else { $("reportpanel").style.display="none"; }
}
init();
</script>
</body></html>
"""


if __name__ == "__main__":
    sys.exit(main())
