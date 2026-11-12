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

    def test_a_bot_chat_message_the_queue_turned_away_is_kept_in_the_decision_core(self):
        # 9 Nov 2026: a message another Bot sent with message_agent was turned away and gone.
        import tempfile
        from pathlib import Path
        f = Fleet()
        notes = Path(tempfile.mkdtemp()) / "turned-away"
        rift = gate(f, "rift")
        sprocket = TurnGate(ready_policy(), "sprocket", f.ask, wait_seconds=0, sleep=lambda s: None,
                            turned_away_dir=notes)
        self.assertIsNone(rift.before_llm(session_id="r1", task_id="12"))
        out = sprocket.before_llm(session_id="sess-bc", task_id="sess-bc", platform="cli",
                                  user_message="@Sprocket, why does the launcher not start?")
        self.assertIn("The message is kept", out["context"])
        kept = [a for b, op, a in f.seen if (b, op) == ("decide", "keep_turned_away")]
        self.assertEqual(len(kept), 1)
        self.assertEqual((kept[0]["bot"], kept[0]["text"], kept[0]["session"]),
                         ("sprocket", "@Sprocket, why does the launcher not start?", "sess-bc"))
        self.assertIn("running", kept[0]["reason"])
        self.assertTrue(json.loads((notes / "sess-bc.json").read_text(encoding="utf-8"))["kept"],
                        "the connector reads that it was kept and puts nothing back itself")
        # A gateway turn (a room, a webhook) is not a Bot Chat message: nothing is kept.
        f.seen.clear()
        out = sprocket.before_llm(session_id="room-1", platform="raft", user_message="hello room")
        self.assertIn("nothing starts it again", out["context"])
        self.assertEqual([op for b, op, a in f.seen if b == "decide"], [])
        self.assertFalse(json.loads((notes / "room-1.json").read_text(encoding="utf-8"))["kept"])

    def test_a_bots_message_into_a_bot_chat_made_in_desktop_is_kept(self):
        # 11 Nov 2026: Magpie's and Rift's hand-backs reached Bramble's Bot Chat,
        # a desktop session, while the Bots reloaded; neither was kept, and both
        # were told to send it again after their own turns had ended.
        f = Fleet()
        rift = gate(f, "rift")
        self.assertIsNone(rift.before_llm(session_id="r1", task_id="12"))
        bramble = TurnGate(ready_policy(), "bramble", f.ask, wait_seconds=0, sleep=lambda s: None)
        text = "Message from magpie (@magpie): #5765: blocked on the push, and no PR exists."
        out = bramble.before_llm(session_id="20261101_154903_0aec22", platform="desktop", user_message=text)
        self.assertIn("The message is kept", out["context"])
        kept = [a for b, op, a in f.seen if (b, op) == ("decide", "keep_turned_away")]
        self.assertEqual([(k["bot"], k["text"]) for k in kept], [("bramble", text)])
        self.assertFalse(self.owed_any(f), "a message is kept, not owed as a sweep")

    def test_a_kept_turn_gives_its_ticket_back_and_holds_nobody_in_line(self):
        # 12 Nov 2026, 9:15 PM Mountain on: kept turns of Bramble's Bot Chat
        # left their tickets waiting, nothing asked with them again, and the
        # line held every Bot's turn behind them until each went silent for
        # ten minutes, so every Bot sat idle.
        f = Fleet()
        rift = gate(f, "rift")
        self.assertIsNone(rift.before_llm(session_id="r1", task_id="12"))
        bramble = TurnGate(ready_policy(), "bramble", f.ask, wait_seconds=0, sleep=lambda s: None)
        out = bramble.before_llm(session_id="20261101_154903_0aec22", platform="desktop",
                                 user_message="Message from sprocket (@sprocket): #4431 is done.")
        self.assertIn("The message is kept", out["context"])
        self.assertIn("Refused by the Thornswood queue", bramble.before_tool("terminal", session_id="20261101_154903_0aec22"))
        bramble.after_turn(session_id="20261101_154903_0aec22")
        self.assertEqual(f.policy.db.execute("SELECT state FROM tickets WHERE bot='bramble'").fetchall(), [("done",)])
        rift.after_turn(session_id="r1")
        self.assertIsNone(gate(f, "wisp").before_llm(session_id="w1", task_id="9"),
                          "the next Bot is admitted at once, not after ten minutes")

    def test_a_message_waiting_when_turns_are_held_for_a_reload_is_kept_at_once(self):
        # 12 Nov 2026, 10:09 PM Mountain: Magpie's message to Sprocket about
        # #5830 waited in Sprocket's Bot Chat; the 10:11 PM reload ended it
        # unkept, and Magpie had been told not to send it again.
        f = Fleet()
        f.policy.hold_turns("root", {"seconds": 900})
        ticks = iter(range(10000))
        g = TurnGate(ready_policy(), "sprocket", f.ask, wait_seconds=540, sleep=lambda s: None,
                     clock=lambda: next(ticks))
        out = g.before_llm(session_id="20261030_153904_d46b76", platform="desktop",
                           user_message="Message from magpie (@magpie): #5830 is in your code.")
        self.assertIn("The message is kept", out["context"])
        self.assertIn("reloaded", out["context"])
        self.assertNotIn("waited", out["context"], "it did not wait out its 540 seconds")
        asks = [a for b, op, a in f.seen if op == "admit"]
        self.assertEqual(len(asks), 1, "kept at the first answer that says new turns are held")
        g.after_turn(session_id="20261030_153904_d46b76")
        self.assertEqual(f.policy.db.execute("SELECT state FROM tickets WHERE bot='sprocket'").fetchall(), [("done",)])
        # A sweep's wake is owed at once too.
        b = TurnGate(ready_policy(), "bramble", f.ask, wait_seconds=540, sleep=lambda s: None, clock=lambda: next(ticks))
        self.assertIn("is owed", b.before_llm(session_id="w", platform="webhook")["context"])
        # A turn that cannot be kept waits as before.
        f.seen.clear()
        r = TurnGate(ready_policy(), "rift", f.ask, wait_seconds=3, sleep=lambda s: None, clock=lambda: next(ticks))
        r.before_llm(session_id="room-1", platform="raft", user_message="hello room")
        self.assertGreater(len([a for b_, op, a in f.seen if op == "admit"]), 1)

    def test_a_turn_turned_away_and_not_kept_keeps_its_place(self):
        f = Fleet()
        rift = gate(f, "rift")
        self.assertIsNone(rift.before_llm(session_id="r1", task_id="12"))
        wisp = gate(f, "wisp")
        self.assertIn("nothing starts it again", wisp.before_llm(session_id="room-1", platform="raft",
                                                                  user_message="hello room")["context"])
        wisp.after_turn(session_id="room-1")
        self.assertEqual(f.policy.db.execute("SELECT state FROM tickets WHERE bot='wisp'").fetchall(), [("waiting",)])

    def owed_any(self, f):
        return [a for b, op, a in f.seen if op == "owe_wake"]

    def test_a_message_the_core_would_not_keep_says_it_needs_asking_again(self):
        f = Fleet()
        rift = gate(f, "rift")
        self.assertIsNone(rift.before_llm(session_id="r1", task_id="12"))

        def ask(broker, op, args):
            if broker == "decide":
                raise BrokerRefused("the broker at /run/tw/decide.sock is not answering")
            return f.ask(broker, op, args)
        wisp = TurnGate(ready_policy(), "wisp", ask, wait_seconds=0, sleep=lambda s: None)
        out = wisp.before_llm(session_id="w1", platform="cli", user_message="hi")
        self.assertIn("nothing starts it again", out["context"])

    def test_keeping_is_not_one_of_the_models_decide_ops(self):
        self.assertNotIn("keep_turned_away", brokers.DECIDE_OPS)

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

    def test_a_waiting_turn_tw_policy_then_refuses_gives_its_ticket_back(self):
        # 12 Nov 2026, 9:14 PM Mountain: the install restarted tw-policy while
        # two of Bramble's turns waited out the hold. Each was refused and
        # forgot its ticket, and the two tickets held every Bot's turn in line
        # for ten minutes after the hold ended.
        f = Fleet()
        rift = gate(f, "rift")
        rift.before_llm(session_id="r1", task_id="12")
        bounce = {"on": False}

        def ask(broker, op, args):
            if bounce["on"] and op == "admit":
                raise BrokerRefused("the broker at /run/tw/policy/policy.sock is not answering")
            return f.ask(broker, op, args)
        wisp = TurnGate(ready_policy(), "wisp", ask, wait_seconds=0, sleep=lambda s: None)
        self.assertIn("not admitted", wisp.before_llm(session_id="w1", task_id="9")["context"])
        bounce["on"] = True
        self.assertIn("not answering", wisp.before_llm(session_id="w1", task_id="9")["context"])
        bounce["on"] = False
        wisp.after_turn(session_id="w1")
        self.assertEqual(f.policy.db.execute("SELECT state FROM tickets WHERE bot='wisp'").fetchall(), [("done",)])
        rift.after_turn(session_id="r1")
        self.assertIsNone(gate(f, "echo").before_llm(session_id="e1", task_id="3"),
                          "no ticket of a refused turn holds the line")

    def test_an_admitted_turn_whose_touch_is_refused_gives_its_ticket_back(self):
        f = Fleet()
        bounce = {"on": False}

        def ask(broker, op, args):
            if bounce["on"] and op == "touch":
                raise BrokerRefused("the broker at /run/tw/policy/policy.sock is not answering")
            return f.ask(broker, op, args)
        rift = TurnGate(ready_policy(), "rift", ask, wait_seconds=0, sleep=lambda s: None)
        self.assertIsNone(rift.before_llm(session_id="r1", task_id="12"))
        bounce["on"] = True
        self.assertIn("not answering", rift.before_tool("read_file", session_id="r1"))
        bounce["on"] = False
        rift.after_turn(session_id="r1")
        self.assertIsNone(gate(f, "wisp").before_llm(session_id="w1", task_id="9"))

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

    def full(self, f, *bots):
        """Spend each Bot's hour of turns (50 per Bot, Patrick, 9 Nov 2026); Bramble's by default."""
        for bot in bots or ("bramble",):
            for i in range(50):
                t = f.policy.admit("hermes", {"bot": bot, "task": f"t{i}"})["ticket"]
                f.policy.finish("hermes", {"bot": bot, "ticket": t})

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
        self.assertIn("at most 50 an hour", owed["reason"])

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

    def test_the_wake_check_finds_no_room_for_a_sweep_while_one_runs(self):
        """TWHermesAgents #47: the morning and free worker wakes ask for a
        whole sweep, which waits for a running one as an owed wake does."""
        f = Fleet()
        g = gate(f, "bramble")
        self.assertIsNone(g.before_llm(session_id="w1", platform="webhook"))
        got = f.ask("policy", "turn_room", {"bot": "bramble", "sweep": True})
        self.assertFalse(got["room"])
        self.assertIn("one sweep runs at a time", got["why_not"])
        g.after_turn(session_id="w1")
        self.assertTrue(f.ask("policy", "turn_room", {"bot": "bramble", "sweep": True})["room"])

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
        self.full(f, "bramble", "rift")
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
