"""The VM's group disk caps: only the Bots' work groups have one.

On 2 Nov the Whisper model's download was refused with "Disk quota exceeded"
and every install stopped. Patrick's read on 3 Nov: tw-decide-voice, which
owns the model cache, had a 32 MB hard cap, which is exactly the canary's
test cap. The canary deleted its tw-work-canary group with that cap still on
its gid, and groupadd gave the gid to the next system group. These check that
the canary takes its cap off before it deletes the group, and that the
install takes off every cap that is not a Bot's.
"""

from __future__ import annotations

import sys as _sys
import unittest as _unittest

if not _sys.platform.startswith("linux"):
    # The canary and the install run only on the Oracle VM; these run in the
    # VM half of scripts/test-at-head.sh.
    raise _unittest.SkipTest("the disk caps are the Oracle VM's; their tests run there")

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import REPO, policy

from thornswood_brokers import canary, disk_caps

REPORT = """*** Report for group quotas on device /dev/sda1
Block grace time: 7days; Inode grace time: 7days
                        Block limits                File limits
Group           used    soft    hard  grace    used  soft  hard  grace
----------------------------------------------------------------------
#0        -- 17300000       0       0         361000     0     0
#1002     -- 8960000       0       0         361000     0     0
#963      --    3476       0   32768             41     0     0
#962      --       0       0   32768              0     0     0
#2001     +- 9000000 8000000 10485760  6days    100     0     0
#2002     --   12000       0 10485760            30     0   500
#2003     -+      10       0       0              9     5    10   none

"""


class Reading(unittest.TestCase):
    def test_every_group_with_a_limit_is_read_with_or_without_grace_columns(self):
        self.assertEqual(disk_caps.capped(REPORT), {
            963: (0, 32768, 0, 0), 962: (0, 32768, 0, 0), 2001: (8000000, 10485760, 0, 0),
            2002: (0, 10485760, 0, 500), 2003: (0, 0, 5, 10)})

    def test_the_bots_caps_are_kept_and_every_other_is_named(self):
        self.assertEqual(disk_caps.leftover(REPORT, {2001, 2002}), [962, 963, 2003])

    def test_a_line_in_another_shape_stops_rather_than_guessing(self):
        with self.assertRaises(ValueError):
            disk_caps.capped("#963 -- 3M 0 32M 41 0 0\n")

    def test_the_command_refuses_what_is_not_repquota_output(self):
        r = subprocess.run([sys.executable, "-I", str(REPO / "thornswood_brokers" / "disk_caps.py"), "2001"],
                           input="nothing here\n", capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (2, ""))
        r = subprocess.run([sys.executable, "-I", str(REPO / "thornswood_brokers" / "disk_caps.py"), "2001", "2002"],
                           input=REPORT, capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.split()), (0, ["962", "963", "2003"]), r.stderr)


