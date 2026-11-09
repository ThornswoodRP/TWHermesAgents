"""tw-git against a fake GitHub and real git repositories on disk.

Attack tests 6 to 11 (what a worker may not do on GitHub, through the broker)
and 20 (a lost merge answer is read back, never merged twice) from 08
section 8.
"""

import sys as _sys
import unittest as _unittest

if not _sys.platform.startswith("linux"):
    # The brokers are Linux code (Unix sockets with SO_PEERCRED, pwd, O_NOFOLLOW
    # walks, systemd): they run only on the Oracle VM, and these tests run in
    # the VM half of scripts/test-at-head.sh.
    raise _unittest.SkipTest("the brokers run only on the Oracle VM; their tests run there")

import base64
import json
import subprocess
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from fake_github import FakeGitHub, git
from helpers import FIXTURES, policy

from test_policy_service import service as policy_service

from thornswood_brokers.git_service import GitService
from thornswood_brokers.github_api import GitHub
from thornswood_brokers.wire import Refused

APP = "ThornswoodRP/Thornswood"
OPS = "ThornswoodRP/Thornswood-Ops"
FORK = "ThornswoodRP/skymp"
# main's ruleset on Thornswood (read 2 Nov 2026) and the fork branch's.
APP_RULES = ["Build check result", "Test tasks in the pull request", "Release note"]
FORK_RULES = ["build-pr", "save-directory-linux"]
HERMES = "ThornswoodRP/TWHermesAgents"


class World:
    """A fake GitHub, tw-policy on a temp store, and tw-git between them."""

    def __init__(self, db: Path | None = None, fake: FakeGitHub | None = None, root: Path | None = None):
        self.root = root or Path(tempfile.mkdtemp())
        self.fake = fake or FakeGitHub(self.root / "gh")
        if fake is None:
            self.fake.make_repo(APP)
            self.fake.make_repo(OPS)
            self.fake.make_repo(FORK, base="thornswood/engine-changes")
            self.fake.branch_rules[(APP, "main")] = list(APP_RULES)
            self.fake.branch_rules[(FORK, "thornswood/engine-changes")] = list(FORK_RULES)
        self.db = db or self.root / "policy.db"
        self.policy = policy_service(db=self.db)
        self.spool = self.root / "spool"
        p = policy()
        apps = {k: dict(v, key_file="/nonexistent") for k, v in p.data["brokers"]["apps"].items()}
        gh = GitHub(apps, transport=self.fake, signer=lambda data, key: b"sig")
        # Patrick's answers on outside pull request cards, as tw-decide-bots
        # gives them to tw-git (#132); tests add to it.
        self.answers: list[dict] = []
        # Patrick's answers on Main cards, by card id, as main_answer gives them (step 6).
        self.mains: dict[str, dict] = {}
        self.git = GitService(p, gh, self.ask, mirrors=self.root / "mirrors", spool=self.spool,
                              git_base=f"file://{self.fake.remotes}/", allow_file_protocol=True,
                              ask_decide=self.ask_decide)

    def ask_decide(self, op: str, args: dict):
        if op == "main_answer":
            return json.loads(json.dumps(self.mains.get(args["decision_id"])))
        assert op == "outside_answers", op
        return json.loads(json.dumps(self.answers))

    def ask(self, op: str, args: dict) -> dict:
        handler, callers = self.policy.ops()[op]
        assert "tw-git" in callers, f"tw-git may not call {op}"
        return handler("tw-git", json.loads(json.dumps(args)))

    def restart(self) -> "World":
        """tw-git and tw-policy stop and start again on the same store and GitHub."""
        self.policy.db.close()
        return World(db=self.db, fake=self.fake, root=self.root)

    def bundle(self, bot: str, branch: str, files: dict[str, str], repo: str = APP, base: str = "main",
               name: str = "work.bundle") -> tuple[str, str]:
        work = Path(tempfile.mkdtemp())
        subprocess.run(["git", "clone", "-q", "-b", base, str(self.fake.remote(repo)), str(work)], check=True,
                       capture_output=True)
        git(work, "checkout", "-q", "-B", branch)
        for path, text in files.items():
            (work / path).parent.mkdir(parents=True, exist_ok=True)
            (work / path).write_text(text)
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", "work")
        out = self.spool / "out" / bot
        out.mkdir(parents=True, exist_ok=True)
        git(work, "bundle", "create", "-q", str(out / name), f"refs/heads/{branch}")
        return name, git(work, "rev-parse", "HEAD")

    def push(self, bot: str, branch: str, files: dict[str, str], **kw) -> dict:
        name, _ = self.bundle(bot, branch, files, **{k: v for k, v in kw.items() if k in ("repo", "base", "name")})
        return self.git.push("hermes", {"bot": bot, "repo": kw.get("repo", APP), "task": kw.get("task", "t-1"),
                                        "branch": branch, "bundle": name})

    CHECKS = {APP: ("Launcher builds", "Build check result"), OPS: ("check",), FORK: ("build-pr",)}

    def green_pr(self, number: int = 42, branch: str = "",
                 files: list[str] | None = None, repo: str = OPS, checks: bool = True) -> str:
        # Ops is Bramble's alone (policy.yaml github_apps): its pull requests are Bramble's own.
        branch = branch or ("bramble/3561-path-fence" if repo == OPS else "sprocket/3561-path-fence")
        base = "thornswood/engine-changes" if repo == FORK else "main"
        sha = self.push(branch.split("/")[0], branch, {"launcher/PathFence.cs": "fence\n"}, repo=repo, base=base)["sha"]
        self.fake.add_pr(repo, number, head_ref=branch, head_sha=sha, base=base,
                         files=files or ["launcher/PathFence.cs"])
        for name in self.CHECKS[repo] if checks else ():
            self.fake.add_check(repo, sha, name)
        return sha

    def merge(self, bot: str = "bramble", number: int = 42, sha: str = "", repo: str = OPS, **extra) -> dict:
        return self.git.merge("hermes", {"bot": bot, "repo": repo, "task": "merge-42", "number": number,
                                         "sha": sha, **extra})


class Push(unittest.TestCase):
    def test_a_worker_pushes_its_own_branch(self):
        w = World()
        out = w.push("sprocket", "sprocket/3561-path-fence", {"launcher/PathFence.cs": "fence\n"})
        self.assertTrue(out["done"])
        self.assertEqual(w.fake.ref(APP, "sprocket/3561-path-fence"), out["sha"])
        self.assertEqual(out["changed_files"], 1)
        # The token asked for was the workers' App, one repo, contents only.
        writes = [t for t in w.fake.tokens if t["permissions"].get("contents") == "write"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["installation"], "177393064")
        self.assertEqual(writes[0]["repositories"], ["Thornswood"])
        self.assertNotIn("workflows", writes[0]["permissions"])

    def test_attack_6_no_push_to_main(self):
        w = World()
        before = w.fake.ref(APP, "main")
        with self.assertRaises(Refused) as e:
            w.push("sprocket", "main", {"x.txt": "x\n"})
        self.assertIn("merged pull request", str(e.exception))
        self.assertEqual(w.fake.ref(APP, "main"), before)

    def test_attack_7_no_push_to_another_bots_branch(self):
        w = World()
        with self.assertRaises(Refused) as e:
            w.push("sprocket", "rift/12-load-order", {"x.txt": "x\n"})
        self.assertIn("belongs to Rift", str(e.exception))
        self.assertIsNone(w.fake.ref(APP, "rift/12-load-order"))

    def test_attack_8_a_worker_cannot_push_a_workflow(self):
        w = World()
        with self.assertRaises(Refused) as e:
            w.push("magpie", "magpie/77-ci-cache", {".github/workflows/build.yml": "on: push\n"})
        self.assertIn("Workflows permission", str(e.exception))
        self.assertIsNone(w.fake.ref(APP, "magpie/77-ci-cache"))

    def test_bramble_carries_magpies_workflow_change_with_its_own_app(self):
        w = World()
        out = w.push("bramble", "magpie/77-ci-cache", {".github/workflows/build.yml": "on: push\n"})
        self.assertTrue(out["workflow_change"])
        writes = [t for t in w.fake.tokens if t["permissions"].get("contents") == "write"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["installation"], "177392923")
        self.assertEqual(writes[0]["permissions"].get("workflows"), "write")

    def test_only_wisp_works_in_the_fork(self):
        w = World()
        with self.assertRaises(Refused):
            w.push("sprocket", "sprocket/9-engine", {"a.cpp": "x\n"}, repo=FORK, base="thornswood/engine-changes")
        out = w.push("wisp", "wisp/9-engine", {"a.cpp": "x\n"}, repo=FORK, base="thornswood/engine-changes")
        self.assertTrue(out["done"])

    def test_no_bot_writes_to_thornswood_hermes(self):
        w = World()
        with self.assertRaises(Refused) as e:
            w.git.push("hermes", {"bot": "bramble", "repo": HERMES, "task": "t", "branch": "bramble/1-x", "bundle": "a.bundle"})
        self.assertIn("Patrick's", str(e.exception))

    def test_a_bundle_outside_the_bots_spool_is_refused(self):
        w = World()
        w.bundle("rift", "rift/12-load-order", {"x": "x\n"}, name="rift.bundle")
        for name in ("../rift/rift.bundle", "rift.bundle", "/etc/passwd"):
            with self.subTest(name=name), self.assertRaises(Refused):
                w.git.push("hermes", {"bot": "sprocket", "repo": APP, "task": "t", "branch": "sprocket/1-a", "bundle": name})

    def test_a_rewrite_of_the_own_branch_goes_with_a_lease(self):
        w = World()
        first = w.push("sprocket", "sprocket/1-a", {"a.txt": "one\n"}, task="t-1")["sha"]
        second = w.push("sprocket", "sprocket/1-a", {"a.txt": "two\n"}, task="t-2")["sha"]
        self.assertNotEqual(first, second)
        self.assertEqual(w.fake.ref(APP, "sprocket/1-a"), second)

    def test_the_same_push_twice_is_done_once(self):
        w = World()
        name, sha = w.bundle("sprocket", "sprocket/1-a", {"a.txt": "one\n"})
        args = {"bot": "sprocket", "repo": APP, "task": "t-1", "branch": "sprocket/1-a", "bundle": name}
        w.git.push("hermes", args)
        with self.assertRaises(Refused) as e:
            w.git.push("hermes", args)
        self.assertIn("already done", str(e.exception))

    def test_the_file_transport_stays_off_for_everything_but_the_bundle(self):
        w = World()
        w.git.allow_file_protocol = False
        mirror = w.git.mirrors / "probe.git"
        subprocess.run(["git", "init", "-q", "--bare", str(mirror)], check=True)
        with self.assertRaises(Refused) as e:
            w.git._git(mirror, "fetch", "-q", str(w.fake.remote(APP)), "+refs/heads/*:refs/x/*")
        self.assertIn("not allowed", str(e.exception))
        name, sha = w.bundle("sprocket", "sprocket/1-a", {"a": "a\n"})
        w.git._git(mirror, "fetch", "-q", str(w.git._bundle("sprocket", name)), "+refs/heads/sprocket/1-a:refs/tw/in",
                   extra_config=["protocol.file.allow=always"])
        self.assertEqual(w.git._git(mirror, "rev-parse", "refs/tw/in"), sha)

    def test_only_the_gateway_user_calls_tw_git(self):
        # All but vm_install, which is root's follower's alone (vm_report.py).
        ops = World().git.ops()
        self.assertTrue(all(users == {"hermes"} for name, (_, users) in ops.items() if name != "vm_install"))
        self.assertEqual(ops["vm_install"][1], {"root"})


