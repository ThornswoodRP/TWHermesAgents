"""Retry, undo, edit and resend, and branch, on one message of a chat (rewinds.py, TWHermesAgents #80).

Hermes Desktop offers the four on a message; the app offers them in his own Hermes and One and in a Bot's Bot
Chat or a session a Board row opened. Checks:
- the core: an undo or a branch needs what a message to that chat needs (own_chat_write, a listed chat, a
  passkey sign-in from the last 30 minutes, the gateway answering; mobile_chat_write for a Bot, never a room),
  one ask per idempotency key, and the order kept in each chat: a rewind, a retry or an edit goes only once
  nothing sent to that chat before it is on its way, and nothing after it goes until it is done; a lease that
  ran out is never done again by itself; a branch done names the new chat; no words in the event chain;
- the routes, the way back from migration 24;
- both connectors handing each to the helper and saying what came of it, and a retry's or an edit's turn
  carrying --tw-rewind, a refused rewind failing the message;
- room_send.py's op rewind and its --tw-rewind turn, against stand-ins for Hermes's gateway that keep Hermes's
  own rules (only one of his turns is a target, a retry refuses a turn of a photo, the chat must be idle).
Everything runs in a scratch test workspace; nothing reaches a real Hermes.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import timedelta
from pathlib import Path

from decisions_helpers import World
from test_decisions_api import ORIGIN, Served
from test_decisions_chat import flags, seat
from test_decisions_control import STUB_CONSTANTS, STUB_RUNTIME
from test_decisions_own import READY, SID, FakeCore, Gateways, on, passkey, state_db
from test_decisions_own_turns import MAIN, VIEW

from thornswood_decisions import connector, own_connector
from thornswood_decisions.chat import Chat
from thornswood_decisions.feeds import Feeds
from thornswood_decisions.rewinds import rewind_of
from thornswood_decisions.store import CoreError

HELPER = Path(own_connector.__file__).with_name("room_send.py")


class OwnCore(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.g = Gateways(self.w)
        self.own = self.g.own
        self.g.hello("one")
        self.g.feed("one")
        self.n = 0

    def key(self):
        self.n += 1
        return f"rw-key-{self.n:04d}"

    def ask(self, op="undo", mid=5, p=None, sid=SID, key=None):
        self.w.clock.advance(seconds=1)  # each ask a moment after the one before, as on the phone
        return self.own.rewind(p or passkey(self.w), "one", sid, op, mid, key or self.key())

    def send(self, text="hello", rewind=None, rewind_to=None, sid=SID, **kw):
        self.w.clock.advance(seconds=1)
        return self.own.send(passkey(self.w), "one", sid, None, text, self.key(), rewind=rewind, rewind_to=rewind_to, **kw)

    def claim(self, limit=3):
        return self.own.claim(self.w.ws, "one", self.g.tok["one"], limit, True)

    def rewound(self, r, outcome="done", **more):
        return self.own.rewound(self.w.ws, "one", self.g.tok["one"], {"id": r["id"], "outcome": outcome, **more})

    def refused(self, reason, fn):
        with self.assertRaises(CoreError) as e:
            fn()
        self.assertEqual(e.exception.reason, reason)

    def test_an_undo_needs_what_a_message_needs(self):
        self.refused("passkey_needed", lambda: self.ask(p=self.w.patrick()))
        self.refused("passkey_needed", lambda: self.ask(p=passkey(self.w, minutes_ago=31)))
        self.refused("not_listed", lambda: self.ask(sid="20261101_000000_zz99zz"))
        self.refused("bad_op", lambda: self.ask(op="redo"))
        self.refused("bad_message", lambda: self.ask(mid=0))
        self.refused("bad_message", lambda: self.ask(mid="5"))
        self.w.store.set_flag(self.w.ws, "own_chat_write", False, by="tests")
        self.refused("own_chat_off", self.ask)
        on(self.w, "own_chat_write")
        self.g.hello("one", ready=False, why="the gateway is absent")
        with self.assertRaises(CoreError) as e:
            self.ask()
        self.assertEqual(e.exception.status, 409)
        self.g.hello("one")
        self.assertEqual(self.ask()["status"], "queued")

    def test_one_ask_per_key(self):
        first = self.ask(op="branch", mid=9, key="rw-key-same")
        self.assertEqual((first["op"], first["message_id"], first["status"], first["session_id"]),
                         ("branch", 9, "queued", SID))
        again = self.ask(op="branch", mid=9, key="rw-key-same")
        self.assertTrue(again["replayed"])
        self.assertEqual(again["id"], first["id"])
        self.refused("key_reused", lambda: self.ask(op="undo", mid=9, key="rw-key-same"))
        self.assertEqual(self.w.store.db.execute("SELECT count(*) FROM chat_rewinds").fetchone()[0], 1)

    def test_an_undo_waits_for_what_was_sent_before_it_and_holds_what_comes_after(self):
        before = self.send("first")["message_id"]
        undo = self.ask(mid=5)
        after = self.send("second")["message_id"]
        got = self.claim()
        self.assertEqual(([i["id"] for i in got["items"]], got["rewinds"]), ([before], []))
        self.own.result(self.w.ws, "one", self.g.tok["one"], before, "confirmed", "turn finished")
        got = self.claim()
        self.assertEqual(got["items"], [])
        self.assertEqual(got["rewinds"], [{"id": undo["id"], "chat": SID, "op": "undo", "message_id": 5,
                                           "session_id": SID}])
        self.assertEqual(self.claim()["items"], [], "nothing goes while the undo is on its way")
        self.assertEqual(self.rewound(got["rewinds"][0]), {"status": "done"})
        self.assertEqual([i["id"] for i in self.claim()["items"]], [after])

    def test_another_chat_is_not_held(self):
        other = "20261101_201600_cd34ef"
        self.g.feed("one", chats=[{"id": SID, "title": "Plan", "last_at": self.w.clock().isoformat(), "preview": ""},
                                  {"id": other, "title": "Other", "last_at": self.w.clock().isoformat(), "preview": ""}])
        undo = self.ask()
        msg = self.send("elsewhere", sid=other)["message_id"]
        got = self.claim()
        self.assertEqual(([r["id"] for r in got["rewinds"]], [i["id"] for i in got["items"]]), ([undo["id"]], [msg]))

    def test_an_undo_or_a_branch_takes_no_place_among_the_messages_a_claim_takes(self):
        undo = self.ask()
        got = self.claim(limit=0)
        self.assertEqual([r["id"] for r in got["rewinds"]], [undo["id"]])

    def test_a_retry_and_an_edit_are_messages_that_take_the_chat_back_first(self):
        retry = self.send("Which branch is green?", rewind="retry", rewind_to=7)
        self.assertEqual(retry["status"], "queued")
        (item,) = self.claim()["items"]
        self.assertEqual((item["id"], item["rewind"], item["rewind_to"], item["text"]),
                         (retry["message_id"], "retry", 7, "Which branch is green?"))
        edit = self.send("Which branches are green?", rewind="edit", rewind_to=7)
        self.assertEqual(self.claim()["items"], [], "an edit waits while the retry before it is on its way")
        self.own.result(self.w.ws, "one", self.g.tok["one"], retry["message_id"], "confirmed", "turn finished")
        (item,) = self.claim()["items"]
        self.assertEqual((item["id"], item["rewind"]), (edit["message_id"], "edit"))
        mine = self.own.view(passkey(self.w), "one", SID)["mine"]
        self.assertEqual([(m["rewind"], m["rewind_to"]) for m in mine], [("retry", 7), ("edit", 7)])

    def test_a_retry_waits_while_a_message_before_it_is_on_its_way(self):
        before = self.send("first")["message_id"]
        self.assertEqual([i["id"] for i in self.claim()["items"]], [before])
        self.send("again", rewind="retry", rewind_to=3)
        self.assertEqual(self.claim()["items"], [])

    def test_a_retry_or_an_edit_is_words_to_a_chat_there_is(self):
        self.refused("bad_rewind", lambda: self.send(rewind="redo", rewind_to=3))
        self.refused("bad_rewind", lambda: self.send(rewind="retry", rewind_to=0))
        self.refused("bad_rewind", lambda: self.send(rewind="retry"))
        self.refused("bad_rewind", lambda: self.own.send(passkey(self.w), "one", None, True, "hi", self.key(),
                                                         rewind="edit", rewind_to=3))
        self.refused("bad_rewind", lambda: self.send(rewind="edit", rewind_to=3, reply_to=2, quote="hi",
                                                     quote_by="hermes"))
        self.assertEqual(rewind_of(None, None), (None, None))

    def test_a_lease_that_ran_out_is_uncertain_and_never_done_again(self):
        undo = self.ask(op="branch")
        (r,) = self.claim()["rewinds"]
        self.w.clock.advance(minutes=3)
        self.assertEqual(self.claim()["rewinds"], [])
        (shown,) = self.own.view(passkey(self.w), "one", SID)["rewinds"]
        self.assertEqual((shown["id"], shown["status"], shown["reason"]),
                         (undo["id"], "uncertain", "the connector stopped before it said what happened"))
        self.assertEqual(self.rewound(r, session_id="s_child"), {"status": "uncertain", "stale": True})
        self.assertEqual(self.claim()["rewinds"], [])

    def test_what_came_of_each(self):
        branch = self.ask(op="branch", mid=9)
        (r,) = self.claim()["rewinds"]
        self.refused("bad_result", lambda: self.rewound(r))
        self.refused("bad_outcome", lambda: self.rewound(r, outcome="maybe"))
        self.assertEqual(self.rewound(r, session_id="20261110_101500_aa11bb"),
                         {"status": "done", "session_id": "20261110_101500_aa11bb"})
        (shown,) = self.own.view(passkey(self.w), "one", SID)["rewinds"]
        self.assertEqual((shown["id"], shown["made_session_id"]), (branch["id"], "20261110_101500_aa11bb"))
        # Not ready: nothing was done, so it goes back in the queue and is handed out again.
        undo = self.ask()
        (r,) = self.claim()["rewinds"]
        self.assertEqual(self.rewound(r, outcome="not_ready", reason="the gateway is absent"), {"status": "queued"})
        (r,) = self.claim()["rewinds"]
        self.assertEqual(r["id"], undo["id"])
        self.assertEqual(self.rewound(r, outcome="failed", reason="Hermes refused it (session_busy)"),
                         {"status": "failed"})
        shown = self.own.view(passkey(self.w), "one", SID)["rewinds"][-1]
        self.assertEqual((shown["status"], shown["reason"], shown["made_session_id"]),
                         ("failed", "Hermes refused it (session_busy)", None))
        self.refused("no_item", lambda: self.rewound({"id": "rw_doesnotexist"}))

    def test_no_words_go_into_the_event_chain(self):
        self.send("my secret words", rewind="edit", rewind_to=4)
        self.ask(op="branch", mid=4)
        chain = json.dumps([list(r) for r in self.w.store.db.execute("SELECT * FROM events").fetchall()])
        self.assertNotIn("secret", chain)
        self.assertIn("chat.rewind_branch", chain)
        self.assertTrue(self.w.store.verify_events()[0])


class BotCore(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        seat(self.w)
        flags(self.w, "mobile_chat_write", "connector_effects")
        self.chat = Chat(self.w.store)
        self.tok = Feeds(self.w.store).new_token(self.w.ws, "connector", by="tests")
        self.n = 0

    def key(self):
        self.n += 1
        return f"bot-rw-key-{self.n:04d}"

    def test_a_bots_chat_and_its_sessions_but_never_a_room(self):
        p = self.w.patrick()
        with self.assertRaises(CoreError) as e:
            self.chat.rewind(p, "room", "undo", 4, self.key())
        self.assertEqual(e.exception.reason, "no_room_rewinds")
        with self.assertRaises(CoreError) as e:
            self.chat.send(p, "room", "hi", self.key(), rewind="retry", rewind_to=4)
        self.assertEqual(e.exception.reason, "bad_rewind")
        self.w.store.set_flag(self.w.ws, "mobile_chat_write", False, by="tests")
        with self.assertRaises(CoreError) as e:
            self.chat.rewind(p, "rift", "undo", 4, self.key())
        self.assertEqual(e.exception.reason, "chat_write_off")
        flags(self.w, "mobile_chat_write")
        got = self.chat.rewind(p, "rift", "branch", 4, self.key(), session="s_rift_lo")
        self.assertEqual((got["target"], got["session"], got["op"], got["status"]), ("rift", "s_rift_lo", "branch", "queued"))
        self.assertEqual([r["op"] for r in self.chat.view(p, "rift", session="s_rift_lo")["rewinds"]], ["branch"])
        self.assertEqual(self.chat.view(p, "rift")["rewinds"], [], "the Bot Chat's are its own")

    def test_one_thing_at_a_time_for_each_bot_in_the_order_he_asked(self):
        p = self.w.patrick()
        first = self.chat.send(p, "rift", "first", self.key())["message_id"]
        self.w.clock.advance(seconds=1)  # each a moment after the one before, as on the phone
        undo = self.chat.rewind(p, "rift", "undo", 4, self.key())
        self.w.clock.advance(seconds=1)
        retry = self.chat.send(p, "rift", "again", self.key(), rewind="retry", rewind_to=6)["message_id"]
        other = self.chat.send(p, "wisp", "elsewhere", self.key())["message_id"]
        got = self.chat.claim(self.w.ws, self.tok, 7)
        self.assertEqual(({i["id"] for i in got["items"]}, got["rewinds"]), ({first, other}, []))
        self.chat.result(self.w.ws, self.tok, first, "confirmed", "turn finished")
        got = self.chat.claim(self.w.ws, self.tok, 7)
        self.assertEqual(got["items"], [])
        self.assertEqual(got["rewinds"], [{"id": undo["id"], "chat": "rift", "op": "undo", "message_id": 4, "bot": "rift"}])
        self.assertEqual(self.chat.claim(self.w.ws, self.tok, 7)["items"], [])
        self.assertEqual(self.chat.rewound(self.w.ws, self.tok, {"id": undo["id"], "outcome": "done"}), {"status": "done"})
        (item,) = self.chat.claim(self.w.ws, self.tok, 7)["items"]
        self.assertEqual((item["id"], item["rewind"], item["rewind_to"]), (retry, "retry", 6))

    def test_a_branch_of_a_session_names_the_session(self):
        r = self.chat.rewind(self.w.patrick(), "rift", "branch", 4, self.key(), session="s_rift_lo")
        (got,) = self.chat.claim(self.w.ws, self.tok, 7)["rewinds"]
        self.assertEqual((got["id"], got["session"], got["bot"]), (r["id"], "s_rift_lo", "rift"))
        out = self.chat.rewound(self.w.ws, self.tok, {"id": r["id"], "outcome": "done", "session_id": "s_rift_lo2"})
        self.assertEqual(out, {"status": "done", "session_id": "s_rift_lo2"})


class Routes(unittest.TestCase):
    def test_the_phone_asks_and_only_the_connectors_answer(self):
        s = Served(self)
        g = Gateways(s.w)
        seat(s.w)
        on(s.w, "mobile_read", "mobile_chat_write", "connector_effects")
        g.hello("one")
        g.feed("one")
        dev = s.w.device()
        sess = s.w.core.auth.issue_session(s.w.ws, method="pairing", device_id=dev)
        h = {"Cookie": f"tw_session={sess.token}", "Origin": ORIGIN, "X-CSRF-Token": sess.csrf}
        status, _, body = s.call("POST", "/own/one/rewind", headers=h,
                                 body={"session_id": SID, "op": "undo", "message_id": 5, "idempotency_key": "route-rw-0001"})
        self.assertEqual((status, body["error"]), (403, "passkey_needed"), "a pairing sign-in is no passkey")
        status, _, body = s.call("POST", "/chat/rift/rewind", headers=h,
                                 body={"op": "branch", "message_id": 5, "idempotency_key": "route-rw-0002"})
        self.assertEqual((status, body["status"], body["target"]), (200, "queued", "rift"), body)
        tok = Feeds(s.w.store).new_token(s.w.ws, "connector", by="tests")
        status, _, body = s.call("POST", "/connector/claim", headers={"Authorization": f"Bearer {tok}"}, body={"limit": 3})
        (r,) = body["rewinds"]
        status, _, body = s.call("POST", "/connector/rewound", headers=h, body={"id": r["id"], "outcome": "done"})
        self.assertEqual((status, body["error"]), (403, "not_a_feed"))
        status, _, body = s.call("POST", "/connector/rewound", headers={"Authorization": f"Bearer {tok}"},
                                 body={"id": r["id"], "outcome": "done", "session_id": "s_new"})
        self.assertEqual((status, body), (200, {"status": "done", "session_id": "s_new"}))
        # One's connector answers its own on its own route.
        own = g.own
        own.rewind(passkey(s.w), "one", SID, "undo", 5, "route-rw-0003")
        (r,) = own.claim(s.w.ws, "one", g.tok["one"], 0, True)["rewinds"]
        status, _, body = s.call("POST", "/own/one/rewound", headers={"Authorization": f"Bearer {g.tok['one']}"},
                                 body={"id": r["id"], "outcome": "done"})
        self.assertEqual((status, body), (200, {"status": "done"}))


class Migration24(unittest.TestCase):
    def test_the_way_back_refuses_while_one_is_on_its_way(self):
        from thornswood_decisions.schema import DOWNS
        w = World()
        self.addCleanup(w.close)
        g = Gateways(w)
        g.hello("one")
        g.feed("one")
        checks = DOWNS[24][0]
        count = lambda: [w.store.db.execute(q).fetchone()[0] for q, _ in checks]
        self.assertEqual(sum(count()), 0)
        g.own.rewind(passkey(w), "one", SID, "undo", 5, "mig-rw-0001")
        g.own.send(passkey(w), "one", SID, None, "again", "mig-rw-0002", rewind="retry", rewind_to=5)
        self.assertEqual(sum(count()), 2)


# ── the connectors ──────────────────────────────────────────────────────────

READY_REWIND = READY.replace('req = json.loads(sys.stdin.read())', textwrap.dedent('''\
    req = json.loads(sys.stdin.read())
    if req.get("op") == "rewind":
        with open(os.environ["TW_FAKE_LOG"], "a", encoding="utf-8") as f:
            f.write(json.dumps(req) + "\\n")
        if req["message_id"] == 13:
            print(json.dumps({"done": False, "reason": "Hermes refused it (session_busy)"}))
        elif req["action"] == "branch":
            print(json.dumps({"done": True, "session_id": "20261110_101500_aa11bb"}))
        else:
            print(json.dumps({"done": True}))
        sys.exit(0)'''))


class RewindCore(FakeCore):
    def post(self, op, body):
        if op == "rewound":
            try:
                return 200, self.own.rewound(self.ws, self.gw, self.token, body)
            except CoreError as exc:
                return exc.status, {"error": exc.reason}
        return super().post(op, body)


class OwnConnector(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.g = Gateways(self.w)
        self.g.hello("one")
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.db = state_db(self.dir)
        (self.dir / "ready.py").write_text(READY_REWIND, encoding="utf-8")
        self.log = self.dir / "helper.jsonl"
        os.environ["TW_FAKE_LOG"] = str(self.log)
        self.addCleanup(lambda: os.environ.pop("TW_FAKE_LOG", None))
        hermes = own_connector.Hermes([sys.executable, "-c", "pass"], [sys.executable, "-I", str(self.dir / "ready.py")],
                                      self.dir, "One's Hermes")
        self.lines = []
        self.conn = own_connector.OwnConnector(RewindCore(self.g.own, self.w.ws, "one", self.g.tok["one"]), hermes,
                                               self.db, say=self.lines.append)
        self.g.feed("one", chats=[{"id": "s_plan", "title": "Plan the week", "last_at": "", "preview": ""}], opened=[])

    def test_each_goes_to_the_helper_and_what_came_of_it_to_the_core(self):
        branch = self.g.own.rewind(passkey(self.w), "one", "s_plan", "branch", 9, "conn-rw-0001")
        self.conn.once()
        (req,) = [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(req, {"op": "rewind", "profile": "", "action": "branch", "session_id": "s_plan", "message_id": 9})
        (shown,) = self.g.own.view(passkey(self.w), "one", "s_plan")["rewinds"]
        self.assertEqual((shown["id"], shown["status"], shown["made_session_id"]),
                         (branch["id"], "done", "20261110_101500_aa11bb"))
        self.assertIn("PASS chat branch: done", self.lines)
        self.g.own.rewind(passkey(self.w), "one", "s_plan", "undo", 13, "conn-rw-0002")
        self.conn.once()
        shown = self.g.own.view(passkey(self.w), "one", "s_plan")["rewinds"][-1]
        self.assertEqual((shown["status"], shown["reason"]), ("failed", "Hermes refused it (session_busy)"))

    def test_the_helpers_answers_are_read_as_they_are_meant(self):
        h = self.conn.hermes
        self.assertEqual(h.rewind("undo", "s_plan", 5), ("done", "", None))
        self.assertEqual(h.rewind("undo", "s_plan", 13), ("failed", "Hermes refused it (session_busy)", None))


class BotsConnector(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        db = self.dir / ".hermes" / "profiles" / "rift" / "state.db"
        db.parent.mkdir(parents=True)
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, archived INTEGER DEFAULT 0, started_at REAL)")
        con.execute("INSERT INTO sessions(id, title) VALUES ('s_rift_lo', 'Rebuild the load order')")
        con.commit()
        con.close()
        self.seen = self.dir / "seen.json"

    def hermes(self, stream=(), answer=None) -> connector.Hermes:
        script = self.dir / "helper.py"
        script.write_text(textwrap.dedent(f"""
            import json, os, sys
            if "chat" not in sys.argv:
                req = json.loads(sys.stdin.read())
                if req.get("op") == "rewind":
                    open({str(self.seen)!r}, "w").write(json.dumps(req))
                    print(json.dumps({answer!r}))
                else:
                    print(json.dumps({{"ready": True}}))
                sys.exit(0)
            q = sys.argv[sys.argv.index("--query-file") + 1]
            open({str(self.seen)!r}, "w").write(json.dumps({{"argv": sys.argv[1:], "text": open(q).read()}}))
            for line in {list(stream)!r}:
                print(json.dumps(line), flush=True)
            sys.exit(0)
        """), encoding="utf-8")
        return connector.Hermes(["hermes"], [sys.executable, "-I", str(script)], self.dir)

    def test_a_retry_into_a_session_carries_the_turn_it_goes_back_to(self):
        h = self.hermes([{"type": "rewind", "status": "done", "count": 2}, {"type": "admission", "status": "started"},
                         {"type": "result"}])
        self.assertEqual(h.to_session("rift", "s_rift_lo", "again", rewind=("retry", 7)),
                         ("confirmed", "turn finished", ""))
        seen = json.loads(self.seen.read_text())
        self.assertEqual(seen["argv"][:4], ["chat", "--tw-rewind", "retry", "7"])
        self.assertEqual(seen["argv"][-2:], ["--resume", "s_rift_lo"])

    def test_a_refused_rewind_fails_the_message(self):
        h = self.hermes([{"type": "rewind", "status": "refused", "reason": "Hermes refused it (session_busy)"}])
        self.assertEqual(h.to_session("rift", "s_rift_lo", "new words", rewind=("edit", 7)),
                         ("failed", "", "Hermes refused it (session_busy)"))

    def test_an_undo_or_a_branch_goes_only_to_a_session_of_that_bot(self):
        h = self.hermes(answer={"done": True, "session_id": "s_rift_lo2"})
        self.assertEqual(h.rewind("rift", "s_nobody", "branch", 4), ("failed", "that session is not one of rift's", None))
        self.assertFalse(self.seen.exists())
        self.assertEqual(h.rewind("rift", "s_rift_lo", "branch", 4), ("done", "", "s_rift_lo2"))
        self.assertEqual(json.loads(self.seen.read_text()), {"op": "rewind", "profile": "rift", "action": "branch",
                                                              "session_id": "s_rift_lo", "message_id": 4})
        h = self.hermes(answer={"done": None, "reason": "Hermes did not answer in 60 seconds; it may be done"})
        self.assertEqual(h.rewind("rift", "s_rift_lo", "undo", 4)[0], "uncertain")
        h = self.hermes(answer={"done": False, "ready": False, "reason": "rift's gateway is absent"})
        self.assertEqual(h.rewind("rift", "s_rift_lo", "undo", 4), ("not_ready", "rift's gateway is absent", None))

    def test_the_connector_does_each_it_was_handed_and_says_what_came_of_it(self):
        class Stand:
            def __init__(self):
                self.calls = []

            def rewind(self, bot, session, action, mid):
                self.calls.append((bot, session, action, mid))
                return ("done", "", "s_new") if action == "branch" else ("failed", "Hermes refused it", None)

            def to_bot(self, bot, text, doing=None, live=None, rewind=None):
                self.calls.append(("to_bot", bot, text, rewind))
                return "confirmed", "turn finished", ""

        class Posts(connector.Core):
            def __init__(self):
                self.posted = []

            def post(self, path, body):
                self.posted.append((path, body))
                return 200, {}

        stand, core = Stand(), Posts()
        c = connector.Connector(core, stand, say=lambda line: None)
        self.assertEqual(c.rewinds([{"id": "rw_aaaaaaaa", "bot": "rift", "op": "branch", "message_id": 4},
                                    {"id": "rw_bbbbbbbb", "bot": "rift", "op": "undo", "message_id": 6,
                                     "session": "s_rift_lo"},
                                    {"id": "rw_cccccccc", "bot": "../x", "op": "undo", "message_id": 6}]), 1)
        self.assertEqual(stand.calls, [("rift", None, "branch", 4), ("rift", "s_rift_lo", "undo", 6)])
        c._offer(list(c.unsaid))
        self.assertEqual([(p, b["id"], b["outcome"], b.get("session_id")) for p, b in core.posted],
                         [("/connector/rewound", "rw_aaaaaaaa", "done", "s_new"),
                          ("/connector/rewound", "rw_bbbbbbbb", "failed", None)])
        c.deliver({"id": "msg_1", "target": "rift", "text": "again", "rewind": "retry", "rewind_to": 6})
        self.assertEqual(stand.calls[-1], ("to_bot", "rift", "again", ("retry", 6)))


# ── the helper, against stand-ins for Hermes's gateway ─────────────────────

# A chat as Hermes's gateway keeps it: rows with Hermes's own ids, his turns user rows (one of them a photo),
# a chat that is busy while TW_STUB_BUSY is set. session.mutate rewind takes the chat back to before the
# target and answers the target's row, as hermes_state_mutations.py rewind does; branch answers the new chat.
STUB_CLIENT = textwrap.dedent('''
    import json, os
    from contextlib import asynccontextmanager
    class GatewayClientError(ValueError):
        pass
    COMPRESS_RPC_TIMEOUT = 600
    def state():
        return json.load(open(os.environ["TW_STUB_CHAT"], encoding="utf-8"))
    def log(entry):
        with open(os.environ["TW_STUB_LOG"], "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\\n")
    class Client:
        async def rpc(self, method, **p):
            log([method, {k: v for k, v in p.items() if k not in ("request_id", "input_id")}])
            s = state()
            if method == "session.resume":
                return {"session_id": p["session_id"], "revision": s["revision"], "execution_generation": 3,
                        "messages": [m for m in s["messages"] if m["active"]]}
            if method == "session.mutate":
                if os.environ.get("TW_STUB_BUSY"):
                    raise GatewayClientError("session_busy")
                if (p["expected_revision"], p.get("expected_generation")) != (s["revision"], 3):
                    raise GatewayClientError("revision_conflict")
                if p["operation"] == "branch":
                    return {"status": "applied", "branched_session_id": "s_child"}
                mid = p["payload"]["target_message_id"]
                target = next(m for m in s["messages"] if m["row_id"] == mid)
                gone = [m for m in s["messages"] if m["row_id"] >= mid and m["active"]]
                for m in gone:
                    m["active"] = False
                s["revision"] += 1
                json.dump(s, open(os.environ["TW_STUB_CHAT"], "w", encoding="utf-8"))
                return {"status": "applied", "rewound_count": len(gone), "target_message": target}
            if method == "prompt.submit":
                return {"admission_id": "a2", "status": "started"}
            raise GatewayClientError("method_not_found")
    @asynccontextmanager
    async def connect_gateway(endpoint=None, subject=None):
        yield Client()
''')

# hermes_cli/gateway_mutations.py PreparedMutations as v0.23.0 has it, without the confirmation prompt (a rewind
# and a branch never ask one): a payload may be a function of the snapshot its fences are read from.
STUB_MUTATIONS = textwrap.dedent('''
    import json, uuid
    class PreparedMutations:
        def __init__(self):
            self.pending = {}
        async def apply(self, client, session_id, operation, payload, *, confirm=None):
            key = (session_id, operation, None if callable(payload) else json.dumps(payload, sort_keys=True))
            if key not in self.pending:
                snapshot = await client.rpc("session.resume", session_id=session_id)
                body = payload(snapshot) if callable(payload) else json.loads(key[2])
                if body is None:
                    return {"status": "nothing", "session_id": session_id, "operation": operation}
                self.pending[key] = {"params": dict(session_id=session_id, operation=operation, payload=body,
                    request_id=uuid.uuid4().hex, expected_revision=snapshot["revision"],
                    expected_generation=snapshot["execution_generation"])}
            entry = self.pending[key]
            entry["result"] = await client.rpc("session.mutate", **entry["params"])
            return entry["result"]
        def acknowledge(self, session_id, operation, payload):
            self.pending.pop((session_id, operation, None if callable(payload) else json.dumps(payload, sort_keys=True)), None)
''')

# hermes_cli/gateway_chat_commands.py _user_turns and agent/context_compressor.py's turn checks, as v0.23.0 has
# them for a plain chat: his live turns, and a turn of a photo (content a list) that /retry refuses.
STUB_COMMANDS = textwrap.dedent('''
    from agent.context_compressor import is_user_originated_turn
    def _user_turns(snapshot):
        return [row for row in snapshot.get("messages", [])
                if type(row.get("row_id")) is int and row.get("display_kind") in (None, "", "steer")
                and not row.get("archived") and is_user_originated_turn(row)]
''')
STUB_COMPRESSOR = textwrap.dedent('''
    def is_user_originated_turn(row):
        return row.get("role") == "user"
    def user_originated_turn_view(row):
        return row
    def retryable_user_text(content):
        if not isinstance(content, str):
            raise ValueError("an image turn cannot be resent as text")
        return content
''')

CHAT = {"revision": 7, "messages": [
    {"row_id": 1, "role": "user", "content": "Which branch is green?", "active": True},
    {"row_id": 2, "role": "assistant", "content": "main is green.", "active": True},
    {"row_id": 3, "role": "user", "content": [{"type": "image"}], "active": True},
    {"row_id": 4, "role": "assistant", "content": "A photo of the board.", "active": True},
    {"row_id": 5, "role": "user", "content": "And the fork?", "active": True},
    {"row_id": 6, "role": "assistant", "content": "The fork is green too.", "active": True}]}


class TheHelper(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        src = self.dir / "src"
        for pkg in ("hermes_cli", "agent"):
            (src / pkg).mkdir(parents=True)
            (src / pkg / "__init__.py").write_text("", encoding="utf-8")
        for name, text in (("hermes_cli/gateway_runtime.py", STUB_RUNTIME), ("hermes_cli/gateway_client.py", STUB_CLIENT),
                           ("hermes_cli/gateway_mutations.py", STUB_MUTATIONS),
                           ("hermes_cli/gateway_chat_commands.py", STUB_COMMANDS),
                           ("agent/context_compressor.py", STUB_COMPRESSOR), ("hermes_constants.py", STUB_CONSTANTS),
                           # The attended turn's own stand-ins (test_decisions_own_turns), with the view's
                           # PreparedMutations and the gateway's chat above.
                           ("hermes_cli/gateway_chat_view.py", VIEW.replace(
                               "        self.done = None\n",
                               "        self.done = None\n"
                               "        from hermes_cli.gateway_mutations import PreparedMutations\n"
                               "        self.mutations = PreparedMutations()\n")),
                           ("hermes_cli/main.py", MAIN.replace(
                               '        raise SystemExit("unexpected " + method)',
                               '        from hermes_cli.gateway_client import Client as Gateway\n'
                               '        return await Gateway().rpc(method, **p)'))):
            (src / name).write_text(text, encoding="utf-8")
        self.src, self.root = src, self.dir / "hermes"
        self.root.mkdir()
        self.chat, self.log = self.dir / "chat.json", self.dir / "rpc.jsonl"
        self.chat.write_text(json.dumps(CHAT), encoding="utf-8")
        self.calls = self.dir / "calls.jsonl"
        self.env = {**os.environ, "TW_STUB_LOG": str(self.log), "TW_STUB_ROOT": str(self.root),
                    "TW_STUB_CHAT": str(self.chat), "TW_CALLS": str(self.calls), "TW_STUB": json.dumps({"ask": None})}

    def op(self, action, mid, **env) -> dict:
        out = subprocess.run([sys.executable, "-I", str(HELPER), "--hermes-root", str(self.src)],
                             input=json.dumps({"op": "rewind", "profile": "", "action": action, "session_id": "s1",
                                               "message_id": mid}).encode(),
                             capture_output=True, timeout=60, env={**self.env, **env})
        self.assertEqual(out.returncode, 0, out.stderr.decode())
        return json.loads(out.stdout.decode().strip().splitlines()[-1])

    def rpcs(self) -> list:
        return [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()] if self.log.exists() else []

    def active(self) -> list[int]:
        return [m["row_id"] for m in json.loads(self.chat.read_text())["messages"] if m["active"]]

    def test_undo_takes_back_that_turn_and_everything_after_it_fenced_to_the_chat(self):
        self.assertEqual(self.op("undo", 5), {"done": True})
        self.assertEqual(self.active(), [1, 2, 3, 4])
        resume, mutate = self.rpcs()
        self.assertEqual(resume[0], "session.resume")
        self.assertEqual(mutate, ["session.mutate", {"session_id": "s1", "operation": "rewind",
                                                     "payload": {"target_message_id": 5}, "expected_revision": 7,
                                                     "expected_generation": 3}])

    def test_only_one_of_his_turns_is_taken_back_to(self):
        out = self.op("undo", 6)
        self.assertEqual(out, {"done": False, "reason": "Hermes refused it (that turn is no longer one of yours in the chat)"})
        self.assertEqual([r[0] for r in self.rpcs()], ["session.resume"])
        self.assertEqual(self.active(), [1, 2, 3, 4, 5, 6])

    def test_a_busy_chat_is_refused_and_kept_as_it_is(self):
        self.assertEqual(self.op("undo", 5, TW_STUB_BUSY="1"), {"done": False, "reason": "Hermes refused it (session_busy)"})
        self.assertEqual(self.active(), [1, 2, 3, 4, 5, 6])

    def test_a_branch_names_the_new_chat(self):
        self.assertEqual(self.op("branch", 4), {"done": True, "session_id": "s_child"})
        self.assertEqual(self.rpcs()[-1][1]["payload"], {"through_message_id": 4})

    def test_a_gateway_not_running_does_nothing_and_says_so(self):
        out = self.op("undo", 5, TW_STUB_DOWN="1")
        self.assertEqual((out["done"], out["ready"]), (False, False))
        self.assertEqual(self.rpcs(), [])

    def test_a_bad_request_changes_nothing(self):
        for action, mid in (("redo", 5), ("undo", 0), ("undo", "5")):
            self.assertEqual(self.op(action, mid), {"done": False, "reason": "bad request"})
        self.assertEqual(self.rpcs(), [])

    def turn(self, kind, mid, words="the phone's words"):
        query = self.dir / "message.txt"
        query.write_text(words, encoding="utf-8")
        self.calls.write_text("", encoding="utf-8")
        out = subprocess.run([sys.executable, "-I", str(HELPER), "--hermes-root", str(self.src), "attended-chat",
                              "--tw-rewind", kind, str(mid), "-Q", "--format", "stream-json", "--query-file", str(query),
                              "--resume", "s1"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                             timeout=60, env=self.env)
        lines = [json.loads(x) for x in out.stdout.splitlines() if x.startswith("{")]
        return out.returncode, lines, out.stderr

    def submitted(self) -> list:
        return [c[1]["text"] for c in (json.loads(x) for x in self.calls.read_text(encoding="utf-8").splitlines())
                if c[0] == "prompt.submit"]

    def test_a_retry_takes_the_chat_back_and_sends_that_turn_again_as_hermes_kept_it(self):
        code, lines, err = self.turn("retry", 5)
        self.assertEqual(code, 0, err)
        self.assertIn({"type": "rewind", "status": "done", "count": 2}, lines)
        self.assertEqual(self.submitted(), ["And the fork?"])
        self.assertEqual(self.active(), [1, 2, 3, 4])

    def test_an_edit_sends_his_new_words_in_its_place(self):
        code, lines, err = self.turn("edit", 1, "Which branches are green?")
        self.assertEqual(code, 0, err)
        self.assertIn({"type": "rewind", "status": "done", "count": 6}, lines)
        self.assertEqual(self.submitted(), ["Which branches are green?"])
        self.assertEqual(self.active(), [])

    def test_a_retry_of_a_photo_is_refused_before_anything_changes(self):
        code, lines, _ = self.turn("retry", 3)
        self.assertNotEqual(code, 0)
        (said,) = [x for x in lines if x.get("type") == "rewind"]
        self.assertEqual(said["status"], "refused")
        self.assertIn("cannot send that turn again", said["reason"])
        self.assertEqual(self.submitted(), [])
        self.assertEqual(self.active(), [1, 2, 3, 4, 5, 6])

    def test_a_refused_rewind_sends_nothing(self):
        (self.dir / "message.txt").write_text("new words", encoding="utf-8")
        env_busy = dict(self.env, TW_STUB_BUSY="1")
        query = self.dir / "message.txt"
        out = subprocess.run([sys.executable, "-I", str(HELPER), "--hermes-root", str(self.src), "attended-chat",
                              "--tw-rewind", "edit", "1", "-Q", "--format", "stream-json", "--query-file", str(query),
                              "--resume", "s1"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                             timeout=60, env=env_busy)
        said = [x for x in (json.loads(ln) for ln in out.stdout.splitlines() if ln.startswith("{"))
                if x.get("type") == "rewind"]
        self.assertEqual(said, [{"type": "rewind", "status": "refused", "reason": "Hermes refused it (session_busy)"}])
        self.assertNotEqual(out.returncode, 0)


if __name__ == "__main__":
    unittest.main()
