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

    def test_every_job_runs_on_githubs_own_runners(self):
        """The repository is public: a pull request from anyone can name any
        runner label, so a runner of ours (the VM, the box) would run a
        stranger's code. Every job takes one of GitHub's standard images."""
        hosted = re.compile(r"^(ubuntu|windows|macos)-[0-9a-z.]+$")
        for wf in (REPO / ".github" / "workflows").glob("*.yml"):
            for name, job in yaml.safe_load(wf.read_text(encoding="utf-8"))["jobs"].items():
                runs_on = job["runs-on"]
                labels = ([m["os"] for m in job["strategy"]["matrix"]["include"]]
                          if runs_on == "${{ matrix.os }}" else [runs_on])
                for label in labels:
                    self.assertRegex(label, hosted, f"{wf.name} {name}")

    def test_windows_has_gits_bash_before_any_bash_step(self):
        """8 Nov 2026: tw-hermes-box's first jobs stopped at "bash: command
        not found", since a service's PATH holds Git's cmd folder only."""
        steps = self.wf["jobs"]["tests"]["steps"]
        names = [s.get("name", "") for s in steps]
        i = names.index("Git's bash on Windows, as on One")
        step = steps[i]
        self.assertEqual(step["if"], "runner.os == 'Windows'")
        self.assertEqual(step["shell"], "pwsh")
        self.assertIn("Join-Path $env:ProgramFiles 'Git\\bin'", step["run"])
        self.assertIn("$env:GITHUB_PATH", step["run"])
        first_bash = min(n for n, s in enumerate(steps) if s.get("shell") == "bash")
        self.assertLess(i, first_bash)

    def test_the_vms_job_runs_the_vms_node_first(self):
        """10 Nov 2026: the VM had Ubuntu's node 18, a pane test needed node
        19, and only the VM's suite went red. 11 Nov 2026 (Thornswood-Ops
        #275): the VM's node is deploy/node.sh's pinned release, and the VM's
        job runs the same script, so the two cannot differ."""
        steps = self.wf["jobs"]["tests"]["steps"]
        names = [s.get("name", "") for s in steps]
        i = names.index("The VM's node first, as on the VM")
        step = steps[i]
        self.assertEqual(step["if"], "runner.os == 'Linux'")
        self.assertIn('bash deploy/node.sh "$RUNNER_TEMP/vm-node"', step["run"])
        self.assertIn("$GITHUB_PATH", step["run"])
        self.assertLess(i, names.index("The suite"))
        install = (REPO / "deploy" / "install.sh").read_text(encoding="utf-8")
        self.assertIn('bash "$here/deploy/node.sh" "$opt/node"', install)
        self.assertIn('link_in "$nodebin/node" /usr/local/bin/node', install)
        self.assertNotIn("apt-get install -y -q nodejs", install)

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
