"""Patrick's reactions on messages in a chat with his own Hermes (redesign step 2, plan Part 9).

Checks the core's side (a reaction is one wish per message: the same tap twice
is one, a new one replaces it, a late answer about an older wish is dropped,
a lease that ran out goes out again; own_chat_write, a listed chat, the
gateway answering, only the phone's emoji, only Patrick), the routes, that a
page keeps what Hermes holds and nothing else, the connector reading Hermes's
reactions from state.db and handing each wish to the helper, and the helper
against a stand-in for Hermes's store with Hermes's own rule (one reaction per
person per message; the same emoji again takes it off), so asking twice never
takes his reaction off. Everything runs in a scratch test workspace; nothing
reaches a real Hermes.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from decisions_helpers import World
from test_decisions_api import Served
from test_decisions_own import READY, SID, FakeCore, Gateways, on, passkey, state_db

from thornswood_decisions import own_connector
from thornswood_decisions.own import REACTIONS, clean_page
from thornswood_decisions.store import CoreError

THUMBS, HEART = REACTIONS[0], REACTIONS[1]
HELPER = Path(__file__).resolve().parent.parent / "thornswood_decisions" / "room_send.py"


class Wishes(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.g = Gateways(self.w)
        self.own = self.g.own
        self.g.hello("one")
        self.g.feed("one")
        self.p = passkey(self.w)

    def react(self, emoji=THUMBS, mid=7, gw="one", sid=SID):
        return self.own.react(self.p, gw, sid, mid, emoji)

    def claim(self, ready=True):
        return self.own.claim(self.w.ws, "one", self.g.tok["one"], 0, ready)["reactions"]

    def answer(self, r, outcome="set", reason=""):
        return self.own.reacted(self.w.ws, "one", self.g.tok["one"], {
            "session_id": r["session_id"], "message_id": r["message_id"], "version": r["version"],
            "outcome": outcome, "reason": reason})

    def row(self, mid=7):
        return self.w.store.db.execute("SELECT emoji, version, status, reason FROM chat_reactions WHERE message_id = ?",
                                       (mid,)).fetchone()

    def test_one_wish_per_message_and_the_same_tap_twice_is_one(self):
        self.assertEqual(self.react()["status"], "queued")
        self.assertTrue(self.react()["replayed"])
        self.assertEqual(tuple(self.row()), (THUMBS, 1, "queued", None))
        self.react(HEART)
        self.assertEqual(tuple(self.row()), (HEART, 2, "queued", None))
        (r,) = self.claim()
        self.assertEqual((r["emoji"], r["version"]), (HEART, 2))

    def test_set_and_taken_off(self):
        self.react()
        (r,) = self.claim()
        self.assertEqual(self.answer(r), {"status": "set"})
        self.assertEqual(self.row()["status"], "set")
        self.assertTrue(self.react()["replayed"])  # already there: nothing to hand out
        self.assertEqual(self.claim(), [])
        self.react(None)
        (r,) = self.claim()
        self.assertIsNone(r["emoji"])
        self.answer(r)
        self.assertEqual(tuple(self.row())[:3], (None, 2, "set"))

    def test_a_late_answer_about_an_older_wish_is_dropped(self):
        self.react()
        (old,) = self.claim()
        self.react(HEART)
        self.assertTrue(self.answer(old)["stale"])
        self.assertEqual(self.row()["status"], "queued")
        (new,) = self.claim()
        self.assertEqual(new["emoji"], HEART)
        self.answer(new)
        self.assertEqual(tuple(self.row())[:3], (HEART, 2, "set"))

    def test_a_lease_that_ran_out_goes_out_again(self):
        self.react()
        self.claim()
        self.assertEqual(self.claim(), [])  # still leased
        self.w.clock.advance(minutes=3)
        (r,) = self.claim()
        self.assertEqual(r["version"], 1)

    def test_failed_says_why_and_a_new_tap_tries_again(self):
        self.react()
        (r,) = self.claim()
        self.answer(r, "failed", "that message is not in this chat")
        self.assertEqual(tuple(self.row())[2:], ("failed", "that message is not in this chat"))
        self.assertEqual(self.react()["status"], "queued")
        self.assertEqual(len(self.claim()), 1)

    def test_nothing_is_handed_out_while_hermes_is_not_ready(self):
        self.react()
        self.assertEqual(self.claim(ready=False), [])
        self.assertEqual(self.row()["status"], "queued")

    def test_the_chat_view_shows_each_wish_and_how_it_went(self):
        self.react(mid=7)
        self.react(HEART, mid=8)
        (a, b) = self.claim()
        self.answer(a)
        got = {r["message_id"]: (r["emoji"], r["status"]) for r in self.own.view(self.p, "one", SID)["reactions"]}
        self.assertEqual(got, {7: (THUMBS, "set"), 8: (HEART, "leased")})

    def test_refusals(self):
        cases = [
            (lambda: self.react("\U0001F4A9"), 400, "bad_emoji"),
            (lambda: self.react("<b>"), 400, "bad_emoji"),
            (lambda: self.react(mid=0), 400, "bad_message"),
            (lambda: self.react(mid=True), 400, "bad_message"),
            (lambda: self.react(mid="7"), 400, "bad_message"),
            (lambda: self.react(sid="not_listed_here"), 404, "not_listed"),
            (lambda: self.react(gw="me", sid=SID), 404, "not_listed"),
            (lambda: self.own.react(self.w.bramble, "one", SID, 7, THUMBS), 403, None),
        ]
        for call, status, reason in cases:
            with self.assertRaises(CoreError) as e:
                call()
            self.assertEqual(e.exception.status, status)
            if reason:
                self.assertEqual(e.exception.reason, reason)
        self.assertIsNone(self.row())

    def test_off_and_hermes_away(self):
        self.g.hello("one", ready=False, why="absent", state="gateway_down")
        with self.assertRaises(CoreError) as e:
            self.react()
        self.assertEqual((e.exception.status, e.exception.reason), (409, "gateway_off"))
        self.w.store.set_flag(self.w.ws, "own_chat_write", False, by="tests")
        with self.assertRaises(CoreError) as e:
            self.react()
        self.assertEqual(e.exception.status, 423)

    def test_a_token_answers_only_for_its_own_gateway(self):
        self.react()
        (r,) = self.claim()
        with self.assertRaises(CoreError) as e:
            self.own.reacted(self.w.ws, "one", self.g.tok["me"], {**r, "outcome": "set"})
        self.assertEqual(e.exception.status, 401)
        with self.assertRaises(CoreError) as e:
            self.own.reacted(self.w.ws, "me", self.g.tok["me"], {**r, "outcome": "set"})
        self.assertEqual(e.exception.reason, "no_item")

    def test_the_event_chain_has_no_message_text(self):
        self.react()
        body = self.w.store.db.execute("SELECT body_json FROM events WHERE kind = 'reaction.wanted'").fetchone()[0]
        self.assertEqual(json.loads(body)["emoji"], THUMBS)


    def test_a_page_says_when_the_core_asked_for_it_on_the_cores_clock(self):
        # The phone shows a set wish until a page asked for after it was set, so both times are the core's.
        self.assertEqual(self.own.page(self.p, "one", "messages", chat=SID), {"pending": True})
        self.w.clock.advance(seconds=2)
        asked = self.w.clock().isoformat()
        (req,) = self.own.claim(self.w.ws, "one", self.g.tok["one"], 0, True)["pages"]
        self.w.clock.advance(seconds=5)
        self.own.page_post(self.w.ws, "one", self.g.tok["one"], {"id": req["id"], "page": {
            "messages": [], "more": False, "read_at": "2030-01-01T00:00:00+00:00"}})
        got = self.own.page(self.p, "one", "messages", chat=SID)
        self.assertEqual((got["handed_at"], got["read_at"]), (asked, "2030-01-01T00:00:00+00:00"))


class Pages(unittest.TestCase):
    def test_a_page_keeps_hermes_reactions_and_nothing_else(self):
        page = clean_page("messages", {"more": False, "messages": [
            {"mid": 1, "role": "assistant", "text": "hi", "at": "", "reactions": [
                {"emoji": THUMBS, "by": "patrick"}, {"emoji": HEART, "by": "hermes"},
                {"emoji": "x", "by": "someone"}, {"emoji": "", "by": "patrick"}, "junk", {"emoji": "y" * 40, "by": "hermes"}]},
            {"mid": 2, "role": "user", "text": "yo", "at": ""}]})
        self.assertEqual(page["messages"][0]["reactions"],
                         [{"emoji": THUMBS, "by": "patrick"}, {"emoji": HEART, "by": "hermes"}, {"emoji": "y" * 16, "by": "hermes"}])
        self.assertEqual(page["messages"][1]["reactions"], [])
        with self.assertRaises(CoreError):
            clean_page("messages", {"more": False, "messages": [{"mid": 1, "role": "user", "text": "", "at": "",
                                                                  "reactions": "lots"}]})


class Routes(unittest.TestCase):
    def setUp(self):
        self.s = Served(self)
        self.w = self.s.w
        self.g = Gateways(self.w)
        on(self.w, "mobile_read")
        self.g.hello("one")
        self.g.feed("one")

    def test_the_phone_reacts_and_only_the_connector_answers(self):
        from test_decisions_api import ORIGIN
        dev = self.w.device()
        sess = self.w.core.auth.issue_session(self.w.ws, method="pairing", device_id=dev)
        h = {"Cookie": f"tw_session={sess.token}", "Origin": ORIGIN, "X-CSRF-Token": sess.csrf}
        status, _, body = self.s.call("POST", "/own/one/react", headers=h,
                                      body={"session_id": SID, "message_id": 3, "emoji": THUMBS})
        self.assertEqual((status, body["status"]), (200, "queued"), body)  # no turn runs, so no fresh passkey
        status, _, body = self.s.call("GET", f"/own/one/{SID}", headers=h)
        self.assertEqual(body["reactions"][0]["emoji"], THUMBS)
        status, _, _ = self.s.call("POST", "/own/one/react", headers=h | {"Origin": "https://evil.example"},
                                   body={"session_id": SID, "message_id": 3, "emoji": HEART})
        self.assertEqual(status, 403)
        tok = self.g.tok["one"]
        status, _, body = self.s.call("POST", "/own/one/claim", headers={"Authorization": f"Bearer {tok}"},
                                      body={"limit": 0, "ready": True})
        (r,) = body["reactions"]
        status, _, body = self.s.call("POST", "/own/one/reacted", headers=h, body={**r, "outcome": "set"})
        self.assertEqual((status, body["error"]), (403, "not_a_feed"))
        status, _, body = self.s.call("POST", "/own/one/reacted", headers={"Authorization": f"Bearer {tok}"},
                                      body={**r, "outcome": "set"})
        self.assertEqual((status, body["status"]), (200, "set"))


# A stand-in for Hermes's store with Hermes's own rule (hermes_state_messages.py set_message_reaction):
# one reaction per author per message, the same emoji again takes it off, a different one replaces it,
# and a row outside the chat is None. It counts its writes.
FAKE_HERMES = {
    "hermes_constants.py": """
        import os
        def get_default_hermes_root():
            return os.environ["TW_FAKE_HOME"]
    """,
    "hermes_state_registry.py": """
        import json, os
        from pathlib import Path
        class DB:
            def __init__(self, path):
                self.path = Path(path or Path(os.environ["TW_FAKE_HOME"]) / "state.db").with_suffix(".json")
                self.data = json.loads(self.path.read_text()) if self.path.exists() else {"rows": {}, "writes": 0}
            def get_message_reactions(self, sid, mid):
                return self.data["rows"].get(f"{sid}:{mid}", [])
            def set_message_reaction(self, sid, mid, emoji, author="user"):
                key = f"{sid}:{mid}"
                if sid != "s_plan" or mid > 100:
                    return None
                existing = self.data["rows"].get(key, [])
                keep = [r for r in existing if r["author"] != author]
                prev = next((r for r in existing if r["author"] == author), None)
                if emoji and (prev is None or prev["emoji"] != emoji):
                    keep.append({"emoji": emoji, "author": author})
                self.data["rows"][key] = keep
                self.data["writes"] += 1
                self.path.write_text(json.dumps(self.data))
                return keep
        def acquire(path=None):
            return DB(path)
        def release_or_close(db):
            pass
    """,
}


class TheHelper(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        for name, text in FAKE_HERMES.items():
            (self.dir / name).write_text(textwrap.dedent(text), encoding="utf-8")
        self.env = {**os.environ, "PYTHONPATH": str(self.dir), "TW_FAKE_HOME": str(self.dir)}

    def ask(self, emoji, mid=7, sid="s_plan", profile=""):
        out = subprocess.run([sys.executable, str(HELPER)], input=json.dumps(
            {"op": "react", "profile": profile, "session_id": sid, "message_id": mid, "emoji": emoji}),
            capture_output=True, text=True, env=self.env, timeout=60)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def store(self, name="state"):
        return json.loads((self.dir / f"{name}.json").read_text())

    def test_it_sets_exactly_what_was_asked_and_asking_twice_never_takes_it_off(self):
        self.assertEqual(self.ask(THUMBS), {"set": True})
        self.assertEqual(self.ask(THUMBS), {"set": True})
        s = self.store()
        self.assertEqual((s["rows"]["s_plan:7"], s["writes"]), ([{"emoji": THUMBS, "author": "user"}], 1))
        self.ask(HEART)
        self.assertEqual(self.store()["rows"]["s_plan:7"], [{"emoji": HEART, "author": "user"}])
        self.ask(None)
        self.ask(None)
        s = self.store()
        self.assertEqual((s["rows"]["s_plan:7"], s["writes"]), ([], 3))

    def test_hermes_own_reaction_is_left_alone(self):
        self.ask(THUMBS)
        s = self.store()
        s["rows"]["s_plan:7"].append({"emoji": HEART, "author": "agent"})
        (self.dir / "state.json").write_text(json.dumps(s))
        self.ask(None)
        self.assertEqual(self.store()["rows"]["s_plan:7"], [{"emoji": HEART, "author": "agent"}])

    def test_a_message_outside_the_chat_and_a_bad_request_set_nothing(self):
        self.assertEqual(self.ask(THUMBS, mid=500), {"set": False, "reason": "that message is not in this chat"})
        for bad in (dict(mid=0), dict(mid="7"), dict(sid=""), dict(profile="../etc")):
            self.assertEqual(self.ask(THUMBS, **bad), {"set": False, "reason": "bad request"})
        self.assertFalse((self.dir / "state.json").exists())

    def test_a_bot_profile_has_its_own_store(self):
        (self.dir / "profiles" / "bramble").mkdir(parents=True)
        self.ask(THUMBS, profile="bramble")
        self.assertTrue((self.dir / "profiles" / "bramble" / "state.json").exists())
        self.assertFalse((self.dir / "state.json").exists())


READY_REACT = READY.replace('req = json.loads(sys.stdin.read())', textwrap.dedent('''\
    req = json.loads(sys.stdin.read())
    if req.get("op") == "react":
        with open(os.environ["TW_FAKE_LOG"], "a", encoding="utf-8") as f:
            f.write(json.dumps(req) + "\\n")
        ok = req["message_id"] != 99
        print(json.dumps({"set": True} if ok else {"set": False, "reason": "that message is not in this chat"}))
        sys.exit(0)'''))


class ReactingCore(FakeCore):
    def post(self, op, body):
        if op == "reacted":
            try:
                return 200, self.own.reacted(self.ws, self.gw, self.token, body)
            except CoreError as exc:
                return exc.status, {"error": exc.reason}
        return super().post(op, body)


class TheConnector(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.g = Gateways(self.w)
        self.g.hello("one")
        self.dir = Path(tempfile.mkdtemp())
        self.db = state_db(self.dir)
        (self.dir / "ready.py").write_text(READY_REACT, encoding="utf-8")
        self.log = self.dir / "helper.jsonl"
        os.environ["TW_FAKE_LOG"] = str(self.log)
        self.addCleanup(lambda: os.environ.pop("TW_FAKE_LOG", None))
        hermes = own_connector.Hermes([sys.executable, "-c", "pass"], [sys.executable, "-I", str(self.dir / "ready.py")],
                                      self.dir, "One's Hermes")
        self.lines = []
        self.conn = own_connector.OwnConnector(ReactingCore(self.g.own, self.w.ws, "one", self.g.tok["one"]), hermes,
                                               self.db, say=self.lines.append)
        self.g.feed("one", chats=[{"id": "s_plan", "title": "Plan the week", "last_at": "", "preview": ""}], opened=[])

    def status(self, mid):
        return self.w.store.db.execute("SELECT status, reason FROM chat_reactions WHERE message_id = ?", (mid,)).fetchone()

    def test_each_wish_goes_to_the_helper_and_comes_back(self):
        p = passkey(self.w)
        self.g.own.react(p, "one", "s_plan", 1, THUMBS)
        self.g.own.react(p, "one", "s_plan", 99, None)
        self.conn.once()
        reqs = [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(r["op"], r["profile"], r["session_id"], r["message_id"], r["emoji"]) for r in reqs],
                         [("react", "", "s_plan", 1, THUMBS), ("react", "", "s_plan", 99, None)])
        self.assertEqual(tuple(self.status(1)), ("set", None))
        self.assertEqual(tuple(self.status(99)), ("failed", "that message is not in this chat"))
        self.assertIn("PASS reaction: set", self.lines)

    def test_a_page_carries_the_reactions_hermes_keeps(self):
        con = sqlite3.connect(self.db)
        con.execute("ALTER TABLE messages ADD COLUMN display_metadata TEXT")
        meta = {"delivered": True, "reactions": [{"emoji": THUMBS, "author": "user", "at": 1, "seen": True},
                                                 {"emoji": HEART, "author": "agent", "at": 2}, {"emoji": "x"}]}
        con.execute("UPDATE messages SET display_metadata = ? WHERE id = 2", (json.dumps(json.dumps(meta)),))
        con.commit()
        con.close()
        page = own_connector.messages_page(self.db, "s_plan", None)
        self.assertEqual([m["reactions"] for m in page["messages"]],
                         [[], [{"emoji": THUMBS, "by": "patrick"}, {"emoji": HEART, "by": "hermes"}]])
        clean_page("messages", page)

    def test_an_older_state_db_without_the_column_reads_as_no_reactions(self):
        page = own_connector.messages_page(self.db, "s_plan", None)
        self.assertEqual([m["reactions"] for m in page["messages"]], [[], []])


if __name__ == "__main__":
    unittest.main()


BOT_HELPER = textwrap.dedent('''\
    import json, os, sys
    req = json.loads(sys.stdin.read())
    with open(os.environ["TW_FAKE_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps(req) + "\\n")
    ok = req.get("op") == "react" and req["message_id"] != 99
    print(json.dumps({"set": True} if ok else {"set": False, "reason": "that message is not in this chat"}))
''')


class BotChats(unittest.TestCase):
    """A reaction in a Bot's Bot Chat: the same rows, on the Bots' gateway, set by the Bots' connector."""

    def setUp(self):
        from test_decisions_chat import flags, seat
        from thornswood_decisions.chat import Chat
        from thornswood_decisions.feeds import Feeds
        self.w = World()
        self.addCleanup(self.w.close)
        seat(self.w)
        self.flags = lambda *n: flags(self.w, *n)
        self.chat = Chat(self.w.store)
        self.tok = Feeds(self.w.store).new_token(self.w.ws, "connector", by="tests")

    def react(self, emoji=THUMBS, mid=7, target="bramble"):
        return self.chat.react(self.w.patrick(), target, mid, emoji)

    def test_it_rides_mobile_chat_write_and_a_room_takes_none(self):
        with self.assertRaises(CoreError) as e:
            self.react()
        self.assertEqual(e.exception.reason, "chat_write_off")
        self.flags("mobile_chat_write")
        self.assertEqual(self.react()["status"], "queued")
        self.assertTrue(self.react()["replayed"])
        for target, why in (("room", "no_room_reactions"), ("nobody", "no_chat")):
            with self.assertRaises(CoreError) as e:
                self.react(target=target)
            self.assertEqual(e.exception.reason, why)
        for mid, emoji, why in ((0, THUMBS, "bad_message"), ("7", THUMBS, "bad_message"), (7, "x", "bad_emoji")):
            with self.assertRaises(CoreError) as e:
                self.react(emoji, mid)
            self.assertEqual(e.exception.reason, why)

    def test_the_connector_sets_it_only_while_connector_effects_is_on(self):
        self.flags("mobile_chat_write")
        self.react(mid=7)
        with self.assertRaises(CoreError) as e:
            self.chat.claim(self.w.ws, self.tok)
        self.assertEqual(e.exception.reason, "connector_effects_off")
        self.flags("connector_effects")
        (r,) = self.chat.claim(self.w.ws, self.tok)["reactions"]
        self.assertEqual((r["bot"], r["message_id"], r["emoji"], r["version"]), ("bramble", 7, THUMBS, 1))
        self.assertEqual(self.chat.claim(self.w.ws, self.tok)["reactions"], [])  # leased
        self.assertEqual(self.chat.reacted(self.w.ws, self.tok, {**r, "outcome": "set"}), {"status": "set"})
        view = self.chat.view(self.w.patrick(), "bramble")
        self.assertEqual([(x["message_id"], x["status"]) for x in view["reactions"]], [(7, "set")])
        self.assertEqual(self.chat.view(self.w.patrick(), "rift")["reactions"], [])
        self.assertEqual(self.chat.view(self.w.patrick(), "room")["reactions"], [])
        with self.assertRaises(CoreError):
            self.chat.reacted(self.w.ws, "not-the-token", {**r, "outcome": "set"})

    def test_a_bot_chat_and_his_own_chat_with_the_same_id_stay_apart(self):
        # The Bots' gateway and his own two never share a row, so neither connector is handed the other's.
        self.flags("mobile_chat_write", "connector_effects")
        self.react(mid=7)
        rows = self.w.store.db.execute("SELECT gateway, chat FROM chat_reactions").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("bots", "bramble")])
        self.assertEqual(len(self.chat.claim(self.w.ws, self.tok)["reactions"]), 1)