class Merge(unittest.TestCase):
    """Ops is private on GitHub Free, with no auto-merge: Bramble merges a
    green pull request itself, at its exact head."""

    def test_bramble_merges_on_its_check_at_the_exact_head(self):
        w = World()
        sha = w.green_pr()
        out = w.merge(sha=sha)
        self.assertTrue(out["done"])
        self.assertEqual(len(w.fake.writes("PUT", "/merge")), 1)
        self.assertEqual(w.fake.writes("PUT", "/merge")[0][2], {"sha": sha, "merge_method": "squash"})
        merge_tokens = [t for t in w.fake.tokens if t["permissions"].get("pull_requests") == "write"]
        self.assertEqual([t["installation"] for t in merge_tokens], ["177392923"])

    def test_attack_9_a_worker_cannot_merge_its_own_pr(self):
        w = World()
        sha = w.green_pr(branch="wisp/3561-path-fence", repo=FORK)
        with self.assertRaises(Refused) as e:
            w.merge(bot="wisp", sha=sha, repo=FORK)
        self.assertIn("only Bramble merges", str(e.exception))
        self.assertEqual(w.fake.writes("PUT"), [])

    def test_attack_10_a_check_posted_by_another_app_does_not_count(self):
        w = World()
        sha = w.push("sprocket", "sprocket/3561-path-fence", {"a": "a\n"})["sha"]
        w.fake.add_pr(OPS, 42, head_ref="sprocket/3561-path-fence", head_sha=sha, files=["a"])
        w.fake.add_check(OPS, sha, "check", app="thornswood-workers")
        with self.assertRaises(Refused) as e:
            w.merge(sha=sha)
        self.assertIn("'check'", str(e.exception))
        self.assertEqual(w.fake.writes("PUT"), [])

    def test_the_latest_run_decides(self):
        w = World()
        sha = w.green_pr()
        w.fake.add_check(OPS, sha, "check", conclusion="failure")
        with self.assertRaises(Refused) as e:
            w.merge(sha=sha)
        self.assertIn("failure", str(e.exception))

    def test_attack_11_a_moved_head_is_not_merged(self):
        w = World()
        sha = w.green_pr()
        with self.assertRaises(Refused) as e:
            w.merge(sha="d" * 40)
        self.assertIn("head moved", str(e.exception))
        self.assertEqual(w.fake.writes("PUT"), [])
        self.assertTrue(sha)

    def test_a_pr_that_hides_files_is_not_merged(self):
        w = World()
        sha = w.green_pr()
        w.fake.prs[(OPS, 42)]["changed_files"] = 4000
        with self.assertRaises(Refused) as e:
            w.merge(sha=sha)
        self.assertIn("nothing merges unseen", str(e.exception))

    def test_a_workflow_or_gate_change_merges_with_no_yes(self):
        # Patrick, 2 Nov 2026: "I shouldn't need to approve".
        w = World()
        sha = w.green_pr(files=[".github/workflows/box-run.yml", "scripts/vps/gate/Run-Verb.ps1"])
        out = w.merge(sha=sha)
        self.assertTrue(out["done"])
        self.assertEqual(w.fake.writes("PUT", "/merge")[0][2], {"sha": sha, "merge_method": "squash"})

    def test_attack_20_a_lost_merge_answer_is_read_back_not_merged_again(self):
        w = World()
        sha = w.green_pr()
        w.fake.lose_answer.add(f"PUT /repos/{OPS}/pulls/42/merge")
        with self.assertRaises(Refused) as e:
            w.merge(sha=sha)
        self.assertIn("read back", str(e.exception))
        # tw-git and tw-policy restart before anyone looks.
        w2 = w.restart()
        out = w2.merge(sha=sha)
        self.assertTrue(out["reconciled"])
        self.assertEqual(out["receipt"]["merge_commit_sha"], "c" * 40)
        self.assertEqual(len(w2.fake.writes("PUT", "/merge")), 1)
        with self.assertRaises(Refused):
            w2.merge(sha=sha)
        self.assertEqual(len(w2.fake.writes("PUT", "/merge")), 1)

    def test_attack_20_a_crash_mid_write_is_read_back_after_restart(self):
        w = World()
        sha = w.green_pr()
        # tw-git dies after op_begin and after GitHub merged, before op_finish.
        op = w.ask("op_begin", {"bot": "bramble", "task": "merge-42", "action": "merge", "repo": OPS,
                                "target": "pr/42", "commit": sha})
        self.assertEqual(op["state"], "go")
        w.fake.prs[(OPS, 42)]["merged"] = True
        w.fake.prs[(OPS, 42)]["merge_commit_sha"] = "e" * 40
        w2 = w.restart()
        out = w2.merge(sha=sha)
        self.assertTrue(out["reconciled"])
        self.assertEqual(w2.fake.writes("PUT"), [])

    def test_a_lost_answer_for_a_merge_that_did_not_happen_tries_once_more(self):
        w = World()
        sha = w.green_pr()
        w.ask("op_begin", {"bot": "bramble", "task": "merge-42", "action": "merge", "repo": OPS,
                           "target": "pr/42", "commit": sha})
        w2 = w.restart()
        out = w2.merge(sha=sha)
        self.assertTrue(out["done"])
        self.assertNotIn("reconciled", out)
        self.assertEqual(len(w2.fake.writes("PUT", "/merge")), 1)

    def test_someone_elses_merge_is_not_claimed(self):
        w = World()
        sha = w.green_pr()
        w.fake.prs[(OPS, 42)]["merged"] = True
        with self.assertRaises(Refused) as e:
            w.merge(sha=sha)
        self.assertIn("already merged", str(e.exception))



