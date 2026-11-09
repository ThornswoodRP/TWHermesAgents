"""A connector learns of new work the moment it is there, and a Bot's turn holds up no other (wakes.py).

Patrick, 9 Nov: "it doesnt feel live iot feels very delayed and I want tit all to be live". A connector
claimed every five seconds, and the Bots' connector waited for every turn of a round before it claimed
again, so a message to one Bot waited for another Bot's turn to end.

Checks:
- the core's number for a gateway answers a wait the moment it changes, at once without `after`, and after
  the wait otherwise; a write bumps it only once it is in, and a write rolled back never does;
- each thing a connector is handed bumps its gateway: a Bot Chat message ("bots"), a message to his own
  Hermes, a page the phone asked for, a read or change of a chat's model, a Stop or an answer, a reaction;
- `POST /connector/wait` and `POST /own/<gateway>/wait` answer with a number, the moment a message is sent,
  each only with that connector's token;
- the Waker sets its events at its first answer and with each change, and falls back to the old round
  when the core does not answer the wait;
- the Bots' connector starts a turn and claims again while it runs, and each turn says its result the moment
  it ends; his own Hermes's connector is woken when a turn ends or Hermes takes a message.
"""

from __future__ import annotations

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
from test_decisions_live_turns import LIVE_STREAM, clear
from test_decisions_own import SID, Gateways, passkey

from thornswood_decisions import connector, own_connector
from thornswood_decisions.chat import Chat
from thornswood_decisions.feeds import Feeds
from thornswood_decisions.own import PAGES
from thornswood_decisions.own_control import CONTROLS
from thornswood_decisions.own_turns import ORDERS
from thornswood_decisions.reactions import REACTIONS, Reactions
from thornswood_decisions.store import CoreError
from thornswood_decisions.wakes import WAKES


def number(ws, gw):
    return WAKES.wait(ws, gw, None, 0)["n"]


class TheNumber(unittest.TestCase):
    def test_a_wait_is_answered_when_it_changes_and_not_before(self):
        ws = "ws_wait_test"
        n = number(ws, "me")
        start = time.monotonic()
        self.assertEqual(WAKES.wait(ws, "me", n, 0.3), {"n": n})
        self.assertGreaterEqual(time.monotonic() - start, 0.25)
        got = {}
        t = threading.Thread(target=lambda: got.update(WAKES.wait(ws, "me", n, 10)))
        t.start()
        time.sleep(0.1)
        start = time.monotonic()
        WAKES.bump(ws, "one")  # another gateway's work wakes nobody here
        WAKES.bump(ws, "me")
        t.join(5)
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(got, {"n": n + 1})
        self.assertEqual(WAKES.wait(ws, "me", n + 7, 10), {"n": n + 1}, "a number it never had: answered at once")
        for bad in ({"after": "x"}, {"seconds": "soon"}):
            with self.assertRaises(CoreError):
                WAKES.wait(ws, "me", bad.get("after", 0), bad.get("seconds", 0))

    def test_a_write_wakes_once_it_is_in_and_never_when_rolled_back(self):
        w = World()
        self.addCleanup(w.close)
        n = number(w.ws, "bots")
        seen = []
        with w.store.tx():
            w.store.wake(w.ws, "bots")
            seen.append(number(w.ws, "bots"))
        self.assertEqual(seen, [n], "not while the write is not in")
        self.assertEqual(number(w.ws, "bots"), n + 1)
        with self.assertRaises(RuntimeError):
            with w.store.tx():
                w.store.wake(w.ws, "bots")
                raise RuntimeError("rolled back")
        with w.store.tx():
            pass
        self.assertEqual(number(w.ws, "bots"), n + 1)
        w.store.wake(w.ws, "bots")
        self.assertEqual(number(w.ws, "bots"), n + 2, "outside a transaction: at once")


