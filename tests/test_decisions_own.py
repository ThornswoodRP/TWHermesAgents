"""Patrick's chat with his own Hermes from the phone: my-hermes on the VM and his Hermes on One.

Checks the core's side (own_chat_write, the passkey sign-in of the last 30
minutes, "One is off", idempotency, one turn at a time in each chat, a lapsed
lease is unclear and never resent, a new chat's id coming back, the chat list
feed and the chats the phone asks for), that the Bots' connector can never
lease one of these messages and one gateway's token cannot reach the other,
migration 7 keeping every older row, the restore quarantine, the API's routes,
and the connector itself against a scratch state.db and a stand-in `hermes`
command and readiness helper. Everything runs in a scratch test workspace;
nothing reaches a real Hermes.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import textwrap
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from decisions_helpers import World
from test_decisions_api import ORIGIN, Served

from thornswood_decisions import backup, own_connector
from thornswood_decisions.chat import Chat
from thornswood_decisions.feeds import Feeds
from thornswood_decisions.own import OwnChat, clean
from thornswood_decisions.schema import MIGRATIONS
from thornswood_decisions.store import CoreError, Store, connect, migrate

SID = "20261101_201500_ab12cd"


def on(w: World, *names: str) -> None:
    for n in names:
        w.store.set_flag(w.ws, n, True, by="tests")


def passkey(w: World, *, minutes_ago: int = 0, method: str = "passkey"):
    """Patrick's principal as a session signed in `minutes_ago` with `method`."""
    from datetime import timedelta
    from thornswood_decisions.store import iso
    return replace(w.patrick(), method=method, signed_in_at=iso(w.clock() - timedelta(minutes=minutes_ago)))


class Gateways:
    """A world with both own-Hermes connector tokens, own_chat_write on, and both gateways heard from."""

    def __init__(self, w: World, *, write: bool = True):
        self.w = w
        self.own = OwnChat(w.store)
        self.tok = {gw: Feeds(w.store).new_token(w.ws, f"own-{gw}", by="tests") for gw in ("me", "one")}
        if write:
            on(w, "own_chat_write")

    def hello(self, gw: str, ready: bool = True, why: str = "") -> dict:
        """The connector says it is there; with own_chat_write off the core still notes it, and answers 423."""
        try:
            return self.own.claim(self.w.ws, gw, self.tok[gw], 0, ready, why)
        except CoreError as exc:
            if exc.status != 423:
                raise
            return {}

    def feed(self, gw: str, chats=None, opened=None) -> dict:
        body = {"source_time": self.w.clock().isoformat(),
                "chats": chats if chats is not None else [{"id": SID, "title": "Plan the week",
                                                          "last_at": self.w.clock().isoformat(), "preview": "ok"}],
                "open": opened if opened is not None else [{"id": SID, "messages": [
                    {"role": "user", "text": "hello", "at": ""}, {"role": "assistant", "text": "hi", "at": ""}]}]}
        return self.own.feed(self.w.ws, gw, self.tok[gw], body)


