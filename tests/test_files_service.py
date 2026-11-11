"""tw-files: each Bot's tree, and nothing outside it.

Attack tests 1 (paths out of the tree), 2 in part (what a check can see, from
its systemd-run line) and 4 (hooks, filters and other repositories do
nothing) from 08 section 8, plus the hand-off to tw-git: a Bot's commit goes
out as a bundle tw-git pushes after its own checks.
"""

import sys as _sys
import unittest as _unittest

if not _sys.platform.startswith("linux"):
    # The brokers are Linux code (Unix sockets with SO_PEERCRED, pwd, O_NOFOLLOW
    # walks, systemd): they run only on the Oracle VM, and these tests run in
    # the VM half of scripts/test-at-head.sh.
    raise _unittest.SkipTest("the brokers run only on the Oracle VM; their tests run there")

import copy
import errno
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fake_github import FakeGitHub, git
from helpers import policy

from test_policy_service import service as policy_service

from thornswood_brokers.files_service import FilesService, check_argv
from thornswood_brokers.git_service import GitService
from thornswood_brokers.github_api import GitHub
from thornswood_brokers.wire import Refused
from thornswood_guard.policy import Policy

APP = "ThornswoodRP/Thornswood"
FORK = "ThornswoodRP/skymp"


class World:
    def __init__(self, data: dict | None = None, runner=None):
        self.root = Path(tempfile.mkdtemp())
        self.fake = FakeGitHub(self.root / "gh")
        self.fake.make_repo(APP, files={"README.md": "Thornswood\n", "tests/test-fence.js": "console.log('PASS fence')\n"})
        self.p = Policy(data) if data else policy()
        self.policy = policy_service(data=data) if data else policy_service()
        self.work = self.root / "work"
        self.work.mkdir()
        # As the install makes them: one setgid folder per Bot.
        for bot in self.p.bots:
            (self.work / bot).mkdir()
            (self.work / bot).chmod(0o2770)
        self.spool = self.root / "spool"
        self.files = FilesService(self.p, self.ask("tw-files"), work=self.work, spool=self.spool, runner=runner,
                                  check_helper=["helper"])
        apps = {k: dict(v, key_file="/nonexistent") for k, v in self.p.data["brokers"]["apps"].items()}
        gh = GitHub(apps, transport=self.fake, signer=lambda d, k: b"sig")
        self.git = GitService(self.p, gh, self.ask("tw-git"), mirrors=self.root / "mirrors", spool=self.spool,
                              git_base=f"file://{self.fake.remotes}/", allow_file_protocol=True)
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self.canary = self.outside / "hermes.env"
        self.canary.write_text("CANARY=not-a-secret\n")

    def ask(self, user):
        def ask(op, args):
            handler, callers = self.policy.ops()[op]
            assert user in callers, f"{user} may not call {op}"
            return handler(user, json.loads(json.dumps(args)))
        return ask

    def base_bundle(self, bot: str, repo: str = APP, base: str = "main") -> str:
        d = self.spool / "in" / bot
        d.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(self.fake.remote(repo)), "bundle", "create", "-q", str(d / "base.bundle"),
                        f"refs/heads/{base}"], check=True, capture_output=True)
        return "base.bundle"

    def checkout(self, bot="sprocket", task="3561-path-fence", branch=None, repo=APP) -> dict:
        return self.files.checkout("hermes", {"bot": bot, "repo": repo, "task": task, "bundle": self.base_bundle(bot),
                                              "branch": branch or f"{bot}/{task}"})

    def a(self, bot="sprocket", task="3561-path-fence", **kw):
        return {"bot": bot, "repo": APP, "task": task, **kw}

    def tree(self, bot="sprocket", task="3561-path-fence") -> Path:
        return self.work / bot / "Thornswood" / task


