"""tw-git's HTTP transport: a redirect to another host goes without the App's
token, and what comes back is read by its kind (JSON, a zip, or text).

GitHub sends a job's log and an artifact's zip to a signed storage address on
another host. Until 9 Nov 2026 the token went along and that host refused
every read with 403 (Thornswood #5580 and #5585). Both servers here are on
the loopback, told apart by name (127.0.0.1 and localhost), so the suite
still reaches no network."""

import io
import json
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import helpers  # noqa: F401  (puts src/ on the path)
from thornswood_brokers.github_api import decode_body, urllib_transport

LOG = "2026-11-09T03:40:00Z FAIL Read-SiteFeed reads the live feed\n"


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Redirects(unittest.TestCase):
    def setUp(self):
        seen = self.seen = []

        class Storage(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                if self.headers.get("Authorization"):
                    self.send_response(403)
                    self.end_headers()
                    return
                if self.path == "/log":
                    body, kind = LOG.encode(), "text/plain"
                else:
                    buf = io.BytesIO()
                    with zipfile.ZipFile(buf, "w") as z:
                        z.writestr("gate-report.json", json.dumps({"failed": ["test-feed-store.ps1"]}))
                        z.writestr("trace.bin", b"\xff\xfe\x00")
                    body, kind = buf.getvalue(), "application/zip"
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.storage = serve(Storage)
        storage_port = self.storage.server_address[1]

        class Api(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(("api", self.headers.get("Authorization")))
                if self.path.endswith("/same-host"):
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/json")
                    self.end_headers()
                    return
                if self.path == "/json":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(json.dumps({"auth": self.headers.get("Authorization")}).encode())
                    return
                where = "log" if self.path.endswith("/logs") else "zip"
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{storage_port}/{where}")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.api = serve(Api)
        self.base = f"http://127.0.0.1:{self.api.server_address[1]}"

    def tearDown(self):
        for s in (self.api, self.storage):
            s.shutdown()
            s.server_close()

    def get(self, path):
        return urllib_transport("GET", self.base + path, {"Authorization": "token ghs_test"}, None)

    def test_a_jobs_log_is_read_without_the_token_leaving_github(self):
        r = self.get("/repos/o/r/actions/jobs/1/logs")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.body, LOG)
        self.assertEqual(self.seen, [("api", "token ghs_test"), None])

    def test_an_artifact_comes_back_as_its_files(self):
        r = self.get("/repos/o/r/actions/artifacts/2/zip")
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(r.body["files"]["gate-report.json"]), {"failed": ["test-feed-store.ps1"]})
        self.assertEqual(r.body["files"]["trace.bin"], "(binary, 3 bytes)")
        self.assertNotIn("token ghs_test", self.seen[1:])

    def test_a_redirect_on_the_same_host_keeps_the_token(self):
        r = self.get("/repos/o/r/same-host")
        self.assertEqual(r.body, {"auth": "token ghs_test"})


class Bodies(unittest.TestCase):
    def test_json_by_its_content_type(self):
        self.assertEqual(decode_body(b'{"a": 1}', "application/vnd.github+json"), {"a": 1})

    def test_text_stays_text(self):
        self.assertEqual(decode_body(LOG.encode(), "application/octet-stream"), LOG)

    def test_empty_is_none(self):
        self.assertIsNone(decode_body(b"", "application/json"))


if __name__ == "__main__":
    unittest.main()
