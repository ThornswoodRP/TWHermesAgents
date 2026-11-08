"""The VM on its own (#2862, Patrick's answers on 31 Oct): it follows main by
itself, copies every backup to Google Drive, and takes the game box's
nightly through a drop the box can only add to.
"""

from __future__ import annotations

import sys as _sys
import unittest as _unittest

if not _sys.platform.startswith("linux"):
    raise _unittest.SkipTest("the VM's services run only on the Oracle VM; their tests run there")

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from helpers import REPO, policy

from thornswood_brokers import follow_main, offsite

UNITS = REPO / "deploy" / "systemd"


def unit(name: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in (UNITS / name).read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            out.setdefault(k, []).append(v)
    return out


def drop(folder: Path, name: str, data: bytes, mark: str | None = None) -> None:
    (folder / name).write_bytes(data)
    if mark is None:
        mark = f"{hashlib.sha256(data).hexdigest()} {len(data)}\n"
    (folder / (name + ".done")).write_text(mark)


class Intake(unittest.TestCase):
    def setUp(self):
        t = Path(tempfile.mkdtemp())
        self.drop, self.store, self.rejected = t / "in", t / "store", t / "rejected"
        self.drop.mkdir()
        self.store.mkdir()

    def test_a_whole_drop_is_taken_and_leaves_the_jail(self):
        drop(self.drop, "thornswood-20261101-031500.zip.enc", b"encrypted bytes")
        taken, problems = offsite.intake(self.drop, self.store, self.rejected)
        self.assertEqual((taken, problems), (["thornswood-20261101-031500.zip.enc"], []))
        self.assertEqual(list(self.drop.iterdir()), [])
        f = self.store / "thornswood-20261101-031500.zip.enc"
        self.assertEqual(f.read_bytes(), b"encrypted bytes")
        self.assertEqual(f.stat().st_mode & 0o777, 0o440)

    def test_a_file_still_arriving_waits_for_its_marker(self):
        (self.drop / "thornswood-20261101-031500.zip.enc").write_bytes(b"half")
        self.assertEqual(offsite.intake(self.drop, self.store, self.rejected), ([], []))
        self.assertTrue((self.drop / "thornswood-20261101-031500.zip.enc").exists())

    def test_a_file_that_is_not_what_its_marker_says_is_set_aside(self):
        drop(self.drop, "thornswood-20261101-031500.zip.enc", b"cut short", mark="0" * 64 + " 999\n")
        taken, problems = offsite.intake(self.drop, self.store, self.rejected)
        self.assertEqual(taken, [])
        self.assertIn("is not the file its marker names", problems[0])
        self.assertEqual(list(self.store.iterdir()), [])
        self.assertEqual(len(list(self.rejected.iterdir())), 2)

    def test_only_a_name_the_box_gives_a_backup_is_taken(self):
        for bad in ("thornswood-20261101-031500.zip", "../x.zip.enc", "notes.txt"):
            (self.drop / (Path(bad).name + ".done")).write_text("x")
        taken, problems = offsite.intake(self.drop, self.store, self.rejected)
        self.assertEqual(taken, [])
        self.assertEqual(len(problems), 3)
        self.assertEqual(list(self.store.iterdir()), [])

    def test_the_same_backup_sent_twice_keeps_the_first(self):
        drop(self.drop, "thornswood-20261101-031500.zip.enc", b"first")
        offsite.intake(self.drop, self.store, self.rejected)
        drop(self.drop, "thornswood-20261101-031500.zip.enc", b"other bytes")
        taken, problems = offsite.intake(self.drop, self.store, self.rejected)
        self.assertEqual(taken, [])
        self.assertIn("the first is kept", problems[0])
        self.assertEqual((self.store / "thornswood-20261101-031500.zip.enc").read_bytes(), b"first")


class Copy(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp())
        self.p = policy()
        self.p.data["brokers"]["state"]["backups"] = str(self.t / "backups")
        r = self.p.data["backups"]["offsite"]
        r["box_store"] = str(self.t / "boxbackups")
        r["rclone_config"] = str(self.t / "rclone.conf")
        for d in ("backups", "boxbackups"):
            (self.t / d).mkdir()
        self.calls = []

    def run_(self, args, **kw):
        self.calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    def test_both_kinds_go_to_drive_and_old_drive_copies_go(self):
        (self.t / "rclone.conf").write_text("[thornswood-gdrive]\ntype = drive\n")
        (self.t / "backups" / "hermes-20261101-093000.tar.gz.age").write_bytes(b"a")
        (self.t / "boxbackups" / "thornswood-20261101-031500.zip.enc").write_bytes(b"b")
        lines, problems = offsite.copy(self.p, run=self.run_)
        self.assertEqual(problems, [])
        copies = [c for c in self.calls if c[1] == "copy"]
        self.assertEqual([c[3] for c in copies], ["thornswood-gdrive:Thornswood-Hermes-Backups",
                                                  "thornswood-gdrive:Thornswood-Box-Backups"])
        self.assertTrue(all("--config" in c and str(self.t / "rclone.conf") in c for c in self.calls))
        deletes = [c for c in self.calls if c[1] == "delete"]
        # The VM's own 90 days; the box's 30, as the public privacy page promises.
        self.assertEqual([c[c.index("--min-age") + 1] for c in deletes], ["90d", "30d"])
        # Only names the backups have are ever copied or deleted.
        self.assertEqual([c[c.index("--include") + 1] for c in self.calls],
                         ["hermes-*.tar.gz.age", "hermes-*.tar.gz.age", "thornswood-*.zip.enc", "thornswood-*.zip.enc"])

    def test_a_drive_folder_no_copy_has_made_yet_is_not_a_failure(self):
        # 1 Nov on the VM: the box had sent nothing yet, so the copy made no
        # Drive folder and rclone delete said exit 3, directory not found.
        (self.t / "rclone.conf").write_text("x")
        (self.t / "backups" / "hermes-20261101-093000.tar.gz.age").write_bytes(b"a")

        def run(args, **kw):
            self.calls.append(args)
            missing = args[1] == "delete" and args[2].endswith("Thornswood-Box-Backups")
            return subprocess.CompletedProcess(args, 3 if missing else 0, "", "directory not found" if missing else "")

        lines, problems = offsite.copy(self.p, run=run)
        self.assertFalse([p for p in problems if "rclone" in p], problems)
        self.assertIn("PASS nothing of the box's is on Drive in thornswood-gdrive:Thornswood-Box-Backups yet, "
                      "so nothing there is older than 30 days", lines)
        # The box's missing nightly is still a failure of its own.
        self.assertEqual(problems, [f"the box's backups: there is none in {self.t / 'boxbackups'}"])

    def test_any_other_rclone_delete_failure_still_fails(self):
        (self.t / "rclone.conf").write_text("x")

        def run(args, **kw):
            self.calls.append(args)
            return subprocess.CompletedProcess(args, 1 if args[1] == "delete" else 0, "", "")

        lines, problems = offsite.copy(self.p, run=run)
        self.assertEqual(len([p for p in problems if "could not delete" in p]), 2, problems)

    def test_no_sign_in_yet_is_a_failure_that_says_so(self):
        (self.t / "backups" / "hermes-20261101-093000.tar.gz.age").write_bytes(b"a")
        (self.t / "boxbackups" / "thornswood-20261101-031500.zip.enc").write_bytes(b"b")
        lines, problems = offsite.copy(self.p, run=self.run_)
        self.assertEqual(self.calls, [])
        self.assertIn("no Drive sign-in", problems[0])

    def test_a_missed_night_of_either_kind_fails(self):
        (self.t / "rclone.conf").write_text("x")
        old = self.t / "boxbackups" / "thornswood-20261029-031500.zip.enc"
        old.write_bytes(b"b")
        os.utime(old, (time.time() - 30 * 3600,) * 2)
        lines, problems = offsite.copy(self.p, run=self.run_)
        self.assertTrue(any("the VM's backups: there is none" in p for p in problems), problems)
        self.assertTrue(any("the box's newest backup arrived 30 hours ago" in p for p in problems), problems)

    def test_the_box_copies_here_are_pruned_but_the_newest_seven_stay(self):
        (self.t / "rclone.conf").write_text("x")
        for day in range(1, 11):
            f = self.t / "boxbackups" / f"thornswood-202609{day:02d}-031500.zip.enc"
            f.write_bytes(b"b")
            os.utime(f, (time.time() - 60 * 86400,) * 2)
        offsite.copy(self.p, run=self.run_)
        self.assertEqual(len(list((self.t / "boxbackups").iterdir())), 7)


class TheDropJail(unittest.TestCase):
    def test_the_box_can_only_add(self):
        text = (REPO / "deploy" / "sshd" / "51-tw-boxdrop.conf").read_text()
        self.assertIn("Match User tw-boxdrop", text)
        self.assertIn("ChrootDirectory /srv/tw/boxdrop", text)
        force = [l.strip() for l in text.splitlines() if l.strip().startswith("ForceCommand")][0]
        self.assertIn("-u 0777", force)
        denied = force.split("-P ", 1)[1].split()[0].split(",")
        for req in ("read", "readdir", "opendir", "remove", "rename", "posix-rename", "setstat", "fsetstat",
                    "hardlink", "symlink", "copy-data"):
            self.assertIn(req, denied)
        for no in ("AllowTcpForwarding no", "X11Forwarding no", "PermitTTY no", "PasswordAuthentication no"):
            self.assertIn(no, text)

    def test_its_key_works_only_from_the_box(self):
        text = (REPO / "deploy" / "install.sh").read_text()
        self.assertIn("printf 'from=\"%s\",restrict %s\\n' \"$box_addr\"", text)
        self.assertIn("install -d -m 0730 -o root -g tw-boxdrop /srv/tw/boxdrop/in", text)
        self.assertEqual(policy().data["backups"]["offsite"]["box_address"], "192.135.112.23")

    def test_drive_and_the_units(self):
        self.assertEqual(unit("tw-offsite.service")["User"], ["tw-offsite"])
        self.assertEqual(unit("tw-offsite.service")["ExecStartPre"], ["+/opt/thornswood-hermes/bin/tw-offsite intake"])
        self.assertEqual(unit("tw-offsite.path")["PathChanged"], ["/srv/tw/boxdrop/in"])
        self.assertEqual(unit("tw-offsite.timer")["OnCalendar"], ["*-*-* 04:30:00 America/Denver"])
        self.assertEqual(unit("tw-follow-main.timer")["OnUnitInactiveSec"], ["10min"])


class FakeVM:
    """Records each command the follower runs and answers it."""

    def __init__(self, tip: str, fail: dict[str, int] | None = None):
        self.tip = tip
        self.fail = fail or {}
        self.calls: list[list[str]] = []
        self.inputs: dict[int, bytes] = {}

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        if "input" in kw:
            self.inputs[len(self.calls) - 1] = kw["input"]
        joined = " ".join(args)
        for needle, code in self.fail.items():
            if needle in joined:
                return subprocess.CompletedProcess(args, code, "", f"{needle} failed")
        out = ""
        if "rev-parse" in args:
            out = self.tip + "\n"
        if args[0] == "systemd-run":
            out = "Ran 412 tests in 30s\n\nOK\n"
        if "bundle" in args and "create" in args:
            return subprocess.CompletedProcess(args, 0, b"# v2 git bundle\n" + joined.encode(), b"")
        return subprocess.CompletedProcess(args, 0, out, "")

    def ran(self, needle: str) -> list[str]:
        return [" ".join(c) for c in self.calls if needle in " ".join(c)]


OLD = "a" * 40
NEW = "b" * 40


class FollowMain(unittest.TestCase):
    def follower(self, vm: FakeVM, installed: str | None = OLD) -> follow_main.Follower:
        self.root = Path(tempfile.mkdtemp())
        self.said: list[str] = []
        f = follow_main.Follower(policy(), run=vm, root=self.root, say=self.said.append)
        if installed:
            (f.opt / "releases" / installed).mkdir(parents=True)
            (f.opt / "current").symlink_to(f.opt / "releases" / installed)
        return f

    def test_main_already_installed_does_nothing(self):
        vm = FakeVM(OLD)
        self.assertEqual(self.follower(vm).follow(), 0)
        self.assertEqual(vm.ran("install.sh"), [])
        self.assertEqual(vm.ran("systemd-run"), [])

    def test_a_new_commit_is_tested_installed_applied_restarted_and_checked_in_that_order(self):
        vm = FakeVM(NEW)
        f = self.follower(vm)
        self.assertEqual(f.follow(), 0, self.said)
        order = [i for i, c in enumerate(vm.calls) for k in ("systemd-run", "install.sh", "runuser", "tw-reload-bots", "tw-canary")
                 if k in " ".join(c)]
        self.assertEqual(len(order), 5, vm.calls)
        self.assertEqual(order, sorted(order))
        self.assertEqual(len(vm.ran("install.sh")), 1)
        self.assertIn(f"releases/{NEW}/deploy/install.sh {NEW}", vm.ran("install.sh")[0])
        apply = vm.ran("runuser")[0]
        self.assertIn(f"apply.py --expect '{NEW}' --check", apply)
        self.assertLess(apply.index("--check"), apply.rindex(f"apply.py --expect '{NEW}'\n"))
        # The tests run as tw-ci with no network.
        tests = vm.ran("systemd-run")[0]
        self.assertIn("User=tw-ci", tests)
        self.assertIn("PrivateNetwork=yes", tests)
        self.assertEqual(json.loads(f.state_file.read_text())["installed"], NEW)

    # 2 Nov, #68: the install passed and only the follower's summary line was
    # kept, so a card asking for the install's own lines could not be answered.
    def test_every_steps_output_is_kept_when_the_install_passes(self):
        vm = FakeVM(NEW)
        f = self.follower(vm)

        def run(args, **kw):
            if "install.sh" in " ".join(args):
                return subprocess.CompletedProcess(args, 0, "PASS the App's webhook is on\n", "")
            return vm(args, **kw)

        f.run_ = run
        self.assertEqual(f.follow(), 0, self.said)
        st = json.loads(f.state_file.read_text())
        log = Path(st["log"])
        self.assertEqual(log, self.root / "var/lib/thornswood/follow-main" / f"{NEW}.log")
        text = log.read_text()
        self.assertIn("PASS the App's webhook is on", text)
        self.assertIn("Ran 412 tests", text)
        self.assertIn("--- install: bash", text)
        self.assertIn("--- canary:", text)
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(log.parent.stat().st_mode), 0o700)
        self.assertIn(str(log), self.said[0])

    def test_a_failed_run_keeps_its_output_and_the_put_back_in_the_same_log(self):
        vm = FakeVM(NEW)
        f = self.follower(vm)
        canaries = []

        def run(args, **kw):
            if args[0].endswith("tw-canary"):
                canaries.append(1)
                if len(canaries) == 1:
                    return subprocess.CompletedProcess(args, 1, "FAIL attack 3", "")
            return vm(args, **kw)

        f.run_ = run
        self.assertEqual(f.follow(), 1)
        text = Path(json.loads(f.state_file.read_text())["log"]).read_text()
        self.assertIn("FAIL attack 3", text)
        self.assertIn(f"releases/{OLD}/deploy/install.sh", text)

    def test_only_the_newest_logs_are_kept(self):
        vm = FakeVM(NEW)
        f = self.follower(vm)
        f.logs.mkdir(parents=True)
        for i in range(follow_main.LOGS_KEPT + 5):
            q = f.logs / f"{i:040d}.log"
            q.write_text("old\n")
            os.utime(q, (1_000_000 + i, 1_000_000 + i))
        self.assertEqual(f.follow(), 0, self.said)
        logs = sorted(p.name for p in f.logs.glob("*.log"))
        self.assertEqual(len(logs), follow_main.LOGS_KEPT)
        self.assertIn(f"{NEW}.log", logs)
        self.assertNotIn(f"{0:040d}.log", logs)

    def test_the_fetch_uses_only_the_read_key_and_githubs_host_keys(self):
        vm = FakeVM(OLD)
        seen = {}

        def run(args, **kw):
            if "fetch" in args and "env" in kw:
                seen["ssh"] = kw["env"]["GIT_SSH_COMMAND"]
            return vm(args, **kw)

        f = self.follower(vm)
        f.run_ = run
        f.follow()
        self.assertIn("-i " + str(self.root / "etc/thornswood/hermes-read-key"), seen["ssh"])
        self.assertIn("StrictHostKeyChecking=yes", seen["ssh"])
        self.assertIn("UserKnownHostsFile=" + str(self.root / "etc/thornswood/github_known_hosts"), seen["ssh"])
        known = (REPO / "deploy" / "ssh" / "github_known_hosts").read_text()
        self.assertIn("github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl", known)

    def test_a_refused_read_key_says_how_to_add_it(self):
        vm = FakeVM(NEW)
        f = self.follower(vm)

        def run(args, **kw):
            if "fetch" in args and "git@github.com:ThornswoodRP/TWHermesAgents.git" in args:
                return subprocess.CompletedProcess(args, 128, "", "Permission denied (publickey).")
            return vm(args, **kw)

        f.run_ = run
        self.assertEqual(f.follow(), 1)
        self.assertIn("deploy key", self.said[-1])
        self.assertIn("Allow write access left off", self.said[-1])

    def test_a_red_suite_installs_nothing_and_is_not_tried_again(self):
        vm = FakeVM(NEW, fail={"systemd-run": 1})
        f = self.follower(vm)
        self.assertEqual(f.follow(), 1)
        self.assertEqual(vm.ran("install.sh"), [])
        self.assertEqual(json.loads(f.state_file.read_text())["failed"], NEW)
        vm.calls.clear()
        self.assertEqual(f.follow(), 1)
        self.assertEqual(vm.ran("systemd-run"), [], "a commit that failed is never run again")

    def test_a_failed_canary_puts_the_earlier_release_back(self):
        vm = FakeVM(NEW)
        f = self.follower(vm)
        canaries = []

        def run(args, **kw):
            if args[0].endswith("tw-canary"):
                canaries.append(1)
                if len(canaries) == 1:
                    return subprocess.CompletedProcess(args, 1, "FAIL attack 3", "")
            return vm(args, **kw)

        f.run_ = run
        self.assertEqual(f.follow(), 1)
        installs = vm.ran("install.sh")
        self.assertEqual(len(installs), 2)
        self.assertIn(f"releases/{NEW}/", installs[0])
        self.assertIn(f"releases/{OLD}/deploy/install.sh {OLD}", installs[1])
        st = json.loads(f.state_file.read_text())
        self.assertEqual((st["failed"], st["step"], st["put_back"]), (NEW, "canary", f"put back {OLD}"))


    # 1 Nov: once the decision core is installed, each merge moves current, and
    # its feeds and connector run the new code at once; the core itself has to
    # be installed again at that commit or it keeps running the old one.
    def core_installed(self, f: follow_main.Follower, *releases: str) -> None:
        env = f._at(f.r["decide_env"])
        env.parent.mkdir(parents=True, exist_ok=True)
        env.write_text("TW_DECIDE_ROOM=54b3ad46-3419-4adb-b60d-c79cb347fcee\nTW_DECIDE_ORIGIN=\nTW_HERMES_BIN=/usr/local/bin/hermes\n")
        for c in releases:
            core = f.opt / "releases" / c / "deploy" / "decisions" / "install.sh"
            core.parent.mkdir(parents=True, exist_ok=True)
            core.write_text("#!/usr/bin/env bash\n")

    def test_the_decision_core_is_installed_again_right_after_each_install(self):
        vm = FakeVM(NEW)
        f = self.follower(vm)
        self.core_installed(f, NEW)
        self.assertEqual(f.follow(), 0, self.said)
        installs = vm.ran("install.sh")
        self.assertEqual(installs, [f"bash {f.opt}/releases/{NEW}/deploy/install.sh {NEW}",
                                    f"bash {f.opt}/releases/{NEW}/deploy/decisions/install.sh {NEW}"])
        # No --room or --origin: the core's install keeps them from its env file.
        order = [i for i, c in enumerate(vm.calls) for k in ("decisions/install.sh", "runuser", "tw-reload-bots", "tw-canary")
                 if k in " ".join(c)]
        self.assertEqual(len(order), 4, vm.calls)
        self.assertEqual(order, sorted(order))

    def test_no_decision_core_on_the_vm_means_no_core_install(self):
        vm = FakeVM(NEW)
        f = self.follower(vm)
        core = f.opt / "releases" / NEW / "deploy" / "decisions" / "install.sh"
        core.parent.mkdir(parents=True, exist_ok=True)
        core.write_text("#!/usr/bin/env bash\n")
        self.assertEqual(f.follow(), 0, self.said)
        self.assertEqual(vm.ran("decisions/install.sh"), [])

    def test_a_failed_core_install_puts_the_earlier_release_back_core_and_all(self):
        vm = FakeVM(NEW, fail={f"decisions/install.sh {NEW}": 1})
        f = self.follower(vm)
        self.core_installed(f, NEW, OLD)
        self.assertEqual(f.follow(), 1)
        self.assertEqual(vm.ran("tw-canary"), [f"{f.opt}/bin/tw-canary"], "only the put-back release's canary ran")
        self.assertEqual(vm.ran("install.sh"), [
            f"bash {f.opt}/releases/{NEW}/deploy/install.sh {NEW}",
            f"bash {f.opt}/releases/{NEW}/deploy/decisions/install.sh {NEW}",
            f"bash {f.opt}/releases/{OLD}/deploy/install.sh {OLD}",
            f"bash {f.opt}/releases/{OLD}/deploy/decisions/install.sh {OLD}"])
        st = json.loads(f.state_file.read_text())
        self.assertEqual((st["failed"], st["step"], st["put_back"]), (NEW, "install", f"put back {OLD}"))

    def test_a_release_without_the_core_install_stops_rather_than_leave_the_core_behind(self):
        vm = FakeVM(NEW)
        f = self.follower(vm)
        self.core_installed(f)  # the env file, and no deploy/decisions/install.sh in either release
        self.assertEqual(f.follow(), 1)
        self.assertEqual(vm.ran("tw-reload-bots"), [])
        self.assertIn("has no deploy/decisions/install.sh", self.said[-2])
        self.assertIn("putting back", json.loads(f.state_file.read_text())["put_back"])

    def test_the_policy_names_the_cores_env_file_as_its_install_writes_it(self):
        self.assertEqual(policy().data["follow_main"]["decide_env"], "/etc/thornswood/tw-decide.env")
        self.assertIn("envfile=/etc/thornswood/tw-decide.env",
                      (REPO / "deploy" / "decisions" / "install.sh").read_text())


    # 1 Nov on the VM: git refused the root-owned release to hermes as
    # "dubious ownership", so the apply and its put-back both stopped.
    def test_hermes_gets_a_bundle_and_never_the_root_owned_release(self):
        vm = FakeVM(NEW)
        canaries = []

        def run(args, **kw):
            if args[0].endswith("tw-canary"):
                canaries.append(1)
                if len(canaries) == 1:
                    return subprocess.CompletedProcess(args, 1, "FAIL attack 3", "")
            return vm(args, **kw)

        f = self.follower(vm)
        f.run_ = run
        self.assertEqual(f.follow(), 1)
        applies = vm.ran("runuser")
        self.assertEqual(len(applies), 2, "the apply and the put-back's apply")
        for commit, script in zip((NEW, OLD), applies):
            self.assertNotIn(str(f.opt), script, "hermes never runs git in a folder under /opt")
            self.assertNotIn("/srv/tw", script, "and needs no folder of root's open to it (1 Nov: /srv/tw/follow is 0700)")
            self.assertIn(f"mktemp '{f.hermes_releases}/.incoming-XXXXXX.bundle'", script)
            self.assertIn('cat > "$bundle"', script)
            self.assertIn('fetch --quiet "$bundle" \'+refs/remotes/origin/main:refs/remotes/origin/main\'', script)
            made = [c for c in vm.calls if "bundle" in c and "create" in c and f"releases/{commit}" in " ".join(c)]
            self.assertEqual(len(made), 1, f"root bundles {commit[:4]} from its own release")
            self.assertEqual(made[0][-2:], ["-", "refs/remotes/origin/main"], "to stdout, never to a file")
            named = [c for c in vm.calls if "update-ref" in c and f"releases/{commit}" in " ".join(c)]
            self.assertEqual(named[0][-2:], ["refs/remotes/origin/main", commit])
        handed = [vm.inputs[i] for i, c in enumerate(vm.calls) if c[0] == "runuser"]
        self.assertEqual(len(handed), 2)
        self.assertTrue(all(b.startswith(b"# v2 git bundle") for b in handed), "the bundle's bytes go in on stdin")
        self.assertIn(f"releases/{NEW}".encode(), handed[0])
        self.assertIn(f"releases/{OLD}".encode(), handed[1])
        self.assertEqual(json.loads(f.state_file.read_text())["put_back"], f"put back {OLD}")

    def store_at(self, f: follow_main.Follower, schema: int) -> None:
        env = f._at(f.r["decide_env"])
        env.parent.mkdir(parents=True, exist_ok=True)
        env.write_text("TW_DECIDE_ROOM=\n")
        db = f._at(f.r["decide_db"])
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO meta VALUES('schema_version', ?)", (str(schema),))
        con.commit()
        con.close()
        for c in (NEW, OLD):
            core = f.opt / "releases" / c / "deploy" / "decisions" / "install.sh"
            core.parent.mkdir(parents=True, exist_ok=True)
            core.write_text("#!/usr/bin/env bash\n")

    def knows(self, vm: FakeVM, old: int, *, downgrade: int = 0, active: str = ""):
        canaries = []

        def run(args, **kw):
            if args[:2] == ["python3", "-I"] and "MIGRATIONS" in args[3]:
                vm.calls.append(list(args))
                return subprocess.CompletedProcess(args, 0, f"{old}\n", "")
            if args[0].endswith("tw-canary") and not canaries:
                canaries.append(1)
                vm.calls.append(list(args))
                return subprocess.CompletedProcess(args, 1, "FAIL attack 3", "")
            if "downgrade" in args:
                vm.calls.append(list(args))
                if downgrade:
                    return subprocess.CompletedProcess(args, downgrade, "", (
                        "STOP store_written: the store holds what the older schema cannot "
                        "(schema 6: 2 phone alerts that keep their address's SHA-256); fix forward"))
                return subprocess.CompletedProcess(args, 0, '{"from": 6, "to": 5}', "")
            if args[:2] == ["systemctl", "is-active"]:
                vm.calls.append(list(args))
                return subprocess.CompletedProcess(args, 3, active, "")
            return vm(args, **kw)
        return run

    # What tw-decide's installs leave in /etc/systemd/system, the phone test's included.
    UNITS = ["tw-decide.service", "tw-decide-connector.service", "tw-decide-feed.service", "tw-decide-feed.timer",
             "tw-decide-push.service", "tw-decide-test.service", "tw-decide-push-test.service"]

    def units(self, f: follow_main.Follower, installed: list[str], old_ships: list[str]) -> None:
        etc = f._at("/etc/systemd/system")
        etc.mkdir(parents=True, exist_ok=True)
        (etc / "tw-decide-connector.service.d").mkdir(exist_ok=True)
        for u in installed:
            (etc / u).write_text("[Service]\n")
        ships = f.opt / "releases" / OLD / "deploy" / "decisions"
        ships.mkdir(parents=True, exist_ok=True)
        for u in old_ships:
            (ships / u).write_text("[Service]\n")

    def at(self, vm: FakeVM, needle: str) -> int:
        hits = [i for i, c in enumerate(vm.calls) if needle in " ".join(c)]
        self.assertTrue(hits, f"nothing ran with {needle!r}: {vm.calls}")
        return hits[0]

    # 1 Nov: 5d92dda8 migrated the store to schema 6, then its apply failed,
    # and putting back 507dfdbf stopped half way: its code refuses schema 6.
    # The failed release's own downgrade (#36) puts the store back first.
    def test_the_failed_release_puts_the_store_back_before_the_older_release_goes_in(self):
        vm = FakeVM(NEW)
        f = self.follower(vm)
        self.store_at(f, 6)
        self.units(f, self.UNITS + ["tw-decide-own.service"], self.UNITS)
        f.run_ = self.knows(vm, 5)
        self.assertEqual(f.follow(), 1)
        stop = vm.calls[self.at(vm, "systemctl stop")]
        # Every unit of the core, timers first; never the phone test's own.
        self.assertEqual(stop[2], "tw-decide-feed.timer")
        self.assertEqual(sorted(stop[2:]), sorted(["tw-decide.service", "tw-decide-connector.service", "tw-decide-feed.service",
                                                   "tw-decide-feed.timer", "tw-decide-push.service", "tw-decide-own.service"]))
        down = vm.calls[self.at(vm, "downgrade")]
        self.assertEqual(down[:4], ["runuser", "-u", "tw-decide", "--"])
        self.assertTrue(down[4].endswith("/opt/thornswood-hermes/decisions-venv/bin/python3"), down)
        self.assertTrue(down[8].endswith(f"releases/{NEW}"), "the failed release's code, which knows the way back")
        self.assertEqual(down[-5:], ["--db", str(f._at("/srv/tw/decide/core.db")), "downgrade", "--to", "5"])
        disable = vm.calls[self.at(vm, "systemctl disable")]
        self.assertEqual(disable[-1:], ["tw-decide-own.service"], "only what the older release does not ship")
        order = [self.at(vm, "systemctl stop"), self.at(vm, "downgrade"), self.at(vm, "systemctl disable"),
                 self.at(vm, f"releases/{OLD}/deploy/install.sh")]
        self.assertEqual(order, sorted(order))
        self.assertEqual(json.loads(f.state_file.read_text())["put_back"], f"put back {OLD}")
        self.assertTrue(any("back at schema 5 from 6" in s for s in self.said), self.said)

    def test_no_put_back_when_the_downgrade_refuses_and_the_core_starts_again(self):
        vm = FakeVM(NEW)
        f = self.follower(vm)
        self.store_at(f, 6)
        self.units(f, self.UNITS + ["tw-decide-own.service"], self.UNITS)
        units = f.core_units()  # is-active answers in this order
        active = "\n".join("inactive" if u == "tw-decide-feed.service" else "active" for u in units) + "\n"
        f.run_ = self.knows(vm, 5, downgrade=1, active=active)
        self.assertEqual(f.follow(), 1)
        self.assertEqual(vm.ran(f"releases/{OLD}/deploy"), [], "nothing of the older release is installed")
        self.assertEqual(vm.ran("systemctl disable"), [], "the newer release keeps every unit")
        start = vm.calls[self.at(vm, "systemctl start")]
        self.assertEqual(sorted(start[2:]), sorted(u for u in units if u != "tw-decide-feed.service"))
        self.assertGreater(self.at(vm, "systemctl start"), self.at(vm, "downgrade"))
        st = json.loads(f.state_file.read_text())
        self.assertEqual((st["result"], st["failed"], st["step"]), ("failed", NEW, "canary"))
        self.assertIn("not put back: the core's store is at schema 6, aaaaaaaaaaaa knows only 5", st["put_back"])
        self.assertIn("store_written", st["put_back"])
        self.assertIn("fix it forward", st["put_back"])

    def test_the_put_back_runs_when_the_older_release_knows_the_store(self):
        vm = FakeVM(NEW)
        f = self.follower(vm)
        self.store_at(f, 5)
        self.units(f, self.UNITS + ["tw-decide-own.service"], self.UNITS)
        f.run_ = self.knows(vm, 5)
        self.assertEqual(f.follow(), 1)
        self.assertEqual(vm.ran("downgrade"), [])
        self.assertEqual(vm.ran("systemctl stop"), [])
        self.assertEqual(vm.calls[self.at(vm, "systemctl disable")][-1:], ["tw-decide-own.service"])
        self.assertEqual(len(vm.ran(f"releases/{OLD}/deploy")), 2, "its install and its core's install")
        self.assertEqual(json.loads(f.state_file.read_text())["put_back"], f"put back {OLD}")

    def test_the_downgrade_line_reaches_the_cli_with_its_own_arguments(self):
        # The -c line, run for real against a stand-in release: that release's
        # code is what runs, and its CLI gets exactly --db ... downgrade --to N.
        f = self.follower(FakeVM(NEW))
        rel = Path(tempfile.mkdtemp())
        (rel / "thornswood_decisions").mkdir()
        (rel / "thornswood_decisions" / "__init__.py").write_text("")
        (rel / "thornswood_decisions" / "cli.py").write_text(
            "import json, sys\ndef main(argv):\n    print(json.dumps({'from': sys.path[0], 'argv': argv}))\n    return 3\n")
        # Everything after runuser -u tw-decide -- <venv python>, with this python.
        f.run_ = lambda args, **kw: subprocess.run([_sys.executable, *args[5:]], **kw)
        with self.assertRaises(follow_main.Stop) as e:
            f.downgrade(rel, 5)
        self.assertIn("exited 3", str(e.exception))
        out = json.loads(str(e.exception).split(": ", 1)[1])
        self.assertEqual(out["from"], str(rel))
        self.assertEqual(out["argv"], ["--db", str(f._at("/srv/tw/decide/core.db")), "downgrade", "--to", "5"])

    def test_a_pause_file_stops_everything_and_survives_an_install(self):
        vm = FakeVM(NEW)
        f = self.follower(vm)
        pause = f._at(f.r["pause_file"])
        pause.parent.mkdir(parents=True, exist_ok=True)
        pause.write_text("")
        self.assertEqual(f.follow(), 0)
        self.assertEqual(vm.calls, [], "not even a fetch")
        self.assertIn("paused", self.said[-1])
        # The install starts the timer again; the pause is the file, which it leaves alone.
        self.assertNotIn(f.r["pause_file"], (REPO / "deploy" / "install.sh").read_text())