class Work(unittest.TestCase):
    def test_a_task_from_checkout_to_tw_git_push(self):
        w = World()
        out = w.checkout()
        self.assertEqual(out["branch"], "sprocket/3561-path-fence")
        w.files.write("hermes", w.a(path="launcher/PathFence.cs", content="class PathFence {}\n"))
        w.files.edit("hermes", w.a(path="README.md", old="Thornswood", new="Thornswood, fenced"))
        self.assertIn("PathFence.cs", w.files.status("hermes", w.a())["status"])
        c = w.files.commit("hermes", w.a(message="Fence the launcher's paths"))
        self.assertEqual(c["files"], 2)
        b = w.files.bundle("hermes", w.a())
        pushed = w.git.push("hermes", {"bot": "sprocket", "repo": APP, "task": "3561", "branch": b["branch"],
                                       "bundle": b["bundle"]})
        self.assertEqual(pushed["sha"], c["commit"])
        self.assertEqual(w.fake.ref(APP, "sprocket/3561-path-fence"), c["commit"])
        author = git(w.fake.remote(APP), "log", "-1", "--format=%an <%ae>", "sprocket/3561-path-fence")
        self.assertEqual(author, "Sprocket (Thornswood Bot) <sprocket@bots.thornswood.invalid>")

    def test_tw_git_hands_tw_files_the_base(self):
        w = World()
        b = w.git.base("hermes", {"bot": "rift", "repo": APP})
        self.assertEqual(b["sha"], w.fake.ref(APP, "main"))
        out = w.files.checkout("hermes", {"bot": "rift", "repo": APP, "task": "12-load-order", "bundle": b["bundle"],
                                          "branch": "rift/12-load-order"})
        self.assertEqual(out["head"], b["sha"])
        with self.assertRaises(Refused):
            w.git.base("hermes", {"bot": "rift", "repo": FORK})

    def test_a_newer_base_replaces_the_bots_older_one(self):
        # 11 Nov 2026: no base bundle was ever deleted; 16 GB of them took the
        # VM's disk under the fleet queue's floor and no Bot turn ran.
        w = World()
        first = w.git.base("hermes", {"bot": "rift", "repo": APP})["bundle"]
        sprockets = w.git.base("hermes", {"bot": "sprocket", "repo": APP})["bundle"]
        work = Path(tempfile.mkdtemp())
        subprocess.run(["git", "clone", "-q", str(w.fake.remote(APP)), str(work)], check=True, capture_output=True)
        (work / "NEW.md").write_text("new\n")
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", "new")
        git(work, "push", "-q", "origin", "HEAD:main")
        second = w.git.base("hermes", {"bot": "rift", "repo": APP})
        self.assertNotEqual(first, second["bundle"])
        self.assertEqual(sorted(p.name for p in (w.spool / "in" / "rift").iterdir()), [second["bundle"]])
        self.assertTrue((w.spool / "in" / "sprocket" / sprockets).is_file())
        out = w.files.checkout("hermes", {"bot": "rift", "repo": APP, "task": "12-load-order",
                                          "bundle": second["bundle"], "branch": "rift/12-load-order"})
        self.assertEqual(out["head"], second["sha"])

    def test_tw_git_starts_with_one_base_per_repo_for_each_bot(self):
        w = World()
        d = w.spool / "in" / "echo"
        d.mkdir(parents=True)
        for i, name in enumerate(["Thornswood-main-aaaaaaaaaaaa.bundle", "Thornswood-main-bbbbbbbbbbbb.bundle",
                                  "Thornswood-Ops-main-cccccccccccc.bundle", "Thornswood-Ops-main-dddddddddddd.bundle",
                                  ".Thornswood-main-eeeeeeeeeeee.bundle.tmp", "work.bundle"]):
            (d / name).write_text("x")
            os.utime(d / name, (1000 + i, 1000 + i))
        gone = w.git.prune_spool_in()
        self.assertEqual(sorted(p.name for p in d.iterdir()),
                         ["Thornswood-Ops-main-dddddddddddd.bundle", "Thornswood-main-bbbbbbbbbbbb.bundle", "work.bundle"])
        self.assertEqual(len(gone), 3)
        self.assertEqual(w.git.prune_spool_in(), [])

    def test_a_tree_is_on_the_bots_own_branch_only(self):
        w = World()
        with self.assertRaises(Refused):
            w.checkout(branch="rift/3561-path-fence")
        with self.assertRaises(Refused):
            w.checkout(branch="main")

    def test_only_wisp_has_a_fork_tree(self):
        w = World()
        with self.assertRaises(Refused):
            w.files.checkout("hermes", {"bot": "sprocket", "repo": FORK, "task": "9-engine", "bundle": "base.bundle",
                                        "branch": "sprocket/9-engine"})

    def test_reads_list_and_search(self):
        w = World()
        w.checkout()
        self.assertEqual(w.files.read("hermes", w.a(path="README.md"))["text"], "Thornswood\n")
        names = [e["name"] for e in w.files.list("hermes", w.a())["entries"]]
        self.assertEqual(names, ["README.md", "tests"])
        self.assertIn("README.md:1:Thornswood", w.files.search("hermes", w.a(pattern="Thorns"))["matches"])

    def test_sizes(self):
        w = World()
        w.checkout()
        with self.assertRaises(Refused):
            w.files.write("hermes", w.a(path="big.txt", content="x" * (2 * 1024 * 1024 + 1)))
        w.files.write("hermes", w.a(path="dup.txt", content="a a"))
        with self.assertRaises(Refused) as e:
            w.files.edit("hermes", w.a(path="dup.txt", old="a", new="b"))
        self.assertIn("2 times", str(e.exception))

    def test_another_bots_tree_is_not_reachable_by_name(self):
        w = World()
        w.checkout(bot="rift", task="12-load-order")
        with self.assertRaises(Refused):
            w.files.read("hermes", w.a(task="12-load-order", path="README.md"))
        for task in ("../../rift/Thornswood/12-load-order", "12-load-order/..", "UPPER"):
            with self.subTest(task=task), self.assertRaises(Refused):
                w.files.read("hermes", w.a(task=task, path="README.md"))

    def test_trees_lists_only_the_bots_own_trees(self):
        # 31 Oct: Rift was asked for git status, and status takes a task name
        # Rift had no way to find.
        w = World()
        self.assertEqual(w.files.trees("hermes", {"bot": "sprocket"}), {"trees": []})
        w.checkout()
        w.checkout(task="3600-second")
        w.checkout(bot="rift", task="12-load-order")
        mine = w.files.trees("hermes", {"bot": "sprocket"})["trees"]
        self.assertEqual(mine, [
            {"repo": APP, "task": "3561-path-fence", "branch": "sprocket/3561-path-fence"},
            {"repo": APP, "task": "3600-second", "branch": "sprocket/3600-second"},
        ])
        self.assertEqual([t["task"] for t in w.files.trees("hermes", {"bot": "rift"})["trees"]], ["12-load-order"])
        st = w.files.status("hermes", w.a(task=mine[0]["task"]))["status"]
        self.assertTrue(st.startswith("## sprocket/3561-path-fence"), st)

    def test_trees_never_follows_a_link_and_names_a_stopped_tree(self):
        w = World()
        w.checkout()
        os.symlink(w.outside, w.work / "sprocket" / "Thornswood" / "9-out")
        with open(w.tree() / ".git" / "config", "a") as f:
            f.write('[core]\n\tfsmonitor = true\n')
        got = w.files.trees("hermes", {"bot": "sprocket"})["trees"]
        self.assertEqual([t["task"] for t in got], ["3561-path-fence"])
        self.assertIsNone(got[0]["branch"])
        self.assertIn("core.fsmonitor", got[0]["stopped"])
        with self.assertRaises(Refused):
            w.files.trees("hermes", {"bot": "nobody"})

    def test_the_tool_offers_every_op_the_broker_has(self):
        from thornswood_guard.brokers import FILES_OPS, TW_FILES_SCHEMA
        self.assertEqual(sorted(FILES_OPS), sorted(World().files.ops()))
        self.assertEqual(TW_FILES_SCHEMA["parameters"]["required"], ["op"])

    def test_a_status_without_a_task_points_at_trees(self):
        w = World()
        with self.assertRaises(Refused) as e:
            w.files.status("hermes", {"bot": "rift", "repo": APP})
        self.assertIn("trees", str(e.exception))

    def test_a_removed_tree_is_gone(self):
        w = World()
        w.checkout()
        os.symlink(w.outside, w.tree() / "out-link")
        w.files.remove("hermes", w.a())
        self.assertFalse(w.tree().exists())
        self.assertTrue(w.canary.exists())


