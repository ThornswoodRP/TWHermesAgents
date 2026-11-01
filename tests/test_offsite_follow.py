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

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        joined = " ".join(args)
        for needle, code in self.fail.items():
            if needle in joined:
                return subprocess.CompletedProcess(args, code, "", f"{needle} failed")
        out = ""
        if "rev-parse" in args:
            out = self.tip + "\n"
        if args[0] == "systemd-run":
            out = "Ran 412 tests in 30s\n\nOK\n"
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
            if "fetch" in args and "git@github.com:EvilPatrick06/Thornswood-Hermes.git" in args:
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


if __name__ == "__main__":
    unittest.main()
