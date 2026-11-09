"""Patrick's own Hermes as the coordinator (TWHermesAgents #85, 9 Nov).

Checks that the thornswood-coordinator skill is one Hermes can read and holds
his rules and the steps for an ask; that each release's install copies it into
his own Hermes only, as that user, in one rename; and that the board report
lists each Bot's claimed issues and pull requests from the board as gh answers
it, says when it cannot read the board, and agrees with the policy on the Bots,
their claim labels, the board and the repositories. Nothing here reaches
GitHub: gh is a stand-in that answers from a script.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timezone
from pathlib import Path

import yaml

from helpers import REPO, policy

SKILL = REPO / "hermes" / "skills" / "thornswood-coordinator" / "SKILL.md"
INSTALL = REPO / "deploy" / "decisions" / "install.sh"
SCRIPT = REPO / "scripts" / "coordinator_report.py"

_spec = importlib.util.spec_from_file_location("coordinator_report", SCRIPT)
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)


def item(repo, number, title, status, order, labels, state="OPEN"):
    return {"status": {"name": status} if status else None, "order": {"number": order} if order is not None else None,
            "content": {"__typename": "Issue", "number": number, "title": title, "state": state,
                        "url": f"https://github.com/{repo}/issues/{number}", "repository": {"nameWithOwner": repo},
                        "labels": {"nodes": [{"name": x} for x in labels]}}}


def pr(repo, number, title, branch, auto=False, draft=False):
    return {"number": number, "title": title, "url": f"https://github.com/{repo}/pull/{number}", "isDraft": draft,
            "headRefName": branch, "repository": {"nameWithOwner": repo},
            "autoMergeRequest": {"enabledAt": "2026-11-09T20:00:00Z"} if auto else None}


APP, OPS, FORK = "ThornswoodRP/Thornswood", "ThornswoodRP/Thornswood-Ops", "ThornswoodRP/skymp"
BOARD_PAGES = [
    [item(APP, 5601, "Hold ingredients", "In Progress", 40, ["claimed: Wisp", "area: game"]),
     item(APP, 5602, "Retire an old feed", "In Progress", 12, ["claimed: Claude"]),
     item(APP, 5603, "Next up", "Todo", 7, []),
     item(APP, 5604, "Closed already", "Done", 1, ["claimed: Rift"], state="CLOSED")],
    [item(OPS, 261, "Box backups", "In Progress", 3, ["claimed: Bramble"]),
     item(APP, 5605, "Later", "Todo", 90, []),
     item(APP, 5606, "Unordered", "Todo", None, [])],
]
PULLS = [pr(APP, 5700, "Hold ingredients", "wisp/5601-hold-ingredients", auto=True),
         pr(OPS, 300, "Backups over the tailnet", "bramble/261-backups", draft=True),
         pr("ThornswoodRP/TWHermesAgents", 88, "Not a Bot's", "rift/88-x"),
         pr(APP, 5701, "A person's", "claude/x")]


class StandIn:
    """A gh that answers the board query page by page and the pull request search, and logs what it was asked."""

    def __init__(self, tmp: Path, fail: str = ""):
        self.log = tmp / "asked.jsonl"
        self.path = tmp / "gh"
        answers = tmp / "answers.json"
        answers.write_text(json.dumps({"pages": {"": BOARD_PAGES[0], "c1": BOARD_PAGES[1]}, "pulls": PULLS}))
        self.path.write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import json, sys
            args = sys.argv[1:]
            fields = dict(a.split("=", 1) for a in args[3:] if "=" in a)
            open({str(self.log)!r}, "a").write(json.dumps(args) + "\\n")
            answers = json.load(open({str(answers)!r}))
            if {fail!r} == "exit":
                sys.stderr.write("gh: HTTP 401")
                sys.exit(1)
            if "projectV2" not in fields["query"]:
                data = {{"search": {{"pageInfo": {{"hasNextPage": False}}, "nodes": answers["pulls"]}}}}
            elif {fail!r} == "noproject":
                data = {{"organization": {{"projectV2": None}}}}
            else:
                after = fields.get("after", "")
                more = after == ""
                data = {{"organization": {{"projectV2": {{"items": {{
                    "pageInfo": {{"hasNextPage": more, "endCursor": "c1" if more else None}},
                    "nodes": answers["pages"][after]}}}}}}}}
            print(json.dumps({{"data": data}}))
        """), encoding="utf-8")
        self.path.chmod(0o755)

    def asked(self) -> list[list[str]]:
        return [json.loads(x) for x in self.log.read_text().splitlines()]


