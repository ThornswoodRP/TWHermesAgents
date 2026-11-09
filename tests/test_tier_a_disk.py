"""The disk cap inside a Tier A container (policy.yaml tier_a_disk).

Until 8 Nov 2026 nothing capped what a Bot's container wrote: its system
files were writable, /root was a folder under ~/.hermes/sandboxes, and
/workspace was a plain folder on the host disk. These hold each place it can
write to a cap: the system files are read-only, the container keeps nothing
between sessions (so /root and /home are Hermes's sized tmpfs), and
/workspace is the Bot's own fixed-size filesystem, which the install makes and
mounts, the boot check finds missing, and the canary writes past.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import REPO, policy

from thornswood_brokers import canary, limits_check
from thornswood_guard.profile_config import WORK_ROOT, build

BOTS = ("bramble", "rift", "sprocket", "echo", "magpie", "wisp")


class TheBotsConfig(unittest.TestCase):
    def test_every_container_is_read_only_keeps_nothing_and_works_in_its_own_folder(self):
        p = policy()
        self.assertEqual(p.data["tier_a_disk"]["work_root"], WORK_ROOT)
        for bot in BOTS:
            t = build(p, bot)["terminal"]
            self.assertEqual(t["docker_extra_args"], ["--read-only"], bot)
            self.assertFalse(t["container_persistent"], bot)
            self.assertTrue(t["docker_mount_cwd_to_workspace"], bot)
            self.assertEqual(t["cwd"], f"{WORK_ROOT}/{bot}", bot)
            self.assertEqual(t["docker_volumes"], [], bot)
            # podman's size option needs XFS with project quotas; the cap is the folder's filesystem.
            self.assertEqual(t["container_disk"], 0, bot)

    def test_each_bots_filesystem_is_its_disk_quota_size(self):
        p = policy()
        self.assertEqual(limits_check.tier_a_caps(p), {b: (25 if b == "wisp" else 10) for b in BOTS})


class CanaryReading(unittest.TestCase):
    def test_a_container_held_to_its_caps_passes(self):
        self.assertEqual(canary.parse_disk("WORKSPACE 29\nTMP 512\nDONE\n", 32, 512), [])

    def test_each_cap_it_got_past_is_named(self):
        got = canary.parse_disk("ROOTFS writable\nWORKSPACE 64\nTMP 640\nDONE\n", 32, 512)
        self.assertEqual(got, ["the container wrote to its system files",
                               "the container wrote 64 MB to a 32 MB /workspace",
                               "the container wrote 640 MB to a 512 MB /tmp"])

    def test_a_probe_that_did_not_run_fails(self):
        self.assertIn("did not run", canary.parse_disk("Error: image not known", 32, 512)[0])
        self.assertIn("? MB to a 32 MB /workspace", canary.parse_disk("TMP 1\nDONE\n", 32, 512)[0])


class CanaryRun(unittest.TestCase):
    def test_5b_runs_hermes_flags_and_the_bots_own_on_a_32_mb_filesystem_then_unmounts_it(self):
        calls: list[list[str]] = []

        def run(argv, **kw):
            calls.append(list(argv))
            out = "WORKSPACE 29\nTMP 512\nDONE\n" if "podman" in argv else ""
            return subprocess.CompletedProcess(argv, 0, out, "")

        c = canary.Canary(policy(), run=run)
        hermes = mock.Mock(pw_uid=1002, pw_gid=1002)
        with mock.patch.object(canary.pwd, "getpwnam", return_value=hermes):
            c.tier_a_disk("docker.io/nousresearch/hermes-sandbox:desktop", ["--read-only"])
        self.assertEqual(c.results, [("5b: a container writes nothing to its system files, or past its /workspace or /tmp",
                                      True, "")])
        mkfs = next(a for a in calls if a[0] == "mkfs.ext4")
        self.assertIn("root_owner=1002:1002", mkfs)
        mount = next(a for a in calls if a[0] == "mount")
        self.assertEqual(mount[:3], ["mount", "-o", "loop,nosuid,nodev"])
        podman = next(a for a in calls if "podman" in a)
        self.assertEqual(podman[:6], ["runuser", "-u", "hermes", "--", "env", "XDG_RUNTIME_DIR=/run/user/1002"])
        self.assertIn(f"{mount[-1]}:/workspace", podman)
        self.assertIn("--read-only", podman)
        self.assertLess(podman.index("--read-only"), podman.index("docker.io/nousresearch/hermes-sandbox:desktop"))
        for flag in canary.HERMES_PODMAN_ARGS:
            self.assertIn(flag, podman)
        with mock.patch("builtins.print"):
            c.finish()
        self.assertEqual(calls[-1], ["umount", mount[-1]])
        self.assertFalse(Path(mount[-1]).parent.exists())

    def test_5b_fails_when_the_canary_filesystem_cannot_be_made(self):
        def run(argv, **kw):
            return subprocess.CompletedProcess(argv, 1 if argv[0] == "mkfs.ext4" else 0, "", "no space")

        c = canary.Canary(policy(), run=run)
        with mock.patch.object(canary.pwd, "getpwnam", return_value=mock.Mock(pw_uid=1, pw_gid=1)):
            c.tier_a_disk("img", ["--read-only"])
        self.assertFalse(c.results[-1][1])
        self.assertIn("mkfs.ext4 failed", c.results[-1][2])

    def test_the_canary_runs_5b_with_the_bots_own_extra_flags(self):
        src = (REPO / "thornswood_brokers" / "canary.py").read_text(encoding="utf-8")
        self.assertIn('c.tier_a_disk(SANDBOX_IMAGE, build(policy, "rift")["terminal"]["docker_extra_args"])', src)


@unittest.skipUnless(sys.platform.startswith("linux"), "the install runs only on the Oracle VM")
class TheInstall(unittest.TestCase):
    """Step 7d of deploy/install.sh, run with stand-ins for the tools that touch the host."""

    def setUp(self):
        self.t = Path(tempfile.mkdtemp())
        self.fake, self.images, self.work, self.units = (self.t / n for n in ("bin", "images", "work", "units"))
        for d in (self.fake, self.units):
            d.mkdir()
        self.log = self.t / "calls.log"
        self.mounted = self.t / "mounted"
        self.mounted.write_text("")

    def run_step(self, bots: str = "rift wisp", gateway: str = "active") -> subprocess.CompletedProcess:
        text = (REPO / "deploy" / "install.sh").read_text(encoding="utf-8")
        step = text[text.index("# 7d."):text.index("# 8. Units and slices.")]
        step = step.replace("/etc/systemd/system", str(self.units))
        log, mounted = self.log, self.mounted
        tools = {
            "systemd-escape": 'p="${@: -1}"; echo "home-hermes-tw-work-${p##*/}.mount"\n',
            "mkfs.ext4": f'echo "mkfs.ext4 $*" >> {log}\n',
            "resize2fs": f'echo "resize2fs $*" >> {log}\n',
            "losetup": f'echo "losetup $*" >> {log}\n',
            "findmnt": 'echo /dev/loop7\n',
            "chown": f'echo "chown $*" >> {log}\n',
            "install": f'echo "install $*" >> {log}; mkdir -p "${{@: -1}}"\n',
            "runuser": f'echo "runuser $*" >> {log}\n',
            "mountpoint": f'grep -qxF "${{@: -1}}" {mounted}\n',
            "systemctl": (f'echo "systemctl $*" >> {log}\n'
                          f'case "$1" in\n'
                          f'  is-active) [[ "$*" == *--quiet* ]] && exit 0; echo {gateway};;\n'
                          f'  enable) if [[ "$*" == *--now* ]]; then echo "{self.work}/${{@: -1}}" | sed "s|home-hermes-tw-work-||; s|\\.mount$||" >> {mounted}; fi;;\n'
                          f'esac\n'
                          f'exit 0\n'),
        }
        for name, body in tools.items():
            (self.fake / name).write_text("#!/bin/bash\n" + body)
            (self.fake / name).chmod(0o755)
        script = ('set -euo pipefail\nstop() { echo "STOP: $*"; exit 1; }\nok() { echo "PASS $*"; }\n'
                  f'pol() {{ case "$1" in tier_a_disk.images) echo {self.images};; tier_a_disk.work_root) echo {self.work};; esac; }}\n'
                  f'bots="{bots}"\nhere={REPO}\nhermes_uid=1002\n'
                  'id() { echo 1002; }\n' + step)
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              env={**os.environ, "PATH": f"{self.fake}:{os.environ['PATH']}"})

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_the_first_run_makes_each_filesystem_and_moves_the_bots_work_onto_it(self):
        (self.work / "rift").mkdir(parents=True)
        (self.work / "rift" / "notes.txt").write_text("rift's work\n")
        r = self.run_step()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.images / "rift.img").stat().st_size, 10 * 1024 ** 3)
        self.assertEqual((self.images / "wisp.img").stat().st_size, 25 * 1024 ** 3)
        self.assertEqual(oct((self.images / "rift.img").stat().st_mode & 0o777), "0o600")
        calls = self.calls()
        self.assertIn(f"mkfs.ext4 -q -F -m 0 -L tw-rift -E root_owner=1002:1002 {self.images}/rift.img", calls)
        unit = (self.units / "home-hermes-tw-work-rift.mount").read_text()
        for line in (f"What={self.images}/rift.img", f"Where={self.work}/rift", "Type=ext4",
                     "Options=loop,nosuid,nodev,noatime", "WantedBy=local-fs.target",
                     "Before=tw-hermes-gateway.service tw-limits-check.service"):
            self.assertIn(line, unit)
        # The work moved onto the new filesystem, and nothing was left beside it.
        self.assertEqual((self.work / "rift" / "notes.txt").read_text(), "rift's work\n")
        self.assertFalse((self.work / "rift.before-disk").exists())
        # The gateway was stopped once, Hermes's sandboxes went, and the gateway came back.
        self.assertEqual(calls.count("systemctl stop tw-hermes-gateway.service"), 1)
        self.assertEqual(sum("podman rm -f" in c for c in calls), 1)
        self.assertLess(calls.index("systemctl stop tw-hermes-gateway.service"),
                        next(i for i, c in enumerate(calls) if "enable --quiet --now" in c))
        start = calls.index("systemctl start tw-hermes-gateway.service")
        self.assertEqual(calls[start - 1], "systemctl reset-failed tw-hermes-gateway.service")
        self.assertGreater(start, max(i for i, c in enumerate(calls) if "enable --quiet --now" in c))
        self.assertIn(f"PASS {self.work}/wisp is wisp's own 25 GB filesystem", r.stdout)

    def test_a_second_run_changes_nothing_and_leaves_the_gateway_alone(self):
        self.assertEqual(self.run_step().returncode, 0)
        self.log.unlink()
        r = self.run_step()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = self.calls()
        self.assertFalse([c for c in calls if c.startswith(("mkfs", "runuser", "resize2fs"))], calls)
        self.assertNotIn("systemctl stop tw-hermes-gateway.service", calls)
        self.assertNotIn("systemctl start tw-hermes-gateway.service", calls)

    def test_a_stopped_gateway_is_not_started(self):
        r = self.run_step(gateway="inactive")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("systemctl start tw-hermes-gateway.service", self.calls())

    def test_a_bigger_cap_grows_the_filesystem(self):
        self.assertEqual(self.run_step(bots="rift").returncode, 0)
        os.truncate(self.images / "rift.img", 5 * 1024 ** 3)
        r = self.run_step(bots="rift")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.images / "rift.img").stat().st_size, 10 * 1024 ** 3)
        self.assertIn("losetup -c /dev/loop7", self.calls())
        self.assertIn("resize2fs /dev/loop7", self.calls())

    def test_a_filesystem_bigger_than_its_cap_stops_the_install(self):
        self.images.mkdir()
        (self.images / "rift.img").write_bytes(b"")
        os.truncate(self.images / "rift.img", 11 * 1024 ** 3)
        r = self.run_step(bots="rift")
        self.assertEqual(r.returncode, 1)
        self.assertIn("more than 10 GB; a Bot's filesystem is not shrunk here", r.stdout)

    def test_a_folder_left_from_an_earlier_run_stops_before_anything_moves(self):
        (self.work / "rift").mkdir(parents=True)
        (self.work / "rift.before-disk").mkdir()
        r = self.run_step(bots="rift")
        self.assertEqual(r.returncode, 1)
        self.assertIn("rift.before-disk is there from an earlier run", r.stdout)
        self.assertTrue((self.work / "rift").is_dir())


if __name__ == "__main__":
    unittest.main()
