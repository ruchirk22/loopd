import http.server
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

    def test_env_file(self):
        p = Path(tempfile.mkdtemp()) / ".env"
        p.write_text("# comment\nDATABASE_URL=postgres://x\nexport GCS_BUCKET=b\nEMPTY=\n")
        self.assertEqual(probe_main(["env-file", "--path", str(p),
                                     "--requires", "DATABASE_URL,GCS_BUCKET"]), 0)
        self.assertEqual(probe_main(["env-file", "--path", str(p),
                                     "--requires", "DATABASE_URL,EMPTY"]), 1)
        self.assertEqual(probe_main(["env-file", "--path", str(p) + ".nope",
                                     "--requires", "X"]), 1)

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


if __name__ == "__main__":
    unittest.main()
