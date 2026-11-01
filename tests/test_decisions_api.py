"""The decision core's HTTP API and the operator's commands (step 2).

Each test serves a scratch test workspace on 127.0.0.1 and talks to it over
real HTTP. No test reaches a real workspace, a Bot, a deploy, a merge or a Ship.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout

from decisions_helpers import World
from helpers import REPO

from thornswood_decisions import cli
from thornswood_decisions.api import Api, make_server
from thornswood_decisions.store import CoreError, Store

ORIGIN = "https://hermes.example.ts.net"
HERMES = "hermes-subject:patrick"


class Served:
    """A World served over loopback HTTP."""

    def __init__(self, testcase: unittest.TestCase, *, writes: bool = True, origin: str | None = ORIGIN):
        self.w = World(writes=writes)
        testcase.addCleanup(self.w.close)
        self.api = Api(self.w.path, "test", origin=origin, clock=self.w.clock)
        testcase.addCleanup(self.api.close)
        self.server = make_server(self.api, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        t = threading.Thread(target=self.server.serve_forever, daemon=True)
        t.start()
        testcase.addCleanup(self.server.server_close)
        testcase.addCleanup(self.server.shutdown)

    def call(self, method: str, path: str, *, headers: dict | None = None, body=None):
        con = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            raw = json.dumps(body).encode() if body is not None else None
            hdrs = dict(headers or {})
            if raw is not None:
                hdrs["Content-Type"] = "application/json"
            con.request(method, "/api/v1" + path, body=raw, headers=hdrs)
            resp = con.getresponse()
            return resp.status, dict(resp.getheaders()), json.loads(resp.read() or b"{}")
        finally:
            con.close()

    def code(self, kind: str) -> str:
        return self.w.core.auth.operator_pairing_code(self.w.ws, kind, f"test {kind}", by="tests")["code"]

    def desktop(self, principal: str = HERMES) -> dict:
        status, _, body = self.call("POST", "/pair", headers={"X-TW-Hermes-Principal": principal},
                                    body={"code": self.code("desktop")})
        assert status == 200, body
        return body

    def phone(self) -> tuple[str, dict]:
        status, headers, body = self.call("POST", "/pair", headers={"Origin": ORIGIN}, body={"code": self.code("phone")})
        assert status == 200, body
        cookie = headers["Set-Cookie"].split(";")[0]
        return cookie, body


def bearer(d: dict, principal: str = HERMES, csrf: bool = False) -> dict:
    h = {"Authorization": f"Bearer {d['token']}", "X-TW-Hermes-Principal": principal}
    if csrf:
        h["X-CSRF-Token"] = d["csrf"]
    return h


def answer_body(card: dict, key: str = "desk-key-0001", option: str = "a") -> dict:
    return {"option_key": option, "revision": card["revision"], "attention_revision": card["attention_revision"],
            "artifact_sha256": card["artifact_sha256"], "idempotency_key": key}


class LoopbackOnly(unittest.TestCase):
    def test_refuses_a_public_bind(self):
        w = World()
        self.addCleanup(w.close)
        api = Api(w.path, "test", clock=w.clock)
        self.addCleanup(api.close)
        for host in ("0.0.0.0", "100.100.1.10", ""):
            with self.assertRaises(CoreError):
                make_server(api, host, 0)

    def test_unknown_workspace_is_refused(self):
        w = World()
        self.addCleanup(w.close)
        with self.assertRaises(CoreError):
            Api(w.path, "not-there", clock=w.clock)

    @unittest.skipUnless(os.path.isdir("/proc/self/fd"), "counts open files through /proc")
    def test_requests_do_not_keep_files_open(self):
        # The server answers each request on a thread of its own. The core on the
        # VM stopped on "Too many open files" (1 Nov, 10:41 PM) because every
        # request thread opened the store and nothing ever closed it again.
        s = Served(self)
        s.call("GET", "/me")
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(200):
            s.call("GET", "/me")
        self.assertLessEqual(len(os.listdir("/proc/self/fd")) - before, 4)

    def test_every_answer_carries_the_safety_headers(self):
        s = Served(self)
        status, headers, _ = s.call("GET", "/me")
        self.assertEqual(status, 401)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])


class DesktopPairing(unittest.TestCase):
    def test_desktop_gets_its_token_in_the_body_and_reads_cards(self):
        s = Served(self)
        d = s.desktop()
        self.assertIn("token", d)
        card = s.w.card()
        status, _, body = s.call("GET", "/cards", headers=bearer(d))
        self.assertEqual(status, 200)
        self.assertEqual([c["id"] for c in body["cards"]], [card["id"]])
        status, _, me = s.call("GET", "/me", headers=bearer(d))
        self.assertEqual((me["kind"], me["device_kind"]), ("patrick", "desktop"))

    def test_desktop_without_a_hermes_sign_in_cannot_pair(self):
        s = Served(self)
        status, _, body = s.call("POST", "/pair", body={"code": s.code("desktop")})
        self.assertEqual((status, body["error"]), (403, "no_hermes_sign_in"))

    def test_desktop_answers_only_through_the_sign_in_that_paired_it(self):
        s = Served(self)
        d = s.desktop()
        for who in ("hermes-subject:someone-else", ""):
            status, _, body = s.call("GET", "/cards", headers=bearer(d, principal=who))
            self.assertEqual((status, body["error"]), (401, "wrong_hermes_sign_in"))

    def test_a_code_works_once(self):
        s = Served(self)
        code = s.code("desktop")
        h = {"X-TW-Hermes-Principal": HERMES}
        self.assertEqual(s.call("POST", "/pair", headers=h, body={"code": code})[0], 200)
        status, _, body = s.call("POST", "/pair", headers=h, body={"code": code})
        self.assertEqual((status, body["error"]), (401, "wrong_code"))

    def test_pairing_from_a_foreign_origin_is_refused(self):
        s = Served(self)
        status, _, body = s.call("POST", "/pair", headers={"Origin": "https://evil.example"},
                                 body={"code": s.code("phone")})
        self.assertEqual((status, body["error"]), (403, "origin"))


class Answers(unittest.TestCase):
    def test_desktop_answer_needs_csrf_and_is_revision_checked(self):
        s = Served(self)
        d = s.desktop()
        card = s.w.card()
        path = f"/cards/{card['id']}/answer"
        status, _, body = s.call("POST", path, headers=bearer(d), body=answer_body(card))
        self.assertEqual((status, body["error"]), (403, "csrf"))
        stale = dict(answer_body(card), revision=card["revision"] + 1)
        status, _, body = s.call("POST", path, headers=bearer(d, csrf=True), body=stale)
        self.assertEqual((status, body["error"]), (409, "stale_revision"))
        status, _, receipt = s.call("POST", path, headers=bearer(d, csrf=True), body=answer_body(card))
        self.assertEqual(status, 200)
        self.assertEqual(receipt["option_key"], "a")
        # The same key again is the same answer, not a second one.
        status, _, again = s.call("POST", path, headers=bearer(d, csrf=True), body=answer_body(card))
        self.assertEqual((status, again["response_id"], again["replayed"]), (200, receipt["response_id"], True))
        # A new key on an answered card shows the receipt that stands.
        status, _, body = s.call("POST", path, headers=bearer(d, csrf=True), body=answer_body(card, key="desk-key-0002"))
        self.assertEqual((status, body["error"]), (409, "already_answered"))
        self.assertEqual(body["detail"]["receipt"]["response_id"], receipt["response_id"])

    def test_writes_off_refuses_the_answer(self):
        s = Served(self, writes=False)
        d = s.desktop()
        status, _, body = s.call("POST", "/cards/dc_none/answer", headers=bearer(d, csrf=True),
                                 body={"option_key": "a", "revision": 1, "attention_revision": 1,
                                       "artifact_sha256": "0" * 64, "idempotency_key": "desk-key-0003"})
        self.assertEqual((status, body["error"]), (423, "decision_write_off"))

    def test_get_never_answers(self):
        s = Served(self)
        d = s.desktop()
        card = s.w.card()
        status, _, _ = s.call("GET", f"/cards/{card['id']}/answer", headers=bearer(d))
        self.assertEqual(status, 404)
        self.assertEqual(s.w.core.get_card(s.w.patrick(), card["id"])["status"], "pending")

    def test_two_ways_at_once_is_refused(self):
        s = Served(self)
        d = s.desktop()
        status, _, body = s.call("GET", "/cards", headers={**bearer(d), "Cookie": f"tw_session={d['token']}"})
        self.assertEqual((status, body["error"]), (400, "two_sessions"))


class Phone(unittest.TestCase):
    def test_phone_session_rides_only_in_an_httponly_cookie(self):
        s = Served(self)
        status, headers, body = s.call("POST", "/pair", headers={"Origin": ORIGIN}, body={"code": s.code("phone")})
        self.assertEqual(status, 200)
        self.assertNotIn("token", body)
        cookie = headers["Set-Cookie"]
        for part in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/"):
            self.assertIn(part, cookie)

    def test_phone_reads_only_with_mobile_read_on(self):
        s = Served(self)
        cookie, _ = s.phone()
        status, _, body = s.call("GET", "/cards", headers={"Cookie": cookie})
        self.assertEqual((status, body["error"]), (423, "mobile_read_off"))
        s.w.store.set_flag(s.w.ws, "mobile_read", True, by="tests")
        status, _, body = s.call("GET", "/cards", headers={"Cookie": cookie})
        self.assertEqual(status, 200)

    def test_phone_answers_like_desktop_since_step_4(self):
        s = Served(self)
        s.w.store.set_flag(s.w.ws, "mobile_read", True, by="tests")
        cookie, paired = s.phone()
        card = s.w.card()
        h = {"Cookie": cookie, "Origin": ORIGIN, "X-CSRF-Token": paired["csrf"]}
        s.w.store.set_flag(s.w.ws, "decision_write", False, by="tests")
        status, _, body = s.call("POST", f"/cards/{card['id']}/answer", headers=h, body=answer_body(card))
        self.assertEqual((status, body["error"]), (423, "decision_write_off"))
        s.w.store.set_flag(s.w.ws, "decision_write", True, by="tests")
        status, _, body = s.call("POST", f"/cards/{card['id']}/answer", headers=h, body=answer_body(card))
        self.assertEqual((status, body["option_key"]), (200, "a"))
        self.assertEqual(body["device_id"], s.w.store.db.execute("SELECT id FROM devices WHERE kind = 'phone'").fetchone()[0])

    def test_an_answered_card_leaves_the_list_for_answered(self):
        # Patrick, 1 Nov: an answered card kept showing. It leaves Decisions and
        # shows under Answered for a day, with what he chose.
        s = Served(self)
        s.w.store.set_flag(s.w.ws, "mobile_read", True, by="tests")
        cookie, paired = s.phone()
        card, other = s.w.card(title="First"), s.w.card(title="Second", source_key="second")
        h = {"Cookie": cookie, "Origin": ORIGIN, "X-CSRF-Token": paired["csrf"]}
        s.call("POST", f"/cards/{card['id']}/answer", headers=h, body=answer_body(card))
        status, _, body = s.call("GET", "/cards", headers={"Cookie": cookie})
        self.assertEqual([c["id"] for c in body["cards"]], [other["id"]])
        self.assertEqual([(c["id"], c["title"], c["option"]) for c in body["answered"]],
                         [(card["id"], "First", card["options"][0]["label"])])
        s.w.clock.advance(seconds=25 * 3600)
        self.assertEqual(s.w.core.answered(s.w.patrick()), [], "a day later it is gone from Answered")

    def test_a_cookie_write_needs_the_exact_origin(self):
        s = Served(self)
        s.w.store.set_flag(s.w.ws, "mobile_read", True, by="tests")
        cookie, paired = s.phone()
        for origin in (None, "https://evil.example", ORIGIN + ".evil.example"):
            h = {"Cookie": cookie, "X-CSRF-Token": paired["csrf"]}
            if origin:
                h["Origin"] = origin
            status, _, body = s.call("POST", "/sign-out", headers=h)
            self.assertEqual((status, body["error"]), (403, "origin"))

    def test_no_origin_set_means_no_phone_writes(self):
        s = Served(self, origin=None)
        status, _, body = s.call("POST", "/pair", headers={"Origin": ORIGIN}, body={"code": s.code("phone")})
        self.assertEqual((status, body["error"]), (403, "origin"))


class Polling(unittest.TestCase):
    def test_changes_names_the_cards_that_moved(self):
        s = Served(self)
        d = s.desktop()
        status, _, first = s.call("GET", "/cards", headers=bearer(d))
        card = s.w.card()
        status, _, ch = s.call("GET", f"/changes?since={first['seq']}", headers=bearer(d))
        self.assertEqual(status, 200)
        self.assertIn(card["id"], ch["changed"])
        self.assertGreater(ch["seq"], first["seq"])
        status, _, quiet = s.call("GET", f"/changes?since={ch['seq']}", headers=bearer(d))
        self.assertEqual(quiet["changed"], [])

    def test_sign_out_ends_the_session(self):
        s = Served(self)
        d = s.desktop()
        self.assertEqual(s.call("POST", "/sign-out", headers=bearer(d, csrf=True))[0], 200)
        status, _, body = s.call("GET", "/cards", headers=bearer(d))
        self.assertEqual((status, body["error"]), (401, "signed_out"))

    def test_bad_json_and_big_bodies_are_refused(self):
        s = Served(self)
        con = http.client.HTTPConnection("127.0.0.1", s.port, timeout=10)
        self.addCleanup(con.close)
        con.request("POST", "/api/v1/pair", body=b"{not json", headers={"Content-Type": "application/json"})
        resp = con.getresponse()
        self.assertEqual((resp.status, json.loads(resp.read())["error"]), (400, "bad_json"))
        status, _, body = s.call("POST", "/pair", body={"code": "x" * 70000})
        self.assertEqual(status, 413)


class Operator(unittest.TestCase):
    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.db = str(Path(tempfile.mkdtemp()) / "core.db")

    def test_init_makes_six_bots_and_every_flag_off(self):
        code, out, _ = self.run_cli("--db", self.db, "init", "--workspace", "thornswood")
        self.assertEqual(code, 0)
        flags = json.loads(out)["flags"]
        self.assertTrue(flags and not any(flags.values()))
        s = Store(self.db)
        self.addCleanup(s.close)
        bots = {r[0] for r in s.db.execute("SELECT bot FROM principals WHERE bot IS NOT NULL")}
        self.assertEqual(bots, {"bramble", "rift", "sprocket", "echo", "magpie", "wisp"})

    def test_a_real_workspace_flag_needs_the_switch(self):
        self.run_cli("--db", self.db, "init", "--workspace", "thornswood")
        code, _, err = self.run_cli("--db", self.db, "flag", "--workspace", "thornswood", "decision_write", "on")
        self.assertEqual(code, 1)
        self.assertIn("not_switched", err)
        code, _, _ = self.run_cli("--db", self.db, "flag", "--workspace", "thornswood", "decision_write", "off")
        self.assertEqual(code, 0)

    def test_test_cards_only_in_a_test_workspace(self):
        self.run_cli("--db", self.db, "init", "--workspace", "thornswood")
        code, _, err = self.run_cli("--db", self.db, "test-card", "--workspace", "thornswood")
        self.assertEqual(code, 1)
        self.assertIn("not_a_test_workspace", err)
        self.run_cli("--db", self.db, "init", "--workspace", "try", "--test")
        self.run_cli("--db", self.db, "flag", "--workspace", "try", "decision_write", "on")
        code, out, _ = self.run_cli("--db", self.db, "test-card", "--workspace", "try")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["status"], "pending")

    @unittest.skipIf(os.name == "nt", "SIGTERM is a POSIX signal")
    def test_serve_stops_on_sigterm(self):
        self.run_cli("--db", self.db, "init", "--workspace", "thornswood")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        proc = subprocess.Popen([sys.executable, "-m", "thornswood_decisions.cli", "--db", self.db, "serve",
                                 "--workspace", "thornswood", "--port", str(port)],
                                cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertIn("listening on 127.0.0.1", proc.stdout.readline())
            proc.send_signal(signal.SIGTERM)
            self.assertEqual(proc.wait(timeout=10), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            proc.stderr.close()

    def test_bind_room_pair_and_verify(self):
        self.run_cli("--db", self.db, "init", "--workspace", "thornswood")
        code, _, _ = self.run_cli("--db", self.db, "bind-room", "--workspace", "thornswood",
                                  "--room-id", "54b3ad46-3419-4adb-b60d-c79cb347fcee")
        self.assertEqual(code, 0)
        code, out, _ = self.run_cli("--db", self.db, "bind-room", "--workspace", "thornswood",
                                    "--room-id", "00000000-0000-4000-8000-000000000001", "--name", "Test room")
        self.assertEqual((code, json.loads(out)["name"]), (0, "Test room"))
        code, out, _ = self.run_cli("--db", self.db, "pair", "--workspace", "thornswood",
                                    "--kind", "desktop", "--label", "One")
        self.assertEqual(code, 0)
        self.assertRegex(json.loads(out)["code"], r"^[0-9A-Z]{4}-[0-9A-Z]{4}$")
        code, out, _ = self.run_cli("--db", self.db, "verify")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["ok"])


if __name__ == "__main__":
    unittest.main()
