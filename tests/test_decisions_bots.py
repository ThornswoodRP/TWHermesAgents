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

    def test_bramble_reads_what_waits_in_the_connectors_queue_for_each_bot(self):
        # 11 Nov 2026: the wake check names a worker with no turn running as
        # stopped; a brief already queued for it is its next turn on the way.
        self.assertEqual(self.call("queued_for", "bramble"), {"bots": {}})
        self.call("assign_task", "bramble", task="ThornswoodRP/Thornswood#1", assign_to="rift", brief="Fix #1.")
        self.call("keep_turned_away", "bramble", text="Rift: #4980 is blocked, handing back.", reason="held",
                  session="s1")
        self.assertEqual(self.call("queued_for", "bramble"), {"bots": {"rift": 1, "bramble": 1}})
        self.w.store.db.execute("UPDATE connector_outbox SET status = 'sent' WHERE target = 'rift'")
        self.assertEqual(self.call("queued_for", "bramble"), {"bots": {"bramble": 1}})
        with self.assertRaisesRegex(Exception, "not_bramble"):
            self.call("queued_for", "rift")

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


OUTSIDE_OPTIONS = [
    {"key": "allow", "label": "Auto-Merge", "consequence": "rahul05ranjan's work merges like a Bot's from now on."},
    {"key": "merge", "label": "Merge", "consequence": "#5550 merges once; nothing later of theirs does."},
    {"key": "close", "label": "Close", "consequence": "Bramble closes #5517 and #5550 with thanks."},
]
OUTSIDE_BODY = ("**Safe:** yes. It changes one harness and runs nothing new.\n"
                "**Can we make it safe:** yes, it already is.\n"
                "**Of benefit:** yes. It fixes the path fence check.\n\nFixes made in #5550: ...")
OUTSIDE_LINKS = [{"kind": "pr", "ref": "outside:ThornswoodRP/Thornswood#5517@rahul05ranjan"},
                 {"kind": "pr", "ref": "carried:ThornswoodRP/Thornswood#5550"}]


@unittest.skipUnless(sys.platform.startswith("linux"),
                     "the route's ops are served by the brokers' wire, which is Linux code (Unix sockets, pwd)")
