"""deploy/node.sh: the VM's node, one pinned nodejs.org release.

Thornswood-Ops #275, 11 Nov 2026: the VM had Ubuntu's node 18, and
Thornswood's gamemode builder needs node:module's stripTypeScriptTypes (node
22.13 and later), so no Bot could build game/gamemode.js in its tree. The
script fetches the pinned release, unpacks nothing whose SHA-256 is not the
pinned one, and fetches nothing when that release is already in place.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "deploy" / "node.sh"
TEXT = SCRIPT.read_text(encoding="utf-8")
VERSION = re.search(r"^version=(\S+)$", TEXT, re.M).group(1)


def arch() -> str:
    m = os.uname().machine if hasattr(os, "uname") else ""
    return {"x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(m, "")


class Pin(unittest.TestCase):
    def test_one_release_both_vm_cpus_by_hash(self):
        self.assertRegex(VERSION, r"^\d+\.\d+\.\d+$")
        # Thornswood's CI runs node 24, and the builder needs 22.13 or later.
        self.assertEqual(VERSION.split(".")[0], "24")
        for cpu in ("x86_64) arch=x64", "aarch64|arm64) arch=arm64"):
            line = next(l for l in TEXT.splitlines() if cpu in l)
            self.assertRegex(line, r"sha=[0-9a-f]{64} ;;")
        self.assertIn('"https://nodejs.org/dist/v$version/$name.tar.xz"', TEXT)
        self.assertIn("--proto '=https'", TEXT)

    def test_nothing_is_unpacked_before_its_hash_is_checked(self):
        lines = TEXT.splitlines()
        check = next(i for i, l in enumerate(lines) if '[[ "$got" == "$sha" ]]' in l)
        unpack = next(i for i, l in enumerate(lines) if l.startswith("tar -xJf"))
        self.assertLess(check, unpack)


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("bash") and arch(), "Linux on x64 or arm64")
class Run(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.fetched = self.root / "fetched"
        # curl's stand-in: notes the URL and writes what is not the release.
        curl = self.bin / "curl"
        curl.write_text('#!/bin/sh\nout=""\nwhile [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift;; https:*) '
                        f'echo "$1" >> "{self.fetched}";; esac; shift; done\necho not-node > "$out"\n')
        curl.chmod(0o755)
        self.dest = self.root / "node"

    def run_it(self):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}")
        return subprocess.run(["bash", str(SCRIPT), str(self.dest)], capture_output=True, text=True, env=env)

    def test_a_download_that_is_not_the_release_is_refused(self):
        r = self.run_it()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("not the pinned", r.stderr)
        self.assertIn(f"https://nodejs.org/dist/v{VERSION}/node-v{VERSION}-linux-{arch()}.tar.xz",
                      self.fetched.read_text())
        self.assertFalse(self.dest.exists() and any(self.dest.iterdir()))

    def test_the_release_already_in_place_is_not_fetched_again(self):
        node = self.dest / f"node-v{VERSION}-linux-{arch()}" / "bin" / "node"
        node.parent.mkdir(parents=True)
        node.write_text(f"#!/bin/sh\necho v{VERSION}\n")
        node.chmod(0o755)
        r = self.run_it()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), str(node.parent))
        self.assertFalse(self.fetched.exists())

    def test_another_version_in_place_is_replaced(self):
        node = self.dest / f"node-v{VERSION}-linux-{arch()}" / "bin" / "node"
        node.parent.mkdir(parents=True)
        node.write_text("#!/bin/sh\necho v18.19.1\n")
        node.chmod(0o755)
        r = self.run_it()
        self.assertEqual(r.returncode, 1)
        self.assertTrue(self.fetched.exists(), "a node of another version is fetched again")


if __name__ == "__main__":
    unittest.main()