class BotsConnector(unittest.TestCase):
    def setUp(self):
        from test_decisions_chat import flags, seat
        from thornswood_decisions import connector
        from thornswood_decisions.chat import Chat
        from thornswood_decisions.feeds import Feeds
        self.w = World()
        self.addCleanup(self.w.close)
        seat(self.w)
        flags(self.w, "mobile_chat_write", "connector_effects")
        self.chat = Chat(self.w.store)
        tok = Feeds(self.w.store).new_token(self.w.ws, "connector", by="tests")
        self.dir = Path(tempfile.mkdtemp())
        home = self.dir / ".hermes"
        (home / "profiles" / "bramble").mkdir(parents=True)
        con = sqlite3.connect(home / "profiles" / "bramble" / "state.db")
        from test_decisions_own import STATE_SQL
        con.executescript(STATE_SQL)
        con.execute("INSERT INTO sessions VALUES('s_bc', 'cli', 'Bot Chat', 1, 900, NULL, NULL, NULL, 0, 1)")
        con.commit()
        con.close()
        (self.dir / "helper.py").write_text(BOT_HELPER, encoding="utf-8")
        self.log = self.dir / "helper.jsonl"
        os.environ["TW_FAKE_LOG"] = str(self.log)
        self.addCleanup(lambda: os.environ.pop("TW_FAKE_LOG", None))
        chat, ws = self.chat, self.w.ws

        class Direct:
            def post(self, path, body):
                try:
                    if path == "/connector/claim":
                        return 200, chat.claim(ws, tok, body["limit"])
                    if path == "/connector/reacted":
                        return 200, chat.reacted(ws, tok, body)
                except CoreError as exc:
                    return exc.status, {"error": exc.reason}
                return 404, {}
        hermes = connector.Hermes([sys.executable, "-c", "pass"], sys.executable, self.dir, helper=self.dir / "helper.py")
        self.lines = []
        self.conn = connector.Connector(Direct(), hermes, say=self.lines.append, hermes_home=home)

    def status(self, bot, mid):
        return tuple(self.w.store.db.execute("SELECT status, reason FROM chat_reactions WHERE chat = ? AND message_id = ?",
                                             (bot, mid)).fetchone())

    def test_each_wish_goes_to_its_bots_bot_chat_and_comes_back(self):
        p = self.w.patrick()
        self.chat.react(p, "bramble", 3, THUMBS)
        self.chat.react(p, "bramble", 99, None)
        self.chat.react(p, "rift", 4, HEART)
        self.assertEqual(self.conn.once(), 0)
        reqs = [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(r["op"], r["profile"], r["session_id"], r["message_id"], r["emoji"]) for r in reqs],
                         [("react", "bramble", "s_bc", 3, THUMBS), ("react", "bramble", "s_bc", 99, None)])
        self.assertEqual(self.status("bramble", 3), ("set", None))
        self.assertEqual(self.status("bramble", 99), ("failed", "that message is not in this chat"))
        self.assertEqual(self.status("rift", 4), ("failed", "that Bot has no Bot Chat"))
        self.assertIn("PASS reaction for bramble: set", self.lines)
        self.assertEqual(self.conn.unsaid, [])


