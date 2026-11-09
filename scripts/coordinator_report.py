#!/usr/bin/env python3
"""Each Bot's open work, read from the board now, for Patrick's own Hermes (TWHermesAgents #85).

    python3 -I /opt/thornswood-hermes/current/scripts/coordinator_report.py [--json]

The thornswood-coordinator skill runs it, as the patrick-hermes user with that
user's own gh sign-in, whose project scope reads the org's board. It only
reads. For each of the six Bots it lists the open issues on the board that
carry the Bot's claim label, with their Status, and its open pull requests (a
branch named <bot>/...) in Thornswood, Thornswood-Ops and the skymp fork, with
whether auto-merge is on. Then any other claim still on the board (an older
claim such as `claimed: Claude`), and the issues in Todo that nobody has
claimed, lowest Order first.

A status report for Patrick is built from what this prints, never from memory
(Patrick, 2 Nov: a count given from memory was wrong, and he called it lying).
When gh cannot read the board, it says so and stops with exit code 1; it never
prints a report from part of the board.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ORG = "ThornswoodRP"
BOARD = 1
STATUS_FIELD = "Status"
ORDER_FIELD = "Order"
# The repositories the Bots work in (policy.yaml repos app, ops and fork); TWHermesAgents is never theirs.
REPOS = ("ThornswoodRP/Thornswood", "ThornswoodRP/Thornswood-Ops", "ThornswoodRP/skymp")
# The six Bots, in the guide's order, and each one's claim label (policy.yaml bots).
BOTS = (("bramble", "Bramble"), ("rift", "Rift"), ("sprocket", "Sprocket"),
        ("echo", "Echo"), ("magpie", "Magpie"), ("wisp", "Wisp"))
CLAIM = "claimed: "
MOUNTAIN = ZoneInfo("America/Denver")
NEXT_UP = 5

BOARD_QUERY = """
query($org: String!, $number: Int!, $after: String, $status: String!, $order: String!) {
  organization(login: $org) { projectV2(number: $number) { items(first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      status: fieldValueByName(name: $status) { ... on ProjectV2ItemFieldSingleSelectValue { name } }
      order: fieldValueByName(name: $order) { ... on ProjectV2ItemFieldNumberValue { number } }
      content { __typename ... on Issue { number title url state repository { nameWithOwner }
        labels(first: 30) { nodes { name } } } }
    } } } }
}"""

PULLS_QUERY = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest { number title url isDraft headRefName repository { nameWithOwner }
      autoMergeRequest { enabledAt } } }
  }
}"""


class Unread(Exception):
    """gh could not read what the report needs."""


def graphql(gh: str, query: str, strings: dict, ints: dict) -> dict:
    argv = [gh, "api", "graphql", "-f", f"query={query}"]
    for k, v in strings.items():
        if v is not None:
            argv += ["-f", f"{k}={v}"]
    for k, v in ints.items():
        argv += ["-F", f"{k}={int(v)}"]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Unread(f"gh did not answer: {e}") from e
    if out.returncode != 0:
        raise Unread(f"gh stopped with exit code {out.returncode}: {out.stderr.strip()[:300]}")
    try:
        data = json.loads(out.stdout)
    except ValueError as e:
        raise Unread(f"gh's answer is not JSON: {e}") from e
    if data.get("errors"):
        raise Unread("GitHub said: " + "; ".join(str(x.get("message")) for x in data["errors"])[:300])
    return data.get("data") or {}


def board(gh: str) -> list[dict]:
    """Every open issue on the board, with its Status, Order and labels."""
    rows, after = [], None
    while True:
        data = graphql(gh, BOARD_QUERY, {"org": ORG, "after": after, "status": STATUS_FIELD, "order": ORDER_FIELD},
                       {"number": BOARD})
        project = (data.get("organization") or {}).get("projectV2")
        if project is None:
            raise Unread(f"the board ({ORG} project {BOARD}) is not readable with this sign-in; "
                         "gh needs the read:project or project scope")
        page = project.get("items") or {}
        for node in page.get("nodes") or []:
            c = node.get("content") or {}
            if c.get("__typename") != "Issue" or c.get("state") != "OPEN":
                continue
            rows.append({"repo": (c.get("repository") or {}).get("nameWithOwner"), "number": c.get("number"),
                         "title": c.get("title"), "url": c.get("url"),
                         "status": (node.get("status") or {}).get("name"),
                         "order": (node.get("order") or {}).get("number"),
                         "labels": [x["name"] for x in (c.get("labels") or {}).get("nodes") or []]})
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return rows
        after = info["endCursor"]


