"""Online and offline dots for the Bots (Patrick, 2 Nov: "the design had green and red circles to indicate
bots online or offline yet that isn't in the app").

Checks that each dot comes from what is really running, never a made-up value:
- room_send.py op states: Hermes's own runtime state of each Bot's gateway, found without starting one;
  a Bot whose check raises is check_failed and the others still answer.
- the Bots' connector: its first report asks the core which Bots to check, then reports their states at
  once and every half minute after that; a helper that could not run says why; the reports and the
  phone's pages go on while a claim round waits for a Bot's turn, and a reads thread that raised stops it.
- the core: only the connector's token, only bound Bots, only Hermes's states; it is held in memory and
  read back by the Bots list and each Bot Chat; a report older than two minutes is "not heard from",
  never the last state it said.
- the route, which a browser cannot reach.
Everything runs in a scratch test workspace, with stand-ins for Hermes; nothing reaches a real one.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

from decisions_helpers import World
from test_decisions_api import ORIGIN, Served
from test_decisions_chat import BOTS, flags, seat

from thornswood_decisions import chat as chat_mod, connector
from thornswood_decisions.chat import Chat
from thornswood_decisions.feeds import Feeds
from thornswood_decisions.store import CoreError

STUB_RUNTIME = textwrap.dedent('''
    import json, os
    from pathlib import Path
    from types import SimpleNamespace
    def gateway_profile_home(home):
        return home
    def discover_gateway_endpoint(home, timeout=2.0):
        states = json.loads(os.environ.get("TW_STUB_STATES", "{}"))
        state = states.get(Path(home).name, "absent")
        if state == "boom":
            raise OSError("the control socket could not be read")
        return SimpleNamespace(state=state, endpoint="ep" if state == "ready" else None)
    def attachable(found):
        return found.endpoint is not None
''')


class StatesHelper(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "hermes_cli").mkdir()
        (self.dir / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
        (self.dir / "hermes_cli" / "gateway_runtime.py").write_text(STUB_RUNTIME, encoding="utf-8")
        (self.dir / "hermes_constants.py").write_text(
            "from pathlib import Path\ndef get_default_hermes_root():\n    return Path('/home/hermes/.hermes')\n",
            encoding="utf-8")

    def run_helper(self, req: dict, states: dict) -> dict:
        helper = Path(connector.__file__).with_name("room_send.py")
        code = f"import runpy, sys; sys.path.insert(0, {str(self.dir)!r}); sys.argv = ['room_send']; " \
               f"runpy.run_path({str(helper)!r}, run_name='__main__')"
        out = subprocess.run([sys.executable, "-I", "-c", code], input=json.dumps(req).encode(),
                             capture_output=True, timeout=60, env={**os.environ, "TW_STUB_STATES": json.dumps(states)})
        return json.loads(out.stdout.decode().strip().splitlines()[-1])

    def test_each_bots_state_as_hermes_names_it_in_one_run(self):
        got = self.run_helper({"op": "states", "profiles": ["bramble", "rift", "sprocket", "echo"]},
                              {"bramble": "ready", "rift": "starting", "sprocket": "boom"})
        self.assertEqual(got, {"states": {"bramble": "ready", "rift": "starting", "sprocket": "check_failed",
                                          "echo": "absent"}})

    def test_only_profile_names(self):
        for bad in (None, "bramble", ["../etc"], ["Bramble"], ["x"] * 17, [3]):
            self.assertIsNone(self.run_helper({"op": "states", "profiles": bad}, {})["states"])


class FakeHermes:
    def __init__(self, states):
        self.states_out, self.asked = states, []

    def states(self, profiles):
        self.asked.append(list(profiles))
        return None if self.states_out is None else {p: self.states_out.get(p, "absent") for p in profiles}

    def tasks(self, profiles):
        # The reads thread also starts the Tasks tab's report (test_decisions_tasks checks it).
        return {"tasks": {}}


class FakeCore:
    def __init__(self, bots=BOTS, status=200):
        self.bots, self.status, self.posts = list(bots), status, []

    def post(self, path, body):
        self.posts.append((path, body))
        return self.status, ({"bots": self.bots} if self.status == 200 else {})


class ConnectorReports(unittest.TestCase):
    def make(self, states, core=None):
        self.t = 1000.0
        self.core = core or FakeCore()
        self.hermes = FakeHermes(states)
        self.lines = []
        return connector.Connector(self.core, self.hermes, say=self.lines.append, hermes_home=Path("/nonexistent"))

    def clock(self):
        return self.t

    def test_first_asks_which_bots_then_reports_them_at_once(self):
        c = self.make({"bramble": "ready"})
        self.assertTrue(c.live(self.clock))
        self.assertEqual([p for p, _ in self.core.posts], ["/connector/live", "/connector/live"])
        self.assertEqual(self.core.posts[0][1], {"states": {}})
        self.assertEqual(self.core.posts[1][1]["states"]["bramble"], "ready")
        self.assertEqual(self.core.posts[1][1]["states"]["rift"], "absent")
        self.assertEqual(self.hermes.asked, [list(BOTS)])

    def test_every_half_minute_and_not_between(self):
        c = self.make({"bramble": "ready"})
        c.live(self.clock)
        n = len(self.core.posts)
        self.t += connector.LIVE_SECONDS - 1
        self.assertFalse(c.live(self.clock))
        self.assertEqual(len(self.core.posts), n)
        self.t += 1
        self.assertTrue(c.live(self.clock))
        self.assertEqual(len(self.core.posts), n + 1)

    def test_a_helper_that_could_not_run_says_so_and_claims_no_state(self):
        c = self.make(None)
        c.live(self.clock)
        self.assertEqual(self.core.posts[-1][1], {"states": {}, "why": "could not run Hermes's check"})

    def test_the_core_saying_wait_is_quiet_and_anything_else_is_said(self):
        c = self.make({}, FakeCore(status=423))
        self.assertFalse(c.live(self.clock))
        self.assertEqual(self.lines, [])
        c = self.make({}, FakeCore(status=500))
        c.live(self.clock)
        self.assertEqual(self.lines, ["FAIL live: the core answered 500"])



class ReadsBesideTurns(unittest.TestCase):
    """forever() with a claim round held by a Bot's turn, as on 8 Nov when a three-minute turn to Bramble left
    all six Bots "not heard from"."""

    def setUp(self):
        self.patched = [(connector, n, getattr(connector, n)) for n in ("POLL_SECONDS", "LIVE_SECONDS")]
        connector.POLL_SECONDS, connector.LIVE_SECONDS = 0.01, 0.02
        self.turn = threading.Event()
        self.addCleanup(self.turn.set)

    def tearDown(self):
        for mod, name, value in self.patched:
            setattr(mod, name, value)

    def run_forever(self, c):
        stopped = []

        def run():
            try:
                c.forever()
            except SystemExit as exc:
                stopped.append(str(exc))
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t, stopped

    def test_reports_and_pages_go_on_while_a_turn_holds_the_round(self):
        core = FakeCore()
        c = connector.Connector(core, FakeHermes({b: "ready" for b in BOTS}), say=lambda line: None,
                                hermes_home=Path("/nonexistent"))
        rounds = []

        def once():
            rounds.append(1)
            self.turn.wait(5)  # the turn, still running
            return 1
        c.round = once
        self.run_forever(c)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and sum(p == "/connector/live" for p, _ in core.posts) < 4:
            time.sleep(0.01)
        self.assertEqual(rounds, [1])
        lives = [b for p, b in list(core.posts) if p == "/connector/live"]
        self.assertGreaterEqual(len(lives), 4)
        self.assertEqual(lives[-1]["states"]["bramble"], "ready")
        self.assertIn("/connector/pages", [p for p, _ in list(core.posts)])

    def test_a_reads_thread_that_raised_stops_the_connector(self):
        c = connector.Connector(FakeCore(), FakeHermes({}), say=lambda line: None, hermes_home=Path("/nonexistent"))

        def pages():
            raise RuntimeError("the store went away")
        c.pages = pages
        c.round = lambda: 0
        t, stopped = self.run_forever(c)
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(stopped, ["STOP: the thread for pages and Bot states stopped"])

class CoreKeeps(unittest.TestCase):
    def setUp(self):
        chat_mod._LIVE.clear()
        self.addCleanup(chat_mod._LIVE.clear)
        self.w = World()
        self.addCleanup(self.w.close)
        seat(self.w)
        flags(self.w, "mobile_read")
        self.chat = Chat(self.w.store)
        self.tok = Feeds(self.w.store).new_token(self.w.ws, "connector", by="tests")

    def live(self, bot):
        return next(b for b in self.chat.summary(self.w.patrick())["bots"] if b["bot"] == bot)["live"]

    def test_not_heard_from_until_the_connector_reports(self):
        self.assertEqual(self.live("bramble")["state"], "not_heard")
        self.assertEqual(self.chat.view(self.w.patrick(), "bramble")["live"]["state"], "not_heard")

    def test_the_report_names_the_bots_and_is_what_the_phone_reads(self):
        self.assertEqual(self.chat.live(self.w.ws, self.tok, {"states": {}}), {"bots": list(BOTS)})
        self.chat.live(self.w.ws, self.tok, {"states": {"bramble": "ready", "rift": "absent", "echo": "starting"}})
        self.assertEqual(self.live("bramble"), {"state": "ready", "since": self.w.clock().isoformat()})
        self.assertEqual(self.live("rift")["state"], "absent")
        self.assertEqual(self.chat.view(self.w.patrick(), "echo")["live"]["state"], "starting")
        self.assertEqual(self.live("wisp")["state"], "not_heard", "a Bot the report left out is not known")
        self.assertIsNone(self.chat.view(self.w.patrick(), "room")["live"], "a room has no gateway of its own")

    def test_an_old_report_is_not_heard_from_never_its_last_state(self):
        self.chat.live(self.w.ws, self.tok, {"states": {"bramble": "ready"}})
        self.w.clock.advance(seconds=120)
        self.assertEqual(self.live("bramble")["state"], "ready")
        self.w.clock.advance(seconds=1)
        self.assertEqual(self.live("bramble")["state"], "not_heard")

    def test_a_check_that_could_not_run_is_check_failed_with_why(self):
        self.chat.live(self.w.ws, self.tok, {"states": {}, "why": "could not run Hermes's check"})
        self.assertEqual(self.live("magpie")["state"], "check_failed")
        self.assertEqual(self.live("magpie")["why"], "could not run Hermes's check")

    def test_only_its_token_bound_bots_and_hermess_states(self):
        for tok in (None, "", "nope", Feeds(self.w.store).new_token(self.w.ws, "work", by="tests")):
            with self.assertRaises(CoreError) as e:
                self.chat.live(self.w.ws, tok, {"states": {}})
            self.assertEqual(e.exception.reason, "bad_feed_token")
        for bad in ({"states": {"nobody": "ready"}}, {"states": {"bramble": "on"}}, {"states": []}, [],
                    {"states": {}, "why": 3}):
            with self.assertRaises(CoreError) as e:
                self.chat.live(self.w.ws, self.tok, bad)
            self.assertEqual(e.exception.reason, "bad_states", bad)
        self.assertEqual(self.live("bramble")["state"], "not_heard")

    def test_nothing_while_mobile_read_is_off(self):
        self.w.store.set_flag(self.w.ws, "mobile_read", False, by="tests")
        with self.assertRaises(CoreError) as e:
            self.chat.live(self.w.ws, self.tok, {"states": {"bramble": "ready"}})
        self.assertEqual(e.exception.reason, "mobile_read_off")


class Route(unittest.TestCase):
    def setUp(self):
        chat_mod._LIVE.clear()
        self.addCleanup(chat_mod._LIVE.clear)
        self.s = Served(self)
        seat(self.s.w)
        flags(self.s.w, "mobile_read")
        self.tok = Feeds(self.s.w.store).new_token(self.s.w.ws, "connector", by="tests")

    def test_the_connector_posts_and_a_browser_cannot(self):
        auth = {"Authorization": "Bearer " + self.tok}
        status, _, body = self.s.call("POST", "/connector/live", headers=auth, body={"states": {"bramble": "ready"}})
        self.assertEqual((status, body.get("bots")), (200, list(BOTS)), body)
        status, _, body = self.s.call("POST", "/connector/live", headers={**auth, "Origin": ORIGIN}, body={"states": {}})
        self.assertEqual((status, body["error"]), (403, "not_a_feed"))

if __name__ == "__main__":
    unittest.main()