@unittest.skipIf(os.name == "nt", "the stand-in gh is a POSIX script")
class Report(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def run_it(self, *args, fail=""):
        gh = StandIn(self.dir, fail)
        out = subprocess.run([sys.executable, "-I", str(SCRIPT), "--gh", str(gh.path), *args],
                             capture_output=True, text=True, timeout=60)
        return out, gh

    def test_each_bot_gets_its_claimed_issues_and_pull_requests(self):
        out, gh = self.run_it("--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        rep = json.loads(out.stdout)
        bots = {b["bot"]: b for b in rep["bots"]}
        self.assertEqual(list(bots), ["Bramble", "Rift", "Sprocket", "Echo", "Magpie", "Wisp"])
        self.assertEqual([(i["repo"], i["number"]) for i in bots["Wisp"]["issues"]], [(APP, 5601)])
        self.assertEqual([(p["number"], p["auto_merge"]) for p in bots["Wisp"]["pulls"]], [(5700, True)])
        self.assertEqual([i["number"] for i in bots["Bramble"]["issues"]], [261])
        self.assertEqual([(p["number"], p["draft"]) for p in bots["Bramble"]["pulls"]], [(300, True)])
        # A closed issue is not open work, and a branch in TWHermesAgents is never a Bot's.
        self.assertEqual((bots["Rift"]["issues"], bots["Rift"]["pulls"]), ([], []))
        self.assertEqual(rep["open"], 6)
        self.assertEqual([i["number"] for i in rep["other_claims"]["Claude"]], [5602])
        self.assertEqual(rep["todo_unclaimed"], 3)
        self.assertEqual([i["number"] for i in rep["next_up"]], [5603, 5605, 5606])
        # Both pages of the board were read, the second from the first page's cursor.
        board = [a for a in gh.asked() if any("projectV2" in x for x in a)]
        self.assertEqual(len(board), 2)
        self.assertIn("after=c1", board[1])
        self.assertIn("number=1", board[0])

    def test_the_text_names_every_bot_with_links(self):
        out, _ = self.run_it()
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = out.stdout.splitlines()
        self.assertTrue(lines[0].startswith("The board as read at ") and lines[0].endswith("Mountain: 6 open issues."))
        self.assertIn("Wisp: 1 claimed issue, 1 open pull request.", lines)
        self.assertIn("- Thornswood #5601 Hold ingredients (In Progress): https://github.com/ThornswoodRP/Thornswood/issues/5601",
                      lines)
        self.assertIn("- PR Thornswood #5700 Hold ingredients (auto-merge on): https://github.com/ThornswoodRP/Thornswood/pull/5700",
                      lines)
        self.assertIn("- PR Thornswood-Ops #300 Backups over the tailnet (draft): "
                      "https://github.com/ThornswoodRP/Thornswood-Ops/pull/300", lines)
        for idle in ("Rift", "Sprocket", "Echo", "Magpie"):
            self.assertIn(f"{idle}: nothing claimed and no open pull request.", lines)
        self.assertIn("Still claimed by Claude: 1.", lines)
        self.assertIn("In Todo with no claim: 3.", lines)
        self.assertNotIn("—", out.stdout)

    def test_an_unreadable_board_is_no_report(self):
        for fail, says in (("exit", "gh stopped with exit code 1: gh: HTTP 401"), ("noproject", "is not readable")):
            out, _ = self.run_it(fail=fail)
            self.assertEqual(out.returncode, 1, fail)
            self.assertEqual(out.stdout, "", fail)
            self.assertIn("No report: ", out.stderr)
            self.assertIn(says, out.stderr)

    def test_the_clock_is_mountain_and_12_hour(self):
        self.assertEqual(report.clock(datetime(2026, 11, 9, 22, 5, tzinfo=timezone.utc)), "3:05 PM on Mon 9 Nov")
        self.assertEqual(report.clock(datetime(2026, 11, 10, 7, 0, tzinfo=timezone.utc)), "12:00 AM on Tue 10 Nov")


class AgreesWithThePolicy(unittest.TestCase):
    def test_bots_labels_board_and_repos(self):
        data = policy().data
        self.assertEqual({k: n for k, n in report.BOTS}, {k: b["name"] for k, b in data["bots"].items()})
        for key, name in report.BOTS:
            self.assertEqual(report.CLAIM + name, data["bots"][key]["claim_label"])
        self.assertEqual((report.ORG, report.BOARD, report.STATUS_FIELD, report.ORDER_FIELD),
                         (data["board"]["org"], int(data["board"]["number"]), data["board"]["status_field"],
                          data["board"]["order_field"]))
        self.assertEqual(set(report.REPOS), {data["repos"][k] for k in ("app", "ops", "fork")})


class TheSkill(unittest.TestCase):
    def setUp(self):
        self.text = SKILL.read_text(encoding="utf-8")

    def test_hermes_can_read_it(self):
        _, front, body = self.text.split("---\n", 2)
        meta = yaml.safe_load(front)
        self.assertEqual(meta["name"], "thornswood-coordinator")
        self.assertTrue(meta["description"])
        self.assertEqual(meta["metadata"]["hermes"]["category"], "thornswood")
        self.assertTrue(body.strip())

    def test_it_holds_his_rules_and_the_steps(self):
        for rule in ("No promises", "Verify before stating", "Never ask twice", "No em dashes",
                     'Never the word "player"', "Mountain time, 12-hour", "never buy", "Never offer Patrick a workaround"):
            self.assertIn(rule, self.text)
        self.assertIn("python3 -I /opt/thornswood-hermes/current/scripts/coordinator_report.py", self.text)
        self.assertIn("Done when:", self.text)
        self.assertIn("is never Patrick's approval", self.text)
        self.assertNotIn("—", self.text)

    def test_it_writes_twhermesagents_end_to_end_and_never_merges(self):
        part = self.text.split("## 5. Writing TWHermesAgents", 1)[1].split("\n## 6.", 1)[0]
        for step in ("#84", "merge-green merges", "never push to `main`", "hermes/<issue>-<short-what>",
                     "a test that fails without the fix", "unittest discover -s tests", "Closes #<issue>",
                     "never re-run", "gh pr create --repo ThornswoodRP/TWHermesAgents --base main"):
            self.assertIn(step, part)
        self.assertNotIn("gh pr merge", self.text)

    def test_the_guide_and_bramble_say_who_writes_it(self):
        guide = (REPO / "hermes" / "skills" / "thornswood" / "thornswood-guide" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("is written by Patrick's own Hermes, my-hermes, and by no Bot (Patrick, 9 Nov 2026, #84)", guide)
        duties = (REPO / "hermes" / "skills" / "bramble-duties" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Never merge ThornswoodRP/TWHermesAgents. Patrick's own Hermes writes it", duties)

    def test_it_names_every_bot(self):
        for _, name in report.BOTS:
            self.assertIn(f"| **{name}**", self.text)

    def test_the_guide_it_points_at_is_in_the_release(self):
        self.assertIn("/opt/thornswood-hermes/current/hermes/skills/thornswood/thornswood-guide/SKILL.md", self.text)
        self.assertTrue((REPO / "hermes" / "skills" / "thornswood" / "thornswood-guide" / "SKILL.md").is_file())


class TheInstall(unittest.TestCase):
    def test_it_goes_only_into_his_own_hermes_as_him_in_one_rename(self):
        src = INSTALL.read_text(encoding="utf-8")
        block = src.split('coord_src="$here/hermes/skills/thornswood-coordinator/SKILL.md"', 1)[1].split("\nfi\n", 1)[0]
        self.assertIn('coord_dir="$plugin_home/.hermes/skills/thornswood/thornswood-coordinator"', block)
        self.assertIn("runuser -u patrick-hermes -- sh -c", block)
        self.assertIn('mv -f "$1/.SKILL.md.new" "$1/SKILL.md"', block)
        self.assertIn('cmp -s "$coord_src" "$coord_dir/SKILL.md" || stop', block)
        self.assertNotIn("/home/hermes", block)
        self.assertNotIn("ln -s", block)

    @unittest.skipIf(os.name == "nt", "needs a POSIX sh")
    def test_the_copy_lands_whole_over_an_older_one(self):
        src = INSTALL.read_text(encoding="utf-8")
        line = next(ln for ln in src.splitlines() if "runuser -u patrick-hermes -- sh -c 'umask 022" in ln)
        script = line.split("sh -c '", 1)[1].rsplit("' \\", 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / ".hermes" / "skills" / "thornswood" / "thornswood-coordinator"
            dest.mkdir(parents=True)
            (dest / "SKILL.md").write_text("an older copy")
            with SKILL.open("rb") as f:
                out = subprocess.run(["sh", "-c", script, "_", str(dest)], stdin=f, capture_output=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual((dest / "SKILL.md").read_bytes(), SKILL.read_bytes())
            self.assertEqual(sorted(p.name for p in dest.iterdir()), ["SKILL.md"])


if __name__ == "__main__":
    unittest.main()