def pulls(gh: str) -> list[dict]:
    """Every open pull request in the Bots' repositories."""
    rows, after = [], None
    q = f"org:{ORG} is:pr is:open"
    while True:
        page = graphql(gh, PULLS_QUERY, {"q": q, "after": after}, {}).get("search") or {}
        for n in page.get("nodes") or []:
            repo = (n.get("repository") or {}).get("nameWithOwner")
            if repo in REPOS:
                rows.append({"repo": repo, "number": n.get("number"), "title": n.get("title"), "url": n.get("url"),
                             "branch": n.get("headRefName") or "", "draft": bool(n.get("isDraft")),
                             "auto_merge": n.get("autoMergeRequest") is not None})
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return rows
        after = info["endCursor"]


def report(issues: list[dict], prs: list[dict]) -> dict:
    by_order = sorted(issues, key=lambda r: (r["order"] is None, r["order"] or 0, r["number"] or 0))
    bots = []
    for key, name in BOTS:
        bots.append({"bot": name,
                     "issues": [r for r in by_order if CLAIM + name in r["labels"]],
                     "pulls": sorted((p for p in prs if p["branch"].lower().startswith(key + "/")),
                                     key=lambda p: (p["repo"], p["number"]))})
    ours = {CLAIM + name for _, name in BOTS}
    other: dict[str, list] = {}
    for r in by_order:
        for label in r["labels"]:
            if label.startswith(CLAIM) and label not in ours:
                other.setdefault(label[len(CLAIM):], []).append(r)
    todo = [r for r in by_order if r["status"] == "Todo" and not any(x.startswith(CLAIM) for x in r["labels"])]
    return {"open": len(issues), "bots": bots, "other_claims": other, "todo_unclaimed": len(todo),
            "next_up": todo[:NEXT_UP]}


def short(repo: str) -> str:
    return repo.split("/", 1)[1]


def clock(now: datetime) -> str:
    t = now.astimezone(MOUNTAIN)
    hour = t.hour % 12 or 12
    return f"{hour}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'} on {t.strftime('%a')} {t.day} {t.strftime('%b')}"


def issue_line(r: dict) -> str:
    return f"- {short(r['repo'])} #{r['number']} {r['title']} ({r['status'] or 'no Status'}): {r['url']}"


def pull_line(p: dict) -> str:
    state = "draft" if p["draft"] else ("auto-merge on" if p["auto_merge"] else "auto-merge off")
    return f"- PR {short(p['repo'])} #{p['number']} {p['title']} ({state}): {p['url']}"


def text(rep: dict, now: datetime) -> str:
    out = [f"The board as read at {clock(now)} Mountain: {rep['open']} open issues.", ""]
    for b in rep["bots"]:
        n, m = len(b["issues"]), len(b["pulls"])
        if not n and not m:
            out.append(f"{b['bot']}: nothing claimed and no open pull request.")
            continue
        out.append(f"{b['bot']}: {n} claimed issue{'s' * (n != 1)}, {m} open pull request{'s' * (m != 1)}.")
        out += [issue_line(r) for r in b["issues"]] + [pull_line(p) for p in b["pulls"]]
    for who, rows in sorted(rep["other_claims"].items()):
        out.append(f"Still claimed by {who}: {len(rows)}.")
        out += [issue_line(r) for r in rows]
    out.append(f"In Todo with no claim: {rep['todo_unclaimed']}.")
    if rep["next_up"]:
        out.append("Next by Order:")
        out += [issue_line(r) for r in rep["next_up"]]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--gh", default="gh", help="the gh command (default: gh on PATH)")
    a = ap.parse_args(argv)
    try:
        rep = report(board(a.gh), pulls(a.gh))
    except Unread as e:
        print(f"No report: {e}", file=sys.stderr)
        return 1
    now = datetime.now(timezone.utc)
    print(json.dumps(rep | {"read_at": now.isoformat()}, indent=2) if a.json else text(rep, now))
    return 0


if __name__ == "__main__":
    sys.exit(main())
