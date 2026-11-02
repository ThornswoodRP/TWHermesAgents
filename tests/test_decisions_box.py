"""Patrick's yes on a box card becomes a record tw-git's merge checks (#49).

A pull request that changes what runs on the live box (a workflow on the box's
runner, the gate, a verb list or a script a verb runs) merges only with a
box-change record for that PR at its exact head. Until 2 Nov only root wrote
those records and nothing carried his answer from the phone to tw-policy, so
no such PR could be merged by Bramble at all. Now Bramble opens a box card,
Patrick answers it, and box_yes has tw-policy record his yes.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

from decisions_helpers import World
from helpers import HEAD, OTHER, policy

from thornswood_decisions.store import CoreError

OPS = "ThornswoodRP/Thornswood-Ops"
YES_NO = [{"key": "yes", "label": "Merge it", "consequence": "Bramble merges #205 at this head."},
          {"key": "no", "label": "Not yet", "consequence": "Nothing merges."}]


def box_args(**kw):
    a = dict(kind="change", title="Merge Ops #205 (tunnel.yml)", body="What it changes on the box, and why.",
             options=YES_NO, recommendation="yes", risk="high",
             scope_of_yes=f"Only {OPS}#205 at {HEAD[:12]}, merged by Bramble.",
             links=[("pr", f"{OPS}#205@{HEAD}")])
    a.update(kw)
    return a


class Cards(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)

    def answer(self, card, option="yes"):
        return self.w.core.answer(self.w.patrick(device=self.w.device()), **World.answer_args(card, option=option))

    def test_his_yes_reads_back_as_the_pr_and_head(self):
        card = self.w.card(**box_args())
        self.answer(card)
        yes = self.w.core.box_yes(self.w.bramble, card["id"])
        self.assertEqual((yes["repo"], yes["pr"], yes["commit"], yes["revision"]), (OPS, 205, HEAD, 1))

    def test_the_card_carries_the_line_the_app_shows_built_from_its_binding(self):
        card = self.w.card(**box_args(scope_of_yes=f"Only {OPS}#205 at {HEAD[:12]}, and also deploy everything."))
        self.assertEqual((card["box"]["repo"], card["box"]["pr"], card["box"]["commit"]), (OPS, 205, HEAD))
        self.assertIn(f"{OPS}#205 at {HEAD[:12]} and nothing else", card["box"]["says"])
        self.assertIsNone(self.w.card()["box"])

    def test_a_card_must_say_exactly_what_a_yes_allows(self):
        bad = {
            "a question": box_args(kind="question"),
            "normal risk": box_args(risk="normal"),
            "no yes option": box_args(options=[dict(YES_NO[0], key="go"), YES_NO[1]], recommendation="go"),
            "scope without the head": box_args(scope_of_yes=f"Only {OPS}#205."),
            "scope without the pr": box_args(scope_of_yes=f"Only the tunnel at {HEAD[:12]}."),
            "short head": box_args(links=[("pr", f"{OPS}#205@{HEAD[:12]}")]),
            "two prs": box_args(links=[("pr", f"{OPS}#205@{HEAD}"), ("pr", f"{OPS}#206@{OTHER}")]),
        }
        for why, args in bad.items():
            with self.assertRaises(CoreError, msg=why) as e:
                self.w.card(**args)
            self.assertIn(e.exception.reason, ("box_binding",), why)

    def test_a_specialist_cannot_ask_for_a_box_yes(self):
        self.w.core.assign_task(self.w.bramble, "205", "rift")
        with self.assertRaises(CoreError) as e:
            self.w.card(by=self.w.rift, task="205", **box_args(kind="question"))
        self.assertIn(e.exception.reason, ("box_bramble_only",))

    def test_a_plain_pr_link_is_not_a_binding(self):
        card = self.w.card(links=[("pr", f"{OPS}#205")])
        self.answer(card, option="a")
        with self.assertRaises(CoreError) as e:
            self.w.core.box_yes(self.w.bramble, card["id"])
        self.assertEqual(e.exception.reason, "not_a_box_card")

    def test_no_or_no_answer_is_not_a_yes(self):
        card = self.w.card(**box_args())
        with self.assertRaises(CoreError) as e:
            self.w.core.box_yes(self.w.bramble, card["id"])
        self.assertEqual(e.exception.reason, "not_answered")
        self.answer(card, option="no")
        with self.assertRaises(CoreError) as e:
            self.w.core.box_yes(self.w.bramble, card["id"])
        self.assertEqual(e.exception.reason, "not_yes")

    def test_only_bramble_asks(self):
        card = self.w.card(**box_args())
        self.answer(card)
        with self.assertRaises(CoreError):
            self.w.core.box_yes(self.w.rift, card["id"])

    def test_a_revision_keeps_the_binding_and_his_yes_is_for_that_revision(self):
        card = self.w.card(**box_args())
        with self.assertRaises(CoreError) as e:
            self.w.core.revise_card(self.w.bramble, card["id"], expected_revision=1, title="t", body="b",
                                    options=YES_NO, recommendation="yes", scope_of_yes="Merge whatever.")
        self.assertEqual(e.exception.reason, "box_binding")
        card = self.w.core.revise_card(self.w.bramble, card["id"], expected_revision=1, title="t", body="b2",
                                       options=YES_NO, recommendation="yes",
                                       scope_of_yes=f"Only {OPS}#205 at {HEAD[:12]}.")
        self.answer(card)
        self.assertEqual(self.w.core.box_yes(self.w.bramble, card["id"])["revision"], 2)


@unittest.skipUnless(sys.platform.startswith("linux"),
                     "the route's ops are served by the brokers' wire, which is Linux code (Unix sockets, pwd)")
class Route(unittest.TestCase):
    """The whole path: card, Patrick's answer, box_yes, tw-policy's record,
    and the merge check tw-git makes with it."""

    def setUp(self):
        from test_policy_service import service

        from thornswood_decisions.bots import BotRoute
        self.w = World()
        self.addCleanup(self.w.close)
        self.policy_svc = service()
        # tw-policy's clock and the core's agree, as on the VM.
        self.policy_svc.clock = self.w.clock
        self.asked = []

        def ask(op, args):
            self.asked.append(op)
            return self.policy_svc.ops()[op][0]("tw-decide", json.loads(json.dumps(args)))
        self.route = BotRoute(self.w.path, "test", policy=policy(), ask_policy=ask)
        self.addCleanup(self.route.close)
        self.ops = self.route.ops()

    def call(self, op, bot, **args):
        return json.loads(json.dumps(self.ops[op][0]("hermes", json.loads(json.dumps(dict(args, bot=bot))))))

    def open(self):
        a = box_args()
        a["links"] = [{"kind": k, "ref": r} for k, r in a["links"]]
        return self.call("open_card", "bramble", **a)

    def test_his_yes_becomes_a_record_tw_git_accepts_once(self):
        card = self.open()
        self.w.core.answer(self.w.patrick(device=self.w.device()), **World.answer_args(card, option="yes"))
        out = self.call("box_yes", "bramble", decision_id=card["id"])
        self.assertTrue(out["recorded"])
        self.assertEqual((out["decision_id"], out["card_revision"], out["pr"]), (card["id"], 1, 205))
        self.assertIn("tw_git merge", out["next"])
        v = self.policy_svc.check_decision(card["id"], kind="box-change", repo=OPS, commit=HEAD, pr=205, revision=1)
        self.assertTrue(v.allowed, v.reason)
        self.assertFalse(self.policy_svc.check_decision(card["id"], kind="box-change", repo=OPS, commit=OTHER).allowed)
        # Asking again is the same record; once used it says so.
        self.assertFalse(self.call("box_yes", "bramble", decision_id=card["id"])["recorded"])
        self.policy_svc.decision_use("tw-git", {"id": card["id"]})
        self.assertIn("used already", self.call("box_yes", "bramble", decision_id=card["id"])["next"])

    def test_a_no_never_reaches_tw_policy(self):
        from thornswood_brokers.wire import Refused
        card = self.open()
        self.w.core.answer(self.w.patrick(device=self.w.device()), **World.answer_args(card, option="no"))
        with self.assertRaisesRegex(Refused, "not_yes"):
            self.call("box_yes", "bramble", decision_id=card["id"])
        self.assertEqual(self.asked, [])

    def test_a_specialist_cannot_ask(self):
        from thornswood_brokers.wire import Refused
        card = self.open()
        self.w.core.answer(self.w.patrick(device=self.w.device()), **World.answer_args(card, option="yes"))
        with self.assertRaises(Refused):
            self.call("box_yes", "wisp", decision_id=card["id"])
        self.assertEqual(self.asked, [])

    def test_a_refusal_from_tw_policy_is_said(self):
        from thornswood_brokers.wire import Refused

        def refuse(op, args):
            raise Refused("tw-decide may not ask for decision_record_box")
        self.route._ask_policy = refuse
        card = self.open()
        self.w.core.answer(self.w.patrick(device=self.w.device()), **World.answer_args(card, option="yes"))
        with self.assertRaisesRegex(Refused, "policy_refused: tw-policy did not record it"):
            self.call("box_yes", "bramble", decision_id=card["id"])


class Wiring(unittest.TestCase):
    def test_the_bots_route_may_reach_tw_policy(self):
        import configparser
        from helpers import REPO
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        cp.read_string((REPO / "deploy" / "decisions" / "tw-decide-bots.service").read_text(encoding="utf-8"))
        p = policy().data["brokers"]
        self.assertIn(p["sockets"]["policy"]["group"], cp["Service"]["SupplementaryGroups"].split())
        self.assertIn(p["users"]["decide"], p["sockets"]["policy"]["clients"])
        self.assertEqual(cp["Service"]["User"], p["users"]["decide"])

    def test_the_tool_offers_box_yes(self):
        from thornswood_guard.brokers import DECIDE_OPS, TW_DECIDE_SCHEMA
        self.assertIn("box_yes", DECIDE_OPS)
        self.assertIn("box_yes", TW_DECIDE_SCHEMA["description"])


if __name__ == "__main__":
    unittest.main()
