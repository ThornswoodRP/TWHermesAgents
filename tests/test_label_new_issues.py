"""An issue opened with no label gets one (#58): the workflow that gives it.

The issue forms label what is filed through them, but an issue opened through
the API skips them, so #76, #77, #99, #123 and #181 arrived with no label.
"""

from __future__ import annotations

import unittest

import yaml

from helpers import REPO


class LabelNewIssues(unittest.TestCase):
    def setUp(self):
        self.text = (REPO / ".github" / "workflows" / "label-new-issues.yml").read_text(encoding="utf-8")
        self.wf = yaml.safe_load(self.text)

    def test_it_runs_on_a_new_issue_with_no_label_and_can_only_label(self):
        on = self.wf.get("on", self.wf.get(True))
        self.assertEqual(on, {"issues": {"types": ["opened", "reopened"]}})
        self.assertEqual(self.wf["permissions"], {"issues": "write"})
        job = self.wf["jobs"]["label"]
        self.assertEqual(job["if"], "${{ toJSON(github.event.issue.labels) == '[]' }}")
        self.assertNotIn("secrets.", self.text)
        self.assertNotIn("pull_request_target", self.text)
        self.assertFalse([s for s in job["steps"] if "uses" in s], "it checks out and runs no code")

    def test_the_issue_number_reaches_the_shell_only_through_the_environment(self):
        step = self.wf["jobs"]["label"]["steps"][0]
        self.assertEqual(step["env"]["NUMBER"], "${{ github.event.issue.number }}")
        self.assertNotIn("${{", step["run"])
        self.assertIn('gh issue edit "$NUMBER" --add-label triage', step["run"])


if __name__ == "__main__":
    unittest.main()