@unittest.skipUnless(os.geteuid() == 0 and shutil.which("runuser"), "needs root, to hand the release to another user as the VM does")
class ApplyAsAnotherUser(unittest.TestCase):
    """The real git, a release root owns and a hermes that is not root: the
    apply, then the put-back's apply of the earlier release."""

    def setUp(self):
        self.user = "nobody"
        self.root = Path(tempfile.mkdtemp())
        os.chmod(self.root, 0o755)
        self.f = follow_main.Follower(policy(), root=self.root, say=lambda s: None)
        self.f.r = {**self.f.r, "hermes_user": self.user}
        rel = self.root / "src"
        g = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(rel)]
        subprocess.run(["git", "init", "-q", str(rel)], check=True)
        (rel / "scripts").mkdir()
        self.commits = []
        for n in (1, 2):
            (rel / "scripts" / "apply.py").write_text(f"import sys; print('apply {n}', sys.argv[1:])\n")
            subprocess.run([*g, "add", "-A"], check=True)
            subprocess.run([*g, "commit", "-q", "-m", str(n)], check=True)
            self.commits.append(subprocess.run([*g, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip())
        for c in self.commits:
            dest = self.f.opt / "releases" / c
            subprocess.run(["git", "clone", "-q", "--no-checkout", str(rel), str(dest)], check=True)
            subprocess.run(["git", "-C", str(dest), "-c", "advice.detachedHead=false", "checkout", "-q", "--detach", c], check=True)
        # Root's folders stay closed to hermes, as on the VM (1 Nov:
        # /srv/tw/follow is 0700): the apply must not need any of them.
        for d in (self.f.opt, self.f.opt.parent):
            os.chmod(d, 0o700)
        self.f.mirror.parent.mkdir(parents=True)
        for d in (self.f.mirror.parent, self.f.mirror.parent.parent, self.f.mirror.parent.parent.parent):
            os.chmod(d, 0o700)
        home = self.f.hermes_releases.parent
        home.mkdir(parents=True)
        for d in (home.parent, home.parent.parent):
            os.chmod(d, 0o755)
        shutil.chown(home, self.user)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_the_apply_and_the_put_back_both_work_as_hermes(self):
        new, old = self.commits[1], self.commits[0]
        for c in (new, old):
            self.f.apply(self.f.opt / "releases" / c, c)
            mine = self.f.hermes_releases / c
            self.assertEqual(mine.stat().st_uid, pwd_uid(self.user))
            head = subprocess.run(["git", "-c", f"safe.directory={mine}", "-C", str(mine), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(head, c)
            self.assertEqual(os.readlink(self.f.hermes_releases.parent / "current"), str(mine))


def pwd_uid(name: str) -> int:
    import pwd
    return pwd.getpwnam(name).pw_uid


if __name__ == "__main__":
    unittest.main()
