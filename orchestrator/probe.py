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
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request


def _fail(msg: str) -> int:
    print(f"PROBE FAIL: {msg}")
    return 1


def _ok(msg: str) -> int:
    print(f"PROBE OK: {msg}")
    return 0


def probe_http(args) -> int:
    deadline = time.time() + args.timeout
    last_err = "no attempt made"
    while time.time() < deadline:
        try:
            req = urllib.request.Request(args.url, method=args.method)
            with urllib.request.urlopen(req, timeout=min(10, args.timeout)) as resp:
                body = resp.read(65536).decode("utf-8", errors="replace")
                status = resp.status
        except urllib.error.HTTPError as e:
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
                            text=True, preexec_fn=os.setsid)
    captured: list = []
    # A reader thread avoids platform-specific non-blocking-read quirks on text pipes.
    threading.Thread(target=lambda: captured.extend(iter(proc.stdout.readline, "")),
                     daemon=True).start()

    def teardown():
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

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
            p = subprocess.run(cmd, shell=True, cwd=args.cwd or None,
                               capture_output=True, text=True, timeout=args.timeout)
            sys.stdout.write((p.stdout or "") + (p.stderr or ""))
            if p.returncode != 0:
                return _fail(f"--then command exited {p.returncode}: {cmd}")
        return _ok("process came up and all --then checks passed")
    except subprocess.TimeoutExpired:
        return _fail("a --then command timed out")
    finally:
        teardown()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m orchestrator.probe",
                                 description="Deterministic verification probes (exit 0 = pass).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("http", help="HTTP endpoint returns expected status/body")
    p.add_argument("--url", required=True)
    p.add_argument("--method", default="GET")
    p.add_argument("--expect-status", type=int, default=200)
    p.add_argument("--expect-body", default="")
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
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--interval", type=float, default=0.5)
    p.set_defaults(fn=probe_proc_up)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
