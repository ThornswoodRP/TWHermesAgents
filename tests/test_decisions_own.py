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
import re
import sqlite3
import sys
import tempfile
import textwrap
import threading
import unittest
import weakref
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

    def hello(self, gw: str, ready: bool = True, why: str = "", state: str | None = None,
              forwarded_for: str | None = None) -> dict:
        """The connector says it is there; with own_chat_write off the core still notes it, and answers 423."""
        try:
            return self.own.claim(self.w.ws, gw, self.tok[gw], 0, ready, why, state, forwarded_for)
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

    def test_one_is_off_only_when_the_tailnet_says_so(self):
        # Patrick, 1 Nov: "Show One as unavailable only when One is really off."
        on(self.w, "own_chat_write")
        e = self.refused("gateway_off")
        self.assertEqual(e.detail["state"], "missing")
        self.g.hello("one", ready=False, why="One's Hermes is absent", state="gateway_down",
                     forwarded_for="100.101.102.103")
        e = self.refused("gateway_off")
        self.assertEqual((e.detail["state"], e.detail["why"]), ("gateway_down", "One's Hermes is absent"))
        self.g.hello("one", state="ready", forwarded_for="100.101.102.103")
        self.assertEqual(self.send()["status"], "queued")
        self.w.clock.advance(minutes=2)
        # Silent, and the tailnet's view is not in: never "One is off" on a guess.
        e = self.refused("gateway_off", key="own-key-0002")
        self.assertEqual(e.detail["state"], "not_heard")
        self.assertEqual(e.detail["why"], "its connector has not been heard from in 2 minutes")
        peer = {"TailscaleIPs": ["100.101.102.103"], "Online": True, "LastSeen": "0001-01-01T00:00:00Z"}
        self.assertEqual(self.own.peers(self.w.ws, {"Self": {"TailscaleIPs": ["100.64.0.1"]}, "Peer": {"k": peer}}), 1)
        e = self.refused("gateway_off", key="own-key-0003")
        self.assertEqual(e.detail["state"], "connector_stopped")
        peer.update(Online=False, LastSeen="2026-11-01T19:12:00Z")
        self.own.peers(self.w.ws, {"Peer": {"k": peer}})
        e = self.refused("one_off", key="own-key-0004")
        self.assertEqual(str(e), "One is off")
        self.assertEqual((e.detail["state"], e.detail["since"]), ("machine_off", "2026-11-01T19:12:00+00:00"))
        # A view older than 3 minutes is no view.
        self.w.clock.advance(minutes=4)
        self.assertEqual(self.own.gateway_state(self.w.ws, "one")["state"], "not_heard")

    def test_a_check_that_could_not_run_is_its_own_state(self):
        on(self.w, "own_chat_write")
        self.g.hello("one", ready=False, why="could not run the readiness check with Hermes's Python",
                     state="check_failed")
        st = self.own.gateway_state(self.w.ws, "one")
        self.assertEqual((st["state"], st["why"]), ("check_failed", "could not run the readiness check with Hermes's Python"))
        self.g.hello("one", ready=False, why="x", state="anything else")
        self.assertEqual(self.own.gateway_state(self.w.ws, "one")["state"], "gateway_down")

    def test_turned_off_and_the_vm_never_reads_as_off(self):
        self.assertEqual(self.own.gateway_state(self.w.ws, "me")["state"], "turned_off")
        on(self.w, "own_chat_write")
        self.g.hello("me", state="ready")
        self.w.clock.advance(minutes=3)
        self.assertEqual(self.own.gateway_state(self.w.ws, "me")["state"], "connector_stopped")

    def test_only_a_tailnet_address_is_kept_for_the_machine(self):
        from thornswood_decisions.own import tailnet_ip
        self.assertEqual(tailnet_ip("100.101.102.103"), "100.101.102.103")
        self.assertEqual(tailnet_ip("fd7a:115c:a1e0::1"), "fd7a:115c:a1e0::1")
        for bad in (None, "", "127.0.0.1", "192.135.112.23", "10.0.0.1", "not an address", "100.128.0.1"):
            self.assertIsNone(tailnet_ip(bad), bad)
        on(self.w, "own_chat_write")
        self.g.hello("one", forwarded_for="100.101.102.103")
        self.g.hello("one", forwarded_for=None)
        self.assertEqual(self.own._gw_row(self.w.ws, "one")["peer_ip"], "100.101.102.103")

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

    def test_a_new_chat_is_known_by_its_id_while_its_first_turn_still_runs(self):
        # 10 Nov: a long first turn sat on "Sending" under Starting, with no chat to open until it ended.
        mid = self.send(sid=None)
        other = self.send(sid="other_chat", key="own-key-0002")
        self.claim()
        p = passkey(self.w)
        busy = {"turns": [{"id": mid, "state": "tool", "tool": "terminal", "admitted": True, "session": "made_2"},
                          {"id": other, "state": "writing", "admitted": True, "session": "made_x"}]}
        self.own.claim(self.w.ws, "one", self.g.tok["one"], 3, True, doing=busy)
        self.assertEqual(tuple(self.status(mid)), ("leased", None, "made_2"))
        self.assertIsNone(self.status(other)["made_session_id"], "a message to a chat it has never makes one")
        view = self.own.chats(p, "one")
        self.assertEqual((view["starting"][0]["made_session_id"], view["made"]), ("made_2", {mid: "made_2"}))
        self.assertEqual(self.own.view(p, "one", "made_2")["mine"][0]["id"], mid, "the chat opens while it runs")
        self.g.feed("one", chats=[{"id": "made_2", "title": "New", "last_at": "", "preview": ""}], opened=[])
        self.assertEqual((self.own.chats(p, "one")["starting"], self.own.chats(p, "one")["made"]),
                         ([], {mid: "made_2"}), "listed, the app still finds the chat it started")
        self.own.result(self.w.ws, "one", self.g.tok["one"], mid, "confirmed", session_id="made_2")
        self.assertEqual(tuple(self.status(mid)), ("sent", None, "made_2"))

    def test_a_bad_chat_id_from_the_connector_is_not_kept(self):
        mid = self.send(sid=None)
        self.claim()
        for bad in ("../etc", "--yolo", "x" * 129, 7):
            self.own.claim(self.w.ws, "one", self.g.tok["one"], 3, True,
                           doing={"turns": [{"id": mid, "state": "thinking", "admitted": True, "session": bad}]})
            self.assertIsNone(self.status(mid)["made_session_id"])

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
                                 "connector_effects": 0, "own_chat_write": 0, "session_manage_write": 0})
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
    if req.get("op") == "tasks":
        # The Tasks tab's read (room_send.py op tasks): this Hermes has nothing going besides its chats.
        print(json.dumps({"tasks": {"": {"state": "ready", "subagents": [], "processes": [], "jobs": []}}}))
        sys.exit(0)
    ok = os.environ.get("TW_FAKE_READY", "1") == "1"
    print(json.dumps({"ready": ok, "reason": req.get("name", "") + " is absent"}))
