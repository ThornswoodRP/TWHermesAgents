"""Merge green pull requests: what GitHub's auto-merge would do, on a private repository on the free plan.

Patrick, 2 Nov 2026: "I want automerge on for this shit too". The script and
its workflow stand in for auto-merge: a pull request merges once both guard
rail checks pass on its exact head, and nothing that has not passed merges.
"""

from __future__ import annotations

import importlib.util
import re
import unittest

import yaml

from helpers import REPO

spec = importlib.util.spec_from_file_location("merge_green", REPO / "scripts" / "merge_green.py")
mg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mg)

R = "ThornswoodRP/Thornswood-Hermes"
OK = [{"name": n, "status": "completed", "conclusion": "success"} for n in mg.REQUIRED]


class Fake:
    def __init__(self, prs, checks, mergeable=None):
        self.prs = {p["number"]: p for p in prs}
        self.checks = checks
        self.mergeable = mergeable or {}
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and path.startswith(f"/repos/{R}/pulls?"):
            return 200, [p for p in self.prs.values() if p["base"]["ref"] == "main"]
        m = re.match(rf"^/repos/{R}/pulls/(\d+)$", path)
        if method == "GET" and m:
            p = dict(self.prs[int(m.group(1))])
            p["mergeable"] = self.mergeable.get(p["number"], True)
            return 200, p
        m = re.match(rf"^/repos/{R}/commits/(\w+)/check-runs", path)
        if m:
            return 200, {"check_runs": self.checks.get(m.group(1), [])}
        m = re.match(rf"^/repos/{R}/pulls/(\d+)/merge$", path)
        if method == "PUT" and m:
            n = int(m.group(1))
            del self.prs[n]
            return 200, {"merged": True}
        m = re.match(rf"^/repos/{R}/git/refs/heads/(.+)$", path)
        if method == "DELETE" and m:
            for p in self.prs.values():
                if p["base"]["ref"] == m.group(1):
                    p["base"]["ref"] = "main"  # GitHub aims a stacked pull request at main
            return 204, {}
        return 404, {"message": "not in the fake"}

    def merged(self):
        return [int(re.search(r"/pulls/(\d+)/merge", c[1]).group(1)) for c in self.calls if c[0] == "PUT"]


def pr(n, sha, base="main", ref=None, draft=False, repo=R):
    return {"number": n, "draft": draft, "head": {"sha": sha, "ref": ref or f"b{n}", "repo": {"full_name": repo}}, "base": {"ref": base}}


class MergeGreen(unittest.TestCase):
    def run_it(self, fake):
        said = []
        code = mg.run(R, call=fake, say=said.append, wait=lambda s: None)
        return code, said

    def test_a_green_pull_request_merges_at_its_exact_head_with_a_merge_commit(self):
        f = Fake([pr(1, "a1")], {"a1": OK})
        code, said = self.run_it(f)
        self.assertEqual(code, 0)
        self.assertEqual(f.merged(), [1])
        put = next(c for c in f.calls if c[0] == "PUT")
        self.assertEqual(put[2], {"sha": "a1", "merge_method": "merge"})
        self.assertIn(("DELETE", f"/repos/{R}/git/refs/heads/b1", None), f.calls)

    def test_nothing_merges_without_both_checks_passing(self):
        one = [OK[0]]
        failed = OK + [{"name": "Something else", "status": "completed", "conclusion": "failure"}]
        running = [OK[0], {"name": mg.REQUIRED[1], "status": "in_progress", "conclusion": None}]
        f = Fake([pr(1, "a1"), pr(2, "a2"), pr(3, "a3"), pr(4, "a4")], {"a1": one, "a2": failed, "a3": running, "a4": []})
        code, said = self.run_it(f)
        self.assertEqual(f.merged(), [])
        self.assertTrue(any("#2 not yet: failed: Something else" in s for s in said))

    def test_a_draft_another_repository_and_one_that_does_not_merge_cleanly_stay(self):
        f = Fake([pr(1, "a1", draft=True), pr(2, "a2", repo="someone/fork"), pr(3, "a3")],
                 {"a1": OK, "a2": OK, "a3": OK}, mergeable={3: False})
        self.run_it(f)
        self.assertEqual(f.merged(), [])

    def test_a_stack_merges_in_order_once_each_one_below_has_landed(self):
        f = Fake([pr(92, "s92", ref="phone"), pr(95, "s95", base="phone", ref="chat"), pr(96, "s96", base="chat", ref="board"),
                  pr(97, "s97", base="board")],
                 {"s92": OK, "s95": OK, "s96": OK, "s97": [OK[0]]})
        self.run_it(f)
        self.assertEqual(f.merged(), [92, 95, 96])

    def test_the_workflow_runs_mains_script_after_the_guard_rail_tests(self):
        text = (REPO / ".github" / "workflows" / "merge-green.yml").read_text(encoding="utf-8")
        wf = yaml.safe_load(text)
        on = wf.get("on", wf.get(True))
        self.assertEqual(on["workflow_run"]["workflows"], ["Guard rail tests"])
        self.assertNotIn("pull_request_target", text)
        self.assertNotIn("secrets.", text)
        self.assertEqual(wf["permissions"], {"contents": "write", "pull-requests": "write", "checks": "read"})
        steps = wf["jobs"]["merge"]["steps"]
        checkout = steps[0]
        self.assertEqual(checkout["with"], {"ref": "main", "persist-credentials": False})
        self.assertRegex(checkout["uses"], r"^actions/checkout@[0-9a-f]{40}$")
        self.assertEqual(steps[1]["run"].strip(), "python3 scripts/merge_green.py")
        ci = yaml.safe_load((REPO / ".github" / "workflows" / "guard-rail-tests.yml").read_text(encoding="utf-8"))
        self.assertEqual(ci["name"], "Guard rail tests")
        names = {f"Guard rail tests ({m['where']})" for m in ci["jobs"]["tests"]["strategy"]["matrix"]["include"]}
        self.assertEqual(names, set(mg.REQUIRED))


if __name__ == "__main__":
    unittest.main()