REPO = Path(__file__).resolve().parent.parent
SUID_SGID = stat.S_ISUID | stat.S_ISGID


def restrict_suid_sgid():
    """What RestrictSUIDSGID=yes does to tw-files: its seccomp filter refuses
    with EPERM any mkdir, open with O_CREAT or chmod that asks for a setuid or
    setgid bit, before the kernel looks whether the file is there."""
    real_mkdir, real_open, real_chmod, real_fchmod = os.mkdir, os.open, os.chmod, os.fchmod

    def refuse(mode):
        if mode & SUID_SGID:
            raise PermissionError(errno.EPERM, "Operation not permitted (RestrictSUIDSGID)")

    def mkdir(path, mode=0o777, *a, **kw):
        refuse(mode)
        return real_mkdir(path, mode, *a, **kw)

    def open_(path, flags, mode=0o777, *a, **kw):
        if flags & os.O_CREAT:
            refuse(mode)
        return real_open(path, flags, mode, *a, **kw)

    def chmod(path, mode, *a, **kw):
        refuse(mode)
        return real_chmod(path, mode, *a, **kw)

    def fchmod(fd, mode):
        refuse(mode)
        return real_fchmod(fd, mode)

    return mock.patch.multiple(os, mkdir=mkdir, open=open_, chmod=chmod, fchmod=fchmod)