""")


class FakeCore:
    """The core as an own-Hermes connector reaches it. The connector delivers each message on a thread of its
    own while its main loop goes on posting, and the real core gives every request thread its own store
    connection (api.py); so does this, since one SQLite connection shared by two threads mixes their
    transactions ("cannot start a transaction within a transaction")."""

    def __init__(self, own: OwnChat, ws: str, gw: str, token: str):
        self._own, self.ws, self.gw, self.token = own, ws, gw, token
        self._local = threading.local()
        self._opened: list[Store] = []
        weakref.finalize(self, lambda opened: [s.close() for s in opened], self._opened)

    @property
    def own(self) -> OwnChat:
        if threading.current_thread() is threading.main_thread():
            return self._own
        o = getattr(self._local, "own", None)
        if o is None:
            s = Store(self._own.s.path, clock=self._own.s.clock)
            self._opened.append(s)
            o = self._local.own = OwnChat(s)
        return o

    def post(self, op, body):
        if isinstance(body, bytes):
            body = json.loads(body)
        try:
            if op == "claim":
                return 200, self.own.claim(self.ws, self.gw, self.token, body["limit"], body["ready"], body["why"])
            if op == "feed":
                return 200, self.own.feed(self.ws, self.gw, self.token, body)
            if op == "page":
                return 200, self.own.page_post(self.ws, self.gw, self.token, body)
            if op == "tasks":
                # The Tasks tab's report, which test_decisions_tasks checks against the real core.
                return 200, {}
            if op == "turns":
                return 200, self.own.turns(self.ws, self.gw, self.token, body)
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
        hermes = own_connector.Hermes([sys.executable, str(self.dir / "hermes.py")],
                                      [sys.executable, "-I", str(self.dir / "ready.py")], self.dir, "One's Hermes")
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
        # His Bot Chat first (hidden from Hermes's pickers, listed here), then the pickers' own list.
        self.assertEqual([c["id"] for c in snap["chats"]], ["s_bot", "s_plan", "s_old2", "s_tg"])
        self.assertEqual(snap["chats"][1]["preview"], "three things")
        plan = next(o for o in snap["open"] if o["id"] == "s_plan")
        self.assertEqual([m["text"] for m in plan["messages"]], ["what is on", "three things"])
        self.assertEqual(clean(snap)["chats"][3]["title"], "")
        self.assertEqual([c.get("bot_chat", False) for c in clean(snap)["chats"]], [True, False, False, False])

    def test_pinned_chats_head_the_list_however_old_and_hidden_ones_are_counted(self):
        # #101 and #102. A store from before Hermes kept pins lists by time and pins nothing.
        snap = own_connector.snapshot(self.db, [], datetime(2026, 11, 1, tzinfo=timezone.utc))
        self.assertEqual(([c["id"] for c in snap["chats"]], snap["hidden"]), (["s_bot", "s_plan", "s_old2", "s_tg"], 1))
        self.assertFalse(any("pinned" in c for c in snap["chats"]))
        con = sqlite3.connect(self.db)
        con.execute("ALTER TABLE sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        con.execute("UPDATE sessions SET pinned = 1 WHERE id IN ('s_tg', 's_arch')")
        con.commit()
        con.close()
        snap = own_connector.snapshot(self.db, [], datetime(2026, 11, 1, tzinfo=timezone.utc))
        self.assertEqual([(c["id"], c.get("pinned", False)) for c in snap["chats"]],
                         [("s_bot", False), ("s_tg", True), ("s_plan", False), ("s_old2", False)])
        self.assertEqual(snap["hidden"], 1, "a hidden chat stays hidden, pinned or not")
        kept = clean(snap)
        self.assertEqual(([c.get("pinned", False) for c in kept["chats"]], kept["hidden"]), ([False, True, False, False], 1))
        page = own_connector.chats_page(self.db, None, "", n=3)
        self.assertEqual(([c["id"] for c in page["chats"]], page["more"]), (["s_bot", "s_tg", "s_plan"], True))
        later = own_connector.chats_page(self.db, page["chats"][-1]["last_at"], "", n=3)
        self.assertEqual(([c["id"] for c in later["chats"]], later["more"]), (["s_old2"], False))
        self.assertEqual([c["id"] for c in own_connector.chats_page(self.db, None, "", archived=True)["chats"]], ["s_arch"])
        for bad in (-1, True, "3", 2_000_000):
            with self.assertRaises(CoreError):
                clean(snap | {"hidden": bad})

    def test_the_bot_chat_is_listed_first_by_the_id_it_goes_on_as(self):
        con = sqlite3.connect(self.db)
        # Hermes compressed it: the titled, hidden row ended with 'compression' and goes on as s_bot2.
        con.execute("UPDATE sessions SET end_reason = 'compression' WHERE id = 's_bot'")
        con.execute("INSERT INTO sessions VALUES('s_bot2', 'cli', NULL, 20, 950, NULL, 's_bot', NULL, 0, 1)")
        con.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES('s_bot2', 'assistant', "
                    "'Rift: the load order is done', 951)")
        # A visible chat merely named like it is an ordinary chat; an archived Bot Chat is retired.
        con.execute("INSERT INTO sessions VALUES('s_named', 'cli', 'Bot Chat old', 5, 100, NULL, NULL, NULL, 0, 0)")
        con.commit()
        con.close()
        snap = own_connector.snapshot(self.db, [], datetime(2026, 11, 1, tzinfo=timezone.utc))
        first = snap["chats"][0]
        self.assertEqual((first["id"], first["title"], first["preview"], first["bot_chat"]),
                         ("s_bot2", "Bot Chat", "Rift: the load order is done", True))
        self.assertEqual([c["id"] for c in snap["chats"]].count("s_bot2"), 1)
        self.assertIn("s_bot2", [o["id"] for o in snap["open"]])
        page = own_connector.chats_page(self.db, None, "")
        self.assertEqual(page["chats"][0]["id"], "s_bot2")
        self.assertEqual(own_connector.chats_page(self.db, None, "bot", found=[])["chats"][0]["id"], "s_bot2")
        self.assertNotIn("s_bot2", [c["id"] for c in own_connector.chats_page(
            self.db, None, "plan", found=[{"id": "s_plan", "snippet": ""}])["chats"]])
        later = own_connector.chats_page(self.db, page["chats"][-1]["last_at"], "")
        self.assertNotIn("s_bot2", [c["id"] for c in later["chats"]])
        con = sqlite3.connect(self.db)
        con.execute("UPDATE sessions SET archived = 1 WHERE id = 's_bot'")
        con.commit()
        con.close()
        snap = own_connector.snapshot(self.db, [], datetime(2026, 11, 1, tzinfo=timezone.utc))
        self.assertFalse(any(c.get("bot_chat") for c in snap["chats"]))

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
        # room_send.py's attended turn: `hermes chat -Q` that Hermes can ask and Patrick can stop (own_turns.py).
        for flag in ("attended-chat", "-Q"):
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

    def test_a_gateway_that_is_not_running_is_said_as_that(self):
        os.environ["TW_FAKE_READY"] = "0"
        self.g.own.send(passkey(self.w), "one", "s_plan", None, "hello", "own-key-0001")
        self.round()
        self.assertEqual(self.turns(), [])
        self.assertFalse(self.g.own.online(self.w.ws, "one"))
        with self.assertRaises(CoreError) as e:
            self.g.own.send(passkey(self.w), "one", "s_plan", None, "again", "own-key-0002")
        self.assertEqual((e.exception.reason, e.exception.detail["state"], e.exception.detail["why"]),
                         ("gateway_off", "gateway_down", "One's Hermes is absent"))

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

    def launcher(self, body: str) -> Path:
        """A stand-in hermes launcher: a script that answers --print-runtime-command as `body` says."""
        path = self.dir / "bin-hermes"
        path.write_text(f"#!{sys.executable}\nimport json, sys\n{body}\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    @unittest.skipIf(os.name == "nt", "the stand-in launcher is a #! script")
    def test_the_readiness_check_runs_inside_the_runtime_hermes_says_it_starts(self):
        # One's launcher (1 Nov): hermes.exe in .hermes\bin, its Python in Hermes's store, nothing beside it.
        boot = "import os, sys, runpy; os.environ['TW_BOOTSTRAPPED'] = 'yes'; " + own_connector.RUNTIME_ENTRY
        hermes = self.launcher(f"print(json.dumps([sys.executable, '-I', '-c', {boot!r}])) "
                               "if sys.argv[1:2] == ['--print-runtime-command'] else sys.exit(5)")
        helper = self.dir / "probe.py"
        helper.write_text("import json, os, sys\nreq = json.loads(sys.stdin.read())\n"
                          "print(json.dumps({'ready': os.environ.get('TW_BOOTSTRAPPED') == 'yes', 'name': req['name']}))\n",
                          encoding="utf-8")
        cmd = own_connector.helper_command(str(hermes), helper)
        self.assertEqual(cmd[:3], [sys.executable, "-I", "-c"])
        self.assertNotIn("hermes_cli.main", cmd[3])
        h = own_connector.Hermes([str(hermes)], cmd, self.dir, "One's Hermes")
        self.assertEqual(h.ready(), ("ready", ""))

    def test_a_check_that_cannot_run_says_so_and_never_reads_as_gateway_down(self):
        h = own_connector.Hermes(["hermes"], [str(self.dir / "no-such-python"), "-I", "x.py"], self.dir, "One's Hermes")
        self.assertEqual(h.ready(), ("check_failed", "could not run the readiness check with Hermes's Python"))
        h = own_connector.Hermes(["hermes"], [sys.executable, "-c", "print('[1]')"], self.dir, "One's Hermes")
        self.assertEqual(h.ready()[0], "check_failed")
        h = own_connector.Hermes(["hermes"], [sys.executable, "-c", "print('{\"ready\": false, \"reason\": \"no gateway\"}')"],
                                 self.dir, "One's Hermes")
        self.assertEqual(h.ready(), ("gateway_down", "no gateway"))

    @unittest.skipIf(os.name == "nt", "the stand-in launcher is a #! script")
    def test_an_older_launcher_falls_back_to_the_python_it_names(self):
        hermes = self.launcher("sys.exit(2)")
        self.assertEqual(own_connector.helper_command(str(hermes), self.dir / "h.py"),
                         [sys.executable, "-I", str(self.dir / "h.py")])

    @unittest.skipIf(os.name == "nt", "the stand-in launcher is a #! script")
    def test_a_runtime_command_of_another_shape_stops_it(self):
        for answer in ("[]", '["python", "-c", "x"]', "not json",
                       json.dumps([sys.executable, "-I", "-c", "runpy.run_module('other')"])):
            with self.subTest(answer=answer):
                hermes = self.launcher(f"print({answer!r})")
                with self.assertRaises(SystemExit):
                    own_connector.helper_command(str(hermes), self.dir / "h.py")

    def test_a_given_python_is_used_as_it_is(self):
        self.assertEqual(own_connector.helper_command("hermes", self.dir / "h.py", python="/p/python3"),
                         ["/p/python3", "-I", str(self.dir / "h.py")])

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
        self.assertIsNone(re.search(r"^\s*(from|import)\s+thornswood", src, re.M))


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
        self.assertIn('runpy.run_path(str(Path(__file__).resolve().parents[2] / "src" / "thornswood_decisions" / '
                      '"own_connector.py"), run_name="__main__")', lines)
        import subprocess
        mode = subprocess.run(["git", "-C", str(REPO), "ls-files", "-s", "deploy/decisions/tw-decide-own"],
                              capture_output=True, text=True).stdout.split(" ")[0]
        self.assertEqual(mode, "100755")

    def test_the_install_reads_the_tailnets_view_of_one_as_tw_decide_with_no_network(self):
        from helpers import REPO
        src = (REPO / "deploy/decisions/install.sh").read_text(encoding="utf-8")
        units = re.search(r"for u in (.*?); do", src, re.S).group(1).split()
        self.assertIn("tw-decide-peers.service", units)
        self.assertIn("tw-decide-peers.timer", units)
        self.assertIn("systemctl enable --quiet --now tw-decide-peers.timer", src)
        unit = (REPO / "deploy/decisions/tw-decide-peers.service").read_text(encoding="utf-8")
        self.assertIn("User=tw-decide", unit)
        self.assertIn("IPAddressDeny=any", unit)
        self.assertIn("RestrictAddressFamilies=AF_UNIX", unit)
        self.assertIn("peers --workspace thornswood", unit)
        timer = (REPO / "deploy/decisions/tw-decide-peers.timer").read_text(encoding="utf-8")
        self.assertIn("OnUnitInactiveSec=60s", timer)

    def test_the_install_makes_its_token_for_patrick_hermes_only_and_turns_nothing_on(self):
        from helpers import REPO
        src = (REPO / "deploy/decisions/install.sh").read_text(encoding="utf-8")
        self.assertIn("own=patrick-hermes", src)
        self.assertIn('[[ "$own" != hermes ]] || stop', src)
        self.assertIn('tok="$oh/.config/thornswood/own-me.token"', src)
        self.assertIn("run feed-token --workspace thornswood --name own-me | runuser -u \"$own\" -- sh -c 'umask 077;",
                      src)
        units = re.search(r"for u in (.*?); do", src, re.S).group(1).split()
        self.assertIn("tw-decide-own.service", units)
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
        for rel in ("scripts/one/Install-OwnConnector.ps1", "deploy/layout.json",
                    "src/thornswood_decisions/own_connector.py", "src/thornswood_decisions/room_send.py"):
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
        r = self.install()
        out = r.stdout + r.stderr
        self.assertIn("PASS the VM made the connector's token", out)
        self.assertNotIn("tok_", out, "the token never shows")
        self.assertEqual((self.t / "own-one.token").read_text(encoding="ascii"), "tok_" + "x" * 43)
        self.assertEqual(oct((self.t / "own-one.token").stat().st_mode & 0o777), "0o600")
        call = self.calls.read_text(encoding="utf-8")
        # One's ssh alias for the VM, as install-on-oracle.sh uses it; "hermes" is only the HTTPS name.
        self.assertIn("-o BatchMode=yes thornswood-bots sudo -n runuser -u tw-decide -- /opt/thornswood-hermes/bin/tw-decide "
                      "--db /srv/tw/decide/core.db feed-token --workspace thornswood --name own-one", call)
        self.assertTrue((self.t / "own-connector" / "own_connector.py").exists())
        self.assertTrue((self.t / "own-connector" / "room_send.py").exists())
        # The stand-in address answers nothing, so the round fails, and says so.
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAIL one round did not work", out)

    def test_the_ssh_name_can_be_given_and_a_token_is_never_remade(self):
        r = self.install("-SshHost", "other-name")
        self.assertIn("-o BatchMode=yes other-name sudo", self.calls.read_text(encoding="utf-8"))
        self.calls.unlink()
        r = self.install()
        self.assertIn("PASS the connector's token is already in", r.stdout)
        self.assertFalse(self.calls.exists())

    def github_origin(self, signed_in=True):
        """origin is GitHub's address, as in Patrick's clone; git takes it from the scratch bare repo."""
        import subprocess
        origin = self.t / "origin.git"
        for args in (("remote", "set-url", "origin", "https://github.com/ThornswoodRP/TWHermesAgents.git"),
                     ("config", f"url.{origin}.insteadOf", "https://github.com/ThornswoodRP/TWHermesAgents.git")):
            subprocess.run(["git", *args], cwd=self.clone, check=True, capture_output=True)
        gh = self.bin / "gh"
        gh.write_text(f"#!/bin/sh\n[ \"$1 $2\" = 'auth status' ] && exit {0 if signed_in else 1}\nexit 0\n", encoding="utf-8")
        gh.chmod(0o755)
        (self.t / "own-one.token").write_text("tok_already", encoding="ascii")
        return gh

    def helpers(self):
        import subprocess
        out = subprocess.run(["git", "config", "--local", "--get-all", "credential.https://github.com.helper"],
                             cwd=self.clone, capture_output=True, text=True)
        return out.stdout.splitlines()

    def test_the_clone_signs_in_to_github_with_gh_so_git_never_prompts(self):
        gh = self.github_origin()
        r = self.install()
        self.assertIn(f"PASS this clone signs in to GitHub with gh ({gh})", r.stdout, r.stdout + r.stderr)
        self.assertIn("PASS own_connector.py and room_send.py from", r.stdout)
        # The empty entry first, so the helpers set before it (One's helper selector) are dropped.
        self.assertEqual(self.helpers(), ["", f"!'{gh}' auth git-credential"])
        self.install()
        self.assertEqual(self.helpers(), ["", f"!'{gh}' auth git-credential"], "set once, however often it runs")

    def test_gh_not_signed_in_stops_before_any_fetch(self):
        self.github_origin(signed_in=False)
        r = self.install()
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAIL gh is not signed in to github.com", r.stdout)
        self.assertFalse((self.t / "own-connector" / "own_connector.py").exists())
        self.assertEqual(self.helpers(), [])

    def test_finds_hermess_python_from_its_launcher_not_beside_it(self):
        import subprocess
        boot = "import runpy; " + "runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)"
        self.hermes.write_text("#!/bin/sh\n[ \"$1\" = --print-runtime-command ] && "
                               f"printf '%s\\n' '{json.dumps([sys.executable, '-I', '-c', boot])}'\nexit 0\n",
                               encoding="utf-8")
        (self.t / "own-one.token").write_text("tok_already", encoding="ascii")
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}"}
        r = subprocess.run(["pwsh", "-NoProfile", "-File", str(self.clone / "scripts/one/Install-OwnConnector.ps1"),
                            "-Core", "https://hermes.tail0000.ts.net/api/v1", "-Hermes", str(self.hermes),
                            "-Folder", str(self.t / "own-connector"), "-TokenFile", str(self.t / "own-one.token"),
                            "-NoTask"], capture_output=True, text=True, env=env, timeout=120)
        self.assertIn(f"the connector runs with {sys.executable}", r.stdout)
        self.assertNotIn("python.exe", r.stdout)

    def fresh_home(self):
        """A home with no ~/.config or ~/.local yet, as the VM's test run gives (tw-follow-main: HOME is a new folder)."""
        home = self.t / "home"
        home.mkdir()
        return {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "HOME": str(home),
                "XDG_CONFIG_HOME": "", "XDG_DATA_HOME": ""}, home

    def test_refuses_an_address_that_is_not_the_vms_ts_net(self):
        import subprocess
        env, _ = self.fresh_home()
        r = subprocess.run(["pwsh", "-NoProfile", "-File", str(self.clone / "scripts/one/Install-OwnConnector.ps1"),
                            "-Core", "http://192.135.112.23:9135/api/v1"], capture_output=True, text=True,
                           env=env, timeout=60)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("-Core is the VM's Tailscale address", r.stdout)

    def test_its_own_folders_are_made_when_they_are_not_there_yet(self):
        import subprocess
        env, home = self.fresh_home()
        r = subprocess.run(["pwsh", "-NoProfile", "-File", str(self.clone / "scripts/one/Install-OwnConnector.ps1"),
                            "-Core", "https://hermes.tail0000.ts.net/api/v1", "-Hermes", str(self.hermes),
                            "-HermesPython", sys.executable, "-NoTask"], capture_output=True, text=True,
                           env=env, timeout=120)
        self.assertIn("PASS the VM made the connector's token", r.stdout, r.stderr)
        token = home / ".config" / "Thornswood" / "own-one.token"
        self.assertEqual(token.read_text(encoding="ascii"), "tok_" + "x" * 43)
        self.assertTrue((home / ".local" / "share" / "Thornswood-Hermes" / "own-connector" / "own_connector.py").exists())
        self.assertFalse((home / ".local" / "share" / "Thornswood").exists(), "nothing goes in the launcher's folder")

    def test_refuses_a_folder_inside_a_velopack_install(self):
        """Velopack's Setup.exe deletes its whole folder on a reinstall; on 2 Nov the launcher's took the connector
        with it, and One's connector stopped at 2:58 PM with nothing left to start."""
        import subprocess
        env, home = self.fresh_home()
        root = self.t / "SomeApp"
        (root / "current").mkdir(parents=True)
        (root / "Update.exe").write_bytes(b"")
        for folder, inside in ((root / "own-connector", root), (home / ".local" / "share" / "Thornswood" / "own-connector",
                                                                home / ".local" / "share" / "Thornswood")):
            with self.subTest(folder=str(folder)):
                r = subprocess.run(["pwsh", "-NoProfile", "-File", str(self.clone / "scripts/one/Install-OwnConnector.ps1"),
                                    "-Core", "https://hermes.tail0000.ts.net/api/v1", "-Hermes", str(self.hermes),
                                    "-HermesPython", sys.executable, "-Folder", str(folder), "-NoTask"],
                                   capture_output=True, text=True, env=env, timeout=120)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn(f"FAIL {folder} is inside {inside}, a Velopack install", r.stdout)
                self.assertFalse(folder.exists())
                self.assertFalse(self.calls.exists(), "refused before any token is made")

    def test_never_replaces_a_task_it_does_not_own(self):
        from helpers import REPO
        text = (REPO / "scripts/one/Install-OwnConnector.ps1").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"Register-ScheduledTask[^\n]*-Force")
        self.assertIn("runs something else; it is not replaced", text)
        self.assertIn("-LogonType Interactive -RunLevel Limited", text)


