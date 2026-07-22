"""Deterministic verification probes — the PM's non-gameable gate vocabulary for
deployment-shaped steps (containerize, boot against emulators, config correctness).

Every probe exits 0 on success, 1 on failure, 2 on usage error, and prints what it
checked. Designed to be composed into a step's `verify` commands, e.g.:

  python3 -m orchestrator.probe http --url http://localhost:8080/health --expect-status 200 --expect-body status
  python3 -m orchestrator.probe port --port 5432
  python3 -m orchestrator.probe docker-build --path . --tag agentic-check
  python3 -m orchestrator.probe env-file --path .env.production --requires DATABASE_URL,GCS_BUCKET
  python3 -m orchestrator.probe proc-up --start "npm run preview -- --port 4173" \
      --ready-port 4173 --then "python3 -m orchestrator.probe http --url http://localhost:4173 --expect-status 200"

Stdlib only. `proc-up` starts a process group, waits for readiness (log line and/or
open port), runs the --then commands, and ALWAYS tears the process group down.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


def _killpg(pid: int, wait: "subprocess.Popen | None" = None) -> None:
    """SIGTERM then SIGKILL the whole group; success is the GROUP being gone, not just the
    leader (a child trapping SIGTERM keeps the group alive)."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            break
        deadline = time.time() + (3 if sig == signal.SIGTERM else 2)
        while time.time() < deadline:
            try:
                os.killpg(pid, 0)
            except OSError:
                if wait is not None and wait.poll() is None:
                    try:
                        wait.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
                return
            time.sleep(0.1)


def _fail(msg: str) -> int:
    print(f"PROBE FAIL: {msg}")
    return 1


def _ok(msg: str) -> int:
    print(f"PROBE OK: {msg}")
    return 0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # assert the FIRST response's status, not the redirect target's


def probe_http(args) -> int:
    deadline = time.time() + args.timeout
    last_err = "no attempt made"
    opener = (urllib.request.build_opener(_NoRedirect) if not args.follow_redirects
              else urllib.request.build_opener())
    while time.time() < deadline:
        try:
            req = urllib.request.Request(args.url, method=args.method)
            with opener.open(req, timeout=min(10, args.timeout)) as resp:
                body = resp.read(65536).decode("utf-8", errors="replace")
                status = resp.status
        except urllib.error.HTTPError as e:
            # A 3xx surfaces here as an HTTPError when redirects are suppressed — that's
            # the status we want to assert, not a failure.
            status = e.code
            body = (e.read(65536) or b"").decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_err = f"connection failed: {e}"
            time.sleep(args.interval)
            continue
        if status != args.expect_status:
            last_err = f"status {status} != expected {args.expect_status} (body: {body[:200]!r})"
            time.sleep(args.interval)
            continue
        if args.expect_body and args.expect_body not in body:
            return _fail(f"{args.url} returned {status} but body does not contain "
                         f"{args.expect_body!r} (body: {body[:300]!r})")
        return _ok(f"{args.method} {args.url} -> {status}"
                   + (f", body contains {args.expect_body!r}" if args.expect_body else ""))
    return _fail(f"{args.url}: {last_err} (after {args.timeout}s)")


def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_port(args) -> int:
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        if _port_open(args.host, args.port):
            return _ok(f"{args.host}:{args.port} is accepting connections")
        time.sleep(args.interval)
    return _fail(f"{args.host}:{args.port} not reachable after {args.timeout}s")


def probe_docker_build(args) -> int:
    cmd = ["docker", "build", "-q"]
    if args.file:
        cmd += ["-f", args.file]
    if args.tag:
        cmd += ["-t", args.tag]
    cmd.append(args.path)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout)
    except FileNotFoundError:
        return _fail("docker CLI not found on PATH")
    except subprocess.TimeoutExpired:
        return _fail(f"docker build timed out after {args.timeout}s")
    if p.returncode != 0:
        tail = (p.stderr or p.stdout).strip()[-2000:]
        return _fail(f"docker build exited {p.returncode}:\n{tail}")
    return _ok(f"docker build {args.path} succeeded ({p.stdout.strip()[:80]})")


