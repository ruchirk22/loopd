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
  :root { color-scheme:dark;
    --bg:#0a0d13; --surface:#11161f; --surface2:#161c27; --raise:#1b2330;
    --line:#222b39; --line-hi:#313d4e; --field:#0b0f16;
    --fg:#e9eef6; --mut:#8a95a5; --mut2:#aab4c2;
    --acc:#5b8cff; --acc2:#82a6ff; --grad:linear-gradient(135deg,#5b8cff,#7c5cff);
    --ok:#38d39f; --warn:#f5b544; --bad:#ff6b6b; --r:14px; --r2:10px;
    --shadow:0 10px 30px rgba(0,0,0,.35); }
  * { box-sizing:border-box; }
  body { margin:0; color:var(--fg); -webkit-font-smoothing:antialiased;
    font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    background:
      radial-gradient(900px 460px at 80% -160px, rgba(124,92,255,.10), transparent 60%),
      radial-gradient(1000px 500px at 10% -180px, rgba(91,140,255,.10), transparent 60%),
      var(--bg); }
  code, .mono { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  header { position:sticky; top:0; z-index:5; display:flex; align-items:center; gap:12px;
    padding:13px 22px; border-bottom:1px solid var(--line);
    background:rgba(10,13,19,.72); backdrop-filter:blur(10px); }
  .logo { width:28px; height:28px; border-radius:9px; background:var(--grad);
    display:grid; place-items:center; box-shadow:var(--shadow); }
  .logo svg { width:17px; height:17px; }
  header h1 { font-size:16px; margin:0; font-weight:700; letter-spacing:.2px; }
  header .sub { color:var(--mut); font-size:12px; }
  header .spacer { flex:1; }
  .chip { display:inline-flex; align-items:center; gap:8px; font-size:12px; color:var(--mut2);
    border:1px solid var(--line); border-radius:999px; padding:6px 12px; background:var(--surface); }
  .chip .live { width:7px; height:7px; border-radius:50%; background:var(--ok);
    box-shadow:0 0 0 3px rgba(56,211,159,.15); }
  main { max-width:1500px; margin:0 auto; display:grid; grid-template-columns:380px 1fr;
    gap:18px; padding:20px 22px; align-items:start; }
  @media (max-width:900px){ main { grid-template-columns:1fr; } }
  .panel { background:linear-gradient(180deg,var(--surface),var(--surface2));
    border:1px solid var(--line); border-radius:var(--r); padding:18px; transition:border-color .15s; }
  .panel:hover { border-color:var(--line-hi); }
  .panel h2 { font-size:11px; text-transform:uppercase; letter-spacing:.9px; color:var(--mut);
    margin:0 0 14px; font-weight:600; }
  .stack { display:flex; flex-direction:column; gap:18px; }
  label { display:block; font-size:12px; color:var(--mut); margin:14px 0 6px; font-weight:500; }
  label:first-of-type { margin-top:0; }
  input, textarea { width:100%; background:var(--field); color:var(--fg); border:1px solid var(--line);
    border-radius:var(--r2); padding:10px 11px; font:inherit; transition:border-color .15s,box-shadow .15s; }
  input::placeholder, textarea::placeholder { color:#5c6675; }
  input:focus, textarea:focus { outline:none; border-color:var(--acc); box-shadow:0 0 0 3px rgba(91,140,255,.18); }
  textarea { min-height:190px; resize:vertical; font-family:ui-monospace,Menlo,Consolas,monospace;
    font-size:12.5px; line-height:1.5; }
  .row { display:flex; gap:10px; } .row > * { flex:1; }
  .actions { display:flex; gap:10px; margin-top:16px; }
  button { flex:1; border:0; border-radius:var(--r2); padding:11px 14px; font:inherit; font-weight:600;
    cursor:pointer; transition:transform .05s, filter .15s, border-color .15s; }
  button:active { transform:translateY(1px); }
  .btn-primary { background:var(--grad); color:#fff; box-shadow:var(--shadow); }
  .btn-primary:hover { filter:brightness(1.08); }
  .btn-ghost { background:var(--raise); color:var(--fg); border:1px solid var(--line); }
  .btn-ghost:hover { border-color:var(--line-hi); }
  .hint { color:var(--mut); font-weight:400; }
  .status-head { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:16px; }
  .status { display:inline-flex; align-items:center; gap:9px; font-weight:600; padding:6px 13px;
    border-radius:999px; border:1px solid var(--line); background:var(--surface); }
  .status:has(.dot.run) { border-color:rgba(91,140,255,.4); color:var(--acc2); }
  .status:has(.dot.ok) { border-color:rgba(56,211,159,.4); color:var(--ok); }
  .status:has(.dot.bad) { border-color:rgba(255,107,107,.4); color:var(--bad); }
  .status .muted { color:var(--mut); font-weight:400; }
  .dot { width:9px; height:9px; border-radius:50%; background:var(--mut); flex:none; }
  .dot.run { background:var(--acc); animation:pulse 1.3s infinite; }
  .dot.ok { background:var(--ok); } .dot.bad { background:var(--bad); }
  @keyframes pulse { 0%,100%{ opacity:1; box-shadow:0 0 0 0 rgba(91,140,255,.4); }
    50%{ opacity:.55; box-shadow:0 0 0 5px rgba(91,140,255,0); } }
  .branch { font-size:11.5px; color:var(--mut2); background:var(--field); border:1px solid var(--line);
    border-radius:7px; padding:4px 9px; max-width:46%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .metrics { display:grid; grid-template-columns:1.4fr 1fr 1fr; gap:12px; }
  @media (max-width:560px){ .metrics { grid-template-columns:1fr; } }
  .metric { background:var(--field); border:1px solid var(--line); border-radius:var(--r2); padding:13px 14px; }
  .metric .k { color:var(--mut); font-size:10.5px; text-transform:uppercase; letter-spacing:.6px; font-weight:600; }
  .metric .v { font-size:22px; font-weight:700; margin-top:3px; letter-spacing:-.3px; }
  .gauge { height:7px; background:var(--bg); border:1px solid var(--line); border-radius:999px;
    overflow:hidden; margin-top:10px; }
  .gauge > span { display:block; height:100%; width:0; background:var(--grad); border-radius:999px; transition:width .4s ease; }
  .current { margin-top:14px; color:var(--acc2); font-size:13px; }
  .current:empty { display:none; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  thead th { text-align:left; padding:0 10px 10px; color:var(--mut); font-size:10.5px;
    text-transform:uppercase; letter-spacing:.6px; font-weight:600; border-bottom:1px solid var(--line); }
  tbody td { padding:10px; border-bottom:1px solid var(--line); vertical-align:top; }
  tbody tr:last-child td { border-bottom:0; }
  tbody tr:hover td { background:rgba(255,255,255,.015); }
  .badge { display:inline-flex; align-items:center; gap:6px; font-size:11px; font-weight:600;
    padding:3px 9px; border-radius:999px; border:1px solid var(--line); white-space:nowrap; color:var(--mut2); }
  .badge::before { content:""; width:6px; height:6px; border-radius:50%; background:currentColor; }
  .b-done { color:var(--ok); border-color:rgba(56,211,159,.35); }
  .b-in_progress { color:var(--acc2); border-color:rgba(91,140,255,.35); }
  .b-pending { color:var(--mut); }
  .b-skipped { color:var(--warn); border-color:rgba(245,181,68,.35); }
  .timeline { max-height:260px; overflow:auto; font-family:ui-monospace,Menlo,Consolas,monospace;
    font-size:12px; line-height:1.75; }
  .timeline div { color:var(--mut2); } .timeline .muted { color:var(--mut); } .timeline b { color:var(--fg); font-weight:600; }
  pre.console, pre.report { background:var(--bg); border:1px solid var(--line); border-radius:var(--r2);
    padding:14px; max-height:340px; overflow:auto; color:var(--mut2);
    font-family:ui-monospace,Menlo,Consolas,monospace; font-size:12px; line-height:1.55; white-space:pre-wrap; }
  .empty { color:var(--mut); text-align:center; padding:26px 10px; font-size:13px; }
  .empty .big { font-size:24px; opacity:.45; margin-bottom:8px; }
  .muted { color:var(--mut); } .err { color:var(--bad); } .mt { margin-top:16px; }
  #launchmsg { margin-top:14px; font-size:13px; min-height:18px; }
</style></head>
<body>
<header>
  <div class="logo"><svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.2"
    stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 4v5h-5"/></svg></div>
  <h1>loopd</h1><span class="sub">run dashboard</span>
  <span class="spacer"></span>
  <span class="chip"><span class="live"></span>local · auto-refreshing</span>
</header>
<main>
  <section class="panel">
    <h2>Start a run</h2>
    <label>Target repo</label>
    <input id="repo" placeholder="../my-app">
    <label>Task / brief <span class="hint">— long tasks welcome (your @file)</span></label>
    <textarea id="task" placeholder="What to build: objective, constraints, definition of done…"></textarea>
    <div class="row">
      <div><label>Budget ($)</label><input id="budget" type="number" step="1" min="1"></div>
      <div><label>PM model</label><input id="pm" placeholder="default"></div>
      <div><label>Dev model</label><input id="dev" placeholder="default"></div>
    </div>
    <div class="actions">
      <button id="start" class="btn-primary">Start new run</button>
      <button id="resume" class="btn-ghost">Resume</button>
    </div>
    <div id="launchmsg"></div>
  </section>

  <section class="stack">
    <div class="panel">
      <h2>Run status</h2>
      <div class="status-head">
        <div id="statusline" class="status"><span class="dot"></span><span>Loading…</span></div>
        <code class="branch" id="m-branch">–</code>
      </div>
      <div class="metrics">
        <div class="metric"><div class="k">Cost</div><div class="v" id="m-cost">–</div>
          <div class="gauge"><span id="m-costbar"></span></div></div>
        <div class="metric"><div class="k">Steps</div><div class="v" id="m-steps">–</div></div>
        <div class="metric"><div class="k">Replans</div><div class="v" id="m-replans">–</div></div>
      </div>
      <div id="current" class="current"></div>
    </div>

    <div class="panel">
      <h2>Plan</h2>
      <table><thead><tr><th>Step</th><th>Status</th><th>Att</th><th>Rej</th><th>Cost</th><th>Commit</th></tr></thead>
        <tbody id="steps"><tr><td colspan="6"><div class="empty"><div class="big">◍</div>No run yet — start one on the left.</div></td></tr></tbody></table>
    </div>

    <div class="panel">
      <h2>Timeline</h2>
      <div id="timeline" class="timeline"><div class="empty">No events yet.</div></div>
    </div>

    <div class="panel">
      <h2>Console</h2>
      <pre class="console" id="console">Waiting for output…</pre>
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