class GatewayStatesOverHttp(unittest.TestCase):
    """What the connector posts reaches the core's states as Tailscale Serve hands it on."""

    def setUp(self):
        self.s = Served(self)
        self.tok = Feeds(self.s.w.store).new_token(self.s.w.ws, "own-one", by="tests")
        on(self.s.w, "own_chat_write")

    def claim(self, body, **headers):
        return self.s.call("POST", "/own/one/claim", headers={"Authorization": f"Bearer {self.tok}", **headers},
                           body=body)

    def test_the_state_and_the_tailnet_address_are_kept(self):
        status, _, _ = self.claim({"limit": 0, "ready": False, "why": "no gateway", "state": "gateway_down"},
                                  **{"X-Forwarded-For": "100.101.102.103"})
        self.assertEqual(status, 200)
        own = OwnChat(self.s.w.store)
        row = own._gw_row(self.s.w.ws, "one")
        self.assertEqual((row["state"], row["peer_ip"]), ("gateway_down", "100.101.102.103"))
        self.assertEqual(own.gateway_state(self.s.w.ws, "one")["state"], "gateway_down")

    def test_an_address_from_outside_the_tailnet_is_not_kept(self):
        self.claim({"limit": 0, "ready": True, "state": "ready"}, **{"X-Forwarded-For": "192.135.112.23"})
        self.assertIsNone(OwnChat(self.s.w.store)._gw_row(self.s.w.ws, "one")["peer_ip"])


