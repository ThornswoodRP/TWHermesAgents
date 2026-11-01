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


class Tools(unittest.TestCase):
    def test_14_the_bot_comes_from_the_profile_not_the_model(self):
        f = Fleet()
        ctx = Ctx("rift")
        brokers.register(ctx, ready_policy(), "rift", ask=f.ask)
        _, _, handler = ctx.tools["tw_git"]
        out = json.loads(handler({"op": "merge", "bot": "bramble", "repo": "ThornswoodRP/Thornswood", "number": 42}))
        self.assertTrue(out["ok"])
        self.assertEqual(f.seen[-1], ("git", "merge", {"bot": "rift", "repo": "ThornswoodRP/Thornswood", "number": 42}))

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
        self.assertEqual(sorted(ctx.tools), ["tw_files", "tw_git"])
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
