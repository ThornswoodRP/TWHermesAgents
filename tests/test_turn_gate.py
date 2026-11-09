"""The Bots' side of the brokers: the turn gate and the broker tools, against
a real tw-policy store (attack tests 14 and 19 from the Bot's side).
"""

import sys as _sys
import unittest as _unittest

if not _sys.platform.startswith("linux"):
    # The brokers are Linux code (Unix sockets with SO_PEERCRED, pwd, O_NOFOLLOW
    # walks, systemd): they run only on the Oracle VM, and these tests run in
    # the VM half of scripts/test-at-head.sh.
    raise _unittest.SkipTest("the brokers run only on the Oracle VM; their tests run there")

import copy
import json
import unittest

from helpers import policy

from test_policy_service import service as policy_service

from thornswood_guard import brokers
from thornswood_guard.brokers import BrokerRefused, TurnGate
from thornswood_guard.policy import Policy
from thornswood_guard.tool_guard import check_tool


def ready_policy(**flags) -> Policy:
    data = copy.deepcopy(policy().data)
    data["brokers"]["ready"] = True
    data["brokers"].update(flags)
    return Policy(data)


class Ctx:
    def __init__(self, profile_name):
        self.profile_name = profile_name
        self.hooks = {}
        self.tools = {}

    def register_hook(self, name, fn):
        self.hooks.setdefault(name, []).append(fn)

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = (toolset, schema, handler)


class Fleet:
    """tw-policy as the gateway sees it, and brokers that record what reached them."""

    def __init__(self):
        self.policy = policy_service()
        self.seen = []

    def ask(self, broker, op, args):
        self.seen.append((broker, op, dict(args)))
        if broker != "policy":
            return {"echo": args}
        handler, callers = self.policy.ops()[op]
        if "hermes" not in callers:
            raise BrokerRefused(f"hermes may not ask for {op}")
        from thornswood_brokers.wire import Refused
        try:
            return handler("hermes", json.loads(json.dumps(args)))
        except Refused as e:
            raise BrokerRefused(str(e)) from None


def gate(fleet, bot):
    return TurnGate(ready_policy(), bot, fleet.ask, wait_seconds=0, sleep=lambda s: None)