class BotRoutes(unittest.TestCase):
    def test_the_phone_reacts_in_a_bot_chat_and_only_the_connector_answers(self):
        from test_decisions_api import ORIGIN
        from test_decisions_chat import flags, seat
        from thornswood_decisions.feeds import Feeds
        s = Served(self)
        seat(s.w)
        flags(s.w, "mobile_read", "mobile_chat_write", "connector_effects")
        dev = s.w.device()
        sess = s.w.core.auth.issue_session(s.w.ws, method="pairing", device_id=dev)
        h = {"Cookie": f"tw_session={sess.token}", "Origin": ORIGIN, "X-CSRF-Token": sess.csrf}
        status, _, body = s.call("POST", "/chat/bramble/react", headers=h, body={"message_id": 3, "emoji": THUMBS})
        self.assertEqual((status, body["status"], body["target"]), (200, "queued", "bramble"), body)
        status, _, body = s.call("POST", "/chat/room/react", headers=h, body={"message_id": 3, "emoji": THUMBS})
        self.assertEqual((status, body["error"]), (400, "no_room_reactions"))
        tok = Feeds(s.w.store).new_token(s.w.ws, "connector", by="tests")
        status, _, body = s.call("POST", "/connector/claim", headers={"Authorization": f"Bearer {tok}"}, body={"limit": 3})
        (r,) = body["reactions"]
        status, _, body = s.call("POST", "/connector/reacted", headers=h, body={**r, "outcome": "set"})
        self.assertEqual((status, body["error"]), (403, "not_a_feed"))
        status, _, body = s.call("POST", "/connector/reacted", headers={"Authorization": f"Bearer {tok}"},
                                 body={**r, "outcome": "set"})
        self.assertEqual((status, body["status"]), (200, "set"))
        status, _, body = s.call("GET", "/chat/bramble", headers=h)
        self.assertEqual(status, 200, body)
        self.assertEqual([(x["message_id"], x["status"]) for x in body["reactions"]], [(3, "set")])
