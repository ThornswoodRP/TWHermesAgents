"""A Bot sends a step of the agreed plan to One's Hermes itself, and One's answer comes back to it (#86).

Patrick, 9 Nov, on q-86: "why not just have it message my One bot?", then "yeah so it doesnt have to wait on
me". No Bot can reach One, so a step only his PC can run waited until he pasted it into the One chat.

Checks:
- tw_decide ask_one queues the Bot's words for One's Hermes, signed the way Hermes signs a Bot's message,
  with the core's note on what One refuses; only a Bot, only while own_chat_write is on, at most three
  waiting per Bot, and the same source_key twice is one message;
- One's connector is handed it for its Bot Chat, one Bot's step at a time, beside Patrick's own chats;
- the turn runs in One's Bot Chat by its name, made when missing, never with --yolo, and its whole reply
  goes back with the result;
- the result tells the Bot in its own Bot Chat what One answered (or that it did not go, or may have run),
  one_answer reads it back, and Patrick gets no "answered" alert for a turn he did not start;
- Patrick's Bot Chat on One shows the Bot's step as that Bot's, in its own words, and it is never a new chat
  of his.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from decisions_helpers import World
from test_decisions_chat import flags, seat
from test_decisions_own import SID, Gateways, passkey

from thornswood_decisions import own_connector
from thornswood_decisions.bots import BotRoute
from thornswood_decisions.chat import Chat
from thornswood_decisions.feeds import Feeds
from thornswood_decisions.own import BOT_NOTE, BOT_WAITING, OwnChat
from thornswood_decisions.store import CoreError
from thornswood_guard.brokers import DECIDE_OPS, TW_DECIDE_SCHEMA

BOT_CHAT_SID = "20261109_090000_b0c0aa"


@unittest.skipUnless(sys.platform.startswith("linux"), "the route's ops are served by the brokers' wire (Linux)")
class TheBotsMessage(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        seat(self.w)
        flags(self.w, "mobile_read", "mobile_chat_write", "connector_effects")
        self.g = Gateways(self.w)
        self.g.hello("one")
        self.route = BotRoute(self.w.path, "test")
        self.addCleanup(self.route.close)
        self.ops = self.route.ops()

    def call(self, op, bot, **args):
        handler, callers = self.ops[op]
        self.assertEqual(callers, {"hermes", "root"})
        return json.loads(json.dumps(handler("hermes", dict(args, bot=bot))))

    def claim_one(self, limit=3):
        return self.g.own.claim(self.w.ws, "one", self.g.tok["one"], limit, True)["items"]

    def row(self, mid):
        return self.w.store.db.execute("SELECT * FROM own_messages WHERE id = ?", (mid,)).fetchone()

    def test_it_is_queued_for_one_signed_as_hermes_signs_a_bots_message(self):
        self.assertIn("ask_one", DECIDE_OPS)
        self.assertIn("one_answer", DECIDE_OPS)
        self.assertIn("text", TW_DECIDE_SCHEMA["parameters"]["properties"])
        got = self.call("ask_one", "wisp", text="Run Build-Gamemode.js in the clone and say what it printed.")
        self.assertEqual((got["gateway"], got["status"]), ("one", "queued"))
        self.assertIn("one_online", got, "the Bot learns whether One is answering now")
        row = self.row(got["message_id"])
        self.assertEqual(row["text"], "Message from \U0001F916 Wisp (@wisp): Run Build-Gamemode.js in the clone and "
                                      "say what it printed." + BOT_NOTE)
        self.assertIsNone(row["session_id"])
        self.assertIsNone(row["device_id"])
        self.assertIn("secret, money, or anything destructive", BOT_NOTE)

    def test_who_may_send_it_and_how_many_wait(self):
        with self.assertRaises(CoreError) as e:
            OwnChat(self.w.store).ask_one(passkey(self.w), "hi")
        self.assertEqual(e.exception.reason, "not_a_bot")
        with self.assertRaisesRegex(Exception, "missing"):
            self.call("ask_one", "wisp", text="")
        with self.assertRaisesRegex(Exception, "bad_text"):
            self.call("ask_one", "wisp", text="x" * 8000)
        first = self.call("ask_one", "rift", text="step one", source_key="rift-step-0001")
        self.assertEqual(self.call("ask_one", "rift", text="step one", source_key="rift-step-0001")["message_id"],
                         first["message_id"], "the same source_key twice is one message")
        with self.assertRaisesRegex(Exception, "key_reused"):
            self.call("ask_one", "rift", text="another step", source_key="rift-step-0001")
        for i in range(BOT_WAITING - 1):
            self.call("ask_one", "rift", text=f"step {i + 2}")
        with self.assertRaisesRegex(Exception, "one_busy"):
            self.call("ask_one", "rift", text="one too many")
        self.call("ask_one", "echo", text="another Bot still may")
        self.w.store.set_flag(self.w.ws, "own_chat_write", False, by="tests")
        with self.assertRaisesRegex(Exception, "own_chat_off"):
            self.call("ask_one", "magpie", text="while it is off")

    def test_one_is_handed_one_bots_step_at_a_time_beside_his_own_chats(self):
        a = self.call("ask_one", "wisp", text="first step")["message_id"]
        b = self.call("ask_one", "echo", text="second step")["message_id"]
        mine = self.g.own.send(passkey(self.w), "one", SID, None, "hello", "key-askone-01")["message_id"]
        items = self.claim_one()
        by_id = {i["id"]: i for i in items}
        self.assertEqual(set(by_id), {a, mine})
        self.assertEqual(by_id[a]["bot_chat"], True)
        self.assertNotIn("bot_chat", by_id[mine])
        self.assertEqual(self.claim_one(), [], "the Bot Chat runs one Bot's step at a time")
        self.g.own.result(self.w.ws, "one", self.g.tok["one"], a, "confirmed", "turn finished", "", BOT_CHAT_SID,
                          "Built it: 0 errors.")
        self.assertEqual([i["id"] for i in self.claim_one()], [b])

    def test_the_answer_goes_back_to_the_bot_and_patrick_gets_no_alert(self):
        mid = self.call("ask_one", "wisp", text="Run the gamemode build.")["message_id"]
        self.claim_one()
        self.assertEqual(self.call("one_answer", "wisp", message_id=mid)["status"], "leased")
        with self.assertRaisesRegex(Exception, "no_item"):
            self.call("one_answer", "echo", message_id=mid)
        self.g.own.result(self.w.ws, "one", self.g.tok["one"], mid, "confirmed", "turn finished", "", BOT_CHAT_SID,
                          "Built it: 0 errors, 2 warnings.")
        got = self.call("one_answer", "wisp", message_id=mid)
        self.assertEqual((got["status"], got["answer"]), ("sent", "Built it: 0 errors, 2 warnings."))
        tok = Feeds(self.w.store).new_token(self.w.ws, "connector", by="tests")
        [item] = Chat(self.w.store).claim(self.w.ws, tok, 7)["items"]
        self.assertEqual((item["kind"], item["target"]), ("one.answered", "wisp"))
        self.assertIn(f"One's Hermes answered your message {mid}:\n\nBuilt it: 0 errors, 2 warnings.", item["text"])
        self.assertIn("tw_decide open_card", item["text"])
        self.assertEqual(self.w.store.db.execute("SELECT count(*) FROM chat_alerts").fetchone()[0], 0,
                         "a Bot's turn is not one he started")

    def test_a_step_that_did_not_go_or_may_have_run_says_so(self):
        dead = self.call("ask_one", "wisp", text="step a")["message_id"]
        self.claim_one()
        self.g.own.result(self.w.ws, "one", self.g.tok["one"], dead, "failed", "", "could not start the hermes command")
        maybe = self.call("ask_one", "wisp", text="step b")["message_id"]
        self.claim_one()
        self.g.own.result(self.w.ws, "one", self.g.tok["one"], maybe, "uncertain", "", "no end to the turn", None,
                          "Started the build")
        tok = Feeds(self.w.store).new_token(self.w.ws, "connector", by="tests")
        chat = Chat(self.w.store)
        [first] = chat.claim(self.w.ws, tok, 7)["items"]
        self.assertIn(f"Your message {dead} to One's Hermes did not go: could not start the hermes command", first["text"])
        chat.result(self.w.ws, tok, first["id"], "confirmed", "turn finished")
        [second] = chat.claim(self.w.ws, tok, 7)["items"]
        self.assertIn("may or may not have run", second["text"])
        self.assertIn("Started the build", second["text"])

    def test_his_bot_chat_shows_the_bots_step_as_the_bots_and_it_is_no_new_chat_of_his(self):
        self.g.feed("one", chats=[{"id": BOT_CHAT_SID, "title": "Bot Chat", "bot_chat": True,
                                   "last_at": self.w.clock().isoformat(), "preview": ""}], opened=[])
        mid = self.call("ask_one", "wisp", text="Start the launcher and read its log.")["message_id"]
        view = self.g.own.view(self.w.patrick(), "one", BOT_CHAT_SID)
        [m] = view["mine"]
        self.assertEqual((m["id"], m["from_bot"], m["text"]), (mid, "wisp", "Start the launcher and read its log."))
        self.assertEqual(self.g.own.chats(self.w.patrick(), "one")["starting"], [])
        other = self.g.own.view(self.w.patrick(), "one", SID)
        self.assertEqual(other["mine"], [], "only in his Bot Chat")


class OnesConnector(unittest.TestCase):
    def test_the_turn_runs_in_the_bot_chat_by_name_never_yolo(self):
        h = own_connector.Hermes([sys.executable, "-c", "pass"], [sys.executable, "-c", "pass"],
                                 Path(tempfile.mkdtemp()), "One's Hermes")
        seen = []
        h._run = lambda argv, *a, **k: seen.append(argv) or ("confirmed", "turn finished", "", "s1")
        h.turn(None, "Message from \U0001F916 Wisp (@wisp): hi", bot_chat=True)
        argv = seen[-1]
        i = argv.index("Bot Chat")
        self.assertEqual(argv[i - 1:i + 2], ["-c", "Bot Chat", "--create-if-missing"])
        self.assertNotIn("--yolo", argv)
        self.assertNotIn("--resume", argv)
        h.turn(None, "his own new chat")
        self.assertIn("--yolo", seen[-1], "his own new chats are as before")

    def test_the_whole_reply_goes_back_with_the_result(self):
        class Core:
            def post(self, op, body):
                return 200, {}

        class Hermes:
            kw = None

            def turn(self, sid, text, doing, image, started, live, **kw):
                Hermes.kw = kw
                live.hear({"type": "text", "text": "Built"})
                live.hear({"type": "result", "session_id": BOT_CHAT_SID, "exit_code": 0, "text": "Built it: 0 errors."})
                return "confirmed", "turn finished", "", BOT_CHAT_SID
        conn = own_connector.OwnConnector(Core(), Hermes(), Path(tempfile.mkdtemp()) / "state.db", say=lambda _: None)
        for mid, bot in (("own_b", True), ("own_p", False)):
            conn.lives[mid] = own_connector.LiveTurn()
            t = threading.Thread(target=conn._deliver_live,
                                 args=({"id": mid, "session_id": None, "text": "x", **({"bot_chat": True} if bot else {})},))
            t.start()
            t.join(5)
        self.assertEqual(Hermes.kw, {}, "his own message runs as before")
        got = {r["id"]: r for r in conn.unsaid}
        self.assertEqual(got["own_b"]["reply"], "Built it: 0 errors.")
        self.assertNotIn("reply", got["own_p"], "only a Bot's message carries its answer back")


if __name__ == "__main__":
    unittest.main()