class Gate(unittest.TestCase):
    def test_one_turn_at_a_time_and_the_rest_wait_with_no_tools(self):
        f = Fleet()
        rift, wisp = gate(f, "rift"), gate(f, "wisp")
        self.assertIsNone(rift.before_llm(session_id="r1", task_id="12"))
        out = wisp.before_llm(session_id="w1", task_id="9")
        self.assertIn("not admitted", out["context"])
        self.assertIsNone(rift.before_tool("read_file", session_id="r1"))
        self.assertIn("Thornswood queue", wisp.before_tool("read_file", session_id="w1"))
        rift.after_turn(session_id="r1")
        # Wisp's next turn keeps its place in line and gets the slot.
        self.assertIsNone(wisp.before_llm(session_id="w1", task_id="9"))
        self.assertIsNone(wisp.before_tool("read_file", session_id="w1"))

    def test_a_turn_turned_away_leaves_a_note_for_its_session_and_an_admitted_one_none(self):
        # 2 Nov: the connector could not tell a Bot Chat turn the queue turned away from one that ran.
        import tempfile
        from pathlib import Path
        f = Fleet()
        notes = Path(tempfile.mkdtemp()) / "turned-away"
        rift = gate(f, "rift")
        wisp = TurnGate(ready_policy(), "wisp", f.ask, wait_seconds=0, sleep=lambda s: None, turned_away_dir=notes)
        self.assertIsNone(rift.before_llm(session_id="r1", task_id="12"))
        self.assertIn("not admitted", wisp.before_llm(session_id="sess-1", task_id="9")["context"])
        note = json.loads((notes / "sess-1.json").read_text(encoding="utf-8"))
        self.assertIn("running", note["reason"])
        self.assertTrue(note["at"].endswith("+00:00"))
        rift.after_turn(session_id="r1")
        (notes / "sess-1.json").unlink()
        self.assertIsNone(wisp.before_llm(session_id="sess-1", task_id="9"))
        self.assertEqual(list(notes.iterdir()), [], "a turn that runs leaves no note")
        # tw-policy down: the same note, with why.
        def down(broker, op, args):
            raise BrokerRefused("the broker at /run/tw/policy.sock is not answering")
        g = TurnGate(ready_policy(), "rift", down, wait_seconds=0, sleep=lambda s: None, turned_away_dir=notes)
        g.before_llm(session_id="sess-2")
        self.assertIn("not answering", json.loads((notes / "sess-2.json").read_text(encoding="utf-8"))["reason"])
        # A session id that is not a plain name leaves nothing, and a sweep is owed rather than noted.
        g.before_llm(session_id="../x")
        b = TurnGate(ready_policy(), "bramble", down, wait_seconds=0, sleep=lambda s: None, turned_away_dir=notes,
                     owed_file=notes.parent / "owed")
        b.before_llm(session_id="sess-3", platform="webhook")
        self.assertEqual(sorted(p.name for p in notes.iterdir()), ["sess-2.json"])

    def test_registering_puts_the_notes_in_the_profile(self):
        from pathlib import Path
        g = brokers.register(Ctx("rift"), ready_policy(), "rift", Fleet().ask)
        self.assertEqual(g.turned_away_dir.parts[-3:], brokers.TURNED_AWAY_DIR)
        self.assertEqual(g.turned_away_dir.parents[2], Path(brokers.__file__).resolve().parents[2])

    def test_the_connector_asks_whether_a_turn_would_be_admitted_and_spends_nothing(self):
        f = Fleet()
        self.assertEqual(f.ask("policy", "turn_room", {"bot": "wisp"}), {"room": True, "why_not": ""})
        rift = gate(f, "rift")
        self.assertIsNone(rift.before_llm(session_id="r1", task_id="12"))
        tickets = f.policy.db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        got = f.ask("policy", "turn_room", {"bot": "wisp"})
        self.assertFalse(got["room"])
        self.assertIn("running", got["why_not"])
        self.assertEqual(f.policy.db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], tickets)
        self.assertEqual(set(f.policy.ops()["turn_room"][1]), {"hermes"})

    def test_19_a_turn_with_no_ticket_does_nothing(self):
        f = Fleet()
        g = gate(f, "echo")
        self.assertIn("no ticket", g.before_tool("terminal", session_id="never-asked"))

    def test_a_broker_that_is_down_fails_closed(self):
        def down(broker, op, args):
            raise BrokerRefused("the broker at /run/tw/policy.sock is not answering")
        g = TurnGate(ready_policy(), "rift", down, wait_seconds=0, sleep=lambda s: None)
        self.assertIn("not answering", g.before_llm(session_id="s")["context"])
        self.assertIn("not answering", g.before_tool("read_file", session_id="s"))

    def test_a_stopped_turn_gives_its_ticket_back(self):
        f = Fleet()
        rift, wisp = gate(f, "rift"), gate(f, "wisp")
        rift.before_llm(session_id="r1", task_id="12")
        rift.stopped(session_key="r1")
        self.assertIsNone(wisp.before_llm(session_id="w1", task_id="9"))

    def test_waiting_asks_again_with_the_same_ticket(self):
        f = Fleet()
        gate(f, "rift").before_llm(session_id="r1")
        ticks = iter(range(100))
        g = TurnGate(ready_policy(), "wisp", f.ask, wait_seconds=3, sleep=lambda s: None, clock=lambda: next(ticks))
        g.before_llm(session_id="w1", task_id="9")
        asks = [a for b, op, a in f.seen if op == "admit" and a["bot"] == "wisp"]
        self.assertGreater(len(asks), 1)
        first = f.policy.db.execute("SELECT id FROM tickets WHERE bot='wisp'").fetchall()
        self.assertEqual(len(first), 1)
        self.assertEqual({a.get("ticket") for a in asks[1:]}, {first[0][0]})