class EachThingWakes(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        seat(self.w)
        flags(self.w, "mobile_read", "mobile_chat_write")
        self.g = Gateways(self.w)
        self.g.hello("one")

    def woke(self, gw, do):
        n = number(self.w.ws, gw)
        do()
        self.assertGreater(number(self.w.ws, gw), n, gw)

    def test_each_kind_of_work_wakes_its_connector(self):
        p, ws = self.w.patrick(), self.w.ws
        self.woke("bots", lambda: Chat(self.w.store).send(p, "wisp", "how is the build?", "key-wake-0001"))
        self.woke("bots", lambda: Reactions(self.w.store).want(p, "bots", "wisp", 5, REACTIONS[0]))
        self.woke("bots", lambda: PAGES.ask((ws, "bots", "bot", "wisp", None, ""), self.w.clock()))
        sprocket = self.w.core.auth.ensure_service(ws, "specialist", "sprocket")
        self.woke("bots", lambda: self.w.core.keep_turned_away(sprocket, "@Sprocket, a look?", "no room", "sess-bc"))
        self.woke("one", lambda: self.g.own.send(passkey(self.w), "one", SID, None, "hi", "key-wake-0002"))
        self.woke("one", lambda: PAGES.ask((ws, "one", "messages", SID, None, ""), self.w.clock()))
        self.woke("one", lambda: CONTROLS.ask((ws, "one", "wake_test_chat"), self.w.clock()))
        self.woke("one", lambda: ORDERS.add(ws, "one", "own_x", "stop", None, None, self.w.clock()))
        n = number(ws, "me")
        self.g.own.send(passkey(self.w), "one", SID, None, "hi again", "key-wake-0003")
        self.assertEqual(number(ws, "me"), n, "One's work does not wake the other connector")


class TheWaitRoutes(unittest.TestCase):
    def setUp(self):
        self.s = Served(self)
        seat(self.s.w)
        flags(self.s.w, "mobile_chat_write")
        self.tok = Feeds(self.s.w.store).new_token(self.s.w.ws, "connector", by="tests")

    def wait(self, path, tok, body):
        return self.s.call("POST", path, headers={"Authorization": f"Bearer {tok}"}, body=body)

    def test_a_wait_is_answered_the_moment_a_message_is_sent(self):
        status, _, got = self.wait("/connector/wait", self.tok, {})
        self.assertEqual(status, 200)
        n = got["n"]
        out = {}
        t = threading.Thread(target=lambda: out.update(r=self.wait("/connector/wait", self.tok,
                                                                    {"after": n, "seconds": 10})))
        t.start()
        time.sleep(0.2)
        start = time.monotonic()
        Chat(self.s.w.store).send(self.s.w.patrick(), "rift", "the load order", "key-wake-0010")
        t.join(5)
        self.assertLess(time.monotonic() - start, 2, "answered when the message was sent, not after the wait")
        self.assertEqual(out["r"][0], 200)
        self.assertEqual(out["r"][2], {"n": n + 1})

    def test_only_with_the_connectors_own_token(self):
        self.assertEqual(self.wait("/connector/wait", "not-a-token", {})[0], 401)
        g = Gateways(self.s.w)
        self.assertEqual(self.wait("/own/one/wait", g.tok["one"], {})[0], 200)
        self.assertEqual(self.wait("/own/one/wait", g.tok["me"], {})[0], 401)
        self.assertEqual(self.wait("/own/one/wait", self.tok, {})[0], 401)
        self.assertEqual(self.wait("/own/one/wait", g.tok["one"], {"after": 0, "seconds": "x"})[0], 400)


class TheWaker(unittest.TestCase):
    def test_first_answer_and_each_change_wake_and_a_core_without_waits_falls_back(self):
        answers = [(200, {"n": 4}), (200, {"n": 4}), (200, {"n": 5}), (404, {}), (200, {"n": 5}), (0, {})]
        asked, set_at, slept = [], [], []
        e = threading.Event()

        def ask(body):
            asked.append(body)
            return answers[len(asked) - 1]

        class Recorder:
            def set(self):
                set_at.append(len(asked))
        own_connector.Waker(ask, [Recorder(), e], 15).follow(sleep=slept.append, rounds=len(answers))
        self.assertEqual(asked[:3], [{"seconds": 0}, {"after": 4, "seconds": 15}, {"after": 4, "seconds": 15}])
        self.assertEqual(asked[4], {"seconds": 0}, "after a failed wait it asks for the number again")
        self.assertEqual(set_at, [1, 3, 5], "its first answer, the change, and the first answer after the gap")
        self.assertTrue(e.is_set())
        self.assertEqual(slept, [own_connector.POLL_SECONDS * 6] * 2)


class TheBotsConnectorClaimsWhileATurnRuns(unittest.TestCase):
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
        self.addCleanup(self.go.touch)
        tok = Feeds(self.s.w.store).new_token(self.s.w.ws, "connector", by="tests")
        hermes = connector.Hermes([sys.executable, "-c", "pass"], sys.executable, self.dir, helper=self.dir / "helper.py")
        self.c = connector.Connector(connector.Core(f"http://127.0.0.1:{self.s.port}/api/v1", tok), hermes,
                                     say=lambda _: None)

    def status(self, mid):
        return self.s.w.store.db.execute("SELECT status FROM chat_messages WHERE id = ?", (mid,)).fetchone()[0]

    def send(self, bot, text, key):
        return Chat(self.s.w.store).send(self.s.w.patrick(), bot, text, key)["message_id"]

    def test_a_second_bots_message_goes_while_the_first_turn_runs_and_each_result_goes_at_once(self):
        wisp = self.send("wisp", "how is the build?", "key-wake-0020")
        start = time.monotonic()
        self.assertEqual(self.c.round(), 1)
        self.assertLess(time.monotonic() - start, 3, "the round did not wait for the turn")
        self.assertEqual(self.status(wisp), "leased")
        rift = self.send("rift", "the load order?", "key-wake-0021")
        self.assertEqual(self.c.round(), 1, "claimed while Wisp's turn runs")
        self.assertEqual(self.status(rift), "leased")
        self.c.wakeup.clear()
        self.go.touch()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and {self.status(wisp), self.status(rift)} != {"sent"}:
            time.sleep(0.05)
        self.assertEqual((self.status(wisp), self.status(rift)), ("sent", "sent"),
                         "each turn said its result when it ended, with no round after it")
        self.assertTrue(self.c.wakeup.is_set(), "a turn that ended wakes the claim loop")
        self.assertEqual(self.c.lives, {})
        self.assertEqual(self.c.unsaid, [])

    def test_with_every_slot_busy_it_claims_none_but_still_sets_reactions(self):
        for i, bot in enumerate(("wisp", "rift", "echo")):
            self.send(bot, "busy", f"key-wake-003{i}")
        self.assertEqual(self.c.round(), connector.PARALLEL)
        last = self.send("magpie", "wait your turn", "key-wake-0039")
        self.assertEqual(self.c.round(), 0)
        self.assertEqual(self.status(last), "queued")


class OwnConnectorWakes(unittest.TestCase):
    def test_a_turn_ending_and_hermes_taking_a_message_wake_the_round(self):
        took, end = threading.Event(), threading.Event()

        class Core:
            def post(self, op, body):
                return 200, {}

        class Hermes:
            def turn(self, sid, text, doing, image, started, live):
                doing("thinking", None)
                doing("thinking", None, admitted=True)
                took.set()
                end.wait(10)
                return "confirmed", "turn finished", "", sid
        conn = own_connector.OwnConnector(Core(), Hermes(), Path(tempfile.mkdtemp()) / "state.db", say=lambda _: None)
        conn.lives["own_1"] = own_connector.LiveTurn()
        t = threading.Thread(target=conn._deliver_live, args=({"id": "own_1", "session_id": None, "text": "hi"},))
        t.start()
        self.assertTrue(took.wait(5))
        self.assertTrue(conn.wakeup.is_set(), "Hermes took it: the chat's next message can go")
        conn.wakeup.clear()
        end.set()
        t.join(5)
        self.assertTrue(conn.wakeup.is_set(), "its result goes now")
        self.assertEqual([r["id"] for r in conn.unsaid], ["own_1"])


if __name__ == "__main__":
    unittest.main()
