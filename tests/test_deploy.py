"""The install, the units, the boot limits check, the check helper and the
backups.

Attack tests 16 (the limits check at boot) and 21 (a backup holds no secret
and restores) from 08 section 8, plus the units and install script matching
the policy.
"""

from __future__ import annotations

import sys as _sys
import unittest as _unittest

if not _sys.platform.startswith("linux"):
    # The brokers are Linux code (Unix sockets with SO_PEERCRED, pwd, O_NOFOLLOW
    # walks, systemd): they run only on the Oracle VM, and these tests run in
    # the VM half of scripts/test-at-head.sh.
    raise _unittest.SkipTest("the brokers run only on the Oracle VM; their tests run there")

import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from helpers import HEAD, REPO, policy

from thornswood_brokers import backup, limits_check, run_check
from thornswood_brokers.host import HostState
from thornswood_brokers.wire import Refused
from thornswood_guard.decisions import new_decision
from thornswood_guard.policy import Policy

UNITS = REPO / "deploy" / "systemd"
BROKERS = ("policy", "git", "files", "proxy")


def unit(name: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in (UNITS / name).read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            out.setdefault(k, []).append(v)
    return out


class Units(unittest.TestCase):
    def test_slices_match_the_policy(self):
        spec = importlib.util.spec_from_file_location("render_units", REPO / "scripts" / "render_units.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for path, text in module.files(policy()).items():
            self.assertTrue(path.exists(), f"{path.name} is missing; run python3 scripts/render_units.py")
            self.assertEqual(path.read_text(), text, f"{path.name} is stale; run python3 scripts/render_units.py")

    def test_each_broker_runs_as_its_own_user_in_the_brokers_slice(self):
        users = policy().data["brokers"]["users"]
        for role in BROKERS:
            u = unit(f"tw-{role}.service")
            self.assertEqual(u["User"], [users[role]], role)
            self.assertEqual(u["Slice"], ["tw-brokers.slice"], role)
            self.assertEqual(u["ExecStart"], [f"/opt/thornswood-hermes/bin/tw-serve {role}"], role)
            self.assertEqual(u["NoNewPrivileges"], ["yes"], role)
            self.assertEqual(u["CapabilityBoundingSet"], [""], role)
            self.assertEqual(u["ProtectHome"], ["yes"], role)

    def test_only_the_proxy_has_a_network(self):
        for role in ("policy", "git", "files"):
            u = unit(f"tw-{role}.service")
            if role == "git":
                # tw-git reaches GitHub, nothing else on the box.
                self.assertNotIn("PrivateNetwork", u)
            else:
                self.assertEqual(u["PrivateNetwork"], ["yes"], role)
        self.assertNotIn("PrivateNetwork", unit("tw-proxy.service"))

    def test_the_gateway_reaches_the_brokers_only_through_their_sockets(self):
        u = unit("tw-hermes-gateway.service.in")
        self.assertEqual(u["User"], ["hermes"])
        self.assertEqual(u["Slice"], ["tw-hermes.slice"])
        groups = u["SupplementaryGroups"][0].split()
        self.assertEqual(sorted(groups), ["tw-files-clients", "tw-git-clients", "tw-policy-clients"])
        for role in BROKERS:
            self.assertNotIn(policy().data["brokers"]["users"][role], groups)

    def test_the_socket_groups_are_the_policys(self):
        socks = policy().data["brokers"]["sockets"]
        for role in ("policy", "git", "files"):
            u = unit(f"tw-{role}.service")
            self.assertIn(socks[role]["group"], u["SupplementaryGroups"][0].split(), role)
            self.assertEqual(u["RuntimeDirectory"], [f"tw/{role}"], role)
            self.assertEqual(socks[role]["path"], f"/run/tw/{role}/{role}.sock")

    def test_every_op_is_open_only_to_users_its_socket_lets_in(self):
        from thornswood_brokers.files_service import FilesService
        from thornswood_brokers.git_service import GitService
        from thornswood_brokers.policy_service import PolicyService
        p = policy()
        t = Path(tempfile.mkdtemp())
        socks = p.data["brokers"]["sockets"]
        services = {
            "policy": PolicyService(t / "policy.db", p, host_reader=roomy),
            "git": GitService(p, None, lambda op, args: {}, mirrors=t / "git", spool=t / "spool"),
            "files": FilesService(p, lambda op, args: {}, work=t / "work", spool=t / "spool"),
        }
        for role, svc in services.items():
            for op, (_, users) in svc.ops().items():
                self.assertLessEqual(users, set(socks[role]["clients"]), f"{role}.{op}")
        self.assertNotIn("hermes", services["policy"].ops()["limits_record"][1])
        self.assertNotIn("hermes", services["policy"].ops()["mark_restored"][1]
                         if "mark_restored" in services["policy"].ops() else set())

    def test_the_limits_check_runs_before_the_brokers_and_the_gateway(self):
        before = unit("tw-limits-check.service")["Before"][0].split()
        for name in [f"tw-{r}.service" for r in BROKERS] + ["tw-hermes-gateway.service"]:
            self.assertIn(name, before)

    def test_the_limits_check_starts_the_slices_it_reads(self):
        # 31 Oct, run 2: the check ran before anything was in the slices, so
        # none had a cgroup yet and all three were reported missing.
        u = unit("tw-limits-check.service")
        slices = [s for s in policy().data["slices"] if s != "hermes-user.slice"]
        for s in slices:
            self.assertIn(s, u["Wants"][0].split())
            self.assertIn(s, u["After"][0].split())

    def test_quotas_on_reads_the_words_not_the_exit_status(self):
        # 31 Oct, run 2: quotaon -p exits 1 when quotas are on, and a pipe into
        # grep under pipefail read that as off.
        line = next(l for l in (REPO / "deploy" / "install.sh").read_text().splitlines()
                    if l.startswith("quotas_on()"))
        fake = Path(tempfile.mkdtemp())
        for words, code, want in (("group quota on / (/dev/sda1) is on", 1, "ON"),
                                  ("group quota on / (/dev/sda1) is off", 0, "OFF"),
                                  ("quotaon: Cannot find mountpoint", 2, "OFF")):
            (fake / "quotaon").write_text(f"#!/bin/sh\necho '{words}'\nexit {code}\n")
            (fake / "quotaon").chmod(0o755)
            r = subprocess.run(["bash", "-c", f"set -euo pipefail; srv_mnt=/; {line}\n"
                                "if quotas_on; then echo ON; else echo OFF; fi"],
                               capture_output=True, text=True, env={**os.environ, "PATH": f"{fake}:{os.environ['PATH']}"})
            self.assertEqual(r.stdout.strip(), want, (words, r.stderr))

    def test_stale_procs_counts_only_hermes_processes_without_the_groups(self):
        # 31 Oct: Rift's first Workbench turn was refused at the broker socket
        # by hermes processes that started before usermod gave the groups.
        text = (REPO / "deploy" / "install.sh").read_text()
        fn = re.search(r"^stale_procs\(\) \{\n.*?^\}\n", text, re.S | re.M).group(0)
        proc = Path(tempfile.mkdtemp())

        def p(pid, uid, groups, ns="user:[4026531837]", name="hermes"):
            d = proc / str(pid)
            (d / "ns").mkdir(parents=True)
            (d / "status").write_text(f"Name:\t{name}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\nGroups:\t{groups}\n")
            (d / "ns" / "user").symlink_to(ns)

        p(1, 0, "")
        p(200, 1001, "1001 2001 2002 2003")             # started after usermod
        p(201, 1001, "1001")                            # Desktop over ssh, before usermod
        p(202, 1001, "1001 2001 2002")                  # one group short
        p(203, 1001, "", ns="user:[4026532999]")        # inside a rootless container
        p(204, 1002, "1002")                            # another user
        p(205, 1001, "", name="(sd-pam)")               # PAM's helper, which drops every group itself
        r = subprocess.run(["bash", "-c", f"set -euo pipefail\n{fn}stale_procs {proc} 1001 2001 2002 2003"],
                           capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "2"), r.stderr)

    def test_the_install_ends_stale_hermes_sessions_and_brings_the_gateway_back(self):
        text = (REPO / "deploy" / "install.sh").read_text()
        step = text[text.index("# 9b."):text.index("# 10.")]
        order = ["stale_procs /proc", "loginctl terminate-user hermes", 'systemctl start "user@$hermes_uid.service"',
                 "systemctl restart tw-hermes-gateway.service", "systemctl is-active --quiet tw-hermes-gateway.service"]
        at = [step.index(s) for s in order]
        self.assertEqual(at, sorted(at))
        self.assertIn("tw-policy-clients tw-git-clients tw-files-clients", step)
        self.assertLess(text.index("usermod -a -G tw-policy-clients"), text.index("# 9b."))

    def _place_units(self, systemd_dir: Path) -> tuple[subprocess.CompletedProcess, Path]:
        text = (REPO / "deploy" / "install.sh").read_text()
        block = text[text.index("unit_dir=/etc/systemd/system\n"):text.index("# end of the unit copy")]
        out = Path(tempfile.mkdtemp())
        here = Path(tempfile.mkdtemp())
        shutil.copytree(systemd_dir, here / "deploy" / "systemd")
        script = ('set -euo pipefail\nstop() { echo "STOP: $*"; exit 1; }\n'
                  f'here={here}\nhermes_uid=1007\n' + block.replace("unit_dir=/etc/systemd/system", f"unit_dir={out}"))
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True), out

    def test_every_unit_the_install_enables_is_put_in_place(self):
        # 1 Nov: tw-offsite.path, the first .path unit, was never copied, and
        # "systemctl enable" stopped the #21 install.
        r, out = self._place_units(REPO / "deploy" / "systemd")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        text = (REPO / "deploy" / "install.sh").read_text()
        enabled = set()
        for m in re.finditer(r"systemctl enable[^\n]*", text.replace("\\\n", " ")):
            enabled |= {w for w in m.group(0).split() if re.fullmatch(r"[\w@-]+\.(service|timer|path|socket|slice|target)", w)}
        enabled.discard("tw-hermes-gateway.service")  # made from its .in template in step 9
        self.assertIn("tw-offsite.path", enabled)
        for unit in sorted(enabled):
            self.assertTrue((out / unit).is_file(), f"{unit} is enabled but never put in /etc/systemd/system")
        placed = {f.name for f in out.iterdir()}
        sources = {f.name for f in (REPO / "deploy" / "systemd").iterdir()} - {"tw-hermes-gateway.service.in", "user-hermes.slice.conf"}
        self.assertEqual(placed, sources)
        self.assertIn("user@1007.service", "".join(
            (out / f).read_text() for f in placed))

    def test_a_unit_file_of_an_unknown_kind_stops_the_install(self):
        d = Path(tempfile.mkdtemp()) / "systemd"
        shutil.copytree(REPO / "deploy" / "systemd", d)
        (d / "tw-new.automount").write_text("[Automount]\n")
        r, _ = self._place_units(d)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("tw-new.automount is not a kind of unit file", r.stdout)

    def test_the_install_makes_and_proves_every_bots_folder(self):
        # 31 Oct: a listing of /srv/tw/work showed no wisp folder, and the
        # file broker no longer makes a Bot's own folder.
        text = (REPO / "deploy" / "install.sh").read_text()
        bots = re.search(r'^bots="([^"]+)"', text, re.M).group(1).split()
        self.assertEqual(sorted(bots), sorted(policy().bots))
        make = text.index('install -d -m 2770 -o tw-files -g "tw-work-$b" "/srv/tw/work/$b"')
        prove = text.index('[[ "$got" == "tw-files:tw-work-$b 2770" ]] || stop')
        self.assertLess(make, prove)

    def test_the_backup_runs_at_half_past_three_mountain(self):
        self.assertEqual(unit("tw-backup.timer")["OnCalendar"], ["*-*-* 03:30:00 America/Denver"])
        self.assertEqual(unit("tw-snapshot.timer")["OnCalendar"], ["hourly"])

    def test_sudoers_names_the_helper_and_nothing_else(self):
        lines = [l for l in (REPO / "deploy" / "sudoers" / "tw-files").read_text().splitlines()
                 if l and not l.startswith("#")]
        rules = [l for l in lines if not l.startswith("Defaults")]
        self.assertEqual(rules, ["tw-files ALL=(root) NOPASSWD: /opt/thornswood-hermes/bin/tw-run-check"])

    def test_the_sftp_jail_is_sftp_only(self):
        text = (REPO / "deploy" / "sshd" / "50-tw-backup.conf").read_text()
        self.assertIn("Match User tw-backup", text)
        self.assertIn("ChrootDirectory /srv/tw/backups", text)
        self.assertIn("ForceCommand internal-sftp -R", text)
        for no in ("AllowTcpForwarding no", "X11Forwarding no", "PermitTTY no", "PasswordAuthentication no"):
            self.assertIn(no, text)

    def test_the_wrappers_import_only_the_root_owned_copy(self):
        for f in (REPO / "deploy" / "bin").iterdir():
            text = f.read_text()
            self.assertTrue(text.startswith("#!/usr/bin/python3 -I\n"), f.name)
            self.assertIn('sys.path.insert(0, "/opt/thornswood-hermes/current")', text, f.name)
            self.assertTrue(os.access(f, os.X_OK), f"{f.name} is not executable")

    def test_install_script_parses_and_stops_when_not_root(self):
        script = REPO / "deploy" / "install.sh"
        self.assertEqual(subprocess.run(["bash", "-n", str(script)]).returncode, 0)
        text = script.read_text()
        self.assertIn("set -euo pipefail", text)
        # It moves keys by name and never prints a value.
        self.assertNotRegex(text, r"cat [^|]*(\.env|\.pem|auth\.json)")
        if os.geteuid() != 0:
            r = subprocess.run(["bash", str(script), HEAD], capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("STOP", r.stdout)

    def test_systemd_accepts_the_units(self):
        if not shutil.which("systemd-analyze"):
            self.skipTest("systemd-analyze is not here")
        with tempfile.TemporaryDirectory() as t:
            for f in UNITS.iterdir():
                if f.suffix in (".service", ".timer", ".slice", ".path"):
                    shutil.copy(f, t)
            names = [str(Path(t) / f.name) for f in UNITS.iterdir() if f.suffix in (".service", ".timer", ".slice", ".path")]
            r = subprocess.run(["systemd-analyze", "verify", "--man=no", *names], capture_output=True, text=True)
            # verify also loads the host's own units and warns about them (on
            # the VM, Oracle's unified-monitoring-agent files are marked
            # executable); only a line naming one of ours is about ours. The
            # commands live under /opt only once installed; every other word
            # systemd says about our units is a fault in them.
            ours = tuple(Path(n).name for n in names)
            faults = [l for l in r.stderr.splitlines()
                      if (t in l or l.startswith(ours)) and "is not executable" not in l]
            self.assertEqual(faults, [], r.stderr)


# Attack test 16: the limits check at boot.

def plant_cgroups(root: Path, p: Policy, hermes_uid: int = 1002) -> None:
    for name, want in p.data["slices"].items():
        real = f"user-{hermes_uid}.slice" if name == "hermes-user.slice" else name
        d = root / "sys" / "fs" / "cgroup" / limits_check.slice_path(real)
        d.mkdir(parents=True)
        (d / "memory.max").write_text(f"{limits_check.to_bytes(want['MemoryMax'])}\n")
        (d / "memory.high").write_text(f"{limits_check.to_bytes(want['MemoryHigh'])}\n")
        (d / "pids.max").write_text(f"{want['TasksMax']}\n")
        (d / "cpu.max").write_text(f"{limits_check.quota_us(want['CPUQuota'])} 100000\n")
    (root / "proc").mkdir()
    (root / "proc" / "swaps").write_text("Filename Type Size Used Priority\n/swapfile file 4194300 0 -2\n")


class FakeRun:
    def __init__(self, quota="group quota on /srv (/dev/sda1) is on", sandbox="0::/tw.slice/tw-sandbox.slice/run-u7.service",
                 tier_a="0::/user.slice/user-1002.slice/user@1002.service/user.slice/libpod-ab.scope"):
        self.quota, self.sandbox, self.tier_a = quota, sandbox, tier_a
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[0] == "findmnt":
            return subprocess.CompletedProcess(argv, 0, "/\n", "")
        if argv[0] == "quota":
            gb = 25 if argv[-1] == "tw-work-wisp" else 10
            caps = getattr(self, "caps", {})
            limit = caps.get(argv[-1], gb * 1024 * 1024)
            return subprocess.CompletedProcess(argv, 0, f"Disk quotas for group {argv[-1]} (gid 990):\n"
                                               "     Filesystem  blocks   quota   limit   grace   files   quota   limit   grace\n"
                                               f"      /dev/sda1      12       0 {limit}               3       0       0\n", "")
        if argv[0] == "quotaon":
            return subprocess.CompletedProcess(argv, 0, self.quota, "")
        if argv[0] == "systemd-run":
            return subprocess.CompletedProcess(argv, 0, self.sandbox + "\n", "")
        return subprocess.CompletedProcess(argv, 0, self.tier_a + "\n", "")


def roomy() -> HostState:
    return HostState(16, 0, 0, 0, 0.2, 80, 80)


class LimitsCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.p = policy()
        plant_cgroups(self.root, self.p)

    def check(self, run=None, **kw):
        return limits_check.run_check(self.p, root=self.root, run=run or FakeRun(), hermes_uid=1002,
                                      image="thornswood-sandbox:pinned", host_state=kw.get("host") or roomy())

    def test_slice_paths(self):
        self.assertEqual(limits_check.slice_path("tw-hermes.slice"), "tw.slice/tw-hermes.slice")
        self.assertEqual(limits_check.slice_path("user-1002.slice"), "user.slice/user-1002.slice")
        self.assertEqual(limits_check.to_bytes("2560M"), 2560 * 1024 ** 2)
        self.assertEqual(limits_check.quota_us("150%"), 150000)

    def test_everything_holds(self):
        run = FakeRun()
        self.assertEqual(self.check(run), [])
        self.assertTrue(any(c[0] == "systemd-run" and "--slice=tw-sandbox.slice" in c for c in run.calls))
        self.assertTrue(any("podman" in c and "--network=none" in c for c in run.calls))

    def test_16_a_limit_that_is_not_applied_is_found(self):
        cg = self.root / "sys" / "fs" / "cgroup"
        (cg / "tw.slice/tw-sandbox.slice/memory.max").write_text("max\n")
        (cg / "tw.slice/tw-hermes.slice/cpu.max").write_text("max 100000\n")
        (cg / "tw.slice/tw-brokers.slice/pids.max").write_text("4096\n")
        (cg / "user.slice/user-1002.slice/memory.high").unlink()
        got = "\n".join(self.check())
        self.assertIn("tw-sandbox.slice: memory.max is max", got)
        self.assertIn("tw-hermes.slice: cpu.max is max", got)
        self.assertIn("tw-brokers.slice: pids.max is 4096", got)
        self.assertIn("user-1002.slice: memory.high cannot be read", got)

    def test_16_a_slice_that_is_not_loaded_is_found(self):
        shutil.rmtree(self.root / "sys/fs/cgroup/tw.slice/tw-brokers.slice")
        self.assertIn("tw-brokers.slice has no cgroup: nothing started it (tw-limits-check.service starts the Thornswood slices)",
                      self.check())

    def test_16_a_job_that_lands_outside_its_slice_is_found(self):
        got = "\n".join(self.check(FakeRun(sandbox="0::/system.slice/run-u7.service",
                                           tier_a="0::/tw.slice/tw-hermes.slice/libpod-ab.scope")))
        self.assertIn("a Tier B job did not land in tw-sandbox.slice", got)
        self.assertIn("a Tier A container did not land under the hermes user's slice", got)

    def test_16_swap_quotas_and_floors(self):
        (self.root / "proc" / "swaps").write_text("Filename Type Size Used Priority\n")
        got = "\n".join(self.check(FakeRun(quota="group quota on /srv/tw is off"),
                                   host=HostState(16, 0, 0, 0, 0.2, 20, 5)))
        self.assertIn("swapfile", got)
        self.assertIn("group quotas are not on", got)
        self.assertIn("20 GB free on /srv/tw", got)
        self.assertIn("free on the boot disk", got)

    def test_16_a_bot_without_its_disk_cap_is_found(self):
        run = FakeRun()
        run.caps = {"tw-work-wisp": 10 * 1024 * 1024}
        got = self.check(run)
        self.assertEqual(got, ["tw-work-wisp has no 25 GB hard limit on /"])
        self.assertIn(["quotaon", "-p", "-g", "/"], run.calls)

    def test_16_a_failed_check_holds_every_sandbox_job(self):
        from test_policy_service import service
        data = copy.deepcopy(self.p.data)
        data["admission"]["stage"] = "after"
        (self.root / "sys/fs/cgroup/tw.slice/tw-sandbox.slice/memory.max").write_text("max\n")
        problems = self.check()
        s = service(data=data)
        s.limits_record("root", {"ok": not problems, "details": problems})
        r = s.admit("hermes", {"bot": "rift", "task": "t", "kind": "sandbox"})
        self.assertNotEqual(r["state"], "admitted")
        self.assertIn("limits check", r["reason"])
        self.assertEqual(json.loads(s._meta("limits_details")), problems)


# The root helper a check starts through.

class CheckHelper(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp()) / "work"
        data = copy.deepcopy(policy().data)
        data["brokers"]["state"]["work"] = str(self.work)
        self.p = Policy(data)
        self.tree = self.work / "sprocket" / "Thornswood" / "t1"
        self.tree.mkdir(parents=True)
        self.work = Path(os.path.realpath(self.work))
        self.tree = Path(os.path.realpath(self.tree))
        data["brokers"]["state"]["work"] = str(self.work)
        self.p = Policy(data)

    def test_a_good_check(self):
        line = run_check.validate(self.p, ["sprocket", str(self.tree), "node", "tests/test-path-fence.js"])
        self.assertEqual(line[0], "systemd-run")
        self.assertIn("--uid=tw-sprocket", line)
        self.assertIn("--gid=tw-work-sprocket", line)
        self.assertIn("--slice=tw-sandbox.slice", line)
        self.assertIn("PrivateNetwork=yes", line)
        self.assertEqual(line[-3:], ["--", "node", "tests/test-path-fence.js"])

    def test_refusals(self):
        other = self.work / "wisp" / "Thornswood" / "t1"
        other.mkdir(parents=True)
        link = self.work / "sprocket" / "Thornswood" / "link"
        link.symlink_to(other)
        cases = [
            (["bramble", str(self.tree), "node", "tests/test-a.js"], "no execution user"),
            (["sprocket", str(other), "node", "tests/test-a.js"], "not a tree under"),
            (["sprocket", str(link), "node", "tests/test-a.js"], "not a tree under"),
            (["sprocket", str(self.tree.parent), "node", "tests/test-a.js"], "not a task tree"),
            (["sprocket", str(self.tree) + "/../t1", "node", "tests/test-a.js"], "not a tree under"),
            (["sprocket", str(self.tree), "bash", "-c", "curl x"], "one harness"),
            (["sprocket", str(self.tree), "node", "tests/test-../../x.js"], "one harness"),
            (["sprocket", str(self.tree)], "usage"),
        ]
        for argv, words in cases:
            with self.assertRaises(Refused, msg=argv) as e:
                run_check.validate(self.p, argv)
            self.assertIn(words, str(e.exception), argv)


# Attack test 21: a backup holds no secret, and it restores.

def plant_home(root: Path, p: Policy) -> None:
    h = root / "home/hermes/.hermes"
    (h / "profiles/rift/memories").mkdir(parents=True)
    (h / "config.yaml").write_text("model: x\n")
    (h / "profiles/rift/SOUL.md").write_text("Rift\n")
    (h / "profiles/rift/memories/MEMORY.md").write_text("remember\n")
    for secret in ("auth.json", ".env", "profiles/rift/.env", "profiles/rift/auth.json", "profiles/rift/app.pem",
                   "profiles/rift/credentials.json", "profiles/rift/hosts.yml", ".git-credentials"):
        (h / secret).write_text("SECRET-VALUE\n")
    for skipped in ("cache/page.html", "logs/gateway.log", "hermes-agent/run.py", "profiles/rift/image_cache/a.png"):
        (h / skipped).parent.mkdir(parents=True, exist_ok=True)
        (h / skipped).write_text("skip\n")
    (h / "profiles/rift/linked").symlink_to(root / "etc")
    db = sqlite3.connect(h / "state.db")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE t (x)")
    db.execute("INSERT INTO t VALUES ('kept')")
    db.commit()
    db.close()


def age_keys(t: Path) -> tuple[Path, Path]:
    key = t / "key.txt"
    subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    pub = t / "recipient.txt"
    pub.write_text(subprocess.run(["age-keygen", "-y", str(key)], check=True, capture_output=True, text=True).stdout)
    return key, pub


class Backups(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.p = policy()
        plant_home(self.root, self.p)
        from test_policy_service import Clock
        from thornswood_brokers.policy_service import PolicyService
        db = self.root / "srv/tw/policy/policy.db"
        db.parent.mkdir(parents=True)
        s = PolicyService(db, self.p, host_reader=roomy, clock=Clock())
        d = new_decision(id="d1", kind="main", repo="ThornswoodRP/Thornswood", commit=HEAD, revision=1,
                         answered_by="EvilPatrick06", at=s.clock())
        s.decision_record("root", {"decision": d.__dict__})
        s.db.close()

    def tar_names(self, data: bytes) -> list[str]:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            return tar.getnames()

    def test_21_the_archive_holds_no_secret(self):
        snap = backup.snapshot(self.root / "srv/tw/policy/policy.db", self.root / "snap.db")
        data = backup.build_tar(self.p, root=self.root, store_snapshot=snap)
        names = self.tar_names(data)
        self.assertIn("home/hermes/.hermes/config.yaml", names)
        self.assertIn("home/hermes/.hermes/profiles/rift/memories/MEMORY.md", names)
        self.assertIn("home/hermes/.hermes/state.db", names)
        self.assertEqual(names.count(backup.STORE_IN_ARCHIVE), 1)
        for n in names:
            self.assertNotRegex(n, r"(auth\.json|\.env|\.pem|credentials|hosts\.yml|git-credentials|/cache/|/logs/"
                                   r"|hermes-agent|image_cache|linked|-wal|-shm)", n)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for m in tar.getmembers():
                if m.isfile():
                    self.assertNotIn(b"SECRET-VALUE", tar.extractfile(m).read(), m.name)

    def test_21_encrypted_to_patricks_key_and_restored(self):
        if not shutil.which("age"):
            self.skipTest("age is not here")
        t = Path(tempfile.mkdtemp())
        key, pub = age_keys(t)
        p = self.p
        where = self.root / p.data["backups"]["age_recipient_file"].lstrip("/")
        where.parent.mkdir(parents=True)
        shutil.copy(pub, where)
        out = backup.archive(p, root=self.root, out_dir=t / "backups")
        self.assertTrue(out.name.endswith(".tar.gz.age"))
        self.assertNotEqual(out.read_bytes()[:2], b"\x1f\x8b", "the archive is not encrypted")
        self.assertEqual(list((t / "backups").iterdir()), [out], "only the encrypted file is written")
        plain = t / "plain.tar.gz"
        subprocess.run(["age", "--decrypt", "-i", str(key), "-o", str(plain), str(out)], check=True)

        fresh = Path(tempfile.mkdtemp())
        owners = []
        got = backup.restore(p, plain, root=fresh, owner=lambda m: (owners.append(m.name) or (0, 0)),
                             chown=lambda *a: None)
        self.assertEqual(got["killed"], 1, "the Main record must die on restore")
        self.assertEqual((fresh / "home/hermes/.hermes/profiles/rift/memories/MEMORY.md").read_text(), "remember\n")
        db = sqlite3.connect(fresh / "home/hermes/.hermes/state.db")
        self.assertEqual(db.execute("SELECT x FROM t").fetchall(), [("kept",)])
        db.close()
        self.assertFalse((fresh / "home/hermes/.hermes/auth.json").exists())
        from thornswood_brokers.policy_service import PolicyService
        from test_policy_service import Clock
        s = PolicyService(fresh / "srv/tw/policy/policy.db", p, host_reader=roomy, clock=Clock())
        self.assertIn("restored", s.check_decision("d1", kind="main", repo="ThornswoodRP/Thornswood", commit=HEAD).reason)
        self.assertIn(backup.STORE_IN_ARCHIVE, owners)

    def bad_tar(self, name: str, *, kind=tarfile.REGTYPE, uname="hermes") -> Path:
        path = Path(tempfile.mkdtemp()) / "bad.tar.gz"
        with tarfile.open(path, "w:gz") as tar:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.uname = uname
            body = b"x"
            if kind == tarfile.SYMTYPE:
                info.linkname = "/etc/shadow"
                tar.addfile(info)
            else:
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
        return path

    def test_21_a_bad_archive_is_not_restored(self):
        cases = [
            ("../etc/passwd", {}, "climbs out"),
            ("/etc/passwd", {}, "climbs out"),
            ("home/hermes/.hermes/../../../etc/cron.d/x", {}, "climbs out"),
            ("home/hermes/.hermes/x", {"kind": tarfile.SYMTYPE}, "not a plain file"),
            ("etc/sudoers.d/x", {}, "outside what a backup holds"),
            ("home/hermes/.ssh/authorized_keys", {}, "outside what a backup holds"),
            ("home/hermes/.hermes/auth.json", {}, "should never be in a backup"),
            ("home/hermes/.hermes/profiles/rift/.env", {}, "should never be in a backup"),
            ("home/hermes/.hermes/hermes-agent/run.py", {}, "folder a backup leaves out"),
            ("home/hermes/.hermes/config.yaml", {"uname": "root"}, "belongs to root"),
        ]
        for name, kw, words in cases:
            fresh = Path(tempfile.mkdtemp())
            with self.assertRaises(ValueError, msg=name) as e:
                backup.restore(self.p, self.bad_tar(name, **kw), root=fresh, chown=lambda *a: None,
                               mark_restored=lambda db: {"killed": 0})
            self.assertIn(words, str(e.exception), name)
            self.assertEqual(list(fresh.rglob("*")), [], f"{name}: nothing is written before a refusal")

    def test_snapshots_are_kept_by_count(self):
        folder = Path(tempfile.mkdtemp())
        for day in range(1, 11):
            for hour in range(0, 24, 6):
                (folder / f"policy-202611{day:02d}-{hour:02d}00.db").write_text("x")
        backup.prune_snapshots(folder, keep_hourly=6, keep_daily=7)
        left = sorted(p.name for p in folder.iterdir())
        self.assertEqual(len(left), 6 + 5)  # six newest, plus the newest of five more days
        self.assertIn("policy-20261110-1800.db", left)
        self.assertNotIn("policy-20261103-1800.db", left)

    def test_a_snapshot_of_a_live_store_is_consistent(self):
        db = self.root / "srv/tw/policy/policy.db"
        live = sqlite3.connect(db)
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("CREATE TABLE extra (x)")
        live.execute("INSERT INTO extra VALUES (1)")
        live.commit()
        out = backup.snapshot(db, self.root / "copy.db")
        live.close()
        copy_db = sqlite3.connect(out)
        self.assertEqual(copy_db.execute("SELECT x FROM extra").fetchall(), [(1,)])
        self.assertEqual(copy_db.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        copy_db.close()


if __name__ == "__main__":
    unittest.main()


# One's pull: SFTP and rclone are stand-ins; age and tar are real.

FAKE_SFTP = r'''#!/usr/bin/env python3
import os, shlex, shutil, sys
args = sys.argv[1:]
batch = args[args.index("-b") + 1]
assert args[-1] == "tw-backup@thornswood-bots", args
assert "IdentitiesOnly=yes" in args and "BatchMode=yes" in args
vm = os.environ["FAKE_VM"]
for line in open(batch):
    words = shlex.split(line)
    if words == ["ls", "-1"]:
        for n in sorted(os.listdir(vm)):
            print(n)
    elif words[0] == "get":
        shutil.copy(os.path.join(vm, words[1]), words[2])
    else:
        sys.exit(f"sftp: {line}")
'''

FAKE_RCLONE = r'''#!/usr/bin/env python3
import glob, os, shutil, sys
args = sys.argv[1:]
drive = os.environ["FAKE_DRIVE"]
with open(os.path.join(drive, "calls.txt"), "a") as f:
    f.write(" ".join(args) + "\n")
if args[0] == "copy":
    assert args[2] == "thornswood-gdrive:Thornswood-Hermes-Backups", args
    for p in glob.glob(os.path.join(args[1], "hermes-*.tar.gz.age")):
        shutil.copy(p, drive)
'''


class OnePull(unittest.TestCase):
    def setUp(self):
        for tool in ("pwsh", "age", "age-keygen", "tar"):
            if not shutil.which(tool):
                self.skipTest(f"{tool} is not here")
        self.t = Path(tempfile.mkdtemp())
        self.bin = self.t / "bin"
        self.bin.mkdir()
        for name, text in (("sftp", FAKE_SFTP), ("rclone", FAKE_RCLONE)):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        self.vm, self.drive, self.one, self.keys = (self.t / n for n in ("vm", "drive", "one", "keys"))
        for d in (self.vm, self.drive, self.keys):
            d.mkdir()
        key, pub = age_keys(self.keys)
        pub.unlink()
        (self.t / "sftp-key").write_text("not a real key\n")
        # Two nights' archives, made the way the VM makes them.
        root = Path(tempfile.mkdtemp())
        plant_home(root, policy())
        db = root / "srv/tw/policy/policy.db"
        db.parent.mkdir(parents=True)
        sqlite3.connect(db).execute("CREATE TABLE t (x)").connection.commit()
        where = root / policy().data["backups"]["age_recipient_file"].lstrip("/")
        where.parent.mkdir(parents=True)
        where.write_text(subprocess.run(["age-keygen", "-y", str(key)], capture_output=True, text=True,
                                        check=True).stdout)
        now = datetime.now(timezone.utc)
        for back in (1, 0):
            backup.archive(policy(), root=root, out_dir=self.vm, now=now - timedelta(days=back))

    def pull(self, *extra):
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_VM": str(self.vm),
               "FAKE_DRIVE": str(self.drive)}
        return subprocess.run(["pwsh", "-NoProfile", "-File", str(REPO / "scripts/one/Pull-HermesBackup.ps1"),
                               "-Folder", str(self.one), "-KeyFolder", str(self.keys),
                               "-SftpKey", str(self.t / "sftp-key"), *extra],
                              capture_output=True, text=True, env=env, timeout=120)

    def test_pull_keep_upload_and_prove_one_opens(self):
        r = self.pull("-Keep", "1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("PASS pulled 2 new backup(s); the VM holds 2", r.stdout)
        self.assertRegex(r.stdout, r"PASS hermes-\d{8}-\d{6}\.tar\.gz\.age decrypts and lists \d+ files")
        kept = sorted(p.name for p in self.one.glob("hermes-*.age"))
        self.assertEqual(kept, sorted(p.name for p in self.vm.iterdir())[-1:], "One keeps the newest -Keep")
        self.assertEqual(len(list(self.drive.glob("hermes-*.age"))), 1)
        self.assertIn("--min-age 90d", (self.drive / "calls.txt").read_text())
        self.assertTrue((self.one / "last-restore-test.txt").exists())
        self.assertNotIn("AGE-SECRET-KEY", r.stdout + r.stderr)
        self.assertEqual(list(self.one.glob("*.part")), [])

        again = self.pull("-Keep", "1")
        self.assertIn("PASS pulled 0 new backup(s)", again.stdout)
        self.assertNotIn("decrypts and lists", again.stdout, "the restore test runs every 28 days, not every night")

    def test_the_restore_test_uses_the_tar_it_was_given(self):
        # 31 Oct: on One, Git's GNU tar came first on PATH and read C:\... as
        # a host, so the restore test failed on a good backup. On Windows the
        # script names Windows' own tar by its full path; -Tar says which.
        text = (REPO / "scripts/one/Pull-HermesBackup.ps1").read_text(encoding="utf-8")
        self.assertIn("Join-Path $env:SystemRoot 'System32\\tar.exe'", text)
        self.assertIn("& $Tar -tzf $plain", text)
        self.assertNotIn("& tar ", text)
        r = self.pull("-Tar", str(self.t / "no-such-tar"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("no-such-tar is not installed on One", r.stdout)
        logged = self.t / "tar-calls.txt"
        fake = self.bin / "logging-tar"
        fake.write_text(f"#!/bin/sh\necho \"$@\" >> '{logged}'\nexec tar \"$@\"\n", encoding="utf-8")
        fake.chmod(0o755)
        r = self.pull("-Tar", str(fake))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("decrypts and lists", r.stdout)
        self.assertIn("-tzf", logged.read_text(encoding="utf-8"))

    def test_register_refuses_a_folder_that_gets_cleaned_up(self):
        # 31 Oct: the task was registered from a clone in Hermes's scratch
        # folder, which goes after a day without use.
        import shutil
        copy = Path(tempfile.mkdtemp()) / "Pull-HermesBackup.ps1"
        shutil.copy(REPO / "scripts/one/Pull-HermesBackup.ps1", copy)
        r = subprocess.run(["pwsh", "-NoProfile", "-File", str(copy), "-Folder", str(self.one),
                            "-KeyFolder", str(self.keys), "-Register"],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("which gets cleaned up", r.stdout)

    def test_register_never_replaces_a_task_it_does_not_own(self):
        # 31 Oct: task names ignore case on Windows, and -Force replaced the
        # task for Patrick's own Hermes backup, "Thornswood Hermes Backup Pull".
        text = (REPO / "scripts/one/Pull-HermesBackup.ps1").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"Register-ScheduledTask[^\n]*-Force")
        self.assertIn("$name = 'Thornswood Bots Hermes Backup Pull'", text)
        self.assertIn("runs something else; it is not replaced", text)
        self.assertNotEqual("thornswood bots hermes backup pull", "thornswood hermes backup pull")
        self.assertNotEqual("thornswood bots hermes backup pull", "thornswood patrick hermes backup pull")

    def test_a_file_that_is_not_age_is_not_kept(self):
        (self.vm / "hermes-20261101-033000.tar.gz.age").write_bytes(b"\x1f\x8bnot age")
        r = self.pull()
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAIL hermes-20261101-033000.tar.gz.age is not an age file", r.stdout)
        self.assertFalse((self.one / "hermes-20261101-033000.tar.gz.age").exists())

    def test_a_missed_night_fails(self):
        for p in self.vm.iterdir():
            p.rename(self.vm / "hermes-20261020-033000.tar.gz.age")
        r = self.pull()
        self.assertEqual(r.returncode, 1)
        self.assertIn("last night's did not run", r.stdout)


# The canary on the live VM: what it reads from each attack's output.

class CanaryReading(unittest.TestCase):
    def test_the_probes_run(self):
        from thornswood_brokers import canary
        t = Path(tempfile.mkdtemp())
        (t / "open.txt").write_text("x")
        r = subprocess.run([sys.executable, "-I", "-c", canary.PROBE_TIER_B, str(t / "open.txt"), str(t / "missing")],
                           capture_output=True, text=True, timeout=30)
        self.assertIn(f"READ {t / 'open.txt'}", r.stdout)
        self.assertIn(f"DENIED {t / 'missing'} FileNotFoundError", r.stdout)
        self.assertRegex(r.stdout, r"NETWORK (open|closed)")
        r = subprocess.run([sys.executable, "-I", "-c", canary.MEMORY, "1"], capture_output=True, text=True, timeout=30)
        self.assertIn("ALLOCATED 64 MB", r.stdout)
        r = subprocess.run(["sh", "-c", canary.CONTAINER], capture_output=True, text=True, timeout=30)
        self.assertIn("DONE", r.stdout)

    def test_tier_b_reading(self):
        from thornswood_brokers.canary import parse_cpu, parse_fork, parse_probe
        paths = ["/srv/tw/policy/canary.txt", "/proc/9/environ"]
        good = "DENIED /srv/tw/policy/canary.txt FileNotFoundError\nDENIED /proc/9/environ FileNotFoundError\nNETWORK closed OSError\n"
        self.assertEqual(parse_probe(good, paths), [])
        bad = "READ /srv/tw/policy/canary.txt\nDENIED /proc/9/environ PermissionError\nNETWORK open\n"
        self.assertEqual(parse_probe(bad, paths), ["READ /srv/tw/policy/canary.txt", "the check had a network"])
        self.assertIn("no answer for /proc/9/environ", parse_probe("DENIED /srv/tw/policy/canary.txt x\nNETWORK closed", paths))
        self.assertIsNone(parse_fork("FORK stopped at 251 BlockingIOError\n", 256))
        self.assertIn("not stopped", parse_fork("FORK not stopped at 512\n", 256))
        self.assertIn("got through", parse_fork("FORK stopped at 300 BlockingIOError\n", 256))
        self.assertIsNone(parse_cpu("CPU 4.10 WALL 4.05\n", 100))
        self.assertIn("allows 4.0s", parse_cpu("CPU 15.90 WALL 4.02\n", 100))

    def test_tier_a_reading(self):
        from thornswood_brokers.canary import parse_container
        self.assertEqual(parse_container("IFACE lo\nDONE\n"), [])
        got = parse_container("MOUNTED tmpfs\nHOSTHOME seen\nIFACE lo\nIFACE eth0\nDONE\n")
        self.assertEqual(got, ["the container mounted tmpfs", "the container sees /home/hermes/.hermes",
                               "the container has a network interface eth0"])
        self.assertIn("did not run", parse_container("Error: image not known")[0])

    def test_18_listeners(self):
        from thornswood_brokers.canary import listener_problem, page_problem, parse_listeners
        ss = ('LISTEN 0 4096 127.0.0.1:8611 0.0.0.0:* users:(("python3",pid=10,fd=3))\n'
              'LISTEN 0 128 100.100.1.10:9119 0.0.0.0:* users:(("hermes",pid=11,fd=7))\n'
              'LISTEN 0 128 *:9120 *:* users:(("hermes",pid=11,fd=8))\n'
              'LISTEN 0 128 [::1]:9121 [::]:* users:(("hermes",pid=11,fd=9))\n'
              'LISTEN 0 128 10.0.0.20:9122 0.0.0.0:* users:(("hermes",pid=11,fd=10))\n')
        got = parse_listeners(ss)
        self.assertEqual(got, [("127.0.0.1", 8611, 10), ("100.100.1.10", 9119, 11), ("*", 9120, 11),
                               ("::1", 9121, 11), ("10.0.0.20", 9122, 11)])
        ts = {"100.100.1.10"}
        self.assertEqual([listener_problem(a, ts) is None for a, _, _ in got], [True, True, False, True, False])
        self.assertIsNone(page_problem(200, '<script>window.__HERMES_AUTH_REQUIRED__=true;</script>'))
        self.assertIsNotNone(page_problem(200, 'window.__HERMES_SESSION_TOKEN__="abc123";'))
        self.assertIsNotNone(page_problem(200, 'window.__HERMES_AUTH_REQUIRED__=false;'))
        self.assertIsNone(page_problem(401, ""))
