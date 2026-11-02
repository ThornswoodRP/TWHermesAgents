"""The live line under a message being answered (Patrick, 2 Nov: "no typing or thinking indicators and no
like what they are currently doing"; "Those would be the same lines").

Checks:
- One's connector: what a turn is doing comes from Hermes's own stream-json events, in order, each change
  once: thinking, writing, or a tool by its name. A tool name that is not a plain name is left out. The
  words of the reply, a tool's input and its output never leave the turn.
- the connector says it with each claim while the turn runs, and stops when the turn ends.
- the core: it keeps only well-formed turns, puts each on the message being answered in his chat view,
  never on one still waiting, and drops it once the connector has not been heard from.
- over HTTP: the claim's turns reach the view.
Everything runs in a scratch test workspace, with a stand-in for `hermes`; nothing reaches a real Hermes.
"""

from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path

from decisions_helpers import World
from test_decisions_api import Served
from test_decisions_own import SID, Gateways, on, passkey

from thornswood_decisions import own_connector
from thornswood_decisions.feeds import Feeds
from thornswood_decisions.own import OwnChat

STREAM = [
    {"type": "system", "subtype": "init", "model": "m", "session_id": SID},
    {"type": "tool_use", "name": "terminal", "tool_call_id": "c1", "input": {"command": "cat secrets.txt"}},
    {"type": "tool_result", "name": "terminal", "output": "the secret output", "duration_ms": 5, "is_error": False},
    {"type": "tool_use", "name": "web search; rm -rf", "input": {}},
    {"type": "tool_result", "name": "x", "output": ""},
    {"type": "text", "text": "Here is "},
    {"type": "text", "text": "the answer."},
    {"type": "result", "session_id": SID, "exit_code": 0, "text": "Here is the answer."},
]


class FromTheStream(unittest.TestCase):
    def test_each_change_once_in_order_and_never_the_words(self):
        script = Path(tempfile.mkdtemp()) / "fake_hermes.py"
        script.write_text("import json\n" + "".join(f"print(json.dumps({e!r}), flush=True)\n" for e in STREAM),
                          encoding="utf-8")
        h = own_connector.Hermes([sys.executable], [sys.executable], Path(tempfile.mkdtemp()), "One")
        heard = []
        outcome, _, _, sid = h._run([sys.executable, str(script)], SID, lambda state, tool: heard.append((state, tool)))
        self.assertEqual((outcome, sid), ("confirmed", SID))
        self.assertEqual(heard, [("thinking", None), ("tool", "terminal"), ("thinking", None), ("tool", None),
                                 ("thinking", None), ("writing", None)])
        self.assertNotIn("secret", json.dumps(heard))
        self.assertNotIn("answer", json.dumps(heard))


class FakeHermes:
    def __init__(self):
        self.go = threading.Event()
        self.started = threading.Event()

    def turn(self, session_id, text, doing):
        doing("tool", "read_file")
        self.started.set()
        self.go.wait(10)
        return "confirmed", "turn finished", "", session_id


class FakeCore:
    def post(self, op, body):
        return 200, {}


class SaidWithEachClaim(unittest.TestCase):
    def test_while_the_turn_runs_and_not_after(self):
        hermes = FakeHermes()
        c = own_connector.OwnConnector(FakeCore(), hermes, Path("/nonexistent"), say=lambda _: None)
        t = threading.Thread(target=c._deliver, args=({"id": "own_abc", "session_id": SID, "text": "hi"},))
        t.start()
        hermes.started.wait(10)
        self.assertEqual(c.busy()["turns"], [{"id": "own_abc", "state": "tool", "tool": "read_file"}])
        hermes.go.set()
        t.join(10)
        self.assertEqual(c.busy()["turns"], [])


class TheCore(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.g = Gateways(self.w)
        self.g.hello("one")
        self.g.feed("one")
        self.p = passkey(self.w)

    def leased(self):
        mid = self.g.own.send(self.p, "one", SID, None, "check the build", "own-key-0001")["message_id"]
        self.assertEqual([i["id"] for i in self.g.own.claim(self.w.ws, "one", self.g.tok["one"], 1, True)["items"]], [mid])
        return mid

    def say(self, turns):
        self.g.own.claim(self.w.ws, "one", self.g.tok["one"], 0, True, "", None, None, {"turns": turns})

    def mine(self):
        return {m["id"]: m for m in self.g.own.view(self.p, "one", SID)["mine"]}

    def test_the_message_being_answered_carries_what_its_turn_is_doing(self):
        mid = self.leased()
        waiting = self.g.own.send(self.p, "one", SID, None, "and then this", "own-key-0002")["message_id"]
        self.say([{"id": mid, "state": "tool", "tool": "terminal"}, {"id": waiting, "state": "thinking"}])
        got = self.mine()
        self.assertEqual(got[mid]["doing"], {"state": "tool", "tool": "terminal"})
        self.assertNotIn("doing", got[waiting], "a message still waiting has no turn yet")
        self.say([{"id": mid, "state": "writing", "tool": "terminal"}])
        self.assertEqual(self.mine()[mid]["doing"], {"state": "writing", "tool": None}, "a tool only with a tool")

    def test_only_well_formed_turns(self):
        mid = self.leased()
        for bad in ([{"id": mid, "state": "dancing"}], [{"id": "../x", "state": "thinking"}], ["own_x"], "own_x",
                    [{"id": mid, "state": "tool", "tool": "rm -rf /"}]):
            self.say(bad)
            doing = self.mine()[mid].get("doing")
            self.assertTrue(doing is None or doing == {"state": "tool", "tool": None}, (bad, doing))

    def test_gone_once_the_connector_is_not_heard_from(self):
        mid = self.leased()
        self.say([{"id": mid, "state": "thinking"}])
        self.assertIn("doing", self.mine()[mid])
        self.w.clock.advance(minutes=3)
        self.assertNotIn("doing", self.mine().get(mid, {}))


class OverHttp(unittest.TestCase):
    def test_the_claims_turns_reach_the_view(self):
        s = Served(self)
        g = Gateways(s.w)
        g.hello("one")
        g.feed("one")
        p = passkey(s.w)
        mid = g.own.send(p, "one", SID, None, "hi", "own-key-0003")["message_id"]
        g.own.claim(s.w.ws, "one", g.tok["one"], 1, True)
        status, _, _ = s.call("POST", "/own/one/claim", headers={"Authorization": f"Bearer {g.tok['one']}"},
                              body={"limit": 0, "ready": True, "turns": [{"id": mid, "state": "writing"}]})
        self.assertEqual(status, 200)
        self.assertEqual(next(m for m in g.own.view(p, "one", SID)["mine"] if m["id"] == mid)["doing"],
                         {"state": "writing", "tool": None})


if __name__ == "__main__":
    unittest.main()
