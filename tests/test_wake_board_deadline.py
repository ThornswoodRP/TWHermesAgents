"""The canonical whole-board read must fit its own RPC deadline."""
import contextlib
import io
import inspect
import unittest
from unittest.mock import patch

from helpers import policy
from thornswood_guard import owed_wake
from thornswood_guard.brokers import BrokerRefused, socket_caller


class BoardDeadline(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def transport(self, policy, *, timeout=inspect.signature(socket_caller).parameters["timeout"].default):
        def ask(broker, op, args):
            self.calls.append((broker, op, timeout))
            # Retained live single-call duration was33.096 seconds. The
            # logical clock exercises deadline selection without wall sleeps.
            delay = 33.096 if op == "board" else 10.001
            if delay > timeout:
                raise BrokerRefused("representative transport deadline expired")
            return {"groups": {"echo": {"open": 1, "issues": [{"number": 12}]}}}
        return ask

    def test_measured_board_completes_while_control_and_ordinary_reads_keep_short_bound(self):
        with patch("thornswood_guard.brokers.socket_caller", side_effect=self.transport):
            ask = owed_wake._wake_caller(policy())
            got = ask("git", "board", {"bot": "bramble", "eligible": True,
                                        "groups": {"echo": {"areas": ["Website"]}}})
            self.assertEqual(got["groups"]["echo"]["open"], 1)
            for broker, op, args in (("policy", "turn_room", {}), ("decide", "queued_for", {}),
                                     ("git", "read", {}), ("git", "board", {"eligible": True}),
                                     ("git", "board", {"eligible": False, "groups": {"echo": {}}})):
                with self.subTest(broker=broker, op=op), self.assertRaises(BrokerRefused):
                    ask(broker, op, args)
        self.assertEqual(self.calls[0][2], 120.0)
        self.assertTrue(all(timeout == 10.0 for _, _, timeout in self.calls[1:]))

    def test_main_uses_the_board_caller_and_no_retry_is_added(self):
        requests = []
        def check(home, bot, ask, **kwargs):
            requests.append(ask("git", "board", {"eligible": True, "groups": {"echo": {}}}))
            return [owed_wake.SLEEP]
        with patch("thornswood_guard.policy.Policy.load", return_value=policy()), \
                patch("thornswood_guard.brokers.socket_caller", side_effect=self.transport), \
                patch.object(owed_wake, "check", side_effect=check), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(owed_wake.main(), 0)
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertIn(owed_wake.SLEEP, out.getvalue())