@unittest.skipIf(os.name == "nt", "the stand-in tailscale is a #! script")
class PeersCommand(unittest.TestCase):
    """`tw-decide peers` asks the VM's tailnet client, read only, and keeps Online and LastSeen."""

    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.tok = Feeds(self.w.store).new_token(self.w.ws, "own-one", by="tests")
        on(self.w, "own_chat_write")
        OwnChat(self.w.store).claim(self.w.ws, "one", self.tok, 0, True, "", "ready", "100.101.102.103")
        self.bin = Path(tempfile.mkdtemp())

    def stand_in(self, out: str, code: int = 0) -> str:
        path = self.bin / "tailscale"
        (self.bin / "out.json").write_text(out, encoding="utf-8")
        path.write_text(f"#!/bin/sh\n[ \"$1 $2\" = 'status --json' ] || exit 9\ncat '{self.bin / 'out.json'}'\nexit {code}\n",
                        encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    def run_cli(self, tailscale: str):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from thornswood_decisions import cli
        self.w.close()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--db", str(self.w.path), "peers", "--workspace", "test", "--tailscale", tailscale])
        return code, out.getvalue(), err.getvalue()

    def row(self):
        con = connect(self.w.path)
        try:
            return con.execute("SELECT peer_online, peer_seen, peer_checked FROM own_gateways "
                               "WHERE gateway = 'one'").fetchone()
        finally:
            con.close()

    def test_an_offline_one_is_written_with_when_it_was_last_seen(self):
        peer = {"Peer": {"n1": {"TailscaleIPs": ["100.101.102.103"], "Online": False,
                                "LastSeen": "2026-11-01T19:12:00Z", "HostName": "one"}}}
        code, out, err = self.run_cli(self.stand_in(json.dumps(peer)))
        self.assertEqual(code, 0, err)
        self.assertIn('"written": 1', out)
        online, seen, checked = self.row()
        self.assertEqual((online, seen), (0, "2026-11-01T19:12:00+00:00"))
        self.assertTrue(checked)

    def test_a_tailscale_that_fails_writes_nothing_and_says_so(self):
        code, _, err = self.run_cli(self.stand_in("{}", code=1))
        self.assertEqual(code, 1)
        self.assertIn("STOP no_tailscale", err)
        self.assertEqual(self.row()[2], None)