class Sending(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.g = Gateways(self.w, write=False)
        self.own = self.g.own

    def send(self, gw="one", p=None, sid=SID, new=None, key="own-key-0001", text="hello"):
        return self.own.send(p or passkey(self.w), gw, sid, new, text, key)

    def refused(self, reason, **kw):
        with self.assertRaises(CoreError) as e:
            self.send(**kw)
        self.assertEqual(e.exception.reason, reason)
        return e.exception

    def test_off_until_own_chat_write(self):
        self.g.hello("one")
        self.refused("own_chat_off")
        on(self.w, "own_chat_write")
        self.assertEqual(self.send()["status"], "queued")

    def test_mobile_chat_write_is_not_enough(self):
        on(self.w, "mobile_chat_write", "connector_effects", "mobile_read")
        self.g.hello("one")
        self.refused("own_chat_off")

    def test_needs_a_passkey_sign_in_from_the_last_30_minutes(self):
        on(self.w, "own_chat_write")
        self.g.hello("one")
        self.refused("passkey_needed", p=passkey(self.w, method="pairing"))
        self.refused("passkey_needed", p=passkey(self.w, method="recovery_code"))
        self.refused("passkey_needed", p=passkey(self.w, minutes_ago=30))
        self.refused("passkey_needed", p=self.w.patrick())
        self.assertEqual(self.send(p=passkey(self.w, minutes_ago=29))["status"], "queued")

    def test_one_is_off(self):
        on(self.w, "own_chat_write")
        e = self.refused("one_off")
        self.assertEqual(str(e), "One is off")
        self.g.hello("one", ready=False, why="One's Hermes is absent")
        e = self.refused("one_off")
        self.assertEqual(e.detail["why"], "One's Hermes is absent")
        self.g.hello("one")
        self.assertEqual(self.send()["status"], "queued")
        self.w.clock.advance(minutes=2)
        e = self.refused("one_off", key="own-key-0002")
        self.assertEqual(e.detail["why"], "its connector has not been heard from in 2 minutes")

    def test_my_hermes_off_says_so(self):
        on(self.w, "own_chat_write")
        e = self.refused("gateway_off", gw="me")
        self.assertIn("my-hermes is not answering", str(e))

    def test_only_patrick(self):
        on(self.w, "own_chat_write")
        self.g.hello("one")
        with self.assertRaises(CoreError) as e:
            self.own.send(self.w.bramble, "one", SID, None, "hi", "own-key-0001")
        self.assertEqual(e.exception.reason, "not_patrick")

    def test_same_key_same_receipt_and_never_a_second_message(self):
        on(self.w, "own_chat_write")
        self.g.hello("one")
        first = self.send()
        again = self.send()
        self.assertTrue(again["replayed"])
        self.assertEqual(again["message_id"], first["message_id"])
        self.assertEqual(self.w.store.db.execute("SELECT count(*) FROM own_messages").fetchone()[0], 1)
        self.refused("key_reused", text="something else")

    def test_a_chat_or_a_new_one_never_both(self):
        on(self.w, "own_chat_write")
        self.g.hello("one")
        self.refused("bad_chat", sid=None)
        self.refused("bad_chat", sid=SID, new=True)
        self.refused("bad_chat", sid="../etc/passwd")
        self.refused("bad_chat", sid="--yolo")
        self.refused("no_gateway", gw="bots")
        self.refused("bad_text", text="  ")
        self.refused("bad_text", text="x" * 8001)
        self.assertIsNone(self.send(sid=None, new=True)["session_id"])

    def test_text_never_goes_into_the_event_chain(self):
        on(self.w, "own_chat_write")
        self.g.hello("one")
        self.send(text="a secret plan")
        rows = self.w.store.db.execute("SELECT body_json FROM events").fetchall()
        self.assertFalse(any("secret" in r[0] for r in rows))


class Connecting(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.g = Gateways(self.w)
        self.own = self.g.own
        self.g.hello("one")
        self.g.hello("me")

    def send(self, gw="one", sid=SID, key="own-key-0001", text="hello"):
        return self.own.send(passkey(self.w), gw, sid, None if sid else True, text, key)["message_id"]

    def claim(self, gw="one", limit=3):
        return self.own.claim(self.w.ws, gw, self.g.tok[gw], limit, True)["items"]

    def status(self, mid):
        return self.w.store.db.execute("SELECT status, reason, made_session_id FROM own_messages WHERE id = ?",
                                       (mid,)).fetchone()

    def test_off_means_423_and_no_chat_list(self):
        self.w.store.set_flag(self.w.ws, "own_chat_write", False, by="tests")
        for call in (lambda: self.claim(), lambda: self.g.feed("one")):
            with self.assertRaises(CoreError) as e:
                call()
            self.assertEqual(e.exception.status, 423)
        self.assertIsNone(Feeds(self.w.store).get(self.w.ws, "one")["as_of"])

    def test_a_token_is_for_its_own_gateway(self):
        with self.assertRaises(CoreError) as e:
            self.own.claim(self.w.ws, "one", self.g.tok["me"], 1, True)
        self.assertEqual(e.exception.status, 401)
        with self.assertRaises(CoreError):
            self.own.feed(self.w.ws, "me", self.g.tok["one"], {})
        connector = Feeds(self.w.store).new_token(self.w.ws, "connector", by="tests")
        with self.assertRaises(CoreError):
            self.own.claim(self.w.ws, "me", connector, 1, True)

    def test_the_bots_connector_never_leases_one(self):
        on(self.w, "connector_effects")
        tok = Feeds(self.w.store).new_token(self.w.ws, "connector", by="tests")
        self.send()
        self.assertEqual(Chat(self.w.store).claim(self.w.ws, tok)["items"], [])
        self.assertEqual(len(self.claim()), 1)

    def test_one_turn_at_a_time_in_each_chat(self):
        a = self.send(key="own-key-0001")
        b = self.send(key="own-key-0002")
        c = self.send(sid="other_chat", key="own-key-0003")
        n1 = self.send(sid=None, key="own-key-0004")
        n2 = self.send(sid=None, key="own-key-0005")
        first = [i["id"] for i in self.claim()]
        self.assertEqual(first, [a, c, n1])
        self.assertEqual([i["id"] for i in self.claim()], [n2])
        self.assertEqual(self.claim(), [])
        self.own.result(self.w.ws, "one", self.g.tok["one"], a, "confirmed", "turn finished")
        self.assertEqual([i["id"] for i in self.claim()], [b])

    def test_limit_zero_and_not_ready_lease_nothing(self):
        self.send()
        self.assertEqual(self.claim(limit=0), [])
        self.assertEqual(self.own.claim(self.w.ws, "one", self.g.tok["one"], 3, False, "absent")["items"], [])
        self.assertFalse(self.own.online(self.w.ws, "one"))
        self.assertEqual(len(self.claim()), 1)

    def test_a_new_chat_comes_back_with_its_id_and_leaves_the_starting_list(self):
        mid = self.send(sid=None)
        self.claim()
        p = passkey(self.w)
        self.assertEqual([s["id"] for s in self.own.chats(p, "one")["starting"]], [mid])
        self.own.result(self.w.ws, "one", self.g.tok["one"], mid, "confirmed", session_id="made_1")
        self.assertEqual(tuple(self.status(mid)), ("sent", None, "made_1"))
        self.assertEqual(self.own.chats(p, "one")["starting"][0]["made_session_id"], "made_1")
        self.g.feed("one", chats=[{"id": "made_1", "title": "New", "last_at": "", "preview": ""}], opened=[])
        self.assertEqual(self.own.chats(p, "one")["starting"], [])
        self.assertEqual(self.own.view(p, "one", "made_1")["mine"][0]["id"], mid)

    def test_outcomes(self):
        a, b, c = self.send(key="own-key-0001"), self.send(sid="c2", key="own-key-0002"), self.send(sid="c3",
                                                                                                key="own-key-0003")
        self.claim()
        tok = self.g.tok["one"]
        self.own.result(self.w.ws, "one", tok, a, "confirmed")
        self.own.result(self.w.ws, "one", tok, b, "failed", reason="One's Hermes is absent")
        self.own.result(self.w.ws, "one", tok, c, "uncertain", reason="exit 1; Hermes may have the message")
        self.assertEqual([self.status(m)[0] for m in (a, b, c)], ["sent", "dead", "uncertain"])
        self.assertTrue(self.own.result(self.w.ws, "one", tok, a, "confirmed")["replayed"])
        with self.assertRaises(CoreError) as e:
            self.own.result(self.w.ws, "one", tok, c, "confirmed")
        self.assertEqual(e.exception.reason, "not_leased")
        self.assertEqual(self.claim(), [])  # an uncertain message is never sent again by itself
        with self.assertRaises(CoreError) as e:
            self.own.result(self.w.ws, "me", self.g.tok["me"], a, "confirmed")
        self.assertEqual(e.exception.reason, "no_item")

    def test_a_lapsed_lease_is_unclear_and_never_resent(self):
        mid = self.send()
        self.claim()
        self.w.clock.advance(minutes=21)
        self.g.hello("one")
        self.assertEqual(self.claim(), [])
        self.assertEqual(tuple(self.status(mid))[:2], ("uncertain", "the connector stopped before it said what happened"))

    def test_the_feed_and_the_chats_the_phone_asks_for(self):
        self.g.feed("one")
        p = passkey(self.w)
        summary = self.own.summary(p)
        one = next(g for g in summary["gateways"] if g["gateway"] == "one")
        self.assertEqual((one["name"], one["online"], one["chats"], one["last"]["title"]),
                         ("One", True, 1, "Plan the week"))
        self.assertTrue(summary["can_send"])
        self.assertIsNotNone(summary["sign_in_until"])
        v = self.own.view(p, "one", SID)
        self.assertEqual(([h["who"] for h in v["history"]], v["loaded"]), (["Patrick", "Hermes"], True))
        self.assertEqual(self.g.feed("one")["wanted"], [])
        v = self.own.view(p, "one", "older_chat")
        self.assertFalse(v["loaded"])
        for sid in ("x1", "x2", "x3"):
            self.own.view(p, "one", sid)
        self.assertEqual(self.g.feed("one")["wanted"], ["x3", "x2", "x1"])
        self.assertEqual(self.own.claim(self.w.ws, "one", self.g.tok["one"], 0, True)["wanted"], ["x3", "x2", "x1"])
        self.assertEqual(self.own.chats(p, "me")["chats"], [])

    def test_the_feed_keeps_only_what_the_phone_shows(self):
        snap = clean({"source_time": "2026-11-01T18:00:00+00:00",
                      "chats": [{"id": SID, "title": "t" * 500, "last_at": "", "preview": "p", "secret": "x"}],
                      "open": [{"id": SID, "messages": [{"role": "user", "text": "y" * 5000, "at": "", "tool": 1}]}]})
        self.assertEqual(set(snap["chats"][0]), {"id", "title", "last_at", "preview"})
        self.assertEqual(len(snap["chats"][0]["title"]), 120)
        self.assertEqual(len(snap["open"][0]["messages"][0]["text"]), 2000)
        for bad in ({"source_time": "x", "chats": []},
                    {"source_time": "2026-11-01T18:00:00+00:00", "chats": [{"id": "a b"}]},
                    {"source_time": "2026-11-01T18:00:00+00:00", "chats": [{"id": "a"}, {"id": "a"}]},
                    {"source_time": "2026-11-01T18:00:00+00:00", "chats": [{"id": "a"}] * 51},
                    {"source_time": "2026-11-01T18:00:00+00:00", "chats": [],
                     "open": [{"id": "a", "messages": [{"role": "tool", "text": ""}]}]}):
            with self.assertRaises(CoreError):
                clean(bad)


class Migration7(unittest.TestCase):
    def test_keeps_every_older_row_and_adds_the_flag_off(self):
        path = Path(tempfile.mkdtemp()) / "core.db"
        con = connect(path)
        con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        import thornswood_decisions.store as store_mod
        saved = store_mod.MIGRATIONS
        try:
            store_mod.MIGRATIONS = MIGRATIONS[:6]
            migrate(con)
        finally:
            store_mod.MIGRATIONS = saved
        con.execute("INSERT INTO workspaces(id, name, is_test, created_at) VALUES('ws_1', 'test', 1, 't')")
        for f in ("decision_write", "mobile_read", "mobile_chat_write", "push_send", "connector_effects"):
            con.execute("INSERT INTO flags VALUES('ws_1', ?, ?, 't', 'tests')", (f, int(f == "mobile_read")))
        con.execute("INSERT INTO feed_sources VALUES('ws_1', 'push', ?, 't')", ("a" * 64,))
        con.execute("INSERT INTO feed_snapshots VALUES('ws_1', 'work', '{}', ?, 't', 't')", ("b" * 64,))
        self.assertEqual(migrate(con), len(MIGRATIONS))
        flags = dict(con.execute("SELECT name, enabled FROM flags WHERE workspace_id = 'ws_1'").fetchall())
        self.assertEqual(flags, {"decision_write": 0, "mobile_read": 1, "mobile_chat_write": 0, "push_send": 0,
                                 "connector_effects": 0, "own_chat_write": 0})
        self.assertEqual(con.execute("SELECT name FROM feed_sources").fetchall()[0][0], "push")
        self.assertEqual(con.execute("SELECT name FROM feed_snapshots").fetchall()[0][0], "work")
        con.execute("INSERT INTO feed_sources VALUES('ws_1', 'own-one', ?, 't')", ("c" * 64,))
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("INSERT INTO feed_sources VALUES('ws_1', 'own-two', ?, 't')", ("d" * 64,))
        con.close()

    def test_a_new_workspace_has_own_chat_write_off(self):
        w = World()
        self.addCleanup(w.close)
        self.assertFalse(w.store.flag(w.ws, "own_chat_write"))


class Restore(unittest.TestCase):
    def test_a_restore_quarantines_every_waiting_own_message(self):
        w = World()
        self.addCleanup(w.close)
        g = Gateways(w)
        g.hello("one")
        mid = g.own.send(passkey(w), "one", SID, None, "hello", "own-key-0001")["message_id"]
        dest = w.dir / "backup.db"
        backup.backup(w.store, dest)
        out = backup.restore(dest, w.dir / "restored.db", clock=w.clock)
        self.assertEqual(out["quarantined"]["own_messages"], 1)
        s = Store(w.dir / "restored.db", clock=w.clock)
        self.addCleanup(s.close)
        self.assertEqual(s.db.execute("SELECT status FROM own_messages WHERE id = ?", (mid,)).fetchone()[0],
                         "quarantined")


class Routes(unittest.TestCase):
    def setUp(self):
        self.s = Served(self)
        self.w = self.s.w
        self.g = Gateways(self.w)
        on(self.w, "mobile_read")

    def signed_in(self, method="passkey"):
        dev = self.w.device()
        sess = self.w.core.auth.issue_session(self.w.ws, method=method, device_id=dev)
        return {"Cookie": f"tw_session={sess.token}", "Origin": ORIGIN, "X-CSRF-Token": sess.csrf}

    def test_connector_routes_take_only_their_token_and_never_a_browser(self):
        tok = self.g.tok["one"]
        status, _, body = self.s.call("POST", "/own/one/claim", headers={"Authorization": f"Bearer {tok}"},
                                      body={"limit": 0, "ready": True})
        self.assertEqual((status, body["items"]), (200, []))
        for extra in ({"Origin": ORIGIN}, {"Cookie": "tw_session=x"}):
            status, _, body = self.s.call("POST", "/own/one/claim",
                                          headers={"Authorization": f"Bearer {tok}", **extra}, body={})
            self.assertEqual((status, body["error"]), (403, "not_a_feed"))
        status, _, _ = self.s.call("POST", "/own/one/claim", headers={"Authorization": "Bearer nope"}, body={})
        self.assertEqual(status, 401)
        status, _, body = self.s.call("POST", "/own/one/feed", headers={"Authorization": f"Bearer {tok}"},
                                      body={"source_time": self.w.clock().isoformat(), "chats": []})
        self.assertEqual((status, body["wanted"]), (200, []))

    def test_the_phone_lists_reads_and_sends(self):
        self.g.hello("one")
        self.g.feed("one")
        h = self.signed_in()
        status, _, body = self.s.call("GET", "/own", headers=h)
        self.assertEqual(status, 200, body)
        self.assertEqual([g["gateway"] for g in body["gateways"]], ["me", "one"])
        status, _, body = self.s.call("GET", "/own/one", headers=h)
        self.assertEqual(body["chats"][0]["id"], SID)
        status, _, body = self.s.call("GET", f"/own/one/{SID}", headers=h)
        self.assertEqual(body["history"][1]["text"], "hi")
        status, _, body = self.s.call("POST", "/own/one/send", headers=h,
                                      body={"session_id": SID, "text": "go", "idempotency_key": "phone-key-01"})
        self.assertEqual((status, body["status"]), (200, "queued"))
        status, _, body = self.s.call("POST", "/own/one/send", headers=h,
                                      body={"new": True, "text": "start", "idempotency_key": "phone-key-02"})
        self.assertEqual((status, body["session_id"]), (200, None))

    def test_a_pairing_session_reads_but_never_sends(self):
        self.g.hello("one")
        h = self.signed_in(method="pairing")
        self.assertEqual(self.s.call("GET", "/own", headers=h)[0], 200)
        status, _, body = self.s.call("POST", "/own/one/send", headers=h,
                                      body={"session_id": SID, "text": "go", "idempotency_key": "phone-key-01"})
        self.assertEqual((status, body["error"]), (403, "passkey_needed"))

    def test_mobile_read_off_hides_it_all(self):
        self.w.store.set_flag(self.w.ws, "mobile_read", False, by="tests")
        self.g.hello("one")
        h = self.signed_in()
        self.assertEqual(self.s.call("GET", "/own", headers=h)[0], 423)
        status, _, body = self.s.call("POST", "/own/one/send", headers=h,
                                      body={"session_id": SID, "text": "go", "idempotency_key": "phone-key-01"})
        self.assertEqual((status, body["error"]), (423, "mobile_read_off"))

    def test_a_write_from_another_origin_is_refused(self):
        self.g.hello("one")
        h = self.signed_in() | {"Origin": "https://evil.example"}
        status, _, _ = self.s.call("POST", "/own/one/send", headers=h,
                                   body={"session_id": SID, "text": "go", "idempotency_key": "phone-key-01"})
        self.assertEqual(status, 403)

    def test_the_app_opens_its_hermes_paths(self):
        from thornswood_decisions.api import APP_VIEWS
        for path in ("hermes/me", "hermes/one", f"hermes/one/{SID}"):
            self.assertTrue(APP_VIEWS.match(path), path)
        for path in ("hermes/bots", "hermes/one/a b", "hermes", "hermes/one/-x"):
            self.assertFalse(APP_VIEWS.match(path), path)


# ── the connector process ─────────────────────────────────────────────────

STATE_SQL = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT, started_at REAL NOT NULL,
  last_activity_at REAL, end_reason TEXT, parent_session_id TEXT, model_config TEXT,
  archived INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
  content TEXT, timestamp REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1);
"""


def state_db(folder: Path) -> Path:
    path = folder / "state.db"
    con = sqlite3.connect(path)
    con.executescript(STATE_SQL)
    rows = [("s_plan", "cli", "Plan the week", 100, 500, None, None, None, 0, 0),
            ("s_old", "desktop", "Old chat", 50, 60, "compression", None, None, 0, 0),
            ("s_old2", "desktop", "Old chat", 61, 400, None, "s_old", None, 0, 0),
            ("s_once", "oneshot", "A one-shot run", 10, 900, None, None, None, 0, 0),
            ("s_kanban", "kanban", "A worker", 10, 900, None, None, None, 0, 0),
            ("s_sub", "cli", "Sub-agent", 10, 900, None, "s_plan", '{"_delegate_from": "s_plan"}', 0, 0),
            ("s_arch", "cli", "Archived", 10, 900, None, None, None, 1, 0),
            ("s_bot", "cli", "Bot Chat", 10, 900, None, None, "not json", 0, 1),
            ("s_tg", "telegram", None, 10, 300, None, None, "not json", 0, 0)]
    con.executemany("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
    msgs = [("s_plan", "user", "what is on", 101), ("s_plan", "assistant", '[{"type": "text", "text": "three things"}]',
                                                      102),
            ("s_plan", "tool", "secret tool output", 103), ("s_old2", "user", "carry on", 401),
            ("s_tg", "assistant", "from telegram", 301)]
    con.executemany("INSERT INTO messages(session_id, role, content, timestamp) VALUES(?,?,?,?)", msgs)
    con.commit()
    con.close()
    return path


HERMES = textwrap.dedent("""\
    import json, os, sys
    args = sys.argv[1:]
    log = os.environ["TW_FAKE_LOG"]
    text = open(args[args.index("--query-file") + 1], encoding="utf-8").read()
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"args": args, "text": text}) + "\\n")
    mode = os.environ.get("TW_FAKE_MODE", "ok")
    sid = args[args.index("--resume") + 1] if "--resume" in args else "made_by_hermes"
    print(json.dumps({"type": "system", "subtype": "init", "session_id": ""}))
    print(json.dumps({"type": "text", "text": "the reply"}))
    if mode == "ok":
        print(json.dumps({"type": "result", "session_id": sid, "exit_code": 0, "text": "the reply"}))
        sys.exit(0)
    print(json.dumps({"type": "result", "session_id": sid, "exit_code": 1, "error": "provider down"}))
    sys.exit(1)