class OutsideAnswers(unittest.TestCase):
    """#132: tw-git reads Patrick's answers on Bramble's outside pull request
    cards from the core, on a socket of its own, and carries them out."""

    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.route = BotRoute(self.w.path, "test")
        self.addCleanup(self.route.close)
        self.ops = self.route.ops()

    def call(self, op, bot, **args):
        handler, _ = self.ops[op]
        return json.loads(json.dumps(handler("hermes", json.loads(json.dumps(dict(args, bot=bot))))))

    def card(self, **kw):
        return self.call("open_card", "bramble", **card_args(options=OUTSIDE_OPTIONS, recommendation="merge",
                                                             links=OUTSIDE_LINKS, body=OUTSIDE_BODY, **kw))

    def answers(self):
        handler, callers = self.route.git_ops()["outside_answers"]
        self.assertEqual(callers, {"tw-git", "root"})
        return handler("tw-git", {})

    def test_only_patricks_answer_on_the_current_revision_is_read(self):
        card = self.card()
        self.assertEqual(self.answers(), [])
        self.w.core.answer(self.w.patrick(device=self.w.device()), **World.answer_args(card, option="allow"))
        got = self.answers()
        self.assertEqual(len(got), 1)
        self.assertEqual({k: got[0][k] for k in ("repo", "number", "author", "option")},
                         {"repo": "ThornswoodRP/Thornswood", "number": 5517, "author": "rahul05ranjan",
                          "option": "allow"})
        self.assertEqual(got[0]["carried"], [{"repo": "ThornswoodRP/Thornswood", "number": 5550}])

    def test_an_ordinary_card_is_not_an_outside_answer(self):
        card = self.call("open_card", "bramble", **card_args(links=[{"kind": "pr", "ref": "ThornswoodRP/Thornswood#1"}]))
        self.w.core.answer(self.w.patrick(device=self.w.device()), **World.answer_args(card))
        self.assertEqual(self.answers(), [])

    def test_the_labels_of_the_options_tw_git_carries_out_say_what_they_do(self):
        # Patrick, 8 Nov 2026, 3:05 PM: "the wording should be Auto-Merge,
        # Merge, and Close so we dont have a bunch of clutter".
        keep = {"key": "keep", "label": "Keep it open", "consequence": "Nothing merges or closes."}
        for opts in ([dict(o, label="Keep it open") if o["key"] == "close" else o for o in OUTSIDE_OPTIONS],
                     [dict(o, label="Let rahul05ranjan auto-merge") if o["key"] == "allow" else o
                      for o in OUTSIDE_OPTIONS],
                     [dict(o, label="Merge this one") if o["key"] == "merge" else o for o in OUTSIDE_OPTIONS],
                     [dict(o, label="Close") if o["key"] == "merge" else o for o in OUTSIDE_OPTIONS],
                     OUTSIDE_OPTIONS + [keep],
                     [o for o in OUTSIDE_OPTIONS if o["key"] != "merge"]):
            with self.assertRaises(Exception) as e:
                self.call("open_card", "bramble", **card_args(options=opts, recommendation="close", links=OUTSIDE_LINKS,
                                                              body=OUTSIDE_BODY))
            self.assertIn("outside_label", str(e.exception))

    def test_the_card_says_if_it_is_safe_if_we_can_make_it_safe_and_if_it_is_of_benefit(self):
        # Patrick, 8 Nov 2026, 2:00 PM: "the card also needs to tell me if its
        # safe, if we can make it safe, and if its benifitial to us".
        for drop in ("**Safe:**", "**Can we make it safe:**", "**Of benefit:**"):
            body = "\n".join(l for l in OUTSIDE_BODY.splitlines() if not l.startswith(drop))
            with self.assertRaises(Exception) as e:
                self.call("open_card", "bramble", **card_args(options=OUTSIDE_OPTIONS, recommendation="merge",
                                                              links=OUTSIDE_LINKS, body=body))
            self.assertIn("outside_questions", str(e.exception))
        self.assertEqual(self.card()["status"], "pending")

    def test_a_revision_keeps_the_labels_honest(self):
        card = self.card()
        opts = [dict(o, label="Keep it") if o["key"] == "close" else o for o in OUTSIDE_OPTIONS]
        with self.assertRaises(Exception) as e:
            self.call("revise_card", "bramble", decision_id=card["id"], expected_revision=1, title="t2", body=OUTSIDE_BODY,
                      options=opts, recommendation="merge", scope_of_yes="s")
        self.assertIn("outside_label", str(e.exception))

    def test_one_outside_pull_request_per_card_and_bramble_alone_opens_one(self):
        two = OUTSIDE_LINKS + [{"kind": "pr", "ref": "outside:ThornswoodRP/Thornswood#5523@rahul05ranjan"}]
        with self.assertRaises(Exception):
            self.call("open_card", "bramble", **card_args(options=OUTSIDE_OPTIONS, recommendation="allow", links=two,
                                                          body=OUTSIDE_BODY))
        with self.assertRaises(Exception):
            self.call("open_card", "bramble", **card_args(options=OUTSIDE_OPTIONS, recommendation="allow",
                                                          links=[{"kind": "pr", "ref": "outside:nonsense"}],
                                                          body=OUTSIDE_BODY))


@unittest.skipUnless(sys.platform.startswith("linux"),
                     "the route's ops are served by the brokers' wire, which is Linux code (Unix sockets, pwd)")
