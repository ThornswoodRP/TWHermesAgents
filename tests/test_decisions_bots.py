"""The Bots' route to the decision core (#49): a Bot files a card and reads
Patrick's answer through the core, as itself, holding no credential.

Until 1 Nov the core had no route a Bot could call, so Bramble asked for its
pick in the Workbench room, where nothing counts as Patrick's answer.
"""

import configparser
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from decisions_helpers import OPTIONS, World
from helpers import REPO, policy

from thornswood_decisions.bots import CALLERS, BotRoute
from thornswood_decisions.chat import Chat

DEPLOY = REPO / "deploy" / "decisions"


def card_args(**kw):
    a = {"kind": "question", "title": "Pick the next issue", "body": "What is wrong, what was measured, each option.",
         "options": OPTIONS, "recommendation": "a", "scope_of_yes": "Only #5033, by Echo."}
    a.update(kw)
    return a


@unittest.skipUnless(sys.platform.startswith("linux"),
                     "the route's ops are served by the brokers' wire, which is Linux code (Unix sockets, pwd)")
class Route(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.route = BotRoute(self.w.path, "test")
        self.addCleanup(self.route.close)
        self.ops = self.route.ops()

    def call(self, op, bot, **args):
        handler, callers = self.ops[op]
        self.assertEqual(callers, {"hermes", "root"})
        return json.loads(json.dumps(handler("hermes", json.loads(json.dumps(dict(args, bot=bot))))))

    def test_bramble_files_a_card_and_reads_patricks_answer(self):
        card = self.call("open_card", "bramble", **card_args(links=[{"kind": "issue", "ref": "#5033"}]))
        self.assertEqual(card["status"], "pending")
        got = self.call("get_card", "bramble", decision_id=card["id"])
        self.assertEqual(got["states"]["decision_accepted"], False)
        self.assertNotIn("answer_text", got)
        receipt = self.w.core.answer(self.w.patrick(device=self.w.device()), **World.answer_args(card))
        got = self.call("get_card", "bramble", decision_id=card["id"])
        self.assertEqual(got["status"], "resolved")
        self.assertEqual(got["receipt"]["response_id"], receipt["response_id"])
        self.assertTrue(got["states"]["decision_accepted"])
        # The same words the connector tells the Bot in the room, DECIDED.md steps included.
        cx = self.w.store.db.execute("SELECT id FROM connector_outbox").fetchone()["id"]
        self.assertEqual(got["answer_text"], Chat._told_text(self.w.store.db, cx))
        self.assertIn("Patrick answered the card", got["answer_text"])
        self.assertIn("docs/DECIDED.md", got["answer_text"])
        self.call("report_work_done", "bramble", decision_id=card["id"], detail="merged as abc123")
        self.assertTrue(self.call("get_card", "bramble", decision_id=card["id"])["states"]["work_done"])

    def test_a_specialist_asks_only_about_its_assigned_task(self):
        with self.assertRaisesRegex(Exception, "specialist_needs_task"):
            self.call("open_card", "echo", **card_args())
        with self.assertRaisesRegex(Exception, "not_your_task"):
            self.call("open_card", "echo", **card_args(task="5033"))
        self.call("assign_task", "bramble", task="5033", assign_to="echo")
        card = self.call("open_card", "echo", **card_args(task="5033"))
        self.assertEqual(card["task"], "5033")
        with self.assertRaisesRegex(Exception, "specialist_question_only"):
            self.call("open_card", "echo", **card_args(kind="change", task="5033"))
        # Its own card it reads; Wisp does not see it.
        self.assertEqual(self.call("get_card", "echo", decision_id=card["id"])["id"], card["id"])
        with self.assertRaisesRegex(Exception, "no_card"):
            self.call("get_card", "wisp", decision_id=card["id"])
        self.assertEqual([c["id"] for c in self.call("my_cards", "echo")], [card["id"]])
        self.assertEqual(self.call("my_cards", "wisp"), [])

    def test_only_bramble_assigns_closes_and_reports(self):
        card = self.call("open_card", "bramble", **card_args())
        for op, args in (("assign_task", {"task": "1", "assign_to": "rift"}),
                         ("close_card", {"decision_id": card["id"], "status": "cancelled", "reason": "x"}),
                         ("report_work_done", {"decision_id": card["id"], "detail": "x"})):
            with self.subTest(op=op), self.assertRaisesRegex(Exception, "not_bramble"):
                self.call(op, "rift", **args)
        closed = self.call("close_card", "bramble", decision_id=card["id"], status="cancelled", reason="not needed")
        self.assertEqual(closed["status"], "cancelled")

    def test_no_bot_answers_a_card_and_no_op_does(self):
        self.assertNotIn("answer", self.ops)
        self.assertEqual(sorted(self.ops), sorted(BotRoute.OPS))

    def test_the_bot_must_be_one_of_the_six(self):
        for bot in ("patrick", "operator", "", None, "Bramble"):
            with self.subTest(bot=bot), self.assertRaisesRegex(Exception, "not_a_bot"):
                self.call("my_cards", bot)

    def test_a_revision_and_my_cards(self):
        card = self.call("open_card", "bramble", **card_args())
        rev = self.call("revise_card", "bramble", decision_id=card["id"], expected_revision=1,
                        **{k: v for k, v in card_args(title="Pick the next issue, measured again").items()
                           if k not in ("kind",)})
        self.assertEqual(rev["revision"], 2)
        self.assertEqual([c["revision"] for c in self.call("my_cards", "bramble")], [2])

    def test_writes_off_says_so(self):
        w = World(writes=False)
        self.addCleanup(w.close)
        r = BotRoute(w.path, "test")
        self.addCleanup(r.close)
        with self.assertRaisesRegex(Exception, "decision_write_off"):
            r.ops()["open_card"][0]("hermes", dict(card_args(), bot="bramble"))


@unittest.skipUnless(sys.platform.startswith("linux"), "the route's socket checks the caller's Unix user")
class Socket(unittest.TestCase):
    def test_a_caller_the_route_does_not_know_is_refused_and_hermes_is_served(self):
        from thornswood_brokers import wire
        w = World()
        self.addCleanup(w.close)
        route = BotRoute(w.path, "test")
        self.addCleanup(route.close)
        me = wire.user_name(os.getuid())
        sock = Path(tempfile.mkdtemp()) / "bots.sock"
        # As served, only hermes and root may call; this test's own user is neither
        # unless it runs as root, so it sees the refusal or, as root, the answer.
        server = wire.Server(sock, route.ops(), mode=0o660)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        if me in ("hermes", "root"):
            self.assertEqual(wire.call(sock, "my_cards", {"bot": "bramble"}), [])
        else:
            with self.assertRaisesRegex(wire.Refused, f"{me} may not ask for my_cards"):
                wire.call(sock, "my_cards", {"bot": "bramble"})


class Deploy(unittest.TestCase):
    def unit(self):
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        cp.read_string((DEPLOY / "tw-decide-bots.service").read_text(encoding="utf-8"))
        return cp["Service"]

    def test_it_runs_as_the_cores_user_with_no_network(self):
        u = self.unit()
        self.assertEqual(u["User"], "tw-decide")
        self.assertEqual(u["SupplementaryGroups"], "tw-decide-bots")
        self.assertEqual(u["PrivateNetwork"], "yes")
        self.assertEqual(u["RestrictAddressFamilies"], "AF_UNIX")
        self.assertEqual(u["ReadWritePaths"], "/srv/tw/decide")

    def test_the_socket_is_the_policys(self):
        sock = policy().data["brokers"]["sockets"]["decide"]
        u = self.unit()
        self.assertIn(f"--socket {sock['path']}", u["ExecStart"])
        self.assertIn(f"--group {sock['group']}", u["ExecStart"])
        self.assertEqual(u["RuntimeDirectory"], "tw/decide-bots")
        self.assertTrue(sock["path"].startswith("/run/tw/decide-bots/"))
        self.assertLessEqual(set(CALLERS), set(sock["clients"]))  # every op's callers (BotRoute.ops)

    def test_the_install_checks_the_group_and_the_socket(self):
        text = (DEPLOY / "install.sh").read_text(encoding="utf-8")
        self.assertIn('[[ "$members" == hermes ]]', text)
        self.assertIn("tw-decide-bots.service", text)
        self.assertIn('"660 tw-decide-bots"', text)
        main = (REPO / "deploy" / "install.sh").read_text(encoding="utf-8")
        self.assertIn("tw-decide-bots", main.split("# 1. Groups and users.")[1].split("# 2.")[0])


if __name__ == "__main__":
    unittest.main()