""")

READY = textwrap.dedent("""\
    import json, os, sys
    req = json.loads(sys.stdin.read())
    ok = os.environ.get("TW_FAKE_READY", "1") == "1"
    print(json.dumps({"ready": ok, "reason": req.get("name", "") + " is absent"}))
""")


class FakeCore:
    def __init__(self, own: OwnChat, ws: str, gw: str, token: str):
        self.own, self.ws, self.gw, self.token = own, ws, gw, token

    def post(self, op, body):
        if isinstance(body, bytes):
            body = json.loads(body)
        try:
            if op == "claim":
                return 200, self.own.claim(self.ws, self.gw, self.token, body["limit"], body["ready"], body["why"])
            if op == "feed":
                return 200, self.own.feed(self.ws, self.gw, self.token, body)
            return 200, self.own.result(self.ws, self.gw, self.token, body["id"], body["outcome"], body["ref"],
                                        body["reason"], body.get("session_id"))
        except CoreError as exc:
            return exc.status, {"error": exc.reason}


class TheConnector(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.g = Gateways(self.w)
        self.g.hello("one")
        self.dir = Path(tempfile.mkdtemp())
        self.db = state_db(self.dir)
        (self.dir / "hermes.py").write_text(HERMES, encoding="utf-8")
        (self.dir / "ready.py").write_text(READY, encoding="utf-8")
        self.log = self.dir / "turns.jsonl"
        os.environ["TW_FAKE_LOG"] = str(self.log)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("TW_FAKE_LOG", "TW_FAKE_MODE", "TW_FAKE_READY")])
        hermes = own_connector.Hermes([sys.executable, str(self.dir / "hermes.py")], sys.executable, self.dir,
                                      "One's Hermes", helper=self.dir / "ready.py")
        self.lines = []
        self.conn = own_connector.OwnConnector(FakeCore(self.g.own, self.w.ws, "one", self.g.tok["one"]), hermes,
                                               self.db, say=self.lines.append)

    def round(self):
        n = self.conn.once()
        for t in self.conn.running:
            t.join(30)
        self.conn._report()
        return n

    def turns(self):
        return [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()] if self.log.exists() else []

    def test_lists_chats_the_way_hermes_pickers_do(self):
        snap = own_connector.snapshot(self.db, ["s_tg"], datetime(2026, 11, 1, tzinfo=timezone.utc))
        self.assertEqual([c["id"] for c in snap["chats"]], ["s_plan", "s_old2", "s_tg"])
        self.assertEqual(snap["chats"][0]["preview"], "three things")
        plan = next(o for o in snap["open"] if o["id"] == "s_plan")
        self.assertEqual([m["text"] for m in plan["messages"]], ["what is on", "three things"])
        self.assertEqual(clean(snap)["chats"][2]["title"], "")

    def test_the_feed_fits_under_the_cores_size(self):
        con = sqlite3.connect(self.db)
        con.executemany("INSERT INTO messages(session_id, role, content, timestamp) VALUES(?,?,?,?)",
                        [("s_plan", "user", "é" * 3000, 200 + i) for i in range(200)])
        con.commit()
        con.close()
        old = own_connector.FEED_BYTES
        own_connector.FEED_BYTES = 40 * 1024
        try:
            body = own_connector.fitted(self.db, [], datetime(2026, 11, 1, tzinfo=timezone.utc))
        finally:
            own_connector.FEED_BYTES = old
        self.assertLessEqual(len(body), 40 * 1024)
        clean(json.loads(body))

    def test_a_message_to_a_chat_resumes_it_with_its_own_settings_and_comes_back_confirmed(self):
        mid = self.g.own.send(passkey(self.w), "one", "s_plan", None, "check the build", "own-key-0001")["message_id"]
        self.assertEqual(self.round(), 1)
        (turn,) = self.turns()
        self.assertEqual(turn["text"], "check the build")
        args = turn["args"]
        for flag in ("chat", "-Q"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--format") + 1], "stream-json")
        self.assertEqual(args[args.index("--resume") + 1], "s_plan")
        # Hermes refuses a resume that changes a chat's launch settings, so none is passed.
        for flag in ("--yolo", "--source", "--in", "-c"):
            self.assertNotIn(flag, args)
        self.assertNotIn("check the build", args)
        self.assertFalse(Path(args[args.index("--query-file") + 1]).exists())
        self.assertEqual(self.w.store.db.execute("SELECT status FROM own_messages WHERE id = ?", (mid,)).fetchone()[0],
                         "sent")
        self.assertEqual(self.lines, [f"PASS {mid}: confirmed"])
        self.assertIsNotNone(Feeds(self.w.store).get(self.w.ws, "one")["as_of"])

    def test_a_new_chat_is_full_power_listed_and_brings_back_the_id_hermes_gave_it(self):
        mid = self.g.own.send(passkey(self.w), "one", None, True, "start something", "own-key-0001")["message_id"]
        self.round()
        args = self.turns()[0]["args"]
        self.assertNotIn("--resume", args)
        self.assertNotIn("-c", args)
        self.assertIn("--yolo", args)
        self.assertEqual(args[args.index("--source") + 1], "cli")
        self.assertEqual(args[args.index("--in") + 1], str(self.dir))
        self.assertEqual(self.w.store.db.execute("SELECT made_session_id FROM own_messages WHERE id = ?",
                                                 (mid,)).fetchone()[0], "made_by_hermes")

    def test_a_turn_that_did_not_finish_is_uncertain_and_never_resent(self):
        os.environ["TW_FAKE_MODE"] = "fail"
        mid = self.g.own.send(passkey(self.w), "one", "s_plan", None, "hello", "own-key-0001")["message_id"]
        self.round()
        row = self.w.store.db.execute("SELECT status, reason FROM own_messages WHERE id = ?", (mid,)).fetchone()
        self.assertEqual(row[0], "uncertain")
        self.assertIn("provider down", row[1])
        self.round()
        self.assertEqual(len(self.turns()), 1)

    def test_a_gateway_that_is_not_running_makes_one_off(self):
        os.environ["TW_FAKE_READY"] = "0"
        self.g.own.send(passkey(self.w), "one", "s_plan", None, "hello", "own-key-0001")
        self.round()
        self.assertEqual(self.turns(), [])
        self.assertFalse(self.g.own.online(self.w.ws, "one"))
        with self.assertRaises(CoreError) as e:
            self.g.own.send(passkey(self.w), "one", "s_plan", None, "again", "own-key-0002")
        self.assertEqual((str(e.exception), e.exception.detail["why"]), ("One is off", "One's Hermes is absent"))

    def test_off_it_waits_and_copies_nothing(self):
        self.w.store.set_flag(self.w.ws, "own_chat_write", False, by="tests")
        self.assertEqual(self.round(), -1)
        self.assertIsNone(Feeds(self.w.store).get(self.w.ws, "one")["as_of"])

    def test_the_hermes_python_on_windows_is_beside_hermes_exe(self):
        exe = self.dir / "Scripts" / "hermes.exe"
        exe.parent.mkdir()
        exe.write_bytes(b"MZ")
        with self.assertRaises(SystemExit):
            own_connector.hermes_python(str(exe))
        (exe.parent / "python.exe").write_bytes(b"MZ")
        self.assertEqual(own_connector.hermes_python(str(exe)), str(exe.parent / "python.exe"))

    def test_a_token_others_can_read_is_refused(self):
        if os.name == "nt":
            self.skipTest("on One the installer sets the token file's ACL")
        tok = self.dir / "own-one.token"
        tok.write_text("t", encoding="utf-8")
        os.chmod(tok, 0o644)
        with self.assertRaises(SystemExit):
            own_connector.read_token(tok)
        os.chmod(tok, 0o600)
        self.assertEqual(own_connector.read_token(tok), "t")

    def test_the_core_is_https_or_the_vms_loopback(self):
        with self.assertRaises(SystemExit):
            own_connector.main(["--gateway", "one", "--hermes", sys.executable, "--hermes-python", sys.executable,
                                "--core", "http://hermes.example.ts.net/api/v1"])

    def test_it_imports_nothing_from_this_repository(self):
        src = Path(own_connector.__file__).read_text(encoding="utf-8")
        self.assertNotIn("from .", src)
        self.assertNotIn("thornswood_decisions", src.split('"""', 2)[2])


