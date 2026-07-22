import http.server
import json
import re
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.probe import main as probe_main


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = b'{"status":"ok"}'
            self.send_response(200)
        elif self.path == "/old":
            self.send_response(302)
            self.send_header("Location", "/health")
            self.end_headers()
            return
        else:
            body = b"nope"
            self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class TestProbes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_http_ok(self):
        rc = probe_main(["http", "--url", f"http://127.0.0.1:{self.port}/health",
                         "--expect-status", "200", "--expect-body", "ok", "--timeout", "5"])
        self.assertEqual(rc, 0)

    def test_http_wrong_status(self):
        rc = probe_main(["http", "--url", f"http://127.0.0.1:{self.port}/missing",
                         "--expect-status", "200", "--timeout", "2", "--interval", "0.2"])
        self.assertEqual(rc, 1)

    def test_http_wrong_body(self):
        rc = probe_main(["http", "--url", f"http://127.0.0.1:{self.port}/health",
                         "--expect-status", "200", "--expect-body", "absent-string",
                         "--timeout", "2"])
        self.assertEqual(rc, 1)

    def test_port_open_and_closed(self):
        rc = probe_main(["port", "--port", str(self.port), "--timeout", "3"])
        self.assertEqual(rc, 0)
        with socket.socket() as s:  # find a port that is definitely closed
            s.bind(("127.0.0.1", 0))
            free = s.getsockname()[1]
        rc = probe_main(["port", "--port", str(free), "--timeout", "1", "--interval", "0.2"])
        self.assertEqual(rc, 1)

    def test_http_redirect_not_followed_by_default(self):
        # 3xx status is asserted directly, not the redirect target's 200
        self.assertEqual(probe_main(["http", "--url", f"http://127.0.0.1:{self.port}/old",
                                     "--expect-status", "302", "--timeout", "3"]), 0)
        # a false-pass is prevented: /old does NOT itself serve 200
        self.assertEqual(probe_main(["http", "--url", f"http://127.0.0.1:{self.port}/old",
                                     "--expect-status", "200", "--timeout", "2",
                                     "--interval", "0.2"]), 1)

    def test_http_follow_redirects_opt_in(self):
        self.assertEqual(probe_main(["http", "--url", f"http://127.0.0.1:{self.port}/old",
                                     "--expect-status", "200", "--expect-body", "ok",
                                     "--follow-redirects", "--timeout", "3"]), 0)

    def test_env_file(self):
        p = Path(tempfile.mkdtemp()) / ".env"
        p.write_text("# comment\nDATABASE_URL=postgres://x\nexport GCS_BUCKET=b\nEMPTY=\n")
        self.assertEqual(probe_main(["env-file", "--path", str(p),
                                     "--requires", "DATABASE_URL,GCS_BUCKET"]), 0)
        self.assertEqual(probe_main(["env-file", "--path", str(p),
                                     "--requires", "DATABASE_URL,EMPTY"]), 1)
        self.assertEqual(probe_main(["env-file", "--path", str(p) + ".nope",
                                     "--requires", "X"]), 1)

    def test_env_file_quoted_empty_is_not_defined(self):
        p = Path(tempfile.mkdtemp()) / ".env"
        p.write_text('DATABASE_URL=""\nGCS_BUCKET=\'\'\nREAL="value"\n')
        self.assertEqual(probe_main(["env-file", "--path", str(p), "--requires", "REAL"]), 0)
        self.assertEqual(probe_main(["env-file", "--path", str(p), "--requires", "DATABASE_URL"]), 1)
        self.assertEqual(probe_main(["env-file", "--path", str(p), "--requires", "GCS_BUCKET"]), 1)

    def test_proc_up_ready_log_then_check_and_teardown(self):
        start = ('python3 -c "import time,sys; print(\'READY\', flush=True); time.sleep(30)"')
        rc = probe_main(["proc-up", "--start", start, "--ready-log", "READY",
                         "--then", "test -d .", "--timeout", "15"])
        self.assertEqual(rc, 0)

    def test_proc_up_early_exit_fails(self):
        rc = probe_main(["proc-up", "--start", "python3 -c \"import sys; sys.exit(3)\"",
                         "--ready-log", "NEVER", "--timeout", "5", "--interval", "0.2"])
        self.assertEqual(rc, 1)

    def test_proc_up_failing_then_check(self):
        start = ('python3 -c "import time; print(\'UP\', flush=True); time.sleep(30)"')
        rc = probe_main(["proc-up", "--start", start, "--ready-log", "UP",
                         "--then", "false", "--timeout", "15"])
        self.assertEqual(rc, 1)

    def test_proc_up_then_check_gets_own_budget_not_starved_by_readiness_wait(self):
        # The process only prints READY after ~1s, eating into a short --timeout. With a
        # separate --then-timeout the check still gets its full budget and passes, proving
        # the readiness wait no longer starves the --then phase.
        start = ('python3 -c "import time,sys; time.sleep(1); print(\'READY\', flush=True); '
                 'time.sleep(30)"')
        rc = probe_main(["proc-up", "--start", start, "--ready-log", "READY",
                         "--timeout", "5", "--then", "sleep 2 && test -d .",
                         "--then-timeout", "10"])
        self.assertEqual(rc, 0)

    def test_proc_up_then_timeout_kills_hung_check(self):
        start = ('python3 -c "import time; print(\'UP\', flush=True); time.sleep(30)"')
        rc = probe_main(["proc-up", "--start", start, "--ready-log", "UP",
                         "--then", "sleep 10", "--then-timeout", "1", "--timeout", "15"])
        self.assertEqual(rc, 1)