class UnderTheServiceSandbox(unittest.TestCase):
    """31 Oct: Rift's first checkout failed with EPERM on its own folder,
    because the broker asked mkdir for the setgid bit under RestrictSUIDSGID."""

    def test_tw_files_runs_with_restrict_suid_sgid(self):
        lines = (REPO / "deploy" / "systemd" / "tw-files.service").read_text().splitlines()
        self.assertIn("RestrictSUIDSGID=yes", lines)

    def test_the_stand_in_refuses_what_the_filter_refuses(self):
        d = Path(tempfile.mkdtemp())
        (d / "rift").mkdir()
        with restrict_suid_sgid():
            with self.assertRaises(PermissionError):
                os.mkdir(d / "rift", 0o2770)
            with self.assertRaises(FileExistsError):
                os.mkdir(d / "rift", 0o770)

    def test_a_task_from_checkout_to_bundle_under_the_sandbox(self):
        w = World()
        with restrict_suid_sgid():
            w.checkout(bot="rift", task="0-smoke-check")
            a = w.a(bot="rift", task="0-smoke-check")
            w.files.write("hermes", dict(a, path="deep/new/folder/a.txt", content="a\n"))
            w.files.edit("hermes", dict(a, path="README.md", old="Thornswood", new="Thornswood!"))
            self.assertTrue(w.files.status("hermes", a)["status"].startswith("## rift/0-smoke-check"))
            w.files.commit("hermes", dict(a, message="smoke"))
            w.files.bundle("hermes", a)
            self.assertEqual([t["task"] for t in w.files.trees("hermes", {"bot": "rift"})["trees"]], ["0-smoke-check"])
            w.files.remove("hermes", a)

    def test_new_folders_keep_the_bots_group_and_setgid_bit(self):
        w = World()
        w.checkout(bot="rift", task="0-smoke-check")
        w.files.write("hermes", w.a(bot="rift", task="0-smoke-check", path="deep/a.txt", content="a\n"))
        top = os.stat(w.work / "rift")
        for d in (w.work / "rift" / "Thornswood", w.tree("rift", "0-smoke-check"), w.tree("rift", "0-smoke-check") / "deep"):
            st = os.stat(d)
            self.assertTrue(st.st_mode & stat.S_ISGID, d)
            self.assertEqual(st.st_gid, top.st_gid, d)

    def test_the_broker_never_makes_a_bots_own_folder(self):
        w = World()
        (w.work / "wisp").rmdir()
        with self.assertRaises(Refused) as e:
            w.checkout(bot="wisp", task="1-engine")
        self.assertIn("the install makes", str(e.exception))
        self.assertFalse((w.work / "wisp").exists())