class TheInstall(unittest.TestCase):
    def _run_step(self, report: str) -> tuple[subprocess.CompletedProcess, list[str]]:
        text = (REPO / "deploy" / "install.sh").read_text()
        step = text[text.index("# 7b."):text.index("# 7c.")]
        block = step[step.index("if quotas_on; then\n"):]
        fake = Path(tempfile.mkdtemp())
        log = fake / "setquota.log"
        groups = {"tw-work-rift": 2001, "tw-work-wisp": 2002, "tw-decide-voice": 963}
        tools = {
            "repquota": f"cat <<'EOF'\n{report}EOF\n",
            "setquota": f'echo "$*" >> {log}\n',
            "getent": ("case \"$2\" in\n"
                       + "".join(f"  {n}|{g}) echo '{n}:x:{g}:';;\n" for n, g in groups.items())
                       + "  *) exit 2;;\nesac\n"),
        }
        for name, body in tools.items():
            (fake / name).write_text("#!/bin/bash\n" + body)
            (fake / name).chmod(0o755)
        script = ('set -euo pipefail\nstop() { echo "STOP: $*"; exit 1; }\nok() { echo "ok $*"; }\n'
                  'quotas_on() { true; }\n'
                  f'srv_mnt=/\nbots="rift wisp"\nhere={REPO}\n' + block)
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                           env={**os.environ, "PATH": f"{fake}:{os.environ['PATH']}"})
        return r, (log.read_text().splitlines() if log.exists() else [])

    def test_every_cap_but_the_bots_is_taken_off_and_named(self):
        r, calls = self._run_step(REPORT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        gb = json.loads((REPO / "thornswood_guard" / "policy.json").read_text())["brokers"]["files"]["disk_quota_gb"]
        self.assertEqual(calls[:2], [f"-g tw-work-{b} 0 {gb.get(b, gb['default']) * 1024 * 1024} 0 0 /"
                                     for b in ("rift", "wisp")])
        self.assertEqual(calls[2:], ["-g 962 0 0 0 0 /", "-g 963 0 0 0 0 /", "-g 2003 0 0 0 0 /"])
        self.assertIn("took the cap off gid 963 (tw-decide-voice)", r.stdout)
        self.assertIn("took the cap off gid 962 (no group has it now)", r.stdout)

    def test_output_it_cannot_read_stops_the_install(self):
        r, calls = self._run_step("#963 -- 3M 0 32M 41 0 0\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("STOP: the group quotas on / could not be read", r.stdout)
        self.assertEqual(len(calls), 2)


class TheCanary(unittest.TestCase):
    def _canary(self, setquota_off_fails: bool = False) -> tuple[canary.Canary, list[list[str]]]:
        calls: list[list[str]] = []

        def run(argv, **kw):
            calls.append(list(argv))
            out, rc = "", 0
            if argv[:1] == ["findmnt"]:
                out = "/\n"
            elif argv[:1] == ["quotaon"]:
                out = "group quota on / (/dev/sda1) is on\n"
            elif argv[:1] == ["setquota"] and argv[3] == "0" and setquota_off_fails:
                rc = 1
            elif argv[:1] == ["setpriv"]:
                return subprocess.CompletedProcess(argv, 1, "", "dd: error writing: Disk quota exceeded")
            return subprocess.CompletedProcess(argv, rc, out, "refused" if rc else "")

        work = Path(tempfile.mkdtemp())
        pol = policy()
        pol.data["brokers"]["state"]["work"] = str(work)
        c = canary.Canary(pol, run=run)
        return c, calls

    def _disk_quota(self, c):
        from unittest import mock
        with mock.patch.object(canary.shutil, "chown"):
            c.disk_quota()

    def _finish(self, c) -> tuple[int, str]:
        """finish() prints the canary's PASS and FAIL lines; they are what these tests check,
        so they are read here and kept out of the suite's own output."""
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            rc = c.finish()
        return rc, printed.getvalue()

    def test_the_cap_comes_off_before_the_group_is_deleted(self):
        c, calls = self._canary()
        self._disk_quota(c)
        self.assertEqual(c.results[-1][1], True, c.results)
        rc, printed = self._finish(c)
        self.assertEqual(rc, 0, printed)
        self.assertIn("PASS 3: writing past the disk quota is refused", printed)
        quota = [a for a in calls if a[:1] in (["setquota"], ["groupdel"])]
        self.assertEqual(quota, [["setquota", "-g", "tw-work-canary", "0", str(32 * 1024), "0", "0", "/"],
                                 ["setquota", "-g", "tw-work-canary", "0", "0", "0", "0", "/"],
                                 ["groupdel", "tw-work-canary"]])

    def test_a_cap_that_will_not_come_off_keeps_the_group(self):
        c, calls = self._canary(setquota_off_fails=True)
        self._disk_quota(c)
        rc, printed = self._finish(c)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL cleaning up: the cap on tw-work-canary was not taken off (refused); "
                      "tw-work-canary is kept so its gid is not handed on with a cap", printed)
        self.assertNotIn(["groupdel", "tw-work-canary"], calls)


if __name__ == "__main__":
    unittest.main()