class MainAnswers(unittest.TestCase):
    """Step 6: Bramble opens a Main card through its route, and tw-git reads
    Patrick's answer on it from its own socket, with what the card is for."""

    SHA = "0123456789abcdef0123456789abcdef01234567"

    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.route = BotRoute(self.w.path, "test")
        self.addCleanup(self.route.close)

    def open(self, bot="bramble"):
        handler, _ = self.route.ops()["open_card"]
        return handler("hermes", card_args(
            bot=bot, kind="main", title="Publish revision 9", task="t1",
            options=[{"key": "authorize", "label": "Authorize", "consequence": "Bramble publishes revision 9."},
                     {"key": "not_now", "label": "Not now", "consequence": "Nothing is published."}],
            recommendation="authorize", scope_of_yes=f"One publish at {self.SHA}.",
            links=[{"kind": "release", "ref": f"publish:ThornswoodRP/Thornswood@{self.SHA}"}]))

    def main_answer(self, did):
        handler, callers = self.route.git_ops()["main_answer"]
        self.assertEqual(callers, {"tw-git", "root"})
        return handler("tw-git", {"decision_id": did})

    def test_tw_git_reads_his_authorize_with_the_binding(self):
        card = self.open()
        self.assertEqual(card["risk"], "high")
        self.assertIsNone(self.main_answer(card["id"]))
        self.w.core.answer(self.w.patrick(device=self.w.device()), **World.answer_args(card, option="authorize"))
        a = self.main_answer(card["id"])
        self.assertEqual((a["for"], a["commit"], a["option"], a["restored"]), ("publish", self.SHA, "authorize", False))

    def test_a_specialist_cannot_open_one_and_an_ordinary_card_is_no_main_answer(self):
        from thornswood_brokers.wire import Refused
        with self.assertRaises(Refused):
            self.open(bot="rift")
        handler, _ = self.route.ops()["open_card"]
        card = handler("hermes", card_args(bot="bramble"))
        with self.assertRaises(Refused):
            self.main_answer(card["id"])


class Deploy(unittest.TestCase):
    def unit(self):
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        cp.read_string((DEPLOY / "tw-decide-bots.service").read_text(encoding="utf-8"))
        return cp["Service"]

    def test_it_runs_as_the_cores_user_with_no_network(self):
        u = self.unit()
        self.assertEqual(u["User"], "tw-decide")
        self.assertEqual(u["SupplementaryGroups"], "tw-decide-bots tw-decide-git")
        self.assertEqual(u["PrivateNetwork"], "yes")
        self.assertEqual(u["RestrictAddressFamilies"], "AF_UNIX")
        self.assertEqual(u["ReadWritePaths"], "/srv/tw/decide")

    def test_the_socket_is_the_policys(self):
        sock = policy().data["brokers"]["sockets"]["decide"]
        u = self.unit()
        self.assertIn(f"--socket {sock['path']}", u["ExecStart"])
        self.assertIn(f"--group {sock['group']}", u["ExecStart"])
        self.assertEqual(u["RuntimeDirectory"], "tw/decide-bots tw/decide-git")
        self.assertTrue(sock["path"].startswith("/run/tw/decide-bots/"))
        self.assertLessEqual(set(CALLERS), set(sock["clients"]))  # every op's callers (BotRoute.ops)

    def test_tw_gits_socket_is_the_policys(self):
        sock = policy().data["brokers"]["sockets"]["decide_git"]
        u = self.unit()
        self.assertIn(f"--git-socket {sock['path']}", u["ExecStart"])
        self.assertIn(f"--git-group {sock['group']}", u["ExecStart"])
        self.assertTrue(sock["path"].startswith("/run/tw/decide-git/"))
        self.assertIn(sock["group"], u["SupplementaryGroups"].split())
        from thornswood_decisions.bots import GIT_CALLERS
        self.assertEqual(set(GIT_CALLERS), set(sock["clients"]))
        git = (REPO / "deploy" / "systemd" / "tw-git.service").read_text(encoding="utf-8")
        self.assertIn(sock["group"], [l for l in git.splitlines() if l.startswith("SupplementaryGroups=")][0].split("=")[1].split())
        text = (DEPLOY / "install.sh").read_text(encoding="utf-8")
        self.assertIn('[[ "$members" == tw-git ]]', text)
        self.assertIn('"660 tw-decide-git"', text)

    def test_the_install_checks_the_group_and_the_socket(self):
        text = (DEPLOY / "install.sh").read_text(encoding="utf-8")
        self.assertIn('[[ "$members" == hermes ]]', text)
        self.assertIn("tw-decide-bots.service", text)
        self.assertIn('"660 tw-decide-bots"', text)
        main = (REPO / "deploy" / "install.sh").read_text(encoding="utf-8")
        self.assertIn("tw-decide-bots", main.split("# 1. Groups and users.")[1].split("# 2.")[0])


if __name__ == "__main__":
    unittest.main()