class Attack1Paths(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.checkout()

    def refused(self, op, **kw):
        with self.assertRaises(Refused):
            getattr(self.w.files, op)("hermes", self.w.a(**kw))

    def test_climbing_and_absolute_paths(self):
        for path in ("../../../rift/Thornswood/12-load-order/README.md", "/etc/passwd", "a/../../x", "C:/x", "\\etc\\x",
                     "", "a\0b"):
            with self.subTest(path=path):
                self.refused("read", path=path)
                self.refused("write", path=path, content="x")

    def test_a_planted_symlink_is_never_followed(self):
        t = self.w.tree()
        os.symlink(self.w.canary, t / "env-link")
        os.symlink(self.w.outside, t / "dir-link")
        self.refused("read", path="env-link")
        self.refused("edit", path="env-link", old="CANARY", new="x")
        self.refused("read", path="dir-link/hermes.env")
        self.refused("write", path="dir-link/hermes.env", content="pwned")
        self.refused("list", path="dir-link")
        # Writing over the link replaces the link, never what it points at.
        self.w.files.write("hermes", self.w.a(path="env-link", content="mine\n"))
        self.assertEqual(self.w.canary.read_text(), "CANARY=not-a-secret\n")
        self.assertFalse((t / "env-link").is_symlink())

    def test_a_hard_link_to_a_broker_file(self):
        t = self.w.tree()
        os.link(self.w.canary, t / "hard")
        self.refused("read", path="hard")
        self.refused("write", path="hard", content="pwned")
        self.refused("edit", path="hard", old="CANARY", new="x")
        self.assertEqual(self.w.canary.read_text(), "CANARY=not-a-secret\n")

    def test_fifos_and_devices(self):
        t = self.w.tree()
        os.mkfifo(t / "fifo")
        self.refused("read", path="fifo")
        self.refused("write", path="fifo", content="x")

    def test_the_repository_itself_is_not_a_path(self):
        for path in (".git/config", ".git/hooks/pre-commit", ".GIT/config", "sub/.git/config"):
            with self.subTest(path=path):
                self.refused("read", path=path)
                self.refused("write", path=path, content="x")


class Attack4Git(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.checkout()
        self.t = self.w.tree()
        self.marker = self.w.outside / "ran"

    def test_a_planted_hook_never_runs(self):
        hook = self.t / ".git" / "hooks" / "pre-commit"
        hook.write_text(f"#!/bin/sh\ntouch {self.marker}\n")
        hook.chmod(0o755)
        self.w.files.write("hermes", self.w.a(path="a.txt", content="a\n"))
        self.w.files.commit("hermes", self.w.a(message="a"))
        self.w.files.bundle("hermes", self.w.a())
        self.assertFalse(self.marker.exists())

    def test_a_filter_in_the_config_stops_the_tree(self):
        with open(self.t / ".git" / "config", "a") as f:
            f.write(f'[filter "x"]\n\tclean = touch {self.marker}\n')
        (self.t / ".gitattributes").write_text("* filter=x\n")
        self.w.files.write("hermes", self.w.a(path="a.txt", content="a\n"))
        with self.assertRaises(Refused) as e:
            self.w.files.commit("hermes", self.w.a(message="a"))
        self.assertIn("filter.x.clean", str(e.exception))
        self.assertFalse(self.marker.exists())

    def test_a_filter_named_only_in_gitattributes_does_nothing(self):
        (self.t / ".gitattributes").write_text("* filter=x diff=y\n")
        self.w.files.write("hermes", self.w.a(path="a.txt", content="a\n"))
        self.w.files.commit("hermes", self.w.a(message="a"))
        self.w.files.diff("hermes", self.w.a())
        self.assertFalse(self.marker.exists())

    def test_fsmonitor_pager_and_ssh_command_stop_the_tree(self):
        for key in ("core.fsmonitor", "core.pager", "core.sshCommand", "diff.external", "include.path"):
            with self.subTest(key=key):
                cfg = self.t / ".git" / "config"
                before = cfg.read_text()
                subprocess.run(["git", "config", "--file", str(cfg), key, f"touch {self.marker}"], check=True)
                with self.assertRaises(Refused):
                    self.w.files.status("hermes", self.w.a())
                cfg.write_text(before)
        self.assertFalse(self.marker.exists())

    def test_another_repository_through_alternates_or_a_gitdir_file(self):
        self.w.checkout(bot="sprocket", task="1-other")
        other = self.w.tree(task="1-other")
        alt = self.t / ".git" / "objects" / "info" / "alternates"
        alt.write_text(str(other / ".git" / "objects") + "\n")
        with self.assertRaises(Refused):
            self.w.files.status("hermes", self.w.a())
        alt.unlink()
        import shutil
        shutil.rmtree(self.t / ".git")
        (self.t / ".git").write_text(f"gitdir: {other / '.git'}\n")
        with self.assertRaises(Refused):
            self.w.files.commit("hermes", self.w.a(message="x"))

    def test_a_file_submodule_is_never_fetched(self):
        victim = self.w.outside / "victim"
        subprocess.run(["git", "init", "-q", str(victim)], check=True)
        (self.t / ".gitmodules").write_text(f'[submodule "v"]\n\tpath = v\n\turl = file://{victim}\n')
        self.w.files.write("hermes", self.w.a(path="a.txt", content="a\n"))
        self.w.files.commit("hermes", self.w.a(message="a"))
        self.assertFalse((self.t / "v" / ".git").exists())


class Checks(unittest.TestCase):
    def test_only_the_harness_forms_run(self):
        f = World().files
        for cmd in (["node", "tests/test-fence.js"], ["pwsh", "-NoProfile", "-File", "tests/test-3561-path.ps1"],
                    ["python3", "tests/test-x.py"], ["pwsh", "scripts/dev/Check-Words.ps1"]):
            with self.subTest(cmd=cmd):
                self.assertEqual(f.check_command(cmd), cmd)
        for cmd in (["pwsh", "scripts/dev/Run-Gate.ps1"], ["node", "tests/test-../../x.js"], ["node", "-e", "x"],
                    ["bash", "-c", "id"], ["node", "tests/test-a.js", "--extra"], ["python3", "tests/test-A.py"],
                    "node tests/test-x.js"):
            with self.subTest(cmd=cmd), self.assertRaises(Refused):
                f.check_command(cmd)

    def test_no_check_runs_in_the_pilot(self):
        ran = []
        w = World(runner=lambda argv, t: ran.append(argv))
        w.checkout()
        with self.assertRaises(Refused) as e:
            w.files.run_check("hermes", w.a(command=["node", "tests/test-fence.js"]))
        self.assertIn("pilot", str(e.exception))
        self.assertEqual(ran, [])

    def test_16_no_check_runs_without_a_passing_limits_check(self):
        data = copy.deepcopy(policy().data)
        data["admission"]["stage"] = "after"
        ran = []
        w = World(data=data, runner=lambda argv, t: ran.append(argv))
        w.checkout()
        with self.assertRaises(Refused) as e:
            w.files.run_check("hermes", w.a(command=["node", "tests/test-fence.js"]))
        self.assertIn("limits check", str(e.exception))
        self.assertEqual(ran, [])

    def test_a_check_after_the_pilot_goes_through_the_root_helper(self):
        data = copy.deepcopy(policy().data)
        data["admission"]["stage"] = "after"
        ran = []

        def runner(argv, timeout):
            ran.append(argv)
            return subprocess.CompletedProcess(argv, 1, "PASS one\nFAIL two\nnoise\n", "")

        w = World(data=data, runner=runner)
        w.policy.limits_record("root", {"ok": True, "problems": []})
        w.checkout()
        out = w.files.run_check("hermes", w.a(command=["node", "tests/test-fence.js"]))
        self.assertEqual(out["lines"], ["PASS one", "FAIL two"])
        self.assertEqual(ran[0][:3], ["helper", "sprocket", str(w.tree())])
        self.assertEqual(w.policy.status("hermes", {})["tickets"].get("admitted", 0), 0)

    def test_without_a_stand_in_a_check_goes_through_the_check_socket(self):
        # No sudo: tw-files asks tw-checks.socket, whose helper answers with
        # the check's exit code and output.
        import threading
        from thornswood_brokers.wire import Server
        data = copy.deepcopy(policy().data)
        data["admission"]["stage"] = "after"
        w = World(data=data)
        w.policy.limits_record("root", {"ok": True, "problems": []})
        w.checkout()
        sock = Path(tempfile.mkdtemp()) / "checks.sock"
        asked = []

        def run(caller, args):
            asked.append(args)
            return {"exit": 0, "stdout": "PASS fence\n", "stderr": ""}

        server = Server(sock, {"run": (run, {__import__("pwd").getpwuid(os.getuid()).pw_name})}, mode=0o600)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        files = FilesService(w.p, w.ask("tw-files"), work=w.work, spool=w.spool, check_socket=sock)
        out = files.run_check("hermes", w.a(command=["node", "tests/test-fence.js"]))
        self.assertEqual(out["exit"], 0)
        self.assertEqual(out["lines"], ["PASS fence"])
        self.assertEqual(asked, [{"bot": "sprocket", "tree": str(w.tree()), "command": ["node", "tests/test-fence.js"]}])

    def test_bramble_runs_no_checks(self):
        w = World()
        w.checkout(bot="bramble", task="5-notes")
        with self.assertRaises(Refused):
            w.files.run_check("hermes", w.a(bot="bramble", task="5-notes", command=["node", "tests/test-fence.js"]))

    def test_2_what_a_check_can_see(self):
        argv = check_argv(policy(), "rift", "/srv/tw/work/rift/Thornswood/12-load-order", ["node", "tests/test-x.js"])
        props = {argv[i + 1].split("=", 1)[0]: argv[i + 1].split("=", 1)[1] for i, a in enumerate(argv) if a == "-p"}
        self.assertIn("--uid=tw-rift", argv)
        self.assertIn("--gid=tw-work-rift", argv)
        self.assertIn("--slice=tw-sandbox.slice", argv)
        self.assertEqual(props["PrivateNetwork"], "yes")
        self.assertEqual(props["ProtectProc"], "invisible")
        self.assertEqual(props["ProtectHome"], "yes")
        self.assertEqual(props["TemporaryFileSystem"], "/srv/tw/work:ro")
        self.assertEqual(props["BindPaths"], "/srv/tw/work/rift/Thornswood/12-load-order")
        self.assertEqual(props["TasksMax"], "256")
        self.assertEqual(props["MemoryMax"], "2G")
        self.assertEqual(props["NoNewPrivileges"], "yes")
        for hidden in ("/srv/tw/policy", "/srv/tw/git", "/srv/tw/spool", "/etc/thornswood", "/run/tw"):
            self.assertIn(hidden, props["InaccessiblePaths"].split())
        self.assertEqual(argv[-3:], ["--", "node", "tests/test-x.js"])

    def test_only_the_gateway_user_calls_tw_files(self):
        self.assertTrue(all(users == {"hermes"} for _, users in World().files.ops().values()))


if __name__ == "__main__":
    unittest.main()
