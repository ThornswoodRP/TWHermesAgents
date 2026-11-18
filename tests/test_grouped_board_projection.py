"""Portable tests of the actual board projection, without Linux socket setup.

Compile the unchanged production board/_areas/_bot method AST on Windows.
The full Linux broker suite separately covers caller credentials and wire I/O.
These fixtures exercise whole-snapshot eligibility and broker call budgets.
"""
import ast
import copy
import re
import unittest

from helpers import REPO, policy


class Refused(Exception):
    pass


def projection():
    tree = ast.parse((REPO / "src/thornswood_brokers/git_service.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "GitService")
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in ("board", "_areas", "_bot")]
    constants = [n for n in tree.body if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id in ("BOARD_QUERY", "BOARD_READ") for t in n.targets)]
    scope = {"re": re, "Refused": Refused}
    stub = ast.ClassDef(name="Board", bases=[], keywords=[], body=methods, decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[*constants, stub], type_ignores=[]))
    exec(compile(module, "actual_git_service_board", "exec"), scope)
    return scope["Board"]


APP = "ThornswoodRP/Thornswood"


def issue(n, area="Website", **changes):
    node = {"content": {"__typename": "Issue", "state": "OPEN", "number": n, "title": f"Issue {n}",
                        "repository": {"nameWithOwner": APP}, "author": {"login": "EvilPatrick06"},
                        "labels": {"nodes": [{"name": f"area: {area}"}]}, "assignees": {"nodes": []}},
            "status": {"name": "Todo"}, "order": {"number": n}}
    node["content"].update(changes)
    return node


def page(nodes, more=False, cursor=None):
    return {"organization": {"projectV2": {"items": {"nodes": nodes,
             "pageInfo": {"hasNextPage": more, "endCursor": cursor}}}}}


class Snapshot:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def graphql(self, *args):
        self.calls.append(args[-1])
        return copy.deepcopy(self.pages[min(len(self.calls) - 1, len(self.pages) - 1)])


class GroupedBoard(unittest.TestCase):
    def board(self, pages):
        b = projection()()
        b.policy, b.gh = policy(), Snapshot(pages)
        self.wait_reads = []
        b._waits_on = lambda repo, n: self.wait_reads.append((repo, n))
        return b

    def read(self, b, **more):
        return b.board("gateway", {"bot": "bramble", "eligible": True, "limit": 1,
                                   "groups": {"echo": {"areas": ["Website"]},
                                              "sprocket": {"areas": ["Launcher"]}}, **more})

    def test_later_field_survives_first_hundred_with_one_walk_for_all_workers(self):
        b = self.board([page([issue(n) for n in range(1, 101)], True, "100"),
                        page([issue(101, "Launcher")])])
        result = self.read(b)["groups"]
        self.assertEqual(result["echo"]["issues"][0]["number"], 1)
        self.assertEqual(result["sprocket"]["issues"][0]["number"], 101)
        self.assertEqual(len(b.gh.calls), 2, "not one whole project walk per worker")
        self.assertEqual(self.wait_reads, [(APP, 1), (APP, 101)])

    def test_common_eligibility_excludes_claims_people_and_waits_and_reads_shared_wait_once(self):
        nodes = [issue(1, labels={"nodes": [{"name": "area: Website"}, {"name": "claimed: Echo"}]}),
                 issue(2, author={"login": "WiccanDjinn"}),
                 issue(3, assignees={"nodes": [{"login": "Jhinswife"}]}), issue(4), issue(5)]
        b = self.board([page(nodes)])
        def waits(repo, n):
            self.wait_reads.append((repo, n))
            return "ThornswoodRP/Thornswood-Ops#1" if n == 4 else None
        b._waits_on = waits
        result = self.read(b, groups={"echo": {"areas": ["Website"]}, "also": {"areas": ["Website"]}})["groups"]
        self.assertEqual([i["number"] for i in result["echo"]["issues"]], [5])
        self.assertEqual(result["echo"], result["also"])
        self.assertEqual(self.wait_reads, [(APP, 4), (APP, 5)])

    def test_receipt_excludes_only_exact_task_leaving_next_issue(self):
        b = self.board([page([issue(1), issue(2)])])
        got = self.read(b, excluded_issues=[{"repo": APP, "number": 1}])["groups"]["echo"]
        self.assertEqual([i["number"] for i in got["issues"]], [2])
        self.assertEqual(self.wait_reads, [(APP, 2)])

    def test_unsupported_repository_cannot_bypass_dependency_eligibility(self):
        b = self.board([page([issue(1, repository={"nameWithOwner": "other/repo"}), issue(2)])])
        self.assertEqual(self.read(b)["groups"]["echo"]["issues"][0]["number"], 2)

    def test_missing_sources_or_nonadvancing_cursors_are_not_no_work(self):
        invalid = [{}, {"organization": None}, {"organization": {"projectV2": None}},
                   {"organization": {"projectV2": {"items": None}}}, page([], True, "x"),
                   page([{}]), page([issue(1)], True, None)]
        malformed = page([issue(1)])
        malformed["organization"]["projectV2"]["items"]["pageInfo"]["hasNextPage"] = "false"
        invalid.append(malformed)
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(Refused):
                self.read(self.board([data]))
        with self.assertRaises(Refused):
            self.read(self.board([page([issue(1)], True, "same")]))

    def test_nested_eligibility_data_cannot_disappear_silently(self):
        for change in ({"labels": None}, {"assignees": None}, {"number": True}, {"repository": None}, {"state": None}):
            with self.subTest(change=change), self.assertRaises(Refused):
                self.read(self.board([page([issue(1, **change)])]))

    def test_nullable_unset_status_and_unrelated_repository_do_not_block_real_work(self):
        unset = issue(1)
        unset["status"] = None
        unrelated = issue(2, repository={"nameWithOwner": "other/repo"}, labels=None)
        unrelated["status"] = None
        b = self.board([page([unset, unrelated, issue(3)])])
        self.assertEqual(self.read(b)["groups"]["echo"]["issues"][0]["number"], 3)

    def test_dependency_source_refusal_is_not_no_work_in_grouped_mode(self):
        b = self.board([page([issue(1)])])
        def refused(*args):
            raise Refused("dependency source unavailable")
        b._waits_on = refused
        with self.assertRaisesRegex(Refused, "source unavailable"):
            self.read(b)

    def test_snapshot_page_budget_refuses_incomplete_projection(self):
        b = self.board([page([issue(1)])])
        def endless(*args):
            b.gh.calls.append(args[-1])
            return page([issue(len(b.gh.calls))], True, str(len(b.gh.calls)))
        b.gh.graphql = endless
        with self.assertRaises(Refused):
            self.read(b)
        self.assertLessEqual(len(b.gh.calls), 200)

    def test_groups_are_router_eligible_only_and_exclusions_cannot_broaden_reads(self):
        for more in ({"bot": "echo"}, {"eligible": False}, {"groups": {}},
                     {"excluded_issues": [{"repo": "other/repo", "number": 1}]},
                     {"excluded_issues": [{"repo": APP, "number": True}]}):
            b = self.board([page([])])
            with self.subTest(more=more), self.assertRaises(Refused):
                self.read(b, **more)
            self.assertEqual(b.gh.calls, [])

    def test_legacy_board_reply_keeps_original_shape(self):
        b = self.board([page([issue(1)])])
        result = b.board("gateway", {"bot": "echo", "eligible": True})
        self.assertEqual(set(result), {"issues", "open", "waiting"})
        self.assertEqual(result["issues"][0]["number"], 1)
