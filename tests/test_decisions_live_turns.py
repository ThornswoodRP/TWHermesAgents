"""A turn shows as it happens, as Hermes Desktop draws it (TWHermesAgents #92).

Patrick, 9 Nov: "there was not the indicator like ther in on Desktop if the are typing, thining, all the other
adjectives the desktop shows, and I think they still dont show they have things runnig in the background or
subagents like the desktop does and it doesnt feel live iot feels very delayed and I want tit all to be live."

Checks:
- room_send.py says each gateway event Desktop draws as one line, cut to size, and nothing for the rest;
- the connector keeps a turn's parts in order (LiveTurn): thinking, the reply as it is written, each tool
  with its preview and whether it is done, each subagent, Hermes's status lines; follow_turn feeds it, and
  the state line still never hears the words;
- the core keeps a turn's parts in memory only, refuses parts it does not draw, takes only a connector's
  own messages (a Bot Chat message from the Bots' connector, an own message from that gateway's), answers
  the phone the moment the parts change (a long poll) and drops them when the message's result comes;
- both connectors post a turn's parts while it runs, and its last change before its result.
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
from test_decisions_api import Served
from test_decisions_chat import FAKE_HELPER, flags, seat
from test_decisions_own import SID, Gateways, passkey

from thornswood_decisions import connector, own_connector, room_send
from thornswood_decisions.chat import Chat
from thornswood_decisions.feeds import Feeds
from thornswood_decisions.live_turns import LIVE, clean_parts
from thornswood_decisions.store import CoreError


def clear():
    with LIVE.cond:
        LIVE.held.clear()


class TheHelperSaysIt(unittest.TestCase):
    def test_each_event_desktop_draws_is_one_line(self):
        line = room_send.live_line
        self.assertEqual(line("reasoning.delta", {"text": "Let me look"}), {"type": "thinking", "text": "Let me look"})
        self.assertEqual(line("status.update", {"kind": "process", "text": "npm test finished"}),
                         {"type": "status", "kind": "process", "text": "npm test finished"})
        self.assertEqual(line("tool.start", {"tool_id": "t1", "name": "terminal", "preview": "npm test"}),
                         {"type": "step", "id": "t1", "name": "terminal", "preview": "npm test"})
        self.assertEqual(line("tool.complete", {"tool_id": "t1", "name": "terminal", "duration_s": 2.5}),
                         {"type": "step_done", "id": "t1", "name": "terminal", "error": False, "secs": 2.5})
        self.assertEqual(line("tool.progress", {"tool_id": "t1", "name": "x", "progress": 1, "percent": 40,
                                                "message": "half"}),
                         {"type": "step_progress", "id": "t1", "percent": 40, "message": "half"})
        sub = line("subagent.tool", {"goal": "Read the launcher log", "task_count": 1, "task_index": 0,
                                     "subagent_id": "sa_1", "tool_name": "read_file", "tool_preview": "launcher.log"})
        self.assertEqual((sub["type"], sub["event"], sub["id"], sub["tool"], sub["preview"]),
                         ("subagent", "tool", "sa_1", "read_file", "launcher.log"))
        self.assertEqual(line("background.complete", {"task_id": "bg1", "text": "The build finished."}),
                         {"type": "background", "id": "bg1", "text": "The build finished."})

    def test_what_it_does_not_draw_and_what_is_too_long(self):
        line = room_send.live_line
        for kind, payload in (("message.delta", {"text": "x"}), ("session.info", {}), ("status.update", {"text": " "}),
                              ("reasoning.delta", {"text": ""}), ("tool.start", "not an object")):
            self.assertIsNone(line(kind, payload), kind)
        self.assertEqual(len(line("reasoning.delta", {"text": "x" * 9000})["text"]), room_send.LIVE_TEXT)
        self.assertEqual(len(line("tool.start", {"tool_id": "t", "name": "n", "preview": "p" * 900})["preview"]), 200)


def stream(*events):
    return iter(json.dumps(e) + "\n" for e in events)


class TheConnectorKeepsIt(unittest.TestCase):
    def test_a_turn_in_order_as_desktop_draws_it(self):
        live, heard = own_connector.LiveTurn(), []
        own_connector.follow_turn(stream(
            {"type": "system", "session_id": "s"},
            {"type": "thinking", "text": "Which log? "}, {"type": "thinking", "text": "The launcher's."},
            {"type": "tool_use", "name": "terminal", "input": {"command": "tail launcher.log"}},
            {"type": "step", "id": "t1", "name": "terminal", "preview": "tail launcher.log"},
            {"type": "step_done", "id": "t1", "name": "terminal", "error": False},
            {"type": "subagent", "event": "start", "id": "sa_1", "goal": "Check the mods"},
            {"type": "subagent", "event": "tool", "id": "sa_1", "tool": "read_file", "preview": "plugins.txt"},
            {"type": "subagent", "event": "complete", "id": "sa_1", "status": "completed", "summary": "All 412 load."},
            {"type": "status", "kind": "process", "text": "The build finished."},
            {"type": "text", "text": "The launcher "}, {"type": "text", "text": "is fine."},
            {"type": "result", "exit_code": 0, "text": "The launcher is fine."}),
            {}, lambda s, t: heard.append((s, t)), live)
        v, parts = live.seen()
        self.assertGreater(v, 0)
        self.assertEqual(parts, [
            {"k": "think", "text": "Which log? The launcher's."},
            {"k": "step", "id": "t1", "name": "terminal", "preview": "tail launcher.log", "done": True, "error": False},
            {"k": "sub", "id": "sa_1", "goal": "Check the mods", "status": "completed", "tool": "", "preview": "",
             "tools": 1, "summary": "All 412 load."},
            {"k": "note", "text": "The build finished."},
            {"k": "text", "text": "The launcher is fine."}])
        self.assertEqual(heard, [("thinking", None), ("tool", "terminal"), ("writing", None)])
        self.assertNotIn("launcher", json.dumps(heard), "the state line never hears the words")

    def test_it_keeps_the_newest(self):
        live = own_connector.LiveTurn()
        for i in range(70):
            live.hear({"type": "step", "id": f"t{i}", "name": "terminal", "preview": str(i)})
        live.hear({"type": "text", "text": "x" * 20000})
        _, parts = live.seen()
        self.assertEqual(len(parts), live.PARTS)
        self.assertEqual(len(parts[-1]["text"]), live.TEXT)
        v = live.v
        live.hear({"type": "tool_use", "name": "terminal"})
        self.assertEqual(live.v, v, "a line it does not draw is no change")


class TheCoreHoldsIt(unittest.TestCase):
    def setUp(self):
        clear()
        self.addCleanup(clear)
        self.w = World()
        self.addCleanup(self.w.close)
        seat(self.w)
        flags(self.w, "mobile_read", "mobile_chat_write", "connector_effects")
        self.chat = Chat(self.w.store)
        self.tok = Feeds(self.w.store).new_token(self.w.ws, "connector", by="tests")
        self.mid = self.chat.send(self.w.patrick(), "wisp", "Hello.", "key-live-0001")["message_id"]

    def post(self, parts, mid=None):
        return self.chat.turns(self.w.ws, self.tok, {"turns": [{"id": mid or self.mid, "parts": parts}]})

    def test_parts_it_does_not_draw_are_refused(self):
        for bad in ([{"k": "html", "text": "<b>"}], [{"k": "text"}], ["x"], [{"k": "text", "text": "x"}] * 61, "x"):
            with self.assertRaises(CoreError) as e:
                clean_parts(bad)
            self.assertEqual(e.exception.reason, "bad_live")
        self.assertEqual(clean_parts([{"k": "sub", "goal": "g", "status": "pwned", "tools": -1}])[0]["status"], "running")

    def test_only_his_own_messages_and_only_from_its_connector(self):
        with self.assertRaises(CoreError) as e:
            self.post([{"k": "text", "text": "x"}], mid="msg_notamessage")
        self.assertEqual(e.exception.reason, "bad_live")
        with self.assertRaises(CoreError) as e:
            self.chat.turns(self.w.ws, "wrong-token", {"turns": []})
        self.assertEqual(e.exception.status, 401)
        g = Gateways(self.w)
        g.hello("one")
        own = g.own.send(passkey(self.w), "one", SID, None, "hi", "own-live-0001")["message_id"]
        with self.assertRaises(CoreError):
            self.post([{"k": "text", "text": "x"}], mid=own)  # an own message is not the Bots' connector's
        with self.assertRaises(CoreError):
            g.own.turns(self.w.ws, "me", g.tok["me"], {"turns": [{"id": own, "parts": []}]})  # One's, not me's
        self.assertEqual(g.own.turns(self.w.ws, "one", g.tok["one"], {"turns": [{"id": own, "parts": []}]}), {"kept": 1})

    def test_the_phone_is_answered_when_it_changes_and_not_before(self):
        p = self.w.patrick()
        self.assertEqual(self.chat.turn_live(p, self.mid, 0, 0), {"n": 0, "parts": None})
        self.post([{"k": "think", "text": "Hm"}])
        first = self.chat.turn_live(p, self.mid, 0, 5)
        self.assertEqual(first["parts"], [{"k": "think", "text": "Hm"}])
        self.post([{"k": "think", "text": "Hm"}])  # the same again: no change
        start = time.monotonic()
        self.assertEqual(self.chat.turn_live(p, self.mid, first["n"], 0.3)["n"], first["n"])
        self.assertGreaterEqual(time.monotonic() - start, 0.25)
        got = {}

        def ask():
            got["r"] = self.chat.turn_live(p, self.mid, first["n"], 10)
        t = threading.Thread(target=ask)
        t.start()
        time.sleep(0.2)
        start = time.monotonic()
        self.post([{"k": "think", "text": "Hm"}, {"k": "text", "text": "Hey Patrick."}])
        t.join(5)
        self.assertLess(time.monotonic() - start, 2, "answered when it changed, not at the end of the wait")
        self.assertEqual(got["r"]["parts"][-1], {"k": "text", "text": "Hey Patrick."})
        with self.assertRaises(CoreError) as e:
            self.chat.turn_live(self.w.rift, self.mid, 0, 0)
        self.assertEqual(e.exception.reason, "not_patrick")
        with self.assertRaises(CoreError) as e:
            self.chat.turn_live(p, "msg_someone_elses", 0, 0)
        self.assertEqual(e.exception.status, 404)

    def test_the_result_drops_it_and_the_store_never_has_it(self):
        [item] = self.chat.claim(self.w.ws, self.tok, 3)["items"]
        self.post([{"k": "text", "text": "a private sentence"}])
        self.chat.result(self.w.ws, self.tok, item["id"], "confirmed", "turn finished")
        self.assertEqual(self.chat.turn_live(self.w.patrick(), self.mid, 0, 0), {"n": 0, "parts": None})
        dump = "\n".join(self.w.store.db.iterdump())
        self.assertNotIn("private sentence", dump)


# A Bot's turn as room_send.py chat says it: Hermes's own lines and the live ones, then it waits for the
# test's word before it ends.
LIVE_STREAM = textwrap.dedent('''
    import json, os, sys, time
    from pathlib import Path
    def say(e):
        print(json.dumps(e), flush=True)
    say({"type": "system", "subtype": "init", "session_id": "s_bc"})
    say({"type": "thinking", "text": "Checking the build."})
    say({"type": "tool_use", "name": "terminal", "input": {"command": "npm test"}})
    say({"type": "step", "id": "t1", "name": "terminal", "preview": "npm test"})
    go = Path(os.environ["TW_FAKE_GO"])
    for _ in range(200):
        if go.exists():
            break
        time.sleep(0.05)
    say({"type": "step_done", "id": "t1", "name": "terminal", "error": False})
    say({"type": "text", "text": "All green."})
    say({"type": "result", "session_id": "s_bc", "exit_code": 0, "text": "All green."})
''')


class TheBotsConnectorPostsIt(unittest.TestCase):
    def setUp(self):
        clear()
        self.addCleanup(clear)
        self.s = Served(self)
        seat(self.s.w)
        flags(self.s.w, "mobile_read", "mobile_chat_write", "connector_effects")
        self.dir = Path(tempfile.mkdtemp())
        turn = "import sys\nif sys.argv[1:2] == ['chat']:\n" + textwrap.indent(LIVE_STREAM, "    ") + "    sys.exit(0)\n"
        (self.dir / "helper.py").write_text(turn + FAKE_HELPER, encoding="utf-8")
        self.go = self.dir / "go"
        env = {"TW_FAKE_LOG": str(self.dir / "log"), "TW_FAKE_GO": str(self.go)}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in old.items()])
        tok = Feeds(self.s.w.store).new_token(self.s.w.ws, "connector", by="tests")
        hermes = connector.Hermes([sys.executable, "-c", "pass"], sys.executable, self.dir, helper=self.dir / "helper.py")
        self.c = connector.Connector(connector.Core(f"http://127.0.0.1:{self.s.port}/api/v1", tok), hermes,
                                     say=lambda _: None)

    def test_the_turn_shows_while_it_runs_and_goes_with_its_result(self):
        p = self.s.w.patrick()
        mid = Chat(self.s.w.store).send(p, "wisp", "how is the build?", "key-live-0002")["message_id"]
        t = threading.Thread(target=self.c.once)
        t.start()
        self.addCleanup(t.join, 15)
        self.addCleanup(self.go.touch)
        got = Chat(self.s.w.store).turn_live(p, mid, 0, 10)
        for _ in range(50):
            if got["parts"] and got["parts"][-1]["k"] == "step":
                break
            got = Chat(self.s.w.store).turn_live(p, mid, got["n"], 2)
        self.assertEqual(got["parts"], [
            {"k": "think", "text": "Checking the build."},
            {"k": "step", "name": "terminal", "preview": "npm test", "done": False, "error": False}])
        self.go.touch()
        t.join(15)
        self.assertEqual(Chat(self.s.w.store).turn_live(p, mid, 0, 0), {"n": 0, "parts": None})
        self.assertEqual(self.c.lives, {}, "a finished round keeps no turn")


class OneConnectorPostsIt(unittest.TestCase):
    def test_a_turns_last_change_goes_before_its_result(self):
        posts = []

        class Core:
            def post(self, op, body):
                posts.append((op, json.loads(json.dumps(body))))
                return 200, {}
        conn = own_connector.OwnConnector(Core(), own_connector.Hermes([sys.executable, "-c", "pass"],
                                                                       [sys.executable, "-c", "pass"],
                                                                       Path(tempfile.mkdtemp()), "One's Hermes"),
                                          Path(tempfile.mkdtemp()) / "state.db", say=lambda _: None)
        live = own_connector.LiveTurn()
        conn.lives["own_1"] = live
        live.hear({"type": "text", "text": "Done."})
        conn.ended.add("own_1")
        conn.unsaid.append({"id": "own_1", "outcome": "confirmed", "ref": "turn finished", "reason": ""})
        conn._report()
        self.assertEqual([op for op, _ in posts], ["turns", "result"])
        self.assertEqual(posts[0][1], {"turns": [{"id": "own_1", "parts": [{"k": "text", "text": "Done."}]}]})
        self.assertEqual(conn.lives, {}, "a turn that is over goes once its last change is said")


if __name__ == "__main__":
    unittest.main()