class Waiting(unittest.TestCase):
    """1 Nov, 3:43 PM: the board review rerun, a Hermes cron turn, was refused
    at once and never ran, and each refusal left a waiting ticket behind.
    3:49 PM: Bramble's pick in the Workbench room answered "Queued, not
    started" and ended; nothing ran it when the queue cleared at about 3:59."""

    def clocked(self, f, bot, **kw):
        now = [0.0]
        return TurnGate(ready_policy(), bot, f.ask,
                        sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0], **kw), now

    def test_a_scheduled_turn_waits_for_its_ticket(self):
        f = Fleet()
        rift = gate(f, "rift")
        rift.before_llm(session_id="r1", task_id="12")
        g, now = self.clocked(f, "bramble")
        asks = []
        real = f.ask

        def ask(broker, op, args):
            asks.append(op)
            if op == "admit" and len(asks) == 4:
                rift.after_turn(session_id="r1")  # the slot frees while it waits
            return real(broker, op, args)
        g.ask = ask
        self.assertIsNone(g.before_llm(session_id="cron_x", task_id="board", platform="cron"))
        self.assertIsNone(g.before_tool("read_file", session_id="cron_x"))
        self.assertGreater(now[0], 0, "it waited")
        self.assertEqual(f.policy.db.execute("SELECT COUNT(*) FROM tickets WHERE bot='bramble'").fetchone()[0], 1,
                         "one ticket, asked again while it waited")

    def test_a_room_turn_waits_for_its_slot_too(self):
        f = Fleet()
        rift = gate(f, "rift")
        rift.before_llm(session_id="r1", task_id="12")
        g, now = self.clocked(f, "bramble")
        asks = []
        real = f.ask

        def ask(broker, op, args):
            asks.append(op)
            if op == "admit" and len(asks) == 8:
                rift.after_turn(session_id="r1")
            return real(broker, op, args)
        g.ask = ask
        self.assertIsNone(g.before_llm(session_id="20261031_160420_c3caf4", platform="desktop"))
        self.assertIsNone(g.before_tool("read_file", session_id="20261031_160420_c3caf4"))
        self.assertGreater(now[0], 20, "it waited past the old 20 seconds")

    def test_every_turn_waits_as_long_by_default(self):
        from thornswood_guard.brokers import TURN_WAIT
        self.assertEqual(TurnGate(ready_policy(), "bramble", Fleet().ask).wait_seconds, TURN_WAIT)

    def test_a_scheduled_turn_that_never_gets_one_says_how_long_and_gives_its_place_back(self):
        f = Fleet()
        gate(f, "rift").before_llm(session_id="r1")
        g, now = self.clocked(f, "bramble", wait_seconds=30)
        out = g.before_llm(session_id="cron_y", platform="cron")
        self.assertIn("waited 30 seconds", out["context"])
        self.assertIn("did not start", out["context"])
        g.after_turn(session_id="cron_y")
        left = f.policy.db.execute("SELECT state FROM tickets WHERE bot='bramble'").fetchall()
        self.assertEqual([r[0] for r in left], ["done"], "no waiting ticket left behind")

    def test_the_wait_fits_inside_hermess_hook_bound(self):
        from thornswood_guard.brokers import TURN_WAIT
        from thornswood_guard.profile_config import HOOK_TIMEOUT, build, build_gateway
        self.assertLess(TURN_WAIT + 30, HOOK_TIMEOUT)
        self.assertLessEqual(HOOK_TIMEOUT, 600, "Hermes's maximum")
        p = policy()
        self.assertEqual(build(p, "bramble")["plugins"]["hook_callback_timeout"], HOOK_TIMEOUT)
        self.assertEqual(build_gateway(p)["plugins"]["hook_callback_timeout"], HOOK_TIMEOUT)


class Tools(unittest.TestCase):
    def test_14_the_bot_comes_from_the_profile_not_the_model(self):
        f = Fleet()
        ctx = Ctx("rift")
        brokers.register(ctx, ready_policy(), "rift", ask=f.ask)
        _, _, handler = ctx.tools["tw_git"]
        out = json.loads(handler({"op": "merge", "bot": "bramble", "repo": "ThornswoodRP/Thornswood", "number": 42}))
        self.assertTrue(out["ok"])
        self.assertEqual(f.seen[-1], ("git", "merge", {"bot": "rift", "repo": "ThornswoodRP/Thornswood", "number": 42}))

    def test_tw_decide_names_the_bot_from_the_profile(self):
        f = Fleet()
        ctx = Ctx("echo")
        brokers.register(ctx, ready_policy(), "echo", ask=f.ask)
        out = json.loads(ctx.tools["tw_decide"][2]({"op": "my_cards", "bot": "bramble"}))
        self.assertTrue(out["ok"])
        self.assertEqual(f.seen[-1], ("decide", "my_cards", {"bot": "echo"}))
        self.assertFalse(json.loads(ctx.tools["tw_decide"][2]({"op": "answer"}))["ok"], "no Bot answers a card")

    def test_only_the_named_ops(self):
        f = Fleet()
        ctx = Ctx("rift")
        brokers.register(ctx, ready_policy(), "rift", ask=f.ask)
        out = json.loads(ctx.tools["tw_files"][2]({"op": "shell", "repo": "x", "task": "y"}))
        self.assertFalse(out["ok"])
        self.assertEqual(f.seen, [])

    def test_nothing_is_registered_before_the_brokers_are_ready(self):
        ctx = Ctx("rift")
        self.assertIsNone(brokers.register(ctx, ready_policy(ready=False), "rift", ask=lambda *a: None))
        self.assertEqual(ctx.hooks, {})
        self.assertEqual(ctx.tools, {})

    def test_the_hooks_hermes_calls(self):
        ctx = Ctx("bramble")
        brokers.register(ctx, ready_policy(), "bramble", ask=Fleet().ask)
        self.assertEqual(sorted(ctx.hooks), ["agent_loop_stopped", "on_session_end", "pre_llm_call"])
        self.assertEqual(sorted(ctx.tools), ["tw_decide", "tw_files", "tw_git"])
        for name, (toolset, schema, _) in ctx.tools.items():
            self.assertEqual(toolset, "thornswood")
            self.assertEqual(schema["name"], name)
            self.assertFalse(schema["parameters"]["additionalProperties"])
            self.assertNotIn("bot", schema["parameters"]["properties"])

    def test_tier_b_takes_the_shell_and_file_writes_away(self):
        p = ready_policy(tier_b=True)
        for tool, args in (("terminal", {"command": "ls"}), ("write_file", {"path": "a"}), ("patch", {"path": "a"}),
                           ("execute_code", {})):
            with self.subTest(tool=tool):
                self.assertFalse(check_tool(p, "rift", tool, args).allowed)
        self.assertTrue(check_tool(p, "rift", "read_file", {"path": "README.md"}).allowed)
        self.assertTrue(check_tool(policy(), "rift", "terminal", {"command": "ls"}).allowed)