class MergeQueue(unittest.TestCase):
    """The redesign's part 5: with a merge queue on main, GitHub refuses a
    direct merge, so where auto-merge does not merge, Bramble's merge puts the
    PR in the queue at the exact green head, and the queue merges it."""

    def queue(self) -> tuple[World, str]:
        w = World()
        sha = w.green_pr()
        w.fake.merge_queues.add((OPS, "main"))
        return w, sha

    def test_with_a_queue_the_green_head_joins_it_and_nothing_merges_directly(self):
        w, sha = self.queue()
        out = w.merge(sha=sha)
        self.assertTrue(out["done"])
        self.assertTrue(out["queued"])
        self.assertEqual(out["receipt"], {"queued": True})
        self.assertEqual(w.fake.queued, [(OPS, 42, sha)])
        self.assertEqual(w.fake.writes("PUT"), [], "no direct merge")
        self.assertFalse(w.fake.prs[(OPS, 42)]["merged"])
        # The enqueue went with Bramble's App and a merge token.
        merge_tokens = [t for t in w.fake.tokens if t["permissions"].get("pull_requests") == "write"]
        self.assertEqual([t["installation"] for t in merge_tokens], ["177392923"])
        # The queue's own checks pass and GitHub merges it; asking again is
        # refused (the same task's op is done), and nothing is queued twice.
        w.fake.land_queue(OPS, 42)
        with self.assertRaises(Refused) as e:
            w.merge(sha=sha)
        self.assertIn("already", str(e.exception))
        with self.assertRaises(Refused) as e:
            w.git.merge("hermes", {"bot": "bramble", "repo": OPS, "task": "another-task", "number": 42, "sha": sha})
        self.assertIn("already merged", str(e.exception))
        self.assertEqual(len(w.fake.queued), 1)

    def test_without_a_queue_the_merge_is_direct_as_before(self):
        w = World()
        sha = w.green_pr()
        out = w.merge(sha=sha)
        self.assertNotIn("queued", out)
        self.assertEqual(w.fake.writes("PUT", "/merge")[0][2], {"sha": sha, "merge_method": "squash"})
        self.assertEqual(w.fake.queued, [])

    def test_a_lost_enqueue_answer_is_read_back_not_queued_twice(self):
        w, sha = self.queue()
        w.fake.lose_answer.add("enqueue")
        with self.assertRaises(Refused) as e:
            w.merge(sha=sha)
        self.assertIn("read back", str(e.exception))
        w2 = w.restart()
        out = w2.merge(sha=sha)
        self.assertTrue(out["reconciled"])
        self.assertEqual(out["receipt"], {"queued": True})
        self.assertEqual(len(w2.fake.queued), 1)

    def test_every_rule_still_comes_first(self):
        w, sha = self.queue()
        w.fake.add_check(OPS, sha, "check", conclusion="failure")
        with self.assertRaises(Refused):
            w.merge(sha=sha)
        with self.assertRaises(Refused):
            w.merge(bot="sprocket", sha=sha)
        self.assertEqual(w.fake.queued, [])

    def test_github_saying_no_to_the_enqueue_is_a_refusal(self):
        w, sha = self.queue()
        w.fake.prs[(OPS, 42)]["in_queue"] = True   # someone else queued it
        with self.assertRaises(Refused) as e:
            w.merge(sha=sha)
        self.assertIn("already queued", str(e.exception))
        self.assertEqual(w.fake.queued, [])


class OutsideWork(unittest.TestCase):
    """Patrick, 8 Nov 2026: outside work never merges unless he names that
    person. tw-git reads every commit of the pull request from GitHub itself."""

    RAHUL = "Rahul <12345+rahul05ranjan@users.noreply.github.com>"

    def carried(self, w: World, message: str, repo: str = APP, labels=("outside work",)) -> str:
        sha = w.green_pr(repo=repo, checks=repo != APP)
        w.fake.prs[(repo, 42)]["labels"] = [{"name": l} for l in labels]
        w.fake.pr_commits[(repo, 42)] = [{"sha": sha, "author": None, "commit": {
            "author": {"name": "Sprocket (Thornswood Bot)", "email": "sprocket@bots.thornswood.invalid"},
            "message": message}}]
        return sha

    def test_a_bots_pr_carrying_outside_work_is_never_put_on_auto_merge(self):
        w = World()
        sha = self.carried(w, f"Fix the path fence\n\nCo-authored-by: {self.RAHUL}\n")
        with self.assertRaises(Refused) as e:
            w.merge(sha=sha, repo=APP)
        self.assertIn("rahul05ranjan", str(e.exception))
        self.assertEqual(w.fake.auto_merged, [])

    def test_ops_never_merges_it_directly_either(self):
        w = World()
        sha = self.carried(w, f"x\n\nco-authored-by:   {self.RAHUL}  \n", repo=OPS)
        with self.assertRaises(Refused):
            w.merge(sha=sha)
        self.assertEqual(w.fake.writes("PUT"), [])

    def test_the_label_without_a_credit_is_refused(self):
        w = World()
        sha = self.carried(w, "Fix the path fence")
        with self.assertRaises(Refused) as e:
            w.merge(sha=sha, repo=APP)
        self.assertIn("Co-authored-by", str(e.exception))

    def test_a_team_credit_still_goes_on_auto_merge(self):
        w = World()
        sha = self.carried(w, "x\n\nCo-authored-by: EvilPatrick06 <187923600+EvilPatrick06@users.noreply.github.com>",
                           labels=())
        out = w.merge(sha=sha, repo=APP)
        self.assertTrue(out["auto_merge"])

    def test_commits_github_did_not_list_merge_nothing(self):
        w = World()
        sha = w.green_pr(repo=APP, checks=False)
        w.fake.prs[(APP, 42)]["commits"] = 3
        with self.assertRaises(Refused) as e:
            w.merge(sha=sha, repo=APP)
        self.assertIn("nothing merges unseen", str(e.exception))

    def test_bramble_answers_a_fork_pr_and_nothing_else_does(self):
        w = World()
        w.fake.add_pr(APP, 5517, head_ref="patch-1", head_sha="e" * 40, files=["a"], author="rahul05ranjan")
        w.fake.prs[(APP, 5517)]["head"]["repo"] = {"full_name": "rahul05ranjan/Thornswood"}
        out = w.git.comment("hermes", {"bot": "bramble", "repo": APP, "task": "t", "number": 5517,
                                       "body": "Thanks. This is moving into our own pull request with your credit."})
        self.assertTrue(out["done"], out)
        with self.assertRaises(Refused):
            w.git.comment("hermes", {"bot": "sprocket", "repo": APP, "task": "t", "number": 5517, "body": "x"})
        with self.assertRaises(Refused):
            w.git.edit_pr("hermes", {"bot": "bramble", "repo": APP, "task": "t", "number": 5517, "title": "x"})
        with self.assertRaises(Refused):
            w.merge(number=5517, sha="e" * 40, repo=APP)


