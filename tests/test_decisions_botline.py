"""The live line in a Bot Chat (Patrick, 2 Nov: "no typing or thinking indicators and no like what they are
currently doing"; "Those would be the same lines"): the same line his own chats have (test_decisions_doing.py),
for a message one of the six Bots is answering.

Checks:
- the Bots' connector runs a Bot Chat turn with Hermes's stream-json output and reads it with the same reader
  One's connector uses (own_connector.follow_turn), so both say the same states for the same events;
- while the round's turns run it says what each is doing to the core (`/connector/doing`) when it changes, and
  the message's line shows it; when the turn ends the message is sent and the line is gone;
- the core keeps only well-formed turns, shows one only on a message being answered, drops it when the
  connector has not said it again in a minute or when the result comes, takes only the connector's token,
  and only while mobile_read is on;
- over HTTP: the route takes the connector's post and refuses a browser.
Everything runs in a scratch test workspace, with stand-ins for `hermes` and the helper; nothing reaches a real
Hermes or a Bot.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

from decisions_helpers import World
from test_decisions_api import ORIGIN, Served
from test_decisions_chat import FAKE_HELPER, flags, seat

from thornswood_decisions import connector, own_connector
from thornswood_decisions.chat import Chat
from thornswood_decisions.feeds import Feeds
from thornswood_decisions.store import CoreError

# A Bot's turn as Hermes streams it: started, a tool, then it waits for the test's word before it writes.
FAKE_STREAM = textwrap.dedent('''
    import json, os, sys, time
    from pathlib import Path
    args = sys.argv[1:]
    Path(os.environ["TW_FAKE_LOG"]).write_text(json.dumps(args), encoding="utf-8")
    def say(e):
        print(json.dumps(e), flush=True)
    say({"type": "system", "subtype": "init", "session_id": "s_bc"})
    say({"type": "tool_use", "name": "terminal", "input": {"command": "cat secret.txt"}})
    go = Path(os.environ["TW_FAKE_GO"])
    for _ in range(200):
        if go.exists():
            break
        time.sleep(0.05)
    say({"type": "tool_result", "name": "terminal", "output": "the secret output"})
    say({"type": "text", "text": "All green."})
    say({"type": "result", "session_id": "s_bc", "exit_code": 0, "text": "All green."})
''')


class TheSameReader(unittest.TestCase):
    def test_both_connectors_read_a_turn_with_one_reader(self):
        self.assertIs(connector.follow_turn, own_connector.follow_turn)

    def test_the_reader_says_states_and_keeps_no_words(self):
        lines = [json.dumps(e) + "\n" for e in (
            {"type": "system", "session_id": "s", "text": "hello"}, {"type": "tool_use", "name": "web_search"},
            {"type": "tool_use", "name": "web_search"}, {"type": "tool_result", "output": "x"},
            {"type": "text", "text": "the words"}, {"type": "result", "exit_code": 0, "text": "the words"})]
        seen, heard = {}, []
        own_connector.follow_turn(iter(["not json\n", "[1]\n"] + lines), seen, lambda s, t: heard.append((s, t)))
        self.assertEqual(heard, [("thinking", None), ("tool", "web_search"), ("thinking", None), ("writing", None)])
        self.assertNotIn("the words", json.dumps(seen))
        self.assertEqual(seen["result"]["exit_code"], 0)


class ConnectorTurn(unittest.TestCase):
    def setUp(self):
        self.s = Served(self)
        seat(self.s.w)
        flags(self.s.w, "mobile_read", "mobile_chat_write", "connector_effects")
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "hermes.py").write_text(FAKE_STREAM, encoding="utf-8")
        # The helper runs the Bot Chat turn (room_send.py chat) as well as its other ops.
        turn = "import sys\nif sys.argv[1:2] == ['chat']:\n" + textwrap.indent(FAKE_STREAM, "    ") + "    sys.exit(0)\n"
        (self.dir / "helper.py").write_text(turn + FAKE_HELPER, encoding="utf-8")
        self.go = self.dir / "go"
        env = {"TW_FAKE_LOG": str(self.dir / "log"), "TW_FAKE_GO": str(self.go)}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)

        def restore():
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        tok = Feeds(self.s.w.store).new_token(self.s.w.ws, "connector", by="tests")
        hermes = connector.Hermes([sys.executable, str(self.dir / "hermes.py")], sys.executable, self.dir,
                                  helper=self.dir / "helper.py")
        self.c = connector.Connector(connector.Core(f"http://127.0.0.1:{self.s.port}/api/v1", tok), hermes,
                                     say=lambda _: None)

    def mine(self, mid):
        return next(m for m in Chat(self.s.w.store).view(self.s.w.patrick(), "wisp")["mine"] if m["id"] == mid)

    def test_the_line_follows_the_turn_and_goes_when_it_ends(self):
        mid = Chat(self.s.w.store).send(self.s.w.patrick(), "wisp", "how is the build?", "key-00000901")["message_id"]
        t = threading.Thread(target=self.c.once)
        t.start()
        self.addCleanup(t.join, 15)
        self.addCleanup(self.go.touch)
        for _ in range(100):
            if self.mine(mid).get("doing"):
                break
            time.sleep(0.1)
        m = self.mine(mid)
        self.assertEqual(m["status"], "leased")
        self.assertEqual(m["doing"], {"state": "tool", "tool": "terminal"})
        self.go.touch()
        t.join(15)
        m = self.mine(mid)
        self.assertEqual(m["status"], "sent")
        self.assertNotIn("doing", m)
        argv = json.loads((self.dir / "log").read_text(encoding="utf-8"))
        self.assertEqual(argv[argv.index("--format") + 1], "stream-json")
        self.assertEqual(argv[:8], ["chat", "-p", "wisp", "--in", str(self.dir), "-c", "Bot Chat", "--create-if-missing"])
        self.assertEqual(self.c.doing, {}, "a finished turn is not kept")


class TheCore(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        seat(self.w)
        flags(self.w, "mobile_read", "mobile_chat_write", "connector_effects")
        self.chat = Chat(self.w.store)
        self.tok = Feeds(self.w.store).new_token(self.w.ws, "connector", by="tests")

    def send(self, target, key):
        return self.chat.send(self.w.patrick(), target, "hi", key)["message_id"]

    def mine(self, target="bramble"):
        return {m["id"]: m for m in self.chat.view(self.w.patrick(), target)["mine"]}

    def test_only_on_the_message_being_answered_and_only_well_formed(self):
        a = self.send("bramble", "key-00000902")
        waiting = self.send("bramble", "key-00000903")
        self.assertEqual([i["id"] for i in self.chat.claim(self.w.ws, self.tok)["items"]], [a])
        got = self.chat.doing(self.w.ws, self.tok, {"turns": [
            {"id": a, "state": "tool", "tool": "read_file"}, {"id": waiting, "state": "thinking"},
            {"id": "../x", "state": "thinking"}, {"id": "msg_y", "state": "dancing"}, "msg_z"]})
        self.assertEqual(got["kept"], 2)
        self.assertEqual(self.mine()[a]["doing"], {"state": "tool", "tool": "read_file"})
        self.assertNotIn("doing", self.mine()[waiting], "a message still waiting has no turn yet")
        self.chat.doing(self.w.ws, self.tok, {"turns": [{"id": a, "state": "tool", "tool": "rm -rf /"}]})
        self.assertEqual(self.mine()[a]["doing"], {"state": "tool", "tool": None})
        self.chat.doing(self.w.ws, self.tok, {"turns": [{"id": a, "state": "writing", "tool": "terminal"}]})
        self.assertEqual(self.mine()[a]["doing"], {"state": "writing", "tool": None}, "a tool only with a tool")

    def test_gone_when_not_said_again_or_when_the_result_comes(self):
        a = self.send("rift", "key-00000904")
        self.chat.claim(self.w.ws, self.tok)
        self.chat.doing(self.w.ws, self.tok, {"turns": [{"id": a, "state": "thinking"}]})
        self.assertIn("doing", self.mine("rift")[a])
        self.w.clock.advance(seconds=61)
        self.assertNotIn("doing", self.mine("rift")[a])
        self.chat.doing(self.w.ws, self.tok, {"turns": [{"id": a, "state": "writing"}]})
        self.assertIn("doing", self.mine("rift")[a])
        self.chat.doing(self.w.ws, self.tok, {"turns": []})
        self.assertNotIn("doing", self.mine("rift")[a], "a turn not in the set is over")
        self.chat.doing(self.w.ws, self.tok, {"turns": [{"id": a, "state": "writing"}]})
        self.chat.result(self.w.ws, self.tok, a, "confirmed", "turn finished")
        self.assertNotIn("doing", self.mine("rift")[a])

    def test_its_token_only_and_only_while_mobile_read_is_on(self):
        for tok in (None, "", Feeds(self.w.store).new_token(self.w.ws, "work", by="tests")):
            with self.assertRaises(CoreError) as e:
                self.chat.doing(self.w.ws, tok, {"turns": []})
            self.assertEqual(e.exception.reason, "bad_feed_token")
        with self.assertRaises(CoreError) as e:
            self.chat.doing(self.w.ws, self.tok, {"turns": "msg_x"})
        self.assertEqual(e.exception.reason, "bad_turns")
        self.w.store.set_flag(self.w.ws, "mobile_read", False, by="tests")
        with self.assertRaises(CoreError) as e:
            self.chat.doing(self.w.ws, self.tok, {"turns": []})
        self.assertEqual(e.exception.reason, "mobile_read_off")


class OverHttp(unittest.TestCase):
    def test_the_connector_posts_and_a_browser_is_refused(self):
        s = Served(self)
        seat(s.w)
        flags(s.w, "mobile_read")
        tok = Feeds(s.w.store).new_token(s.w.ws, "connector", by="tests")
        status, _, body = s.call("POST", "/connector/doing", headers={"Authorization": f"Bearer {tok}"},
                                 body={"turns": [{"id": "msg_abc", "state": "writing"}]})
        self.assertEqual((status, body), (200, {"kept": 1}))
        status, _, body = s.call("POST", "/connector/doing", headers={"Authorization": f"Bearer {tok}",
                                                                       "Origin": ORIGIN}, body={"turns": []})
        self.assertEqual((status, body.get("error")), (403, "not_a_feed"))


if __name__ == "__main__":
    unittest.main()