if __name__ == "__main__":
    unittest.main()


# ── the installs ──────────────────────────────────────────────────────────

class VmInstall(unittest.TestCase):
    """my-hermes's connector: its unit, launcher and the install step that starts it."""

    def unit(self):
        import configparser
        from helpers import REPO
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        cp.optionxform = str
        cp.read_string((REPO / "deploy/decisions/tw-decide-own.service").read_text(encoding="utf-8"))
        return cp["Service"]

    def test_runs_as_patrick_hermes_on_loopback_and_holds_no_secret(self):
        s = self.unit()
        self.assertEqual((s["User"], s["IPAddressDeny"], s["IPAddressAllow"]), ("patrick-hermes", "any", "localhost"))
        self.assertEqual(s["ExecStart"], "/opt/thornswood-hermes/bin/tw-decide-own --gateway me --hermes "
                                         "${TW_OWN_HERMES_BIN}")
        self.assertEqual((s["NoNewPrivileges"], s["CapabilityBoundingSet"], s["UMask"]), ("yes", "", "0077"))
        self.assertNotIn("ListenStream", s)
        for key in s:
            self.assertNotRegex(key + s[key], r"(?i)token=|secret")

    def test_the_launcher_runs_only_the_root_owned_copy(self):
        from helpers import REPO
        lines = (REPO / "deploy/decisions/tw-decide-own").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "#!/usr/bin/python3 -I")
        self.assertIn('runpy.run_path("/opt/thornswood-hermes/current/thornswood_decisions/own_connector.py", '
                      'run_name="__main__")', lines)
        import subprocess
        mode = subprocess.run(["git", "-C", str(REPO), "ls-files", "-s", "deploy/decisions/tw-decide-own"],
                              capture_output=True, text=True).stdout.split(" ")[0]
        self.assertEqual(mode, "100755")

    def test_the_install_makes_its_token_for_patrick_hermes_only_and_turns_nothing_on(self):
        from helpers import REPO
        src = (REPO / "deploy/decisions/install.sh").read_text(encoding="utf-8")
        self.assertIn("own=patrick-hermes", src)
        self.assertIn('[[ "$own" != hermes ]] || stop', src)
        self.assertIn('tok="$oh/.config/thornswood/own-me.token"', src)
        self.assertIn("run feed-token --workspace thornswood --name own-me | runuser -u \"$own\" -- sh -c 'umask 077;",
                      src)
        self.assertIn("tw-decide-own.service; do", src)
        self.assertIn("tw-decide-connector tw-decide-push tw-decide-own; do", src)
        # A token the core no longer keeps (a downgrade past schema 7) is made again, not kept.
        self.assertIn('runuser -u "$own" -- cat "$tok" | run token-known --workspace thornswood --name own-me', src)
        self.assertNotIn("own_chat_write on", src)
        self.assertNotIn("own-one", src, "One's token is made only when One asks for it")