class OutsideAnswers(unittest.TestCase):
    """#132: tw-git carries out Patrick's answer on an outside pull request
    card itself: "close" closes the pull request it names, "allow" lets that
    person's work merge, and nothing closes without an answer."""

    RAHUL = "Rahul <12345+rahul05ranjan@users.noreply.github.com>"

    def world(self, option=None, carried=(51,)) -> World:
        w = World()
        w.fake.add_pr(APP, 5517, head_ref="patch-1", head_sha="e" * 40, files=["a"], author="rahul05ranjan")
        w.fake.prs[(APP, 5517)]["head"]["repo"] = {"full_name": "rahul05ranjan/Thornswood"}
        if option:
            w.answers.append({"decision_id": "dec_1", "repo": APP, "number": 5517, "author": "rahul05ranjan",
                              "carried": [{"repo": APP, "number": n} for n in carried], "option": option,
                              "answered_at": "2026-11-08T21:00:00Z"})
        return w

    def carried(self, w: World, number: int = 51, carried_from: str = "") -> str:
        branch = f"sprocket/5513-fix-{number}"
        sha = w.push("sprocket", branch, {"launcher/PathFence.cs": "fence\n"}, repo=APP)["sha"]
        w.fake.add_pr(APP, number, head_ref=branch, head_sha=sha, files=["launcher/PathFence.cs"])
        w.fake.prs[(APP, number)]["labels"] = [{"name": "outside work"}]
        line = f"Carried-from: {carried_from}\n" if carried_from else ""
        w.fake.pr_commits[(APP, number)] = [{"sha": sha, "author": None, "commit": {
            "author": {"name": "Sprocket (Thornswood Bot)", "email": "sprocket@bots.thornswood.invalid"},
            "message": f"Fix the path fence\n\nCo-authored-by: {self.RAHUL}\n{line}"}}]
        return sha

    def test_merge_this_one_merges_the_carried_pull_request_once(self):
        # Patrick, 8 Nov 2026, 1:59 PM: "merge it without allowinfg for automerge".
        w = self.world("merge")
        sha = self.carried(w)
        self.assertTrue(w.merge(number=51, sha=sha, repo=APP)["auto_merge"])
        # Another pull request of theirs is not covered.
        other = self.carried(w, number=52)
        with self.assertRaises(Refused):
            w.merge(number=52, sha=other, repo=APP)

    def test_merge_this_one_binds_by_the_carried_from_line_when_the_card_had_none(self):
        w = self.world("merge", carried=())
        sha = self.carried(w, number=53, carried_from=f"{APP}#5517")
        self.assertTrue(w.merge(number=53, sha=sha, repo=APP)["auto_merge"])
        wrong = self.carried(w, number=54, carried_from=f"{APP}#5523")
        with self.assertRaises(Refused):
            w.merge(number=54, sha=wrong, repo=APP)

    def test_merge_this_one_ends_once_their_pull_request_is_closed(self):
        w = self.world("merge", carried=())
        w.fake.prs[(APP, 5517)]["state"] = "closed"
        sha = self.carried(w, number=53, carried_from=f"{APP}#5517")
        with self.assertRaises(Refused):
            w.merge(number=53, sha=sha, repo=APP)

    def test_theirs_closes_as_replaced_by_a_merged_pull_request_that_carries_it(self):
        w = self.world("merge", carried=())
        self.carried(w, number=53, carried_from=f"{APP}#5517")
        self.carried(w, number=54)
        w.fake.prs[(APP, 54)]["merged"] = True
        with self.assertRaises(Refused) as e:
            w.git.close_pr("hermes", {"bot": "bramble", "repo": APP, "task": "t", "number": 5517, "replaced_by": 53})
        self.assertIn("not merged", str(e.exception))
        with self.assertRaises(Refused) as e:
            w.git.close_pr("hermes", {"bot": "bramble", "repo": APP, "task": "t", "number": 5517, "replaced_by": 54})
        self.assertIn("Carried-from", str(e.exception))
        w.fake.prs[(APP, 53)]["merged"] = True
        out = w.git.close_pr("hermes", {"bot": "bramble", "repo": APP, "task": "t", "number": 5517, "replaced_by": 53})
        self.assertTrue(out["done"])
        self.assertEqual(w.fake.prs[(APP, 5517)]["state"], "closed")

    def close(self, w, number=5517, bot="bramble"):
        return w.git.close_pr("hermes", {"bot": bot, "repo": APP, "task": "5513", "number": number})

    def test_let_them_merge_puts_the_carried_work_on_auto_merge(self):
        w = self.world("allow")
        sha = self.carried(w)
        out = w.merge(number=51, sha=sha, repo=APP)
        self.assertTrue(out["auto_merge"])

    def test_without_that_answer_the_carried_work_waits(self):
        for option in (None, "keep", "close"):
            w = self.world(option)
            sha = self.carried(w)
            with self.assertRaises(Refused) as e:
                w.merge(number=51, sha=sha, repo=APP)
            self.assertIn("rahul05ranjan", str(e.exception))
            self.assertEqual(w.fake.auto_merged, [])

    def test_an_allow_for_a_pull_request_someone_else_opened_lets_nobody_merge(self):
        w = self.world("allow")
        w.fake.prs[(APP, 5517)]["user"] = {"login": "someone-else"}
        sha = self.carried(w)
        with self.assertRaises(Refused):
            w.merge(number=51, sha=sha, repo=APP)

    def test_close_closes_the_outside_pull_request_and_the_carried_one(self):
        w = self.world("close")
        self.carried(w)
        self.assertTrue(self.close(w)["done"])
        self.assertEqual(w.fake.prs[(APP, 5517)]["state"], "closed")
        self.assertTrue(self.close(w, 51)["done"])
        self.assertEqual(w.fake.prs[(APP, 51)]["state"], "closed")
        self.assertEqual([c[2] for c in w.fake.writes("PATCH", "/pulls/")], [{"state": "closed"}] * 2)

    def test_nothing_closes_without_patricks_answer(self):
        for option in (None, "keep", "carry"):
            w = self.world(option)
            with self.assertRaises(Refused) as e:
                self.close(w)
            self.assertIn("answer", str(e.exception))
            self.assertEqual(w.fake.prs[(APP, 5517)]["state"], "open")

    def test_allow_closes_theirs_only_once_ours_merged(self):
        w = self.world("allow")
        self.carried(w)
        with self.assertRaises(Refused) as e:
            self.close(w)
        self.assertIn("#51", str(e.exception))
        w.fake.prs[(APP, 51)]["merged"] = True
        self.assertTrue(self.close(w)["done"])
        self.assertEqual(w.fake.prs[(APP, 5517)]["state"], "closed")

    def test_only_bramble_closes_and_never_a_pull_request_the_card_does_not_name(self):
        w = self.world("close")
        with self.assertRaises(Refused):
            self.close(w, bot="sprocket")
        w.green_pr(repo=APP)
        with self.assertRaises(Refused):
            self.close(w, number=42)
        self.assertEqual(w.fake.prs[(APP, 42)]["state"], "open")

    def test_a_card_that_names_a_teams_pull_request_as_outside_closes_nothing(self):
        w = self.world("close")
        w.fake.prs[(APP, 5517)]["user"] = {"login": "EvilPatrick06"}
        w.fake.prs[(APP, 5517)]["head"]["repo"] = {"full_name": APP}
        with self.assertRaises(Refused):
            self.close(w)

    def test_with_no_route_to_the_answers_nothing_closes(self):
        w = self.world("close")
        w.git.ask_decide = None
        with self.assertRaises(Refused) as e:
            self.close(w)
        self.assertIn("no route", str(e.exception))


class AutoMerge(unittest.TestCase):
    """Patrick, 2 Nov 2026: "He shouldn't have to be merging to begin with".
    In Thornswood and the fork tw-git turns GitHub's auto-merge on for a Bot's
    pull request at its exact head, and GitHub merges once the checks the
    branch's rules require pass. Nothing waits for a yes from Patrick."""

    def test_a_bots_pr_goes_on_auto_merge_and_nothing_merges_directly(self):
        w = World()
        w.fake.merge_queues.add((APP, "main"))
        sha = w.green_pr(repo=APP, checks=False, files=[".github/workflows/build-pr.yml"])
        out = w.merge(sha=sha, repo=APP)
        self.assertTrue(out["done"])
        self.assertTrue(out["auto_merge"])
        # The queue's own method applies, so none is sent.
        self.assertEqual(w.fake.auto_merged, [(APP, 42, sha, None)])
        self.assertEqual(w.fake.writes("PUT"), [], "no direct merge")
        self.assertEqual(w.fake.queued, [], "GitHub queues it once the checks pass, not tw-git")
        merge_tokens = [t for t in w.fake.tokens if t["permissions"].get("pull_requests") == "write"]
        self.assertEqual([t["installation"] for t in merge_tokens], ["177392923"])

    def test_the_fork_goes_on_auto_merge_with_a_merge_commit(self):
        w = World()
        sha = w.green_pr(repo=FORK, branch="wisp/77-equip-drop", checks=False)
        out = w.merge(sha=sha, repo=FORK)
        self.assertTrue(out["auto_merge"])
        self.assertEqual(w.fake.auto_merged, [(FORK, 42, sha, "MERGE")])

    def test_a_threads_pr_is_its_owners_to_put_on_auto_merge(self):
        w = World()
        sha = w.push("sprocket", "sprocket/3561-path-fence", {"a": "a\n"}, repo=APP)["sha"]
        w.fake.add_pr(APP, 42, head_ref="claude/5381-avalonia", head_sha=sha, files=["a"], author="EvilPatrick06")
        with self.assertRaises(Refused) as e:
            w.merge(sha=sha, repo=APP)
        self.assertIn("owner puts it on auto-merge", str(e.exception))
        self.assertEqual(w.fake.auto_merged, [])

    def test_rules_that_do_not_require_the_check_refuse(self):
        for rules in ([], ["Release note"]):
            w = World()
            w.fake.branch_rules[(APP, "main")] = rules
            sha = w.green_pr(repo=APP, checks=False)
            with self.assertRaises(Refused) as e:
                w.merge(sha=sha, repo=APP)
            self.assertIn("Build check result", str(e.exception))
            self.assertEqual(w.fake.auto_merged, [])

    def test_a_red_check_is_never_put_on_auto_merge(self):
        w = World()
        sha = w.green_pr(repo=APP, checks=False)
        w.fake.add_check(APP, sha, "Launcher builds", conclusion="failure")
        with self.assertRaises(Refused) as e:
            w.merge(sha=sha, repo=APP)
        self.assertIn("failure", str(e.exception))
        self.assertEqual(w.fake.auto_merged, [])

    def test_a_moved_head_and_a_worker_are_refused(self):
        w = World()
        sha = w.green_pr(repo=APP, checks=False)
        with self.assertRaises(Refused):
            w.merge(sha="d" * 40, repo=APP)
        with self.assertRaises(Refused):
            w.merge(bot="sprocket", sha=sha, repo=APP)
        self.assertEqual(w.fake.auto_merged, [])

    def test_a_lost_answer_is_read_back_not_sent_twice(self):
        w = World()
        sha = w.green_pr(repo=APP, checks=False)
        w.fake.lose_answer.add("auto_merge")
        with self.assertRaises(Refused) as e:
            w.merge(sha=sha, repo=APP)
        self.assertIn("read back", str(e.exception))
        w2 = w.restart()
        out = w2.merge(sha=sha, repo=APP)
        self.assertTrue(out["reconciled"])
        self.assertEqual(out["receipt"], {"auto_merge": True})
        self.assertEqual(len(w2.fake.auto_merged), 1)


