"""The guard rail tests workflow: the merge check since One left (q-hermes-tests-without-one, answer B).

It must stay unable to write or hold a secret, pin its actions by commit, and
install only hash-pinned wheels.
"""

from __future__ import annotations

import re
import unittest

import yaml

from helpers import REPO

WORKFLOW = REPO / ".github" / "workflows" / "guard-rail-tests.yml"


class Workflow(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")
        self.wf = yaml.safe_load(self.text)

    def test_reads_only_and_holds_no_secret(self):
        self.assertEqual(self.wf["permissions"], {"contents": "read"})
        self.assertNotIn("secrets.", self.text)
        self.assertNotIn("pull_request_target", self.text)
        job = self.wf["jobs"]["tests"]
        self.assertNotIn("permissions", job)
        checkout = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout@"))
        self.assertIs(checkout["with"]["persist-credentials"], False)

    def test_actions_are_pinned_by_commit(self):
        uses = re.findall(r"uses:\s*(\S+)", self.text)
        self.assertTrue(uses)
        for u in uses:
            self.assertRegex(u, r"^actions/[a-z-]+@[0-9a-f]{40}$")

    def test_the_vms_python_and_ones(self):
        runs = {(m["os"], m["python"]) for m in self.wf["jobs"]["tests"]["strategy"]["matrix"]["include"]}
        self.assertEqual(runs, {("ubuntu-24.04", "3.12"), ("windows-latest", "3.13")})

    def test_only_hashed_wheels(self):
        installs = [ln for ln in self.text.splitlines() if "pip install" in ln]
        self.assertTrue(installs)
        for ln in installs:
            for flag in ("--require-hashes", "--only-binary :all:", "--no-deps"):
                self.assertIn(flag, ln)
        reqs = (REPO / "tests" / "requirements-ci.txt").read_text(encoding="utf-8")
        self.assertIn("pyyaml==", reqs)
        self.assertIn("--hash=sha256:", reqs)


if __name__ == "__main__":
    unittest.main()
