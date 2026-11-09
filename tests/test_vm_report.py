"""tw-git writes the VM's install results to GitHub (ThornswoodRP/Thornswood#5564).

The follower hands each new result to tw-git's vm_install, which only root
may ask for. It goes as one comment on the ops ledger issue, and on the board
as one issue that is open while the VM is not installing main: the first stop
opens it, later stops are comments on it, and the next good install closes
it. Only the commit, result, step, a one-line reason with no address and what
was put back go out, and a result sent again after a lost answer writes
nothing twice.
"""

import sys as _sys
import unittest as _unittest

if not _sys.platform.startswith("linux"):
    raise _unittest.SkipTest("the brokers run only on the Oracle VM; their tests run there")

import copy
import unittest

from fake_github import Lost
from test_git_service import APP, BRAMBLE_INSTALLATION, OPS, World

from thornswood_brokers.github_api import GitHubError
from thornswood_brokers.vm_report import BOARD_MARK, one_line
from thornswood_guard.policy import Policy, PolicyError
from thornswood_brokers.wire import Refused

NEW = "b" * 40
OLD = "a" * 40
NEWER = "c" * 40


def result(**kw):
    out = {"result": "installed", "commit": NEW, "installed": NEW, "before": OLD, "step": None, "reason": None,
           "put_back": None, "at": "2026-11-08T23:40:00Z"}
    out.update(kw)
    return out