class PullRequestsAndIssues(unittest.TestCase):
    def test_a_worker_opens_a_pr_from_its_own_branch_once(self):
        w = World()
        w.push("sprocket", "sprocket/1-a", {"a": "a\n"})
        args = {"bot": "sprocket", "repo": APP, "task": "t-1", "head": "sprocket/1-a", "title": "Fence", "body": "b"}
        out = w.git.open_pr("hermes", args)
        self.assertTrue(out["done"])
        with self.assertRaises(Refused):
            w.git.open_pr("hermes", args)
        self.assertEqual(len(w.fake.writes("POST", "/pulls")), 1)

    def test_a_second_pr_for_the_same_issue_is_refused_while_the_first_is_open(self):
        """8 Nov 2026: Bramble opened #5502 and #5503 for #5495 fifteen
        seconds apart, from two branches, and both merged."""
        w = World()
        w.push("sprocket", "sprocket/1-a", {"a": "a\n"})
        w.push("sprocket", "sprocket/1-b", {"b": "b\n"})
        w.push("sprocket", "sprocket/12-c", {"c": "c\n"})
        w.git.open_pr("hermes", {"bot": "sprocket", "repo": APP, "task": "t-1", "head": "sprocket/1-a", "title": "A"})
        with self.assertRaises(Refused) as e:
            w.git.open_pr("hermes", {"bot": "sprocket", "repo": APP, "task": "t-1", "head": "sprocket/1-b", "title": "B"})
        self.assertIn("already open for #1", str(e.exception))
        out = w.git.open_pr("hermes", {"bot": "sprocket", "repo": APP, "task": "t-12", "head": "sprocket/12-c",
                                       "title": "C"})
        self.assertTrue(out["done"], "issue 12 is not issue 1")
        first = next(p for p in w.fake.prs.values() if p["head"]["ref"] == "sprocket/1-a")
        first["state"] = "closed"
        out = w.git.open_pr("hermes", {"bot": "sprocket", "repo": APP, "task": "t-1", "head": "sprocket/1-b",
                                       "title": "B"})
        self.assertTrue(out["done"], "once the first is closed, a new one may open")

    def test_a_pr_from_another_bots_branch_is_refused(self):
        w = World()
        w.push("rift", "rift/1-a", {"a": "a\n"})
        with self.assertRaises(Refused):
            w.git.open_pr("hermes", {"bot": "sprocket", "repo": APP, "task": "t", "head": "rift/1-a", "title": "x"})

    def test_a_worker_comments_only_on_its_claimed_issue(self):
        w = World()
        w.fake.add_issue(APP, 3150, labels=["claimed: Sprocket"])
        w.fake.add_issue(APP, 3151, labels=["claimed: Rift"])
        out = w.git.comment("hermes", {"bot": "sprocket", "repo": APP, "task": "t", "number": 3150, "body": "Fixed."})
        self.assertTrue(out["done"])
        with self.assertRaises(Refused):
            w.git.comment("hermes", {"bot": "sprocket", "repo": APP, "task": "t", "number": 3151, "body": "Mine now."})

    def test_a_comment_on_a_pull_request_is_sent_with_pull_requests_write(self):
        # GitHub answers 403 to a comment on a pull request from a token with
        # issues write alone (Thornswood-Ops #220, 2 Nov 2026); the stand-in
        # does the same, so this fails with the issues-only token.
        w = World()
        w.green_pr(repo=APP)
        out = w.git.comment("hermes", {"bot": "sprocket", "repo": APP, "task": "t", "number": 42, "body": "Pushed the fix."})
        self.assertTrue(out["done"], out)
        self.assertEqual(w.fake.tokens[-1]["permissions"], {"pull_requests": "write", "metadata": "read"})

    def test_a_label_on_a_pull_request_is_sent_with_pull_requests_write(self):
        w = World()
        w.green_pr(repo=APP)
        out = w.git.label("hermes", {"bot": "sprocket", "repo": APP, "task": "t", "number": 42,
                                     "label": "claimed: Sprocket", "action": "add"})
        self.assertTrue(out["done"], out)
        self.assertEqual(w.fake.tokens[-1]["permissions"], {"pull_requests": "write", "metadata": "read"})

    def test_a_comment_on_an_issue_keeps_issues_write(self):
        w = World()
        w.fake.add_issue(APP, 3150, labels=["claimed: Sprocket"])
        w.git.comment("hermes", {"bot": "sprocket", "repo": APP, "task": "t", "number": 3150, "body": "Fixed."})
        self.assertEqual(w.fake.tokens[-1]["permissions"], {"issues": "write", "metadata": "read"})

    def test_hands_off_issues_get_no_comment(self):
        w = World()
        w.fake.add_issue(APP, 3152, author="WiccanDjinn", assignees=["WiccanDjinn"], labels=["claimed: Sprocket"])
        with self.assertRaises(Refused):
            w.git.comment("hermes", {"bot": "sprocket", "repo": APP, "task": "t", "number": 3152, "body": "x"})

    def test_a_lost_comment_answer_is_found_by_its_marker(self):
        w = World()
        w.fake.add_issue(APP, 3150, labels=["claimed: Sprocket"])
        w.fake.lose_answer.add(f"POST /repos/{APP}/issues/3150/comments")
        args = {"bot": "sprocket", "repo": APP, "task": "t", "number": 3150, "body": "Fixed."}
        with self.assertRaises(Refused):
            w.git.comment("hermes", args)
        out = w.git.comment("hermes", args)
        self.assertTrue(out["reconciled"])
        self.assertEqual(len(w.fake.comments[(APP, 3150)]), 1)

    def test_any_bot_opens_a_plain_issue_unclaimed_once(self):
        # Patrick's answer on dec_zf92laU0xp8mvmyfa8C7pA, 2 Nov 2026: an answered card
        # with no issue needs one before its DECIDED.md line, and tw_git had no way to open it.
        w = World()
        args = {"bot": "bramble", "repo": APP, "task": "dec_x", "title": "Record the #5327 merge answer",
                "body": "Patrick answered the #5327 merge card.\n\nDone when DECIDED.md carries it."}
        out = w.git.open_issue("hermes", args)
        self.assertTrue(out["done"], out)
        issue = w.fake.issues[(APP, out["receipt"]["number"])]
        self.assertEqual(issue["title"], "Record the #5327 merge answer")
        self.assertEqual((issue["labels"], issue["assignees"]), ([], []))
        self.assertEqual(issue["user"]["login"], "thornswood-bramble[bot]")
        self.assertIn({"issues": "write", "metadata": "read"}, [t["permissions"] for t in w.fake.tokens])
        # The same ask again files nothing more.
        with self.assertRaises(Refused):
            w.git.open_issue("hermes", args)
        self.assertEqual(len(w.fake.writes("POST", f"/repos/{APP}/issues")), 1)
        # A worker files through the workers' App.
        out = w.git.open_issue("hermes", dict(args, bot="sprocket", title="A finding"))
        self.assertEqual(w.fake.issues[(APP, out["receipt"]["number"])]["user"]["login"], "thornswood-workers[bot]")

    def test_a_lost_issue_answer_is_found_by_its_marker(self):
        w = World()
        w.fake.lose_answer.add(f"POST /repos/{APP}/issues")
        args = {"bot": "bramble", "repo": APP, "task": "t", "title": "A finding", "body": "What. Done when x."}
        with self.assertRaises(Refused):
            w.git.open_issue("hermes", args)
        out = w.git.open_issue("hermes", args)
        self.assertTrue(out["reconciled"], out)
        self.assertEqual(len([k for k in w.fake.issues if k[0] == APP]), 1)

    def test_an_issue_that_is_not_plain_or_not_in_a_bots_repo_is_refused(self):
        w = World()
        base = {"bot": "bramble", "repo": APP, "task": "t", "title": "A finding", "body": "What. Done when x."}
        for bad in ({"body": "What.\n\n---\n_Generated by [Claude Code](https://claude.ai/code)_"},
                    {"body": "What.\n---"}, {"body": " "}, {"title": ""}, {"title": "two\nlines"},
                    {"repo": "ThornswoodRP/Thornswood-Hermes"}, {"repo": "someone/else"}, {"bot": "patrick"}):
            with self.subTest(bad=bad), self.assertRaises(Refused):
                w.git.open_issue("hermes", dict(base, **bad))
        self.assertEqual(w.fake.writes("POST", "/issues"), [])

    def test_the_board_comes_back_in_do_order_and_eligible_keeps_what_may_be_picked(self):
        w = World()

        def item(n, order, status="Todo", labels=(), assignees=(), author="EvilPatrick06", state="OPEN"):
            return {"status": {"name": status}, "order": {"number": order} if order is not None else None,
                    "content": {"__typename": "Issue", "number": n, "title": f"t{n}", "state": state,
                                "repository": {"nameWithOwner": APP}, "author": {"login": author},
                                "labels": {"nodes": [{"name": l} for l in labels]},
                                "assignees": {"nodes": [{"login": a} for a in assignees]}}}

        w.fake.board_items = [item(10000 + i, 10000 + i) for i in range(150)] + [
            item(5, 5), item(6, 6, status="In Progress"), item(7, 7, labels=["claimed: Wisp"]),
            item(8, 8, assignees=["WiccanDjinn"]), item(9, 9, author="WiccanDjinn"),
            item(10, 10, labels=["claimed: Anvil"]), item(11, 11, state="CLOSED"),
            item(12, 12, assignees=["EvilPatrick06"]), item(13, None),
            {"status": None, "order": None, "content": {"__typename": "DraftIssue"}}]
        out = w.git.board("hermes", {"bot": "bramble", "limit": 4})
        self.assertEqual([r["number"] for r in out["issues"]], [5, 6, 7, 8])
        picks = w.git.board("hermes", {"bot": "rift", "eligible": True, "limit": 3})
        self.assertEqual([r["number"] for r in picks["issues"]], [5, 12, 10000])
        self.assertEqual(picks["open"], 150 + 3)
        self.assertEqual(picks["issues"][0]["order"], 5)
        # The workers' App reads it, with organization projects read.
        self.assertIn({"organization_projects": "read", "issues": "read", "metadata": "read"},
                      [t["permissions"] for t in w.fake.tokens])
        with self.assertRaises(Refused):
            w.git.board("hermes", {"bot": "patrick"})

    def test_reads_stay_in_the_repo(self):
        w = World()
        w.fake.add_issue(APP, 3150)
        self.assertEqual(w.git.read("hermes", {"bot": "rift", "repo": APP, "path": f"/repos/{APP}/issues/3150"})["body"]["number"], 3150)

    def test_a_bot_searches_the_issues_of_one_repo_before_filing(self):
        w = World()
        w.fake.add_issue(APP, 5547)
        w.fake.issues[(APP, 5547)]["title"] = "test-feed-store failed once reading the feed"
        w.fake.add_issue(OPS, 12)
        w.fake.issues[(OPS, 12)]["title"] = "test-feed-store note"
        q = urllib.parse.quote(f"repo:{APP} is:issue test-feed-store")
        out = w.git.read("hermes", {"bot": "sprocket", "repo": APP, "path": f"/search/issues?q={q}"})
        self.assertEqual([i["number"] for i in out["body"]["items"]], [5547])
        for bad in (f"repo:{OPS} feed", "org:ThornswoodRP feed", f"repo:{APP} repo:{OPS} feed", "feed",
                    f"repo:{APP} user:someone feed"):
            with self.subTest(q=bad), self.assertRaises(Refused):
                w.git.read("hermes", {"bot": "sprocket", "repo": APP,
                                      "path": f"/search/issues?q={urllib.parse.quote(bad)}"})
        with self.assertRaises(Refused):
            w.git.read("hermes", {"bot": "sprocket", "repo": APP, "path": "/search/code?q=" + q})
        # The repository's own fields, such as its default branch (1 Nov).
        self.assertEqual(w.git.read("hermes", {"bot": "bramble", "repo": APP, "path": f"/repos/{APP}"})["body"]["default_branch"], "main")
        for path in ("/user", f"/repos/{APP}/../../orgs/x", "/repos/EvilPatrick06/Other/issues/1",
                     f"/repos/{APP}-Ops/issues/1", f"/repos/{APP}x", f"/repos/{APP}..", ""):
            with self.subTest(path=path), self.assertRaises(Refused):
                w.git.read("hermes", {"bot": "rift", "repo": APP, "path": path})

    def test_ops_is_read_through_brambles_app_and_never_written_by_a_worker(self):
        # The workers' App is not installed on Ops (policy.yaml github_apps).
        w = World()
        w.fake.add_issue(OPS, 12, labels=["claimed: Rift"])
        before = len(w.fake.tokens)
        self.assertEqual(w.git.read("hermes", {"bot": "rift", "repo": OPS, "path": f"/repos/{OPS}/issues/12"})["body"]["number"], 12)
        self.assertEqual({t["installation"] for t in w.fake.tokens[before:]}, {"177392923"})
        with self.assertRaises(Refused) as e:
            w.git.comment("hermes", {"bot": "rift", "repo": OPS, "task": "t", "number": 12, "body": "Done."})
        self.assertIn("Bramble's alone", str(e.exception))
        with self.assertRaises(Refused):
            w.git.base("hermes", {"bot": "rift", "repo": OPS})
        self.assertEqual(w.fake.writes("POST", "/comments"), [])

    def test_only_bramble_keeps_the_ledger(self):
        w = World()
        with self.assertRaises(Refused):
            w.git.ledger_event("hermes", {"bot": "rift", "repo": APP, "issue": 1, "event": "merged"})