@unittest.skipUnless(__import__("shutil").which("pwsh") and os.name != "nt",
                     "needs pwsh, and the stand-in ssh is a shell script")
class OneInstall(unittest.TestCase):
    """One's installer, run here with a stand-in ssh: the token goes straight to its file and never shows."""

    def setUp(self):
        import shutil
        import subprocess
        from helpers import REPO
        self.t = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.t, True)
        origin, clone = self.t / "origin.git", self.t / "clone"
        run = lambda *a, cwd=None: subprocess.run(a, cwd=cwd, check=True, capture_output=True)  # noqa: E731
        run("git", "init", "-q", "--bare", "-b", "main", str(origin))
        run("git", "init", "-q", "-b", "main", str(clone))
        for rel in ("scripts/one/Install-OwnConnector.ps1", "thornswood_decisions/own_connector.py",
                    "thornswood_decisions/room_send.py"):
            (clone / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO / rel, clone / rel)
        run("git", "-c", "user.name=t", "-c", "user.email=t@t", "add", ".", cwd=clone)
        run("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "main", cwd=clone)
        run("git", "remote", "add", "origin", str(origin), cwd=clone)
        run("git", "push", "-q", "origin", "main", cwd=clone)
        self.clone = clone
        self.bin = self.t / "bin"
        self.bin.mkdir()
        self.calls = self.t / "ssh-calls.txt"
        ssh = self.bin / "ssh"
        ssh.write_text(f"#!/bin/sh\necho \"$@\" >> '{self.calls}'\necho 'tok_{'x' * 43}'\n", encoding="utf-8")
        ssh.chmod(0o755)
        self.hermes = self.t / "hermes"
        self.hermes.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.hermes.chmod(0o755)

    def install(self, *extra):
        import subprocess
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}"}
        return subprocess.run(["pwsh", "-NoProfile", "-File", str(self.clone / "scripts/one/Install-OwnConnector.ps1"),
                               "-Core", "https://hermes.tail0000.ts.net/api/v1", "-Hermes", str(self.hermes),
                               "-HermesPython", sys.executable, "-Folder", str(self.t / "own-connector"),
                               "-TokenFile", str(self.t / "own-one.token"), "-NoTask", *extra],
                              capture_output=True, text=True, env=env, timeout=120)

    def test_makes_the_token_on_the_vm_straight_into_its_file(self):
        r = self.install("-SshHost", "hermes")
        out = r.stdout + r.stderr
        self.assertIn("PASS the VM made the connector's token", out)
        self.assertNotIn("tok_", out, "the token never shows")
        self.assertEqual((self.t / "own-one.token").read_text(encoding="ascii"), "tok_" + "x" * 43)
        self.assertEqual(oct((self.t / "own-one.token").stat().st_mode & 0o777), "0o600")
        call = self.calls.read_text(encoding="utf-8")
        self.assertIn("-o BatchMode=yes hermes sudo -n runuser -u tw-decide -- /opt/thornswood-hermes/bin/tw-decide "
                      "--db /srv/tw/decide/core.db feed-token --workspace thornswood --name own-one", call)
        self.assertTrue((self.t / "own-connector" / "own_connector.py").exists())
        self.assertTrue((self.t / "own-connector" / "room_send.py").exists())
        # The stand-in address answers nothing, so the round fails, and says so.
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAIL one round did not work", out)

    def test_needs_the_ssh_name_the_first_time_and_never_remakes_a_token(self):
        r = self.install()
        self.assertEqual(r.returncode, 1)
        self.assertIn("pass -SshHost", r.stdout)
        (self.t / "own-one.token").write_text("tok_already", encoding="ascii")
        r = self.install("-SshHost", "hermes")
        self.assertIn("PASS the connector's token is already in", r.stdout)
        self.assertFalse(self.calls.exists())

    def test_refuses_an_address_that_is_not_the_vms_ts_net(self):
        import subprocess
        r = subprocess.run(["pwsh", "-NoProfile", "-File", str(self.clone / "scripts/one/Install-OwnConnector.ps1"),
                            "-Core", "http://192.135.112.23:9135/api/v1"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn("-Core is the VM's Tailscale address", r.stdout)

    def test_never_replaces_a_task_it_does_not_own(self):
        from helpers import REPO
        text = (REPO / "scripts/one/Install-OwnConnector.ps1").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"Register-ScheduledTask[^\n]*-Force")
        self.assertIn("runs something else; it is not replaced", text)
        self.assertIn("-LogonType Interactive -RunLevel Limited", text)