class _FlowHandler(http.server.BaseHTTPRequestHandler):
    """A tiny stateful API: login → token → create goal (auth required) → fetch it."""
    goals: dict = {}
    _next = [7]

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def _auth_ok(self):
        return self.headers.get("Authorization") == "Bearer tok-abc"

    def do_POST(self):
        if self.path == "/auth/login":
            return self._json(200, {"token": "tok-abc", "user": "alice"}) \
                if self._read().get("password") == "pw" else self._json(401, {"error": "bad creds"})
        if self.path == "/goals":
            if not self._auth_ok():
                return self._json(401, {"error": "unauth"})
            gid = self._next[0]; self._next[0] += 1
            title = self._read().get("title", "")
            self.__class__.goals[gid] = title
            return self._json(201, {"id": gid, "title": title})
        self._json(404, {"error": "nope"})

    def do_GET(self):
        m = re.match(r"^/goals/(\d+)$", self.path)
        if m:
            if not self._auth_ok():
                return self._json(401, {"error": "unauth"})
            gid = int(m.group(1))
            return self._json(200, {"id": gid, "title": self.__class__.goals[gid]}) \
                if gid in self.__class__.goals else self._json(404, {"error": "missing"})
        if self.path == "/plain":
            body = b"not json"
            self.send_response(200); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
            return
        self._json(404, {"error": "nope"})

    def log_message(self, *a):
        pass


class TestFlowProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FlowHandler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _flow(self, spec):
        p = Path(tempfile.mkdtemp()) / "flow.json"
        p.write_text(json.dumps(spec))
        return str(p)

    def test_happy_flow_with_capture_and_interpolation(self):
        f = self._flow({"steps": [
            {"name": "login", "method": "POST", "path": "/auth/login",
             "json": {"email": "a@x.com", "password": "pw"},
             "expect": {"status": 200, "json": {"$.user": "alice"}}, "capture": {"tok": "$.token"}},
            {"name": "create", "method": "POST", "path": "/goals",
             "headers": {"Authorization": "Bearer ${tok}"}, "json": {"title": "Q3 OKR"},
             "expect": {"status": 201, "json": {"$.title": "Q3 OKR"}}, "capture": {"gid": "$.id"}},
            {"name": "fetch", "method": "GET", "path": "/goals/${gid}",
             "headers": {"Authorization": "Bearer ${tok}"},
             "expect": {"status": 200, "body_contains": "Q3 OKR"}},
        ]})
        self.assertEqual(probe_main(["flow", "--file", f, "--base-url", self.base]), 0)

    def test_failing_status_assertion(self):
        f = self._flow({"steps": [{"name": "login-bad", "method": "POST", "path": "/auth/login",
                                   "json": {"password": "wrong"}, "expect": {"status": 200}}]})
        self.assertEqual(probe_main(["flow", "--file", f, "--base-url", self.base]), 1)

    def test_failing_json_assertion(self):
        f = self._flow({"steps": [{"name": "login", "method": "POST", "path": "/auth/login",
                                   "json": {"password": "pw"},
                                   "expect": {"status": 200, "json": {"$.user": "bob"}}}]})
        self.assertEqual(probe_main(["flow", "--file", f, "--base-url", self.base]), 1)

    def test_auth_must_propagate_via_capture(self):
        # not sending the captured token → /goals is 401, so expecting 201 fails
        f = self._flow({"steps": [{"name": "create-no-auth", "method": "POST", "path": "/goals",
                                   "json": {"title": "x"}, "expect": {"status": 201}}]})
        self.assertEqual(probe_main(["flow", "--file", f, "--base-url", self.base]), 1)

    def test_seed_var_flag(self):
        f = self._flow({"steps": [{"name": "create", "method": "POST", "path": "/goals",
                                   "headers": {"Authorization": "Bearer ${tok}"},
                                   "json": {"title": "seeded"}, "expect": {"status": 201}}]})
        self.assertEqual(probe_main(["flow", "--file", f, "--base-url", self.base, "--var", "tok=tok-abc"]), 0)

    def test_non_json_response_when_json_expected(self):
        f = self._flow({"steps": [{"name": "plain", "path": "/plain", "expect": {"json": {"$.x": 1}}}]})
        self.assertEqual(probe_main(["flow", "--file", f, "--base-url", self.base]), 1)

    def test_missing_json_path_fails(self):
        f = self._flow({"steps": [{"name": "login", "method": "POST", "path": "/auth/login",
                                   "json": {"password": "pw"}, "expect": {"json": {"$.nope.deep": 1}}}]})
        self.assertEqual(probe_main(["flow", "--file", f, "--base-url", self.base]), 1)

    def test_bad_flow_file_is_usage_error(self):
        self.assertEqual(probe_main(["flow", "--file", "/nonexistent/flow.json"]), 2)


if __name__ == "__main__":
    unittest.main()