OPS = "ThornswoodRP/Thornswood-Ops"
BRAMBLE_INSTALLATION = "177392923"
SIX = ["claimed: Bramble", "claimed: Rift", "claimed: Sprocket", "claimed: Echo", "claimed: Magpie", "claimed: Wisp"]


class CloseIssue(unittest.TestCase):
    """Bramble closes an issue as a duplicate or as done, never as not
    planned (Patrick, 9 Nov 2026: the Bots need the access their jobs take;
    Bramble's duplicates #5580 and #5585 waited for a person to close them)."""

    def close(self, w, **args):
        base = {"bot": "bramble", "repo": APP, "task": "t", "number": 5585, "body": "Same failure, same shard."}
        return w.git.close_issue("hermes", dict(base, **args))

    def test_a_duplicate_closes_pointing_at_what_it_repeats(self):
        w = World()
        w.fake.add_issue(APP, 5585, author="thornswood-bramble[bot]")
        w.fake.add_issue(APP, 5547)
        out = self.close(w, reason="duplicate", duplicate_of=5547)
        self.assertTrue(out["done"], out)
        issue = w.fake.issues[(APP, 5585)]
        self.assertEqual((issue["state"], issue["state_reason"]), ("closed", "duplicate"))
        said = w.fake.comments[(APP, 5585)]
        self.assertEqual(len(said), 1)
        self.assertTrue(said[0]["body"].startswith("Duplicate of #5547."))
        # The same ask again changes nothing more.
        with self.assertRaises(Refused):
            self.close(w, reason="duplicate", duplicate_of=5547)
        self.assertEqual(len(w.fake.comments[(APP, 5585)]), 1)

    def test_done_needs_a_merged_pull_request_or_a_successful_run(self):
        w = World()
        w.fake.add_issue(APP, 5585)
        sha = w.green_pr(repo=APP)
        with self.assertRaises(Refused) as e:
            self.close(w, reason="completed", done_by_pr=42)
        self.assertIn("has not merged", str(e.exception))
        with self.assertRaises(Refused):
            self.close(w, reason="completed")
        w.fake.runs.append({"id": 7, "repo": OPS, "workflow": "box-run.yml", "head_sha": sha,
                            "status": "completed", "conclusion": "failure"})
        with self.assertRaises(Refused) as e:
            self.close(w, reason="completed", done_by_run=7, run_repo=OPS)
        self.assertIn("did not succeed", str(e.exception))
        w.fake.runs[-1]["conclusion"] = "success"
        out = self.close(w, reason="completed", done_by_run=7, run_repo=OPS, body="The box run finished it.")
        self.assertTrue(out["done"], out)
        self.assertEqual(w.fake.issues[(APP, 5585)]["state_reason"], "completed")

    def test_a_merged_pull_request_finishes_it(self):
        w = World()
        w.fake.add_issue(APP, 5585)
        w.green_pr(repo=APP)
        w.fake.prs[(APP, 42)]["merged"] = True
        self.assertTrue(self.close(w, reason="completed", done_by_pr=42)["done"])

    def test_what_is_refused(self):
        w = World()
        w.fake.add_issue(APP, 5585)
        w.fake.add_issue(APP, 5547)
        w.fake.add_issue(APP, 5600, author="WiccanDjinn")
        w.green_pr(repo=APP)
        for bad, why in (({"bot": "sprocket", "reason": "duplicate", "duplicate_of": 5547}, "only Bramble"),
                         ({"reason": "not_planned"}, "dropping work is Patrick's"),
                         ({"reason": "duplicate"}, "names the other issue"),
                         ({"reason": "duplicate", "duplicate_of": 5585}, "names the other issue"),
                         ({"reason": "duplicate", "duplicate_of": 5547, "body": " "}, "says why"),
                         ({"reason": "duplicate", "duplicate_of": 5547, "body": "x\n---"}, "plain"),
                         ({"reason": "duplicate", "duplicate_of": 5547, "number": 5600}, "hands off"),
                         ({"reason": "duplicate", "duplicate_of": 5547, "number": 42}, "close_pr"),
                         ({"reason": "duplicate", "duplicate_of": 5547, "repo": "ThornswoodRP/TWHermesAgents"}, "")):
            with self.subTest(bad=bad), self.assertRaises(Refused) as e:
                self.close(w, **bad)
            self.assertIn(why, str(e.exception))
        self.assertEqual(w.fake.writes("PATCH", "/issues/"), [])
        w.fake.issues[(APP, 5585)]["state"] = "closed"
        with self.assertRaises(Refused) as e:
            self.close(w, reason="duplicate", duplicate_of=5547)
        self.assertIn("not open", str(e.exception))


