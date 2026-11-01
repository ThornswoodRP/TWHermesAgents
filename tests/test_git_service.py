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
from pathlib import Path

from fake_github import FakeGitHub, git
from helpers import FIXTURES, policy

from test_policy_service import service as policy_service

from thornswood_brokers.git_service import GitService
from thornswood_brokers.github_api import GitHub
from thornswood_brokers.wire import Refused

APP = "ThornswoodRP/Thornswood"
FORK = "ThornswoodRP/skymp"
HERMES = "ThornswoodRP/Thornswood-Hermes"


class World:
    """A fake GitHub, tw-policy on a temp store, and tw-git between them."""

    def __init__(self, db: Path | None = None, fake: FakeGitHub | None = None, root: Path | None = None):
        self.root = root or Path(tempfile.mkdtemp())
        self.fake = fake or FakeGitHub(self.root / "gh")
        if fake is None:
            self.fake.make_repo(APP)
            self.fake.make_repo(FORK, base="thornswood/engine-changes")
        self.db = db or self.root / "policy.db"
        self.policy = policy_service(db=self.db)
        self.spool = self.root / "spool"
        p = policy()
        apps = {k: dict(v, key_file="/nonexistent") for k, v in p.data["brokers"]["apps"].items()}
        gh = GitHub(apps, transport=self.fake, signer=lambda data, key: b"sig")
        self.git = GitService(p, gh, self.ask, mirrors=self.root / "mirrors", spool=self.spool,
                              git_base=f"file://{self.fake.remotes}/", allow_file_protocol=True)

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

    def green_pr(self, number: int = 42, branch: str = "sprocket/3561-path-fence",
                 files: list[str] | None = None) -> str:
        sha = self.push("sprocket", branch, {"launcher/PathFence.cs": "fence\n"})["sha"]
        self.fake.add_pr(APP, number, head_ref=branch, head_sha=sha, files=files or ["launcher/PathFence.cs"])
        for name in ("Launcher builds", "Build check result"):
            self.fake.add_check(APP, sha, name)
        return sha

    def merge(self, bot: str = "bramble", number: int = 42, sha: str = "", **extra) -> dict:
        return self.git.merge("hermes", {"bot": bot, "repo": APP, "task": "merge-42", "number": number,
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
        ops = World().git.ops()
        self.assertTrue(all(users == {"hermes"} for _, users in ops.values()))


class Merge(unittest.TestCase):
    def test_bramble_merges_on_both_checks_at_the_exact_head(self):
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
        sha = w.green_pr()
        with self.assertRaises(Refused) as e:
            w.merge(bot="sprocket", sha=sha)
        self.assertIn("only Bramble merges", str(e.exception))
        self.assertEqual(w.fake.writes("PUT"), [])

    def test_attack_10_a_check_posted_by_another_app_does_not_count(self):
        w = World()
        sha = w.push("sprocket", "sprocket/3561-path-fence", {"a": "a\n"})["sha"]
        w.fake.add_pr(APP, 42, head_ref="sprocket/3561-path-fence", head_sha=sha, files=["a"])
        w.fake.add_check(APP, sha, "Launcher builds")
        w.fake.add_check(APP, sha, "Build check result", app="thornswood-workers")
        with self.assertRaises(Refused) as e:
            w.merge(sha=sha)
        self.assertIn("Build check result", str(e.exception))
        self.assertEqual(w.fake.writes("PUT"), [])

    def test_the_latest_run_decides(self):
        w = World()
        sha = w.green_pr()
        w.fake.add_check(APP, sha, "Launcher builds", conclusion="failure")
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
        w.fake.prs[(APP, 42)]["changed_files"] = 4000
        with self.assertRaises(Refused) as e:
            w.merge(sha=sha)
        self.assertIn("nothing merges unseen", str(e.exception))

    def test_a_box_path_needs_patricks_decision(self):
        w = World()
        sha = w.green_pr(files=["scripts/vps/gate/Run-Verb.ps1"])
        w.fake.contents[(APP, "main", "scripts/vps/gate/verbs.json")] = (FIXTURES / "verbs.json").read_bytes()
        with self.assertRaises(Refused) as e:
            w.merge(sha=sha)
        self.assertIn("live box", str(e.exception))
        self.assertEqual(w.fake.writes("PUT"), [])

    def test_attack_20_a_lost_merge_answer_is_read_back_not_merged_again(self):
        w = World()
        sha = w.green_pr()
        w.fake.lose_answer.add(f"PUT /repos/{APP}/pulls/42/merge")
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
        op = w.ask("op_begin", {"bot": "bramble", "task": "merge-42", "action": "merge", "repo": APP,
                                "target": "pr/42", "commit": sha})
        self.assertEqual(op["state"], "go")
        w.fake.prs[(APP, 42)]["merged"] = True
        w.fake.prs[(APP, 42)]["merge_commit_sha"] = "e" * 40
        w2 = w.restart()
        out = w2.merge(sha=sha)
        self.assertTrue(out["reconciled"])
        self.assertEqual(w2.fake.writes("PUT"), [])

    def test_a_lost_answer_for_a_merge_that_did_not_happen_tries_once_more(self):
        w = World()
        sha = w.green_pr()
        w.ask("op_begin", {"bot": "bramble", "task": "merge-42", "action": "merge", "repo": APP,
                           "target": "pr/42", "commit": sha})
        w2 = w.restart()
        out = w2.merge(sha=sha)
        self.assertTrue(out["done"])
        self.assertNotIn("reconciled", out)
        self.assertEqual(len(w2.fake.writes("PUT", "/merge")), 1)

    def test_someone_elses_merge_is_not_claimed(self):
        w = World()
        sha = w.green_pr()
        w.fake.prs[(APP, 42)]["merged"] = True
        with self.assertRaises(Refused) as e:
            w.merge(sha=sha)
        self.assertIn("already merged", str(e.exception))


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

    def test_reads_stay_in_the_repo(self):
        w = World()
        w.fake.add_issue(APP, 3150)
        self.assertEqual(w.git.read("hermes", {"bot": "rift", "repo": APP, "path": f"/repos/{APP}/issues/3150"})["body"]["number"], 3150)
        for path in ("/user", f"/repos/{APP}/../../orgs/x", "/repos/EvilPatrick06/Other/issues/1"):
            with self.subTest(path=path), self.assertRaises(Refused):
                w.git.read("hermes", {"bot": "rift", "repo": APP, "path": path})

    def test_only_bramble_keeps_the_ledger(self):
        w = World()
        with self.assertRaises(Refused):
            w.git.ledger_event("hermes", {"bot": "rift", "repo": APP, "issue": 1, "event": "merged"})


OPS = "ThornswoodRP/Thornswood-Ops"
BRAMBLE_INSTALLATION = "177392923"
SIX = ["claimed: Bramble", "claimed: Rift", "claimed: Sprocket", "claimed: Echo", "claimed: Magpie", "claimed: Wisp"]


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
        self.assertEqual(sorted(GIT_OPS + ["refused"]), sorted(World().git.ops()))


class Runs(unittest.TestCase):
    def test_a_worker_starts_no_run(self):
        w = World()
        for name, raw in (("scripts/vps/gate/verbs.json", FIXTURES / "verbs.json"),
                          ("scripts/release/nexus-verbs.json", FIXTURES / "nexus-verbs.json")):
            w.fake.contents[(APP, w.fake.ref(APP, "main"), name)] = raw.read_bytes()
        with self.assertRaises(Refused):
            w.git.start_run("hermes", {"bot": "sprocket", "task": "t", "workflow": "ship.yml", "inputs": {"channel": "dev"}})
        self.assertEqual(w.fake.runs, [])


if __name__ == "__main__":
    unittest.main()
