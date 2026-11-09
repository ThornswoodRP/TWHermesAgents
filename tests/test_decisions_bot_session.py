"""A Board row opens a Bot's session as a chat like any other (Patrick, 9 Nov, TWHermesAgents #77).

"it should bring up that session like it would if I had went their myself with the normal view with the
option to message and all the voice features, it shouldn't be like a separate read only version". Checks
the core's side (a message into one session of a Bot is kept apart from its Bot Chat and handed to the
connector with the session) and the connector's (it goes through room_send.py `chat --resume` in that
Bot's own home, only into a session of that Bot's, and a message Hermes steered into the running turn is
done once Hermes has it). Everything runs in a scratch folder against stand-ins; nothing reaches a Hermes.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from decisions_helpers import World
from test_decisions_chat import ROOM, flags, seat

from thornswood_decisions import connector
from thornswood_decisions.chat import Chat
from thornswood_decisions.feeds import Feeds
from thornswood_decisions.store import CoreError


class TheCore(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        seat(self.w)
        flags(self.w, "mobile_chat_write", "connector_effects")
        self.chat = Chat(self.w.store)
        self.tok = Feeds(self.w.store).new_token(self.w.ws, "connector", by="tests")

    def test_a_message_into_a_session_is_kept_apart_from_the_bot_chat(self):
        p = self.w.patrick()
        into = self.chat.send(p, "rift", "Use the cached sort.", "key-session-1", session="s_rift_lo")
        self.assertEqual(into["session"], "s_rift_lo")
        self.chat.send(p, "rift", "And the Bot Chat.", "key-botchat-1")
        session = self.chat.view(p, "rift", session="s_rift_lo")
        self.assertEqual([m["text"] for m in session["mine"]], ["Use the cached sort."])
        self.assertEqual(session["session"], "s_rift_lo")
        # The feed carries the Bot Chat only, so a session shows no copy of it, and no reactions.
        self.assertEqual((session["history"], session["reactions"]), ([], []))
        self.assertEqual([m["text"] for m in self.chat.view(p, "rift")["mine"]], ["And the Bot Chat."])

    def test_the_connector_is_handed_the_session(self):
        self.chat.send(self.w.patrick(), "rift", "Use the cached sort.", "key-session-2", session="s_rift_lo")
        [item] = self.chat.claim(self.w.ws, self.tok, 3)["items"]
        self.assertEqual((item["target"], item["session"], item["text"]), ("rift", "s_rift_lo", "Use the cached sort."))
        # One message in flight for each Bot, whichever of its chats it goes to.
        self.chat.send(self.w.patrick(), "rift", "And the Bot Chat.", "key-botchat-2")
        self.assertEqual(self.chat.claim(self.w.ws, self.tok, 3)["items"], [])

    def test_a_bot_chat_message_carries_no_session(self):
        self.chat.send(self.w.patrick(), "rift", "Hello.", "key-botchat-3")
        [item] = self.chat.claim(self.w.ws, self.tok, 3)["items"]
        self.assertNotIn("session", item)

    def test_only_a_bots_session_id_is_taken(self):
        p = self.w.patrick()
        for target, sid in (("rift", "../x"), ("rift", ""), ("rift", "a b"), ("rift", 7), (ROOM, "s_rift_lo")):
            with self.assertRaises(CoreError) as e:
                self.chat.send(p, target, "hi", "key-bad-session", session=sid)
            self.assertEqual(e.exception.reason, "bad_session")
            with self.assertRaises(CoreError):
                self.chat.view(p, target, session=sid)

    def test_the_same_key_into_another_chat_is_a_different_request(self):
        p = self.w.patrick()
        self.chat.send(p, "rift", "hi", "key-same-1", session="s_rift_lo")
        self.assertTrue(self.chat.send(p, "rift", "hi", "key-same-1", session="s_rift_lo")["replayed"])
        with self.assertRaises(CoreError) as e:
            self.chat.send(p, "rift", "hi", "key-same-1")
        self.assertEqual(e.exception.reason, "key_reused")


class TheConnector(unittest.TestCase):
    """to_session against a stand-in helper: `ready` on stdin, or a `chat` turn whose stream it scripts."""

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

    def hermes(self, stream: list[dict], code: int = 0, linger: float = 0) -> connector.Hermes:
        script = self.dir / "helper.py"
        script.write_text(textwrap.dedent(f"""
            import json, os, sys, time
            if "chat" not in sys.argv:
                sys.stdin.read()
                print(json.dumps({{"ready": True}}))
                sys.exit(0)
            q = sys.argv[sys.argv.index("--query-file") + 1]
            open({str(self.seen)!r}, "w").write(json.dumps({{"argv": sys.argv[1:], "home": os.environ.get("HERMES_HOME"),
                                                             "text": open(q).read()}}))
            for line in {stream!r}:
                print(json.dumps(line), flush=True)
            time.sleep({linger!r})
            sys.exit({code!r})
        """), encoding="utf-8")
        return connector.Hermes(["hermes"], [sys.executable, "-I", str(script)], self.dir)

    def test_it_goes_into_that_session_in_the_bots_own_home(self):
        h = self.hermes([{"type": "system"}, {"type": "admission", "status": "started"}, {"type": "result"}])
        self.assertEqual(h.to_session("rift", "s_rift_lo", "Use the cached sort."), ("confirmed", "turn finished", ""))
        seen = json.loads(self.seen.read_text())
        self.assertEqual(seen["argv"][:6], ["chat", "-p", "rift", "-Q", "--format", "stream-json"])
        self.assertEqual(seen["argv"][-2:], ["--resume", "s_rift_lo"])
        self.assertEqual(seen["home"], str(self.dir / ".hermes" / "profiles" / "rift"))
        self.assertEqual(seen["text"], "Use the cached sort.")

    def test_a_steer_is_done_once_hermes_has_it(self):
        # The Bot's turn runs on for as long as it takes; the message is in it once Hermes says so.
        h = self.hermes([{"type": "system"}, {"type": "admission", "status": "steered"}], linger=60)
        began = time.monotonic()
        self.assertEqual(h.to_session("rift", "s_rift_lo", "Use the cached sort."),
                         ("confirmed", "steered into the running turn", ""))
        self.assertLess(time.monotonic() - began, 20)

    def test_a_session_that_is_not_the_bots_sends_nothing(self):
        h = self.hermes([])
        self.assertEqual(h.to_session("rift", "s_nobody", "hi"), ("failed", "", "that session is not one of rift's"))
        self.assertEqual(h.to_session("rift", "../x", "hi"), ("failed", "", "that is not a session id Hermes gives"))
        self.assertFalse(self.seen.exists())

    def test_a_turn_that_stops_badly_may_have_it(self):
        h = self.hermes([{"type": "system"}], code=1)
        self.assertEqual(h.to_session("rift", "s_rift_lo", "hi"),
                         ("uncertain", "", "hermes stopped with exit code 1; it may have the message"))

    def test_the_connector_sends_a_session_item_into_the_session(self):
        class Stand:
            def __init__(self):
                self.calls = []

            def to_session(self, bot, sid, text, doing=None):
                self.calls.append(("session", bot, sid, text))
                return "confirmed", "turn finished", ""

            def to_bot(self, bot, text, doing=None):
                self.calls.append(("bot", bot, text))
                return "confirmed", "turn finished", ""
        stand = Stand()
        c = connector.Connector(connector.Core("http://127.0.0.1:9/api/v1", "t"), stand, say=lambda line: None)
        c.deliver({"id": "msg_1", "target": "rift", "text": "a", "session": "s_rift_lo"})
        c.deliver({"id": "msg_2", "target": "rift", "text": "b"})
        self.assertEqual(stand.calls, [("session", "rift", "s_rift_lo", "a"), ("bot", "rift", "b")])


if __name__ == "__main__":
    unittest.main()
