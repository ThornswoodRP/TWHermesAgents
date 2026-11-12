"""A message to Patrick's own Hermes left uncertain is settled from how Hermes itself ended its turn.

11 Nov 2026, 12:40 PM Mountain: Wisp's step for One's Hermes met Anthropic's 429 for Patrick's account, and
Hermes backed off for ten minutes at a time. At 12:55 PM the connector stopped watching the turn after its
15 minutes and told the core "uncertain"; at 12:59 PM One slept. When One woke at 5:33 PM Hermes ended the turn
and closed it with "Your request was not processed. Send it again if you still want me to carry it out." The
core still counted the message as waiting, Wisp had been told not to send it again by itself, and nothing ever
ran it. Hermes had said what happened; nobody read it.

Checks:
- the core hands a connector the messages it holds as uncertain, with their chat and words, while it is ready;
- a settled result moves only an uncertain message, to sent or dead, never back to uncertain, and the Bot that
  sent it hears the outcome in its Bot Chat (and one_answer reads the newest word);
- hermes_verdict reads Hermes's admission for that message's words in that chat: completed is confirmed with
  its answer, failed or interrupted before any tool ran is failed, a tool that ran keeps it uncertain, and a
  turn not ended (or not found) is not known yet;
- the connector settles each message once, reads Hermes again at most once a minute while the turn runs, and
  says nothing for one whose turn left it uncertain.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from decisions_helpers import World
from test_decisions_chat import flags, seat
from test_decisions_own import Gateways

from thornswood_decisions import own_connector
from thornswood_decisions.bots import BotRoute
from thornswood_decisions.chat import Chat
from thornswood_decisions.feeds import Feeds
from thornswood_decisions.store import CoreError

BOT_CHAT_SID = "20261111_194008_b0c0aa"
NOT_PROCESSED = "Your request was not processed. Send it again if you still want me to carry it out."


@unittest.skipUnless(sys.platform.startswith("linux"), "the route's ops are served by the brokers' wire (Linux)")
class TheCore(unittest.TestCase):
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
        handler, _ = self.ops[op]
        return json.loads(json.dumps(handler("hermes", dict(args, bot=bot))))

    def claim(self, limit=3):
        return self.g.own.claim(self.w.ws, "one", self.g.tok["one"], limit, True)

    def result(self, mid, outcome, reason="", sid=None, reply=None, settled=False):
        return self.g.own.result(self.w.ws, "one", self.g.tok["one"], mid, outcome, "", reason, sid, reply, settled)

    def left_uncertain(self, text):
        mid = self.call("ask_one", "wisp", text=text)["message_id"]
        self.claim()
        self.result(mid, "uncertain", "no end to the turn in 15 minutes; Hermes may have the message", BOT_CHAT_SID)
        return mid

    def bot_heard(self):
        tok = Feeds(self.w.store).new_token(self.w.ws, "connector", by="tests")
        chat, said = Chat(self.w.store), []
        while True:
            items = chat.claim(self.w.ws, tok, 7)["items"]
            if not items:
                return said
            for item in items:
                said.append(item["text"])
                chat.result(self.w.ws, tok, item["id"], "confirmed", "turn finished")

    def test_the_uncertain_ones_are_handed_back_with_their_chat_and_words(self):
        mid = self.left_uncertain("Read the Papyrus callers.")
        [u] = self.claim()["uncertain"]
        self.assertEqual((u["id"], u["session_id"]), (mid, BOT_CHAT_SID))
        self.assertTrue(u["text"].startswith("Message from \U0001F916 Wisp (@wisp): Read the Papyrus callers."))
        self.assertEqual(self.g.own.claim(self.w.ws, "one", self.g.tok["one"], 0, False)["uncertain"], [],
                         "only while that Hermes is ready")

    def test_hermes_saying_nothing_ran_makes_it_dead_and_tells_the_bot(self):
        mid = self.left_uncertain("Read the Papyrus callers.")
        self.bot_heard()
        got = self.result(mid, "failed", "Hermes ended the turn before anything ran (Operation interrupted)",
                          settled=True)
        self.assertEqual(got["status"], "dead")
        self.assertEqual(self.claim()["uncertain"], [])
        [said] = self.bot_heard()
        self.assertIn(f"Your message {mid} to One's Hermes did not go: Hermes ended the turn before anything ran", said)
        self.assertIn("Nothing ran on One.", said)
        self.assertEqual(self.call("one_answer", "wisp", message_id=mid)["status"], "dead")
        unsettled = self.w.store.db.execute("SELECT count(*) FROM own_messages WHERE gateway = 'one' "
                                            "AND status IN ('queued', 'leased', 'uncertain')").fetchone()[0]
        self.assertEqual(unsettled, 0, "no longer counted as waiting")

    def test_hermes_saying_it_finished_sends_its_answer_back(self):
        mid = self.left_uncertain("Build the gamemode.")
        self.result(mid, "confirmed", "Hermes finished the turn", reply="Built it: 0 errors.", settled=True)
        got = self.call("one_answer", "wisp", message_id=mid)
        self.assertEqual((got["status"], got["answer"]), ("sent", "Built it: 0 errors."))

    def test_only_an_uncertain_message_is_settled_and_only_to_an_end(self):
        mid = self.call("ask_one", "wisp", text="step")["message_id"]
        self.claim()
        with self.assertRaises(CoreError) as e:
            self.result(mid, "failed", "x", settled=True)
        self.assertEqual(e.exception.reason, "not_uncertain")
        self.result(mid, "uncertain", "no end to the turn", BOT_CHAT_SID)
        for outcome, settled in (("uncertain", True), ("failed", "yes")):
            with self.assertRaises(CoreError) as e:
                self.result(mid, outcome, "x", settled=settled)
            self.assertEqual(e.exception.reason, "bad_outcome")
        self.assertEqual(self.result(mid, "failed", "x", settled=True)["status"], "dead")
        self.assertTrue(self.result(mid, "failed", "x", settled=True)["replayed"])


def state_db(rows: list[tuple]) -> Path:
    """A Hermes state.db with only what hermes_verdict reads: (admission id, chat, text, status, outcome,
    stored result or None)."""
    path = Path(tempfile.mkdtemp()) / "state.db"
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE session_admissions (seq INTEGER PRIMARY KEY AUTOINCREMENT, admission_id TEXT, "
                      "target_session_id TEXT, payload_json TEXT, status TEXT, outcome TEXT);"
                      "CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT);")
    for aid, sid, text, status, outcome, result in rows:
        con.execute("INSERT INTO session_admissions(admission_id, target_session_id, payload_json, status, outcome) "
                    "VALUES(?,?,?,?,?)", (aid, sid, json.dumps({"text": text}), status, outcome))
        if result is not None:
            con.execute("INSERT INTO state_meta VALUES(?,?)", (own_connector.RESULT_PREFIX + aid,
                                                               json.dumps({"result": result, "usage": {}})))
    con.commit()
    con.close()
    return path


INTERRUPTED = {"final_response": "Operation interrupted: waiting for model response (17623.1s elapsed).",
               "interrupted": True, "completed": False, "messages": [{"role": "assistant", "content": NOT_PROCESSED}]}


class HermesVerdict(unittest.TestCase):
    def test_how_hermes_ended_the_turn(self):
        tool_ran = {"final_response": "", "failed": True, "error": "Request timed out.",
                    "messages": [{"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
                                 {"role": "tool", "content": "done"}]}
        db = state_db([
            ("a1", BOT_CHAT_SID, "step one", "terminal", "interrupted", INTERRUPTED),
            ("a2", BOT_CHAT_SID, "step two", "terminal", "completed",
             {"final_response": "Built it.", "messages": [{"role": "assistant", "content": "Built it."}]}),
            ("a3", BOT_CHAT_SID, "step three", "terminal", "failed", tool_ran),
            ("a4", BOT_CHAT_SID, "step four", "started", None, None),
            ("a5", "other_chat", "step five", "terminal", "completed", {"final_response": "x", "messages": []}),
        ])
        outcome, reason, reply = own_connector.hermes_verdict(db, BOT_CHAT_SID, "step one\n")
        self.assertEqual((outcome, reply), ("failed", ""))
        self.assertIn("before anything ran (Operation interrupted", reason)
        self.assertEqual(own_connector.hermes_verdict(db, BOT_CHAT_SID, "step two"),
                         ("confirmed", "Hermes finished the turn", "Built it."))
        outcome, reason, _ = own_connector.hermes_verdict(db, BOT_CHAT_SID, "step three")
        self.assertEqual(outcome, "uncertain")
        self.assertIn("after a tool ran (Request timed out.)", reason)
        self.assertIsNone(own_connector.hermes_verdict(db, BOT_CHAT_SID, "step four"), "still running")
        self.assertIsNone(own_connector.hermes_verdict(db, BOT_CHAT_SID, "step five"), "only in its own chat")
        self.assertIsNone(own_connector.hermes_verdict(db, BOT_CHAT_SID, "never sent"))
        self.assertIsNone(own_connector.hermes_verdict(db, "-bad", "step one"))
        self.assertIsNone(own_connector.hermes_verdict(Path(tempfile.mkdtemp()) / "none.db", BOT_CHAT_SID, "step one"))

    def test_the_newest_admission_with_those_words_is_the_one(self):
        db = state_db([
            ("a1", BOT_CHAT_SID, "yes", "terminal", "completed", {"final_response": "old", "messages": []}),
            ("a2", BOT_CHAT_SID, "yes", "terminal", "interrupted", INTERRUPTED),
        ])
        self.assertEqual(own_connector.hermes_verdict(db, BOT_CHAT_SID, "yes")[0], "failed")


class TheConnector(unittest.TestCase):
    def test_each_is_settled_once_and_read_again_at_most_once_a_minute(self):
        db = state_db([("a1", BOT_CHAT_SID, "step one", "terminal", "interrupted", INTERRUPTED),
                       ("a2", BOT_CHAT_SID, "step two", "started", None, None)])

        class Core:
            posted = []

            def post(self, op, body):
                Core.posted.append((op, body))
                return 200, {"status": "dead"}
        now = [1000.0]
        said = []
        conn = own_connector.OwnConnector(Core(), None, db, say=said.append, clock=lambda: now[0])
        handed = [{"id": "own_1", "session_id": BOT_CHAT_SID, "text": "step one"},
                  {"id": "own_2", "session_id": BOT_CHAT_SID, "text": "step two"}]
        self.assertEqual(conn.settle(handed), 1)
        [(op, body)] = Core.posted
        self.assertEqual((op, body["id"], body["outcome"], body["settled"]), ("result", "own_1", "failed", True))
        self.assertNotIn("reply", body)
        self.assertEqual(conn.settle(handed), 0, "settled once")
        self.assertEqual(len(Core.posted), 1)
        con = sqlite3.connect(db)
        con.execute("UPDATE session_admissions SET status = 'terminal', outcome = 'completed' WHERE admission_id = 'a2'")
        con.execute("INSERT INTO state_meta VALUES(?,?)", (own_connector.RESULT_PREFIX + "a2", json.dumps(
            {"result": {"final_response": "Done.", "messages": []}})))
        con.commit()
        con.close()
        now[0] += 30
        conn.settle(handed)
        self.assertEqual(len(Core.posted), 1, "not read again within the minute")
        now[0] += 31
        self.assertEqual(conn.settle(handed), 1)
        self.assertEqual((Core.posted[-1][1]["id"], Core.posted[-1][1]["outcome"], Core.posted[-1][1]["reply"]),
                         ("own_2", "confirmed", "Done."))
        self.assertTrue(all("settled" in s for s in said))

    def test_a_turn_where_a_tool_ran_stays_uncertain_and_is_not_posted(self):
        db = state_db([("a1", BOT_CHAT_SID, "step", "terminal", "failed",
                        {"failed": True, "error": "x", "messages": [{"role": "tool", "content": "ran"}]})])

        class Core:
            def post(self, op, body):
                raise AssertionError("nothing to post")
        said = []
        conn = own_connector.OwnConnector(Core(), None, db, say=said.append)
        self.assertEqual(conn.settle([{"id": "own_1", "session_id": BOT_CHAT_SID, "text": "step"}]), 0)
        self.assertEqual(len(said), 1)
        self.assertIn("stays uncertain", said[0])


if __name__ == "__main__":
    unittest.main()