class VmInstall(unittest.TestCase):
    def setUp(self):
        self.w = World()
        r = self.w.git.policy.data["follow_main"]["report"]
        self.ledger_issue = int(r["ledger_issue"])
        self.w.fake.add_issue(OPS, self.ledger_issue)
        self.op, self.callers = self.w.git.ops()["vm_install"]

    def ask(self, **kw):
        return self.op("root", result(**kw))

    def ledger(self):
        return [c["body"] for c in self.w.fake.comments.get((OPS, self.ledger_issue), [])]

    def board(self, state="open"):
        return [i for (r, _), i in self.w.fake.issues.items() if r == APP and BOARD_MARK in (i.get("body") or "")
                and i["state"] == state]

    def test_only_root_may_ask(self):
        self.assertEqual(self.callers, {"root"})

    def test_a_good_install_is_one_line_on_the_ledger_and_nothing_on_the_board(self):
        out = self.ask()
        self.assertEqual(len(self.ledger()), 1)
        self.assertIn(f"`{NEW[:12]}` installed.", self.ledger()[0])
        self.assertIn(f"The VM runs `{NEW[:12]}`.", self.ledger()[0])
        self.assertEqual(self.board(), [])
        self.assertIsNone(out["board"])

    def test_a_stop_opens_one_board_issue_and_the_next_stop_comments_on_it(self):
        out = self.ask(result="not installed", step="tests", installed=OLD,
                       reason=f"the suite fails at {NEW[:12]} on the VM (Ran 412 tests)")
        issues = self.board()
        self.assertEqual(len(issues), 1)
        self.assertEqual(out["board"], issues[0]["number"])
        self.assertEqual(issues[0]["title"], "The Hermes VM stopped installing TWHermesAgents main")
        body = issues[0]["body"]
        self.assertIn("at the tests step", body)
        self.assertIn("Ran 412 tests", body)
        self.assertIn("Done when:", body)
        self.assertNotIn("—", body)
        out2 = self.ask(result="failed", commit=NEWER, step="canary", installed=OLD, reason="tw-canary exited 1",
                        put_back=f"put back {OLD}", at="2026-11-08T23:50:00Z")
        self.assertEqual(out2["board"], issues[0]["number"])
        self.assertEqual(len(self.board()), 1)
        comments = self.w.fake.comments[(APP, issues[0]["number"])]
        self.assertEqual(len(comments), 1)
        self.assertIn("at the canary step", comments[0]["body"])
        self.assertIn(f"Put back {OLD}", comments[0]["body"])

    def test_the_next_good_install_closes_the_board_issue(self):
        self.ask(result="not installed", step="tests", installed=OLD, reason="the suite fails")
        n = self.board()[0]["number"]
        out = self.ask(commit=NEWER, installed=NEWER, at="2026-11-09T00:10:00Z")
        self.assertEqual(out["closed"], n)
        self.assertEqual(self.board(), [])
        closed = self.w.fake.issues[(APP, n)]
        self.assertEqual((closed["state"], closed["state_reason"]), ("closed", "completed"))
        self.assertIn("So the VM runs main again.", self.w.fake.comments[(APP, n)][-1]["body"])
        self.assertEqual(len(self.ledger()), 2)

    def test_a_fetch_that_works_again_closes_it_too(self):
        self.ask(result="fetch failed", commit=None, step="fetch", installed=OLD, reason="git ... exited 128")
        self.assertEqual(len(self.board()), 1)
        self.ask(result="fetched", commit=NEW, installed=OLD, at="2026-11-09T00:10:00Z")
        self.assertEqual(self.board(), [])

    def test_a_result_sent_again_after_a_lost_answer_is_written_once(self):
        self.w.fake.lose_answer.add(f"POST /repos/{APP}/issues")
        stop = dict(result="not installed", step="tests", installed=OLD, reason="the suite fails")
        with self.assertRaises(Lost):
            self.ask(**stop)
        self.ask(**stop)
        self.assertEqual(len(self.board()), 1)
        self.assertEqual(len(self.ledger()), 1)
        self.assertEqual(self.w.fake.comments.get((APP, self.board()[0]["number"]), []), [])
        # A good install sent twice: one ledger line, one closing comment.
        self.ask(commit=NEWER, installed=NEWER, at="2026-11-09T00:10:00Z")
        self.ask(commit=NEWER, installed=NEWER, at="2026-11-09T00:10:00Z")
        self.assertEqual(len(self.ledger()), 2)

    def test_an_issue_someone_else_opened_with_the_marker_is_not_taken_for_the_vms(self):
        self.w.fake.add_issue(APP, 77)
        self.w.fake.issues[(APP, 77)]["body"] = BOARD_MARK
        self.ask(result="not installed", step="tests", installed=OLD, reason="the suite fails")
        self.assertEqual([i["number"] for i in self.board() if i["number"] != 77].__len__(), 1)
        self.assertEqual(self.w.fake.comments.get((APP, 77), []), [])

    def test_only_its_own_fields_in_their_forms_are_taken(self):
        for bad in (dict(result="nonsense"), dict(commit="HEAD"), dict(step="rm -rf"), dict(at="yesterday"),
                    dict(result="failed", step="canary", reason="")):
            with self.assertRaises(Refused, msg=bad):
                self.ask(**bad)
        self.assertEqual(self.ledger(), [])

    def test_the_reason_goes_out_as_one_line_with_no_address(self):
        self.assertEqual(one_line("ssh to 100.64.1.2 failed\n  and fe80::1:2:3:4 too"), "ssh to <address> failed and <address> too")
        self.assertTrue(len(one_line("x" * 900)) <= 300)
        self.ask(result="not installed", step="tests", installed=OLD, reason="fetch from 10.1.2.3 refused")
        self.assertNotIn("10.1.2.3", self.board()[0]["body"])
        self.assertNotIn("10.1.2.3", self.ledger()[0])

    def test_it_writes_as_brambles_app_with_issues_write_alone(self):
        self.ask(result="not installed", step="tests", installed=OLD, reason="the suite fails")
        for tok in self.w.fake.tokens:
            self.assertEqual(tok.get("permissions"), {"issues": "write", "metadata": "read"}) if tok.get("permissions", {}).get("issues") == "write" else None
        # Ops is Bramble's alone: the workers' App is not installed there (8 Nov 2026).
        self.assertEqual({t["installation"] for t in self.w.fake.tokens}, {BRAMBLE_INSTALLATION})
        writes = [w for w in self.w.fake.writes() if not w[1].startswith("/app/installations/")]
        self.assertTrue(writes)
        self.assertTrue(all(p.startswith((f"/repos/{APP}/issues", f"/repos/{OPS}/issues")) for _, p, _ in writes))

    def test_an_app_not_on_the_ledgers_repository_gets_githubs_422_and_nothing_is_written(self):
        # What the VM measured from 9:23 PM on 8 Nov, when the reports still went as the workers' App.
        self.w.fake.installed[BRAMBLE_INSTALLATION] = {"Thornswood", "skymp"}
        with self.assertRaises(GitHubError) as e:
            self.ask()
        self.assertEqual(e.exception.status, 422)
        self.assertEqual(self.ledger(), [])

    def test_the_policy_refuses_a_report_app_that_does_not_reach_the_ledger_or_the_board(self):
        data = copy.deepcopy(self.w.git.policy.data)
        data["follow_main"]["report"]["app"] = "workers"
        with self.assertRaises(PolicyError):
            Policy(data)
        data = copy.deepcopy(self.w.git.policy.data)
        data["follow_main"]["report"]["board_repo"] = "nowhere"
        with self.assertRaises(PolicyError):
            Policy(data)
        self.assertEqual(self.w.git.policy.data["follow_main"]["report"]["app_login"], "thornswood-bramble[bot]")


if __name__ == "__main__":
    unittest.main()