class ClaimLabels(unittest.TestCase):
    """Phase 3: Bramble makes the six Bot claim labels and reads them back
    (04 section 6.1)."""

    def make(self, w, bot="bramble", repo=APP, **extra):
        return w.git.claim_labels("hermes", {"bot": bot, "repo": repo, "task": "phase-3-labels", **extra})

    def test_bramble_makes_the_six_with_one_colour_and_reads_them_back(self):
        w = World()
        out = self.make(w)
        self.assertEqual([l["name"] for l in out["labels"]], SIX)
        self.assertEqual({l["color"] for l in out["labels"]}, {"e8590c"})
        self.assertEqual(out["labels"][1]["description"], "Claimed by Rift, a Thornswood Hermes Bot")
        self.assertEqual(len(w.fake.writes("POST", "/labels")), 6)
        # Every label write went through Bramble's App.
        label_tokens = [t for t in w.fake.tokens if t["permissions"].get("issues") == "write"]
        self.assertTrue(label_tokens)
        self.assertEqual({t["installation"] for t in label_tokens}, {BRAMBLE_INSTALLATION})

    def test_the_colour_is_distinct_from_the_old_claim_labels(self):
        # 04 section 6.1: one colour for the six, distinct from the old
        # labels. The old claim labels' colours, read on GitHub 31 Oct.
        old = {"5319e7", "10a37f", "0e8a16"}
        self.assertNotIn(policy().data["claims"]["label_color"].lower(), old)

    def test_a_second_run_writes_nothing(self):
        w = World()
        self.make(w)
        before = len(w.fake.writes())
        again = self.make(w)
        self.assertEqual(len(w.fake.writes()), before)
        self.assertEqual([l["name"] for l in again["labels"]], SIX)

    def test_a_label_that_drifted_is_set_back(self):
        w = World()
        self.make(w)
        w.fake.repo_labels[(APP, "claimed: echo")].update(color="ffffff", description="someone's")
        out = self.make(w)
        echo = next(l for l in out["labels"] if l["name"] == "claimed: Echo")
        self.assertEqual((echo["color"], echo["description"]), ("e8590c", "Claimed by Echo, a Thornswood Hermes Bot"))
        self.assertEqual(len(w.fake.writes("PATCH", "/labels/")), 1)
        # A second drift of the same label is a new operation, not "already done".
        w.fake.repo_labels[(APP, "claimed: echo")].update(color="000000")
        self.make(w)
        self.assertEqual(w.fake.repo_labels[(APP, "claimed: echo")]["color"], "e8590c")

    def test_only_bramble_and_only_the_bots_repos(self):
        w = World()
        for bot in ("rift", "sprocket", "echo", "magpie", "wisp"):
            with self.subTest(bot=bot), self.assertRaises(Refused):
                self.make(w, bot=bot)
        with self.assertRaises(Refused):
            self.make(w, repo=HERMES)
        with self.assertRaises(Refused):
            self.make(w, repo="someone/else")
        self.assertEqual(w.fake.writes(), [])

    def test_nothing_the_model_sends_names_a_label_or_colour(self):
        w = World()
        out = self.make(w, label="claimed: Claude", color="ff0000", name="claimed: Anvil")
        self.assertEqual([l["name"] for l in out["labels"]], SIX)
        self.assertNotIn((APP, "claimed: claude"), w.fake.repo_labels)
        self.assertNotIn((APP, "claimed: anvil"), w.fake.repo_labels)

    def test_other_labels_are_never_touched(self):
        w = World()
        w.fake.repo_labels[(APP, "claimed: claude")] = {"name": "claimed: Claude", "color": "5319e7", "description": "old"}
        self.make(w)
        self.assertEqual(w.fake.repo_labels[(APP, "claimed: claude")]["description"], "old")
        self.assertFalse([c for c in w.fake.writes() if "claimed%3A%20Claude" in c[1]])

    def test_a_lost_answer_is_read_back_not_written_twice(self):
        w = World()
        w.fake.lose_answer.add(f"POST /repos/{APP}/labels")
        with self.assertRaises(Refused):
            self.make(w)
        out = self.make(w)
        self.assertEqual([l["name"] for l in out["labels"]], SIX)
        self.assertEqual(len(w.fake.writes("POST", "/labels")), 6)

    def test_the_tool_offers_every_op_the_broker_has(self):
        from thornswood_guard.brokers import GIT_OPS
        # vm_install is root's follower's, never a Bot's tool (vm_report.py).
        self.assertEqual(sorted(GIT_OPS + ["refused", "vm_install"]), sorted(World().git.ops()))
        self.assertEqual(World().git.ops()["vm_install"][1], {"root"})


class Runs(unittest.TestCase):
    def test_a_worker_starts_no_run(self):
        w = World()
        for name, raw in (("scripts/vps/gate/verbs.json", FIXTURES / "verbs.json"),
                          ("scripts/release/nexus-verbs.json", FIXTURES / "nexus-verbs.json")):
            w.fake.contents[(APP, w.fake.ref(APP, "main"), name)] = raw.read_bytes()
        with self.assertRaises(Refused):
            w.git.start_run("hermes", {"bot": "sprocket", "task": "t", "workflow": "ship.yml", "inputs": {"channel": "dev"}})
        self.assertEqual(w.fake.runs, [])

    def lists(self, w: World) -> None:
        for name, raw in (("scripts/vps/gate/verbs.json", FIXTURES / "verbs.json"),
                          ("scripts/release/nexus-verbs.json", FIXTURES / "nexus-verbs.json")):
            w.fake.contents[(APP, w.fake.ref(APP, "main"), name)] = raw.read_bytes()

    def test_box_run_starts_in_ops_with_the_line_checked_against_thornswoods_main(self):
        # box-run.yml lives in Ops since Thornswood #4013; a dispatch to
        # Thornswood found no such workflow.
        w = World()
        self.lists(w)
        out = w.git.start_run("hermes", {"bot": "bramble", "task": "logs", "workflow": "box-run.yml",
                                         "inputs": {"line": "Get-Logs -Dev -Lines 2000"}})
        self.assertTrue(out["done"], out)
        self.assertEqual(out["commit"], w.fake.ref(APP, "main"))
        self.assertEqual((out["repo"], out["repo_commit"]), (OPS, w.fake.ref(OPS, "main")))
        self.assertEqual([r["workflow"] for r in w.fake.runs], ["box-run.yml"])
        self.assertEqual(w.fake.runs[0]["head_sha"], w.fake.ref(OPS, "main"))
        self.assertEqual(w.fake.writes("POST", f"/repos/{APP}/actions"), [], "nothing was dispatched to Thornswood")
        self.assertEqual(len(w.fake.writes("POST", f"/repos/{OPS}/actions/workflows/box-run.yml/dispatches")), 1)

    def test_a_run_started_with_only_what_the_tool_takes_starts_on_main(self):
        # The cause of every run being refused on 2 Nov ("runs start on main
        # only, not "): the broker read a ref the tw_git tool has no field
        # for, so a Bot could never send one. A call is only what the
        # schema lets a Bot send, and the run starts on main.
        from thornswood_guard.brokers import TW_GIT_SCHEMA
        allowed = set(TW_GIT_SCHEMA["parameters"]["properties"])
        self.assertNotIn("ref", allowed)
        args = {"op": "start_run", "bot": "bramble", "task": "5334", "workflow": "box-run.yml",
                "inputs": {"line": "Get-Logs -Dev -Lines 2000"}}
        self.assertLessEqual(set(args) - {"bot"}, allowed)
        w = World()
        self.lists(w)
        out = w.git.start_run("hermes", {k: v for k, v in args.items() if k != "op"})
        self.assertTrue(out["done"], out)
        sent = w.fake.writes("POST", f"/repos/{OPS}/actions/workflows/box-run.yml/dispatches")
        self.assertEqual([x[2]["ref"] for x in sent], ["main"])

    def test_box_run_still_refuses_a_line_outside_thornswoods_verbs(self):
        w = World()
        self.lists(w)
        with self.assertRaises(Refused) as e:
            w.git.start_run("hermes", {"bot": "bramble", "task": "t", "workflow": "box-run.yml",
                                       "inputs": {"line": "Set-Anything -Now"}})
        self.assertIn("gate would refuse", str(e.exception))
        self.assertEqual(w.fake.runs, [])

    def test_release_runs_still_start_in_thornswood(self):
        w = World()
        self.lists(w)
        main = w.fake.ref(APP, "main")
        out = w.git.start_run("hermes", {"bot": "bramble", "task": "tag", "workflow": "release-tag.yml",
                                         "inputs": {"tag": "v1.2.3", "commit": main}})
        self.assertTrue(out["done"], out)
        self.assertNotIn("repo", out)
        self.assertEqual(w.fake.runs[0]["head_sha"], main)

    def test_a_run_repo_outside_the_repos_is_a_broken_policy(self):
        from thornswood_guard.policy import Policy, PolicyError
        data = json.loads(json.dumps(policy().data))
        data["runs"]["workflows"]["box-run.yml"]["repo"] = "someone/else"
        with self.assertRaises(PolicyError):
            Policy(data)