if __name__ == "__main__":
    unittest.main()


class OwedSweeps(unittest.TestCase):
    """1 Nov, 10:02 PM on: Bramble's wakes were refused with "already took its
    20 tickets this hour" and dropped. A sweep the queue turns away is owed in
    tw-policy; a sweep that runs to its end pays it."""

    def full(self, f):
        for i in range(20):
            t = f.policy.admit("hermes", {"bot": "rift", "task": f"t{i}"})["ticket"]
            f.policy.finish("hermes", {"bot": "rift", "ticket": t})

    def owed(self, f):
        return f.policy.wake_due("hermes", {"bot": "bramble"})

    def test_a_refused_wake_is_owed_not_dropped(self):
        f = Fleet()
        self.full(f)
        g = gate(f, "bramble")
        out = g.before_llm(session_id="w1", task_id="wake", platform="webhook")
        self.assertIn("is owed", out["context"])
        self.assertNotIn("nothing starts it again", out["context"])
        self.assertIn("Refused by the Thornswood queue", g.before_tool("tw_git", session_id="w1"))
        g.after_turn(session_id="w1")
        owed = self.owed(f)
        self.assertTrue(owed["owed"])
        self.assertIn("20 an hour", owed["reason"])

    def test_a_wake_that_waited_out_its_time_is_owed_and_gives_its_ticket_back(self):
        f = Fleet()
        gate(f, "rift").before_llm(session_id="r1")
        g = gate(f, "bramble")
        out = g.before_llm(session_id="w1", platform="webhook")
        self.assertIn("is owed", out["context"])
        g.after_turn(session_id="w1")
        self.assertTrue(self.owed(f)["owed"])
        left = f.policy.db.execute("SELECT state FROM tickets WHERE bot='bramble'").fetchall()
        self.assertEqual([r[0] for r in left], ["done"], "its session has no next turn")

    def test_a_sweep_that_runs_pays_and_a_stopped_one_does_not(self):
        f = Fleet()
        f.policy.owe_wake("hermes", {"bot": "bramble", "reason": "r"})
        g = gate(f, "bramble")
        self.assertIsNone(g.before_llm(session_id="w1", platform="webhook"))
        g.stopped(session_key="w1")
        self.assertTrue(self.owed(f)["owed"], "a stopped sweep did not do the work")
        self.assertIsNone(g.before_llm(session_id="w2", platform="webhook"))
        g.after_turn(session_id="w2")
        self.assertFalse(self.owed(f)["owed"])

    def test_the_owed_wake_check_pays_and_other_cron_jobs_do_not(self):
        from thornswood_guard.owed_wake import PROMPT
        f = Fleet()
        f.policy.owe_wake("hermes", {"bot": "bramble", "reason": "r"})
        g = gate(f, "bramble")
        self.assertIsNone(g.before_llm(session_id="cron_b_1", platform="cron", user_message="Board review, weekly"))
        g.after_turn(session_id="cron_b_1")
        self.assertTrue(self.owed(f)["owed"])
        # Hermes puts its cron hint and the script's output first.
        message = "[cron hint]\n## Script Output\n...\n```\n1 wake(s)\n```\n\n" + PROMPT
        self.assertIsNone(g.before_llm(session_id="cron_o_1", platform="cron", user_message=message))
        g.after_turn(session_id="cron_o_1")
        self.assertFalse(self.owed(f)["owed"])

    def test_a_refused_owed_wake_check_stays_owed(self):
        from thornswood_guard.owed_wake import PROMPT
        f = Fleet()
        f.policy.owe_wake("hermes", {"bot": "bramble", "reason": "r"})
        self.full(f)
        g = gate(f, "bramble")
        self.assertIn("is owed", g.before_llm(session_id="c", platform="cron", user_message=PROMPT)["context"])
        g.after_turn(session_id="c")
        self.assertEqual(self.owed(f)["refused"], 2)

    def test_a_wake_during_a_sweep_is_owed_and_runs_after_it(self):
        """2 Nov, 3:57:55 and 3:58:00 PM: two of Bramble's webhook turns ran the
        whole sweep at once, and both revised #5458's card."""
        import copy
        from thornswood_guard.owed_wake import PROMPT
        from test_policy_service import Clock
        f = Fleet()
        data = copy.deepcopy(f.policy.policy.data)
        data["admission"]["stage"] = "after"
        clock = Clock()
        f.policy = policy_service(data=data, clock=clock)
        g = gate(f, "bramble")
        self.assertIsNone(g.before_llm(session_id="w1", platform="webhook"))
        clock.advance(seconds=5)
        out = g.before_llm(session_id="w2", platform="webhook")
        self.assertIn("one sweep runs at a time", out["context"])
        self.assertIn("is owed", out["context"])
        self.assertIn("Refused by the Thornswood queue", g.before_tool("tw_decide", session_id="w2"))
        self.assertIsNone(g.before_tool("tw_decide", session_id="w1"))
        g.after_turn(session_id="w2")
        self.assertFalse(self.owed(f)["due"], "not while the first sweep runs")
        clock.advance(seconds=60)
        g.after_turn(session_id="w1")
        owed = self.owed(f)
        self.assertTrue(owed["due"], "the first sweep may have read the state before the second wake")
        self.assertIsNone(g.before_llm(session_id="cron_o_1", platform="cron", user_message=PROMPT))
        g.after_turn(session_id="cron_o_1")
        self.assertFalse(self.owed(f)["owed"])

    def test_only_brambles_wakes_are_owed(self):
        f = Fleet()
        self.full(f)
        for bot, platform in (("rift", "webhook"), ("bramble", "desktop"), ("bramble", "discord")):
            out = gate(f, bot).before_llm(session_id=f"{bot}-{platform}", platform=platform)
            self.assertIn("nothing starts it again", out["context"], (bot, platform))
        self.assertFalse(self.owed(f)["owed"])

    def test_with_tw_policy_down_the_debt_waits_in_the_profile_and_the_check_hands_it_over(self):
        import tempfile
        from pathlib import Path
        from thornswood_guard import owed_wake

        home = Path(tempfile.mkdtemp())

        def down(broker, op, args):
            raise BrokerRefused("the broker at /run/tw/policy.sock is not answering")
        g = TurnGate(ready_policy(), "bramble", down, wait_seconds=0, sleep=lambda s: None,
                     owed_file=home.joinpath(*brokers.OWED_FILE))
        self.assertIn("is owed", g.before_llm(session_id="w", platform="webhook")["context"])
        self.assertIn("not answering", home.joinpath(*brokers.OWED_FILE).read_text(encoding="utf-8"))
        f = Fleet()
        lines = owed_wake.check(home, "bramble", f.ask)
        self.assertNotEqual(lines[-1], owed_wake.SLEEP, "the debt is due once tw-policy answers")
        self.assertFalse(home.joinpath(*brokers.OWED_FILE).exists())
        self.assertIn("not answering", self.owed(f)["reason"])

    def test_registering_gives_only_bramble_an_outbox_in_its_profile(self):
        from pathlib import Path
        for bot in ("bramble", "rift"):
            ctx = Ctx(bot)
            g = brokers.register(ctx, ready_policy(), bot, Fleet().ask)
            if bot == "bramble":
                self.assertEqual(g.owed_file.parts[-3:], brokers.OWED_FILE)
                self.assertEqual(g.owed_file.parents[2], Path(brokers.__file__).resolve().parents[2])
            else:
                self.assertIsNone(g.owed_file)
