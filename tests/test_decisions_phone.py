"""The phone, read only (step 3): passkeys, the app's routes and files, and the
Work and Room feeds.

Passkey tests run where py_webauthn and its dependencies are installed (the
core's venv on the VM, from deploy/decisions/requirements.txt) and skip
elsewhere, saying so. Everything else is standard library.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from decisions_helpers import World
from test_decisions_api import Served

from thornswood_decisions import feed_reader, passkey
from thornswood_decisions.feeds import Feeds
from thornswood_decisions.passkey import Passkeys
from thornswood_decisions.schema import MIGRATIONS
from thornswood_decisions.store import CoreError

ORIGIN = "https://hermes.example.ts.net"
RP_ID = "hermes.example.ts.net"
ROOM = "54b3ad46-3419-4adb-b60d-c79cb347fcee"
BOTS = ("bramble", "rift", "sprocket", "echo", "magpie", "wisp")

try:
    import cbor2  # noqa: F401
    import cryptography  # noqa: F401
    HAVE_PASSKEY_LIB = passkey.available()
except ImportError:
    HAVE_PASSKEY_LIB = False
NEEDS_LIB = unittest.skipUnless(HAVE_PASSKEY_LIB, "py_webauthn is not installed here (it is in the core's venv on the VM)")


class Migrations(unittest.TestCase):
    # A migration that has run on a store must never change. These are the
    # hashes of the migrations as merged; a new change is a new migration.
    PINNED = {
        1: "65f4ceb6d80118fd6da4be92a326909e3bfe7c6a8271899f2563bd4c8f423a63",
        2: "0410fe14fec9508e85fb8d9e40eb1887d2d5450309fadf59bcc2ff6c5c55ad61",
        3: "8a79c74a0938504cd8e43e758be0dbbe607cd53ca3e7bae273424c55ad3632da",
        4: "7d599a7b92702912c989d590afdfb21235025cf7fda9f24dcc091fc11027c95c",
        5: "d36394b9f9ba7c25fc560c05a7834d52955de09312aed15256602e6a05ce1406",
        6: "720327f6d99e5e74175c44b603b525df3cdae7ae67c1a6aa56778e8d882c9b63",
        7: "3f7658050982a903a9df24690b2f1c2a4d4759c525f86af3b5d7855c222175e0",
    }

    def test_merged_migrations_never_change(self):
        for n, want in self.PINNED.items():
            self.assertEqual(hashlib.sha256(MIGRATIONS[n - 1].encode()).hexdigest(), want, f"migration {n}")


def phone(w: World):
    """A paired phone and its fresh principal."""
    code = w.core.auth.operator_pairing_code(w.ws, "phone", "Pixel", by="tests")["code"]
    dev, s = w.core.auth.redeem_pairing_code(w.ws, code)
    return dev, s, w.core.auth.authenticate(s.token)


@NEEDS_LIB
class PasskeyCeremonies(unittest.TestCase):
    def setUp(self):
        from thornswood_decisions.softkey import SoftKey
        self.SoftKey = SoftKey
        self.w = World()
        self.addCleanup(self.w.close)
        self.dev, self.s, self.p = phone(self.w)
        self.pk = Passkeys(self.w.store, ORIGIN)
        self.key = SoftKey(ORIGIN, RP_ID)

    def enrol(self, key=None):
        key = key or self.key
        return self.pk.register(self.p, key.create(self.pk.register_options(self.p)), "Pixel")

    def test_add_then_sign_in(self):
        self.enrol()
        dev, s = self.pk.sign_in(self.w.ws, self.key.get(self.pk.sign_in_options(self.w.ws)))
        p = self.w.core.auth.authenticate(s.token)
        self.assertEqual((dev, p.device_id, p.fresh, p.kind), (self.dev, self.dev, True, "patrick"))
        self.assertEqual(len(self.pk.list(p)), 1)

    def test_adding_needs_a_fresh_sign_in(self):
        self.w.clock.advance(minutes=6)
        p = self.w.core.auth.authenticate(self.s.token)
        with self.assertRaises(CoreError) as cm:
            self.pk.register_options(p)
        self.assertEqual(cm.exception.reason, "sign_in_again")

    def test_a_challenge_works_once(self):
        self.enrol()
        answer = self.key.get(self.pk.sign_in_options(self.w.ws))
        self.pk.sign_in(self.w.ws, answer)
        with self.assertRaises(CoreError) as cm:
            self.pk.sign_in(self.w.ws, answer)
        self.assertEqual(cm.exception.reason, "bad_challenge")

    def test_a_challenge_expires(self):
        self.enrol()
        opts = self.pk.sign_in_options(self.w.ws)
        self.w.clock.advance(minutes=6)
        with self.assertRaises(CoreError) as cm:
            self.pk.sign_in(self.w.ws, self.key.get(opts))
        self.assertEqual(cm.exception.reason, "bad_challenge")

    def test_a_registration_challenge_belongs_to_its_session(self):
        opts = self.pk.register_options(self.p)
        _, _, other = phone(self.w)
        with self.assertRaises(CoreError) as cm:
            self.pk.register(other, self.key.create(opts), "Pixel")
        self.assertEqual(cm.exception.reason, "bad_challenge")

    def test_another_origin_is_refused_and_counted(self):
        self.enrol()
        for _ in range(5):
            with self.assertRaises(CoreError) as cm:
                self.pk.sign_in(self.w.ws, self.key.get(self.pk.sign_in_options(self.w.ws),
                                                        origin="https://evil.example"))
            self.assertEqual(cm.exception.reason, "passkey_refused")
        with self.assertRaises(CoreError) as cm:
            self.pk.sign_in_options(self.w.ws)
        self.assertEqual(cm.exception.reason, "too_many_tries")

    def test_another_relying_party_is_refused(self):
        self.enrol()
        with self.assertRaises(CoreError) as cm:
            self.pk.sign_in(self.w.ws, self.key.get(self.pk.sign_in_options(self.w.ws), rp_id="evil.example"))
        self.assertEqual(cm.exception.reason, "passkey_refused")

    def test_user_verification_is_required(self):
        lazy = self.SoftKey(ORIGIN, RP_ID, verified=False)
        with self.assertRaises(CoreError) as cm:
            self.enrol(lazy)
        self.assertEqual(cm.exception.reason, "passkey_refused")

    def test_an_unknown_passkey_is_refused(self):
        self.enrol()
        stranger = self.SoftKey(ORIGIN, RP_ID)
        stranger.user_handle = b"x"
        with self.assertRaises(CoreError) as cm:
            self.pk.sign_in(self.w.ws, stranger.get(self.pk.sign_in_options(self.w.ws)))
        self.assertEqual(cm.exception.reason, "unknown_passkey")

    def test_revoking_the_device_stops_its_passkey(self):
        self.enrol()
        self.w.core.auth.revoke_device(self.p, self.dev)
        with self.assertRaises(CoreError) as cm:
            self.pk.sign_in(self.w.ws, self.key.get(self.pk.sign_in_options(self.w.ws)))
        self.assertEqual(cm.exception.reason, "unknown_passkey")

    def test_a_counter_that_goes_back_is_refused(self):
        key = self.SoftKey(ORIGIN, RP_ID, counter=5)
        self.enrol(key)
        self.pk.sign_in(self.w.ws, key.get(self.pk.sign_in_options(self.w.ws)))  # counter 6
        key.counter = 3  # a cloned key replaying an older counter
        with self.assertRaises(CoreError) as cm:
            self.pk.sign_in(self.w.ws, key.get(self.pk.sign_in_options(self.w.ws)))
        self.assertEqual(cm.exception.reason, "passkey_refused")

    def test_the_same_passkey_is_not_added_twice(self):
        self.enrol()
        p = self.w.core.auth.authenticate(self.s.token)
        with self.assertRaises(CoreError) as cm:
            self.pk.register(p, self.key.create(self.pk.register_options(p)), "again")
        self.assertIn(cm.exception.reason, ("passkey_exists", "passkey_refused"))


class PasskeyWithoutLibraryOrOrigin(unittest.TestCase):
    def test_no_https_origin_means_no_passkeys(self):
        w = World()
        self.addCleanup(w.close)
        for origin in (None, "http://hermes.example.ts.net"):
            with self.assertRaises(CoreError) as cm:
                Passkeys(w.store, origin).sign_in_options(w.ws)
            self.assertEqual(cm.exception.reason, "no_origin")

    def test_no_library_is_503_not_a_weaker_sign_in(self):
        w = World()
        self.addCleanup(w.close)
        with mock.patch.dict(sys.modules, {"webauthn": None}):
            with self.assertRaises(CoreError) as cm:
                Passkeys(w.store, ORIGIN).sign_in_options(w.ws)
        self.assertEqual((cm.exception.status, cm.exception.reason), (503, "passkey_unavailable"))


class AppRoutes(unittest.TestCase):
    def test_csrf_rotates_with_the_cookie_and_the_exact_origin(self):
        s = Served(self)
        cookie, paired = s.phone()
        status, _, body = s.call("POST", "/csrf", headers={"Cookie": cookie})
        self.assertEqual((status, body["error"]), (403, "origin"))
        status, _, body = s.call("POST", "/csrf", headers={"Cookie": cookie, "Origin": ORIGIN})
        self.assertEqual(status, 200)
        new = body["csrf"]
        old_h = {"Cookie": cookie, "Origin": ORIGIN, "X-CSRF-Token": paired["csrf"]}
        status, _, body = s.call("POST", "/sign-out", headers=old_h)
        self.assertEqual((status, body["error"]), (403, "csrf"))
        self.assertEqual(s.call("POST", "/sign-out", headers={**old_h, "X-CSRF-Token": new})[0], 200)

    def test_csrf_rotation_is_only_for_the_app(self):
        s = Served(self)
        d = s.desktop()
        status, _, body = s.call("POST", "/csrf", headers={"Authorization": f"Bearer {d['token']}",
                                                           "X-TW-Hermes-Principal": "hermes-subject:patrick"})
        self.assertEqual((status, body["error"]), (400, "not_the_app"))

    def test_a_recovery_code_signs_in_as_a_new_phone(self):
        s = Served(self)
        p = s.w.patrick(fresh=True)
        codes = s.w.core.auth.new_recovery_codes(p)
        status, headers, body = s.call("POST", "/recovery-sign-in", headers={"Origin": ORIGIN},
                                       body={"code": codes[0], "label": "New phone"})
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        cookie = headers["Set-Cookie"].split(";")[0]
        status, _, me = s.call("GET", "/me", headers={"Cookie": cookie})
        self.assertEqual((me["device_kind"], me["fresh"]), ("phone", True))
        status, _, body = s.call("POST", "/recovery-sign-in", headers={"Origin": ORIGIN}, body={"code": codes[0]})
        self.assertEqual((status, body["error"]), (401, "wrong_code"))

    def test_with_mobile_read_off_the_phone_signs_in_but_reads_nothing(self):
        s = Served(self)
        cookie, _ = s.phone()
        self.assertEqual(s.call("GET", "/me", headers={"Cookie": cookie})[0], 200)
        for path in ("/cards", "/work", "/room", "/changes?since=0"):
            status, _, body = s.call("GET", path, headers={"Cookie": cookie})
            self.assertEqual((status, body["error"]), (423, "mobile_read_off"), path)

    def test_passkey_routes_need_the_exact_origin(self):
        s = Served(self)
        for path in ("/passkeys/sign-in/options", "/passkeys/sign-in", "/recovery-sign-in"):
            status, _, body = s.call("POST", path, headers={"Origin": "https://evil.example"})
            self.assertEqual((status, body["error"]), (403, "origin"), path)
            status, _, body = s.call("POST", path)
            self.assertEqual((status, body["error"]), (403, "origin"), path)


class AppFiles(unittest.TestCase):
    def setUp(self):
        self.s = Served(self)

    def raw(self, method: str, path: str):
        import http.client
        con = http.client.HTTPConnection("127.0.0.1", self.s.port, timeout=10)
        self.addCleanup(con.close)
        con.request(method, path)
        resp = con.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()

    def test_the_shell_and_its_deep_links(self):
        # Decisions, Board and Rooms, and the tabs' first names, which old links still use.
        for path in ("/m/", "/m/decisions", "/m/board", "/m/rooms", "/m/rooms/00000000-0000-4000-8000-000000000001",
                     "/m/needs-you", "/m/work", "/m/room", "/m/decisions/dec_abc-123"):
            status, headers, body = self.raw("GET", path)
            self.assertEqual(status, 200, path)
            self.assertIn(b'<script src="/m/app.js" defer>', body)
            self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
            self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])

    def test_only_the_app_files(self):
        for path in ("/m/../thornswood_decisions/store.py", "/m/app.py", "/m/decisions/a/b", "/m/%2e%2e/x"):
            self.assertEqual(self.raw("GET", path)[0], 404, path)
        self.assertEqual(self.raw("POST", "/m/")[0], 405)

    def test_types_and_the_worker_policy(self):
        _, h, _ = self.raw("GET", "/m/app.js")
        self.assertTrue(h["Content-Type"].startswith("text/javascript"))
        _, h, body = self.raw("GET", "/m/sw.js")
        self.assertEqual(h["Content-Security-Policy"], "default-src 'none'; connect-src 'self'; img-src 'self'")
        files = body.split(b"const FILES = [", 1)[1].split(b"]", 1)[0]
        self.assertNotIn(b"/api/", files, "the worker keeps the shell only")
        self.assertIn(b"!url.pathname.startsWith('/m/')", body, "the worker leaves the API to the network")
        _, h, body = self.raw("GET", "/m/manifest.webmanifest")
        m = json.loads(body)
        self.assertEqual((m["scope"], m["display"]), ("/m/", "standalone"))
        self.assertTrue({"192x192", "512x512"} <= {i["sizes"] for i in m["icons"]})
        status, h, _ = self.raw("GET", "/")
        self.assertEqual((status, h["Location"]), (302, "/m/"))

    def test_the_app_never_writes_markup_from_the_core(self):
        js = (Path(__file__).resolve().parent.parent / "thornswood_decisions" / "app" / "app.js").read_text(encoding="utf-8")
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(bad, js)
        self.assertNotIn("localStorage", js)


class FeedsIn(unittest.TestCase):
    def setUp(self):
        self.s = Served(self)
        self.w = self.s.w
        self.w.core.bind_room(self.w.ws, ROOM, "Thornswood Workbench", by="tests")
        f = Feeds(self.w.store)
        self.tokens = {n: f.new_token(self.w.ws, n, by="tests") for n in ("work", "room")}

    def put(self, name, body, token=None, headers=None):
        h = {"Authorization": f"Bearer {token or self.tokens[name]}", **(headers or {})}
        return self.s.call("POST", f"/feeds/{name}", headers=h, body=body)

    def test_a_snapshot_replaces_the_last_and_keeps_only_what_is_shown(self):
        body = {"source_time": "2026-11-01T18:00:00+00:00", "secret": "x",
                "tasks": [{"id": "t1", "title": "A" * 500, "assignee": "rift", "status": "running", "body": "private",
                           "priority": 3}]}
        self.assertEqual(self.put("work", body)[0], 200)
        snap = Feeds(self.w.store).get(self.w.ws, "work")
        self.assertEqual(snap["as_of"], "2026-11-01T18:00:00+00:00")
        task = snap["work"]["tasks"][0]
        self.assertEqual(set(task), {"id", "title", "assignee", "status", "updated_at", "priority"})
        self.assertEqual(len(task["title"]), 200)
        self.assertNotIn("secret", snap["work"])
        self.put("work", {"source_time": "2026-11-01T18:01:00+00:00", "tasks": []})
        self.assertEqual(Feeds(self.w.store).get(self.w.ws, "work")["work"]["tasks"], [])

    def test_the_wrong_token_or_a_browser_is_refused(self):
        body = {"source_time": "2026-11-01T18:00:00+00:00", "tasks": []}
        self.assertEqual(self.put("work", body, token=self.tokens["room"])[2]["error"], "bad_feed_token")
        status, _, out = self.put("work", body, headers={"Origin": ORIGIN})
        self.assertEqual((status, out["error"]), (403, "not_a_feed"))
        status, _, out = self.put("work", body, headers={"Cookie": "tw_session=x"})
        self.assertEqual((status, out["error"]), (403, "not_a_feed"))

    def test_the_room_must_be_the_bound_one(self):
        body = {"source_time": "2026-11-01T18:00:00+00:00", "room_id": "another", "events": []}
        status, _, out = self.put("room", body)
        self.assertEqual((status, out["error"]), (409, "not_the_room"))

    def test_sizes(self):
        many = {"source_time": "2026-11-01T18:00:00+00:00", "tasks": [{"id": str(i)} for i in range(501)]}
        self.assertEqual(self.put("work", many)[2]["error"], "bad_feed")
        big = {"source_time": "2026-11-01T18:00:00+00:00", "room_id": ROOM,
               "events": [{"seq": i + 1, "text": "x" * 1900} for i in range(150)]}
        self.assertEqual(self.put("room", big)[0], 200)  # about 290 KB: over the 64 KB limit for everything else
        self.assertEqual(self.s.call("POST", "/pair", body={"code": "x" * 70000})[0], 413)

    def test_the_views_read_them(self):
        self.put("room", {"source_time": "2026-11-01T18:00:00+00:00", "room_id": ROOM,
                          "events": [{"seq": 4, "kind": "message.member", "actor": "Bramble", "text": "hi", "at": ""}]})
        d = self.s.desktop()
        status, _, body = self.s.call("GET", "/room", headers={"Authorization": f"Bearer {d['token']}",
                                                               "X-TW-Hermes-Principal": "hermes-subject:patrick"})
        self.assertEqual((status, body["room"]["rooms"][0]["events"][0]["text"]), (200, "hi"))


class FeedReader(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        k = sqlite3.connect(self.dir / "kanban.db")
        k.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT, "
                  "status TEXT NOT NULL, priority INTEGER DEFAULT 0, created_at INTEGER NOT NULL, "
                  "started_at INTEGER, completed_at INTEGER)")
        k.executemany("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?)", [
            ("t1", "Fix the log", "private body", "sprocket", "running", 2, 1761998400, 1761998500, None),
            ("t2", "Old", None, None, "archived", 0, 1761990000, None, None)])
        k.commit()
        k.close()
        r = sqlite3.connect(self.dir / "shared-state.db")
        r.execute("CREATE TABLE hosted_room_events (room_id TEXT, seq INTEGER, event_id TEXT, kind TEXT, "
                  "actor_json TEXT, authority_epoch INTEGER, payload_json TEXT, created_at REAL)")
        r.executemany("INSERT INTO hosted_room_events VALUES(?,?,?,?,?,?,?,?)", [
            (ROOM, 1, "e1", "message.member", json.dumps({"kind": "member", "id": "b", "display_name": "Bramble"}),
             1, json.dumps({"text": "Starting #12"}), 1761998400.0),
            (ROOM, 2, "e2", "turn.started", json.dumps({"kind": "gateway", "id": "g"}), 1, "{}", 1761998401.0),
            ("other", 1, "e3", "message.user", json.dumps({"kind": "user", "id": "p"}), 1,
             json.dumps({"text": "elsewhere"}), 1761998402.0)])
        r.commit()
        r.close()

    def test_snapshots_hold_only_what_the_views_show(self):
        from datetime import datetime, timezone
        now = datetime(2026, 11, 1, 18, 0, tzinfo=timezone.utc)
        work = feed_reader.work_snapshot(self.dir / "kanban.db", now)
        self.assertEqual([t["id"] for t in work["tasks"]], ["t1"])
        self.assertNotIn("body", work["tasks"][0])
        room = feed_reader.room_snapshot(self.dir / "shared-state.db", ROOM, now)
        self.assertEqual([(e["seq"], e["actor"], e["text"]) for e in room["events"]], [(1, "Bramble", "Starting #12")])
        both = feed_reader.rooms_snapshot(self.dir / "shared-state.db", [ROOM, "other"], now)
        self.assertEqual([(r["room_id"], [e["text"] for e in r["events"]]) for r in both["rooms"]],
                         [(ROOM, ["Starting #12"]), ("other", ["elsewhere"])])

    def test_it_never_writes_to_hermes(self):
        before = {p.name: p.read_bytes() for p in self.dir.iterdir()}
        from datetime import datetime, timezone
        feed_reader.work_snapshot(self.dir / "kanban.db", datetime.now(timezone.utc))
        feed_reader.room_snapshot(self.dir / "shared-state.db", ROOM, datetime.now(timezone.utc))
        self.assertEqual({p.name: p.read_bytes() for p in self.dir.iterdir() if p.name in before}, before)

    @unittest.skipIf(os.name == "nt", "file modes are POSIX: the feed reader runs on the VM and checks its tokens are 0600")
    def test_end_to_end_into_the_core(self):
        s = Served(self)
        s.w.core.bind_room(s.w.ws, ROOM, "Thornswood Workbench", by="tests")
        for bot in BOTS:
            s.w.core.bind_bot(s.w.ws, bot, bot, by="tests")
        tokens = self.dir / "tokens"
        tokens.mkdir()
        f = Feeds(s.w.store)
        for name in ("work", "room", "bots"):
            path = tokens / f"feed-{name}.token"
            path.write_text(f.new_token(s.w.ws, name, by="tests"), encoding="utf-8")
            os.chmod(path, 0o600)
        out = []
        with mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))):
            code = feed_reader.main(["--room", ROOM, "--hermes-home", str(self.dir), "--tokens", str(tokens),
                                     "--core", f"http://127.0.0.1:{s.port}/api/v1"])
        self.assertEqual(code, 0, out)
        self.assertEqual(Feeds(s.w.store).get(s.w.ws, "room")["room"]["rooms"][0]["events"][0]["text"], "Starting #12")

    @unittest.skipIf(os.name == "nt", "file modes are POSIX")
    def test_a_token_others_can_read_is_refused(self):
        path = self.dir / "feed-work.token"
        path.write_text("t", encoding="utf-8")
        os.chmod(path, 0o644)
        with self.assertRaises(SystemExit):
            feed_reader.read_token(path)


if __name__ == "__main__":
    unittest.main()