def probe_env_file(args) -> int:
    if not os.path.isfile(args.path):
        return _fail(f"{args.path} does not exist")
    keys = set()
    with open(args.path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.removeprefix("export ").strip()
            val = val.strip()
            # Strip one matching pair of surrounding quotes so KEY="" counts as EMPTY,
            # not defined (the standard placeholder a dev scaffolds without a value).
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            if val.strip():
                keys.add(key)
    required = [k.strip() for k in args.requires.split(",") if k.strip()]
    missing = [k for k in required if k not in keys]
    if missing:
        return _fail(f"{args.path} missing (or empty) required keys: {', '.join(missing)}")
    return _ok(f"{args.path} defines all required keys: {', '.join(required)}")


def probe_proc_up(args) -> int:
    proc = subprocess.Popen(args.start, shell=True, cwd=args.cwd or None,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, start_new_session=True)
    captured: list = []
    # A reader thread avoids platform-specific non-blocking-read quirks on text pipes.
    threading.Thread(target=lambda: captured.extend(iter(proc.stdout.readline, "")),
                     daemon=True).start()

    def teardown():
        # setsid guarantees pgid == proc.pid, so kill the group by pid directly — do NOT
        # go through getpgid(), which raises once the shell leader is reaped and would
        # then skip the kill, leaking a backgrounded server.
        _killpg(proc.pid, wait=proc)

    try:
        deadline = time.time() + args.timeout
        ready = not (args.ready_log or args.ready_port)  # no readiness condition = ready now
        while time.time() < deadline and not ready:
            if proc.poll() is not None:
                out = "".join(captured)[-2000:]
                return _fail(f"process exited early (exit {proc.returncode}):\n{out}")
            log_ok = (not args.ready_log) or (args.ready_log in "".join(captured))
            port_ok = (not args.ready_port) or _port_open(args.host, args.ready_port)
            ready = log_ok and port_ok
            if not ready:
                time.sleep(args.interval)
        if not ready:
            out = "".join(captured)[-2000:]
            return _fail(f"process not ready after {args.timeout}s "
                         f"(waiting for log={args.ready_log!r} port={args.ready_port}):\n{out}")
        print(f"PROBE: process is up ({args.start[:80]!r})")
        for cmd in args.then or []:
            print(f"PROBE THEN: $ {cmd}")
            # Each --then check gets its own full budget — readiness wait must not starve it.
            remaining = args.then_timeout if args.then_timeout else args.timeout
            # Own process group so a hung --then (e.g. playwright spawning browsers)
            # is fully killed on timeout instead of leaking grandchildren.
            tp = subprocess.Popen(cmd, shell=True, cwd=args.cwd or None, start_new_session=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                out, _ = tp.communicate(timeout=remaining)
            except subprocess.TimeoutExpired:
                _killpg(tp.pid)
                return _fail(f"--then command timed out: {cmd}")
            sys.stdout.write(out or "")
            if tp.returncode != 0:
                return _fail(f"--then command exited {tp.returncode}: {cmd}")
        return _ok("process came up and all --then checks passed")
    finally:
        teardown()


_VAR = re.compile(r"\$\{([A-Za-z0-9_]+)\}")


def _interp(s: str, variables: dict) -> str:
    """Replace ${name} with a captured variable, falling back to the environment (handy for
    tokens seeded via --var or exported by a prior setup step). Unknown names are left as-is."""
    def repl(m):
        key = m.group(1)
        if key in variables:
            return str(variables[key])
        return os.environ.get(key, m.group(0))
    return _VAR.sub(repl, s)


def _interp_obj(obj, variables: dict):
    """Interpolate ${name} through a JSON value. A string that is EXACTLY ${name} takes the
    variable's real type (so a captured int/bool stays an int/bool, not a string)."""
    if isinstance(obj, str):
        m = _VAR.fullmatch(obj)
        if m and m.group(1) in variables:
            return variables[m.group(1)]
        return _interp(obj, variables)
    if isinstance(obj, dict):
        return {k: _interp_obj(v, variables) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_interp_obj(v, variables) for v in obj]
    return obj


def _jget(obj, path: str):
    """Minimal JSON accessor: `$.a.b[0].c` (leading `$`/`.` optional). Raises KeyError/
    IndexError/TypeError when the path doesn't resolve — the caller turns that into a fail."""
    p = path.strip()
    if p.startswith("$"):
        p = p[1:]
    cur = obj
    for tok in re.findall(r"[^.\[\]]+|\[\d+\]", p):
        if tok.startswith("["):
            cur = cur[int(tok[1:-1])]          # IndexError/TypeError if not a list
        else:
            cur = cur[tok]                     # KeyError/TypeError if not a dict
    return cur


def _flow_request(url, method, headers, data, timeout, interval):
    """One request, retrying only connection errors up to `timeout` (tolerates a just-booted
    app). A real HTTP response — including 4xx/5xx — is returned immediately to be asserted."""
    deadline = time.time() + max(1, timeout)
    last = "no attempt made"
    opener = urllib.request.build_opener(_NoRedirect)  # assert the first response, not a 3xx target
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            with opener.open(req, timeout=min(15, max(1, timeout))) as resp:
                return resp.status, resp.read(1_000_000).decode("utf-8", errors="replace"), None
        except urllib.error.HTTPError as e:
            body = (e.read(1_000_000) or b"").decode("utf-8", errors="replace")
            e.close()
            return e.code, body, None
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last = f"connection failed: {e}"
            time.sleep(interval)
    return None, "", f"{last} (after {timeout}s)"


def probe_flow(args) -> int:
    """Run a scripted, multi-step HTTP flow from a JSON file and assert each step. This is the
    behavior gate unit tests miss and a browser is overkill for: log in, capture a token, use
    it, read the result back, assert. A flow of health GETs against a deployed URL is also how
    you smoke-test a deploy."""
    try:
        spec = json.loads(Path(args.file).read_text())
    except (OSError, ValueError) as e:
        print(f"PROBE FAIL: cannot read flow file {args.file!r}: {e}")
        return 2
    steps = spec.get("steps") if isinstance(spec, dict) else None
    if not isinstance(steps, list) or not steps:
        print("PROBE FAIL: flow file must be an object with a non-empty 'steps' array")
        return 2

    variables: dict = {}
    for kv in args.var:
        k, _, v = kv.partition("=")
        variables[k.strip()] = v

    base = (args.base_url or "").rstrip("/")
    for i, st in enumerate(steps):
        name = st.get("name") or f"step {i + 1}"
        method = str(st.get("method", "GET")).upper()
        path = _interp(str(st.get("path", "")), variables)
        url = (base + path) if (base and path.startswith("/")) else (base + path if base else path)
        headers = {k: _interp(str(v), variables) for k, v in (st.get("headers") or {}).items()}
        data = None
        if st.get("json") is not None:
            data = json.dumps(_interp_obj(st["json"], variables)).encode()
            headers.setdefault("Content-Type", "application/json")
        elif st.get("body") is not None:
            data = _interp(str(st["body"]), variables).encode()

        status, body, err = _flow_request(url, method, headers, data, args.timeout, args.interval)
        if err:
            return _fail(f"flow step {name!r} ({method} {url}): {err}")

        exp = st.get("expect") or {}
        if "status" in exp and status != exp["status"]:
            return _fail(f"flow step {name!r}: status {status} != expected {exp['status']} "
                         f"(body: {body[:200]!r})")
        if exp.get("body_contains") and exp["body_contains"] not in body:
            return _fail(f"flow step {name!r}: body does not contain {exp['body_contains']!r} "
                         f"(body: {body[:200]!r})")

        parsed, need_json = None, ("json" in exp) or bool(st.get("capture"))
        if need_json:
            try:
                parsed = json.loads(body)
            except ValueError:
                return _fail(f"flow step {name!r}: expected a JSON response for its "
                             f"assertions/captures, got (body: {body[:200]!r})")
        for jp, expected in (exp.get("json") or {}).items():
            try:
                actual = _jget(parsed, jp)
            except (KeyError, IndexError, TypeError):
                return _fail(f"flow step {name!r}: json path {jp} not found (body: {body[:200]!r})")
            if actual != expected:
                return _fail(f"flow step {name!r}: {jp} = {actual!r} != expected {expected!r}")
        for var, jp in (st.get("capture") or {}).items():
            try:
                variables[var] = _jget(parsed, jp)
            except (KeyError, IndexError, TypeError):
                return _fail(f"flow step {name!r}: cannot capture {var!r} from {jp} "
                             f"(body: {body[:200]!r})")

    return _ok(f"flow {Path(args.file).name!r}: all {len(steps)} step(s) passed")


def probe_isolation(args) -> int:
    """Prove tenant/user boundaries hold: each resource's OWNER can read it, every OTHER
    identity (and, by default, an unauthenticated caller) is denied, and — the check that
    catches the classic bug — the owner's data never leaks into anyone else's response.

    This is the multi-tenant safety gate: data-isolation failures are how multi-tenant apps
    leak, and they're invisible to unit tests."""
    try:
        spec = json.loads(Path(args.file).read_text())
    except (OSError, ValueError) as e:
        print(f"PROBE FAIL: cannot read isolation file {args.file!r}: {e}")
        return 2
    identities = spec.get("identities") if isinstance(spec, dict) else None
    resources = spec.get("resources") if isinstance(spec, dict) else None
    if not isinstance(identities, dict) or not identities or not isinstance(resources, list) or not resources:
        print("PROBE FAIL: isolation file needs a non-empty 'identities' object and 'resources' array")
        return 2
    for name, ident in identities.items():
        if not isinstance(ident, dict) or "header" not in ident or "value" not in ident:
            print(f"PROBE FAIL: identity {name!r} must be {{\"header\": ..., \"value\": ...}}")
            return 2

    variables: dict = {}
    for kv in args.var:
        k, _, v = kv.partition("=")
        variables[k.strip()] = v
    base = (args.base_url or "").rstrip("/")
    default_deny = spec.get("deny_status") or [401, 403, 404]
    checks = 0

    def _headers(ident):
        h = {} if ident is None else {ident["header"]: _interp(str(ident["value"]), variables)}
        return h

    for r in resources:
        owner = r.get("owner")
        if owner not in identities:
            return _fail(f"isolation: resource owner {owner!r} is not one of the identities")
        method = str(r.get("method", "GET")).upper()
        path = _interp(str(r.get("url") or r.get("path") or ""), variables)
        url = (base + path) if (base and path.startswith("/")) else (base + path if base else path)
        marker = r.get("leak_marker")
        deny = r.get("deny_status") or default_deny
        data, ct = None, {}
        if r.get("json") is not None:
            data = json.dumps(_interp_obj(r["json"], variables)).encode()
            ct = {"Content-Type": "application/json"}

        # 1) The owner MUST be allowed (and actually see their data).
        st, body, err = _flow_request(url, method, {**ct, **_headers(identities[owner])},
                                      data, args.timeout, args.interval)
        if err:
            return _fail(f"isolation: owner {owner!r} request failed for {method} {url}: {err}")
        if not 200 <= (st or 0) < 300:
            return _fail(f"isolation: owner {owner!r} was denied its OWN resource {method} {url} "
                         f"(status {st}) — the fixture or auth is wrong")
        if marker and marker not in body:
            return _fail(f"isolation: owner {owner!r} read {method} {url} but the expected "
                         f"leak_marker {marker!r} wasn't there — check the fixture (body: {body[:160]!r})")
        checks += 1

        # 2) Every OTHER identity, and an unauthenticated caller, MUST be denied and MUST NOT
        #    receive the owner's data.
        others = [(n, identities[n]) for n in identities if n != owner]
        if not args.no_unauth_check:
            others.append(("<unauthenticated>", None))
        for name, ident in others:
            st, body, err = _flow_request(url, method, {**ct, **_headers(ident)},
                                          data, args.timeout, args.interval)
            if err:
                return _fail(f"isolation: {name!r} request failed for {method} {url}: {err}")
            if marker and marker in body:
                return _fail(f"CROSS-TENANT LEAK: {name!r} received {owner!r}'s data at {method} {url} "
                             f"(found {marker!r}, status {st})")
            if st not in deny:
                return _fail(f"isolation: {name!r} was NOT denied {owner!r}'s resource {method} {url} "
                             f"(status {st}, expected one of {deny})")
            checks += 1

    return _ok(f"isolation {Path(args.file).name!r}: {len(resources)} resource(s), "
               f"{checks} boundary check(s) held")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m orchestrator.probe",
                                 description="Deterministic verification probes (exit 0 = pass).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("http", help="HTTP endpoint returns expected status/body")
    p.add_argument("--url", required=True)
    p.add_argument("--method", default="GET")
    p.add_argument("--expect-status", type=int, default=200)
    p.add_argument("--expect-body", default="")
    p.add_argument("--follow-redirects", action="store_true",
                   help="follow 3xx (default: assert the first response's status)")
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--interval", type=float, default=1.0)
    p.set_defaults(fn=probe_http)

    p = sub.add_parser("port", help="TCP port is accepting connections")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--interval", type=float, default=1.0)
    p.set_defaults(fn=probe_port)

    p = sub.add_parser("docker-build", help="docker build succeeds")
    p.add_argument("--path", default=".")
    p.add_argument("--file", default="")
    p.add_argument("--tag", default="")
    p.add_argument("--timeout", type=int, default=1200)
    p.set_defaults(fn=probe_docker_build)

    p = sub.add_parser("env-file", help="env file exists and defines required keys")
    p.add_argument("--path", required=True)
    p.add_argument("--requires", required=True, help="comma-separated key names")
    p.set_defaults(fn=probe_env_file)

    p = sub.add_parser("proc-up", help="start a process, wait for readiness, run checks, tear down")
    p.add_argument("--start", required=True, help="shell command that starts the process")
    p.add_argument("--cwd", default="")
    p.add_argument("--ready-log", default="", help="substring of stdout that signals readiness")
    p.add_argument("--ready-port", type=int, default=0, help="port that signals readiness when open")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--then", action="append", default=[], help="check command to run while up (repeatable)")
    p.add_argument("--timeout", type=int, default=120, help="readiness timeout (also the default per --then budget)")
    p.add_argument("--then-timeout", type=int, default=0, help="separate timeout for each --then check (0 = use --timeout)")
    p.add_argument("--interval", type=float, default=0.5)
    p.set_defaults(fn=probe_proc_up)

    p = sub.add_parser("flow", help="run a scripted multi-step HTTP flow (with capture) and assert each step")
    p.add_argument("--file", required=True, help="JSON flow file: {\"steps\": [ {method,path,headers,json,expect,capture}, ... ]}")
    p.add_argument("--base-url", default="", help="prepended to each step's path")
    p.add_argument("--var", action="append", default=[], metavar="K=V",
                   help="seed a variable usable as ${K} (repeatable); ${NAME} also falls back to the environment")
    p.add_argument("--timeout", type=int, default=30, help="per-request connection-retry window (seconds)")
    p.add_argument("--interval", type=float, default=0.5)
    p.set_defaults(fn=probe_flow)

    p = sub.add_parser("isolation", help="prove tenant/user boundaries: owner allowed, others denied, no data leak")
    p.add_argument("--file", required=True,
                   help="JSON: {identities:{name:{header,value}}, resources:[{owner,url,leak_marker,method,json,deny_status}]}")
    p.add_argument("--base-url", default="", help="prepended to each resource's url")
    p.add_argument("--var", action="append", default=[], metavar="K=V",
                   help="seed a variable usable as ${K} (repeatable); ${NAME} also falls back to the environment")
    p.add_argument("--no-unauth-check", action="store_true", help="skip the unauthenticated-access check")
    p.add_argument("--timeout", type=int, default=30, help="per-request connection-retry window (seconds)")
    p.add_argument("--interval", type=float, default=0.5)
    p.set_defaults(fn=probe_isolation)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