class OneExecutor(unittest.TestCase):
    """Until the cutover the Bots read and plan only (the shadow run), and
    Patrick's Hermes is the one executor: every GitHub write through the
    socket is refused, whatever a Bot asks, and nothing reaches GitHub."""

    def call(self, w: World, op: str, args: dict) -> dict:
        handler, callers = w.git.ops()[op]
        self.assertEqual(callers, {"hermes"})
        return handler("hermes", json.loads(json.dumps(args)))

    def test_the_shipped_policy_names_bramble_since_the_cutover(self):
        # Patrick, 1 Nov 2:33 PM Mountain: "Bramble can take operational authority now".
        self.assertEqual(policy().data["executor"], "bramble")

    def test_every_write_is_refused_before_the_cutover(self):
        w = World()
        w.git.policy.data["executor"] = "patrick-hermes"
        sha = w.green_pr(repo=APP)
        main = w.fake.ref(APP, "main")
        name, _ = w.bundle("sprocket", "sprocket/3561-other", {"x.txt": "x\n"})
        calls = len(w.fake.calls)
        asks = {
            "push": {"bot": "sprocket", "repo": APP, "task": "t", "branch": "sprocket/3561-other", "bundle": name},
            "open_pr": {"bot": "sprocket", "repo": APP, "task": "t", "head": "sprocket/3561-path-fence", "title": "t", "body": "b"},
            "edit_pr": {"bot": "sprocket", "repo": APP, "task": "t", "number": 42, "title": "t2"},
            "open_issue": {"bot": "sprocket", "repo": APP, "task": "t", "title": "A finding", "body": "Done when x."},
            "comment": {"bot": "sprocket", "repo": APP, "task": "t", "number": 42, "body": "hi"},
            "label": {"bot": "sprocket", "repo": APP, "task": "t", "number": 42, "label": "claimed: Sprocket"},
            "claim_labels": {"bot": "bramble", "repo": APP, "task": "t"},
            "merge": {"bot": "bramble", "repo": APP, "task": "t", "number": 42, "sha": sha},
            "close_pr": {"bot": "bramble", "repo": APP, "task": "t", "number": 42},
            "close_issue": {"bot": "bramble", "repo": APP, "task": "t", "number": 42, "reason": "duplicate",
                            "duplicate_of": 41, "body": "Same."},
            "start_run": {"bot": "bramble", "repo": APP, "task": "t", "workflow": "release.yml",
                          "commit": main, "inputs": {}},
        }
        self.assertEqual(sorted(asks), sorted(GitService.WRITES))
        for op, args in asks.items():
            with self.subTest(op=op), self.assertRaises(Refused) as e:
                self.call(w, op, args)
            self.assertIn("until the cutover the Bots read and plan only", str(e.exception))
            self.assertIn("patrick-hermes is the one executor", str(e.exception))
        self.assertEqual(w.fake.calls[calls:], [], "nothing reached GitHub")
        self.assertFalse(w.fake.prs[(APP, 42)]["merged"])
        self.assertIsNone(w.fake.ref(APP, "sprocket/3561-other"))

    def test_reads_and_the_base_still_work_in_the_shadow_run(self):
        w = World()
        self.assertEqual(self.call(w, "read", {"bot": "rift", "repo": APP, "path": f"/repos/{APP}"})["body"]["default_branch"], "main")
        self.assertTrue(self.call(w, "base", {"bot": "sprocket", "repo": APP})["bundle"])

    def test_after_the_cutover_the_same_merge_goes_through(self):
        w = World()
        sha = w.green_pr()
        w.git.policy.data["executor"] = "bramble"
        out = self.call(w, "merge", {"bot": "bramble", "repo": OPS, "task": "merge-42", "number": 42, "sha": sha})
        self.assertTrue(out["done"], out)
        self.assertTrue(w.fake.prs[(OPS, 42)]["merged"])
        # A worker still never merges.
        with self.assertRaises(Refused):
            self.call(w, "merge", {"bot": "sprocket", "repo": OPS, "task": "t", "number": 42, "sha": sha})

    def test_an_unknown_executor_is_a_broken_policy(self):
        from thornswood_guard.policy import Policy, PolicyError
        data = json.loads(json.dumps(policy().data))
        data["executor"] = "rift"
        with self.assertRaises(PolicyError):
            Policy(data)
        del data["executor"]
        with self.assertRaises(PolicyError):
            Policy(data)


class CanaryOneExecutor(unittest.TestCase):
    """tw-canary's check, against a real tw-git socket: the ASK script the
    canary runs as hermes, and how the canary reads the answer."""

    def serve(self, w: World) -> str:
        import os
        import threading
        from thornswood_brokers import wire
        sock = str(w.root / "git.sock")
        me = wire.user_name(os.getuid())
        ops = {n: (h, {me}) for n, (h, _) in w.git.ops().items()}
        server = wire.Server(sock, ops, mode=0o600)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return sock

    def ask(self, sock: str, sha: str, number: int = 42) -> str:
        from thornswood_brokers import canary
        args = {"bot": "bramble", "repo": APP, "task": "tw-canary", "number": number, "sha": sha}
        return subprocess.run([_sys.executable, "-I", "-c", canary.ASK, sock, "merge", json.dumps(args)],
                              capture_output=True, text=True, timeout=60).stdout

    def test_before_the_cutover_the_rule_refuses_it(self):
        from thornswood_brokers.canary import executor_problem
        w = World()
        w.git.policy.data["executor"] = "patrick-hermes"
        sha = w.green_pr(repo=APP)
        sock = self.serve(w)
        # Even a merge that would go through: the head is right and green.
        out = self.ask(sock, sha)
        self.assertIsNone(executor_problem("patrick-hermes", out), out)
        self.assertFalse(w.fake.prs[(APP, 42)]["merged"])

    def test_after_the_cutover_the_zero_head_is_refused_and_nothing_merges(self):
        from thornswood_brokers.canary import executor_problem
        w = World()
        w.green_pr(repo=APP)
        w.git.policy.data["executor"] = "bramble"
        out = self.ask(self.serve(w), "0" * 40)
        self.assertIsNone(executor_problem("bramble", out), out)
        self.assertIn("head moved", out)
        self.assertFalse(w.fake.prs[(APP, 42)]["merged"])

    def test_reading_the_answer(self):
        from thornswood_brokers.canary import executor_problem
        self.assertIn("did the merge", executor_problem("patrick-hermes", '{"ok": true, "result": {}}'))
        self.assertIn("not by the one-executor rule",
                      executor_problem("patrick-hermes", '{"ok": false, "reason": "the head moved"}'))
        self.assertIn("did not answer", executor_problem("patrick-hermes", '{"ok": false, "reason": "NOCONNECT [Errno 2]"}'))
        self.assertIn("no answer the canary can read", executor_problem("bramble", ""))

class MainCards(unittest.TestCase):
    """Step 6: a decision core card id is Patrick's tap on a Main card. tw-git
    reads his answer from the core and tw-policy records it before the check."""

    HEAD = "c" * 40

    def setUp(self):
        self.w = World()
        self.decisions = self.w.git.rules.decisions

    def main(self, **kw):
        a = {"decision_id": "dec_mainpublish0001", "for": "publish", "repo": APP, "commit": self.HEAD, "pr": None,
             "revision": 1, "option": "authorize", "answered_at": self.w.policy.clock().isoformat(), "restored": False}
        a.update(kw)
        self.w.mains[a["decision_id"]] = a
        return a["decision_id"]

    def check(self, did, commit=None):
        return self.decisions.check(did, kind="publish", repo=APP, commit=commit or self.HEAD)

    def test_unanswered_says_so(self):
        v = self.check("dec_nothing")
        self.assertFalse(v.allowed)
        self.assertIn("has not answered", v.reason)

    def test_authorize_is_recorded_and_allows_once(self):
        did = self.main()
        self.assertTrue(self.check(did).allowed)
        self.assertEqual(self.w.policy._decision(did).answered_by, "EvilPatrick06")
        self.w.ask("decision_use", {"id": did})
        v = self.check(did)
        self.assertFalse(v.allowed)
        self.assertIn("used once", v.reason)

    def test_another_answer_or_commit_allows_nothing(self):
        self.assertIn("authorizes nothing", self.check(self.main(option="not_now")).reason)
        self.assertFalse(self.check(self.main(decision_id="dec_other"), commit="d" * 40).allowed)

    def test_an_answer_from_before_a_restore_allows_nothing(self):
        v = self.check(self.main(restored=True))
        self.assertFalse(v.allowed)
        self.assertIn("restored", v.reason)

    def test_with_no_route_to_the_core_a_main_card_allows_nothing(self):
        self.w.git.rules.decisions.ask_decide = None
        self.assertIn("no route", self.check(self.main()).reason)

    def test_a_record_root_made_is_checked_as_before(self):
        from thornswood_guard.decisions import new_decision
        d = new_decision(id="d-root", kind="publish", repo=APP, commit=self.HEAD, revision=1,
                         answered_by="EvilPatrick06", at=self.w.policy.clock())
        self.w.policy.decision_record("root", {"decision": d.__dict__})
        self.assertTrue(self.check("d-root").allowed)


if __name__ == "__main__":
    unittest.main()
