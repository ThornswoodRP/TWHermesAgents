"""Merge every open pull request into main whose exact head passed both guard rail checks.

GitHub's own auto-merge needs a paid plan on a private repository, so this
does its job here (Patrick, 2 Nov 2026: "I want automerge on for this shit
too"). .github/workflows/merge-green.yml runs it from main, never from a
pull request's code, each time the guard rail tests finish and by hand.

A pull request merges when it is open, not a draft, from this repository,
aimed at main, both "Guard rail tests" checks are a success on its head and
nothing else on that head failed, and GitHub says it merges cleanly. It is
merged with a merge commit at that exact head (a push after the checks
stops it), and its branch is deleted once every pull request stacked on
it is aimed at main, so those are looked at in the next pass. Nothing is retried: a
pull request that is not ready is said and left for the next run.

A merge it makes closes the issues the pull request's body names with a
closing keyword (Closes #N, Fixes #N, Resolves #N and their other forms), here
in this repository. GitHub does that for a merge made by a person, and never
for one made with an Actions token (#126): of the 27 pull requests this job
merged from 4 to 15 Nov 2026 that said Closes, not one closed its issue, even
with issues: write on the token (#141 and #136, 15 Nov), while #113, which
Patrick merged on 10 Nov, closed #101 and #102 two seconds after.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import quote

REQUIRED = ("Guard rail tests (VM's Python)", "Guard rail tests (One's Python)")
# GitHub's closing keywords, then this repository's issue: #N or owner/name#N.
CLOSES = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?[ \t]+(?:([\w.-]+/[\w.-]+))?#(\d+)\b", re.IGNORECASE)
BAD = {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}


def api(method, path, body=None):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": "Bearer " + os.environ["GH_TOKEN"],
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"message": raw.decode(errors="replace")}


def green(repo, sha, call):
    status, body = call("GET", f"/repos/{repo}/commits/{sha}/check-runs?per_page=100")
    if status != 200:
        return False, f"its checks did not read ({status})"
    runs = body.get("check_runs", [])
    by_name = {}
    for r in runs:
        by_name.setdefault(r["name"], []).append(r)
    failed = sorted({r["name"] for r in runs if r.get("conclusion") in BAD})
    if failed:
        return False, "failed: " + ", ".join(failed)
    for name in REQUIRED:
        if not any(r.get("status") == "completed" and r.get("conclusion") == "success" for r in by_name.get(name, [])):
            return False, f"{name} has not passed on {sha[:7]}"
    return True, ""


def closes(repo, body):
    """The issues of repo a pull request's body names with a closing keyword, in order."""
    out = []
    for m in CLOSES.finditer(body or ""):
        if (m.group(1) is None or m.group(1).lower() == repo.lower()) and int(m.group(2)) not in out:
            out.append(int(m.group(2)))
    return out


def close_what_it_names(repo, p, call, say):
    """After the merge, each issue the pull request says it closes, as GitHub would."""
    n = p["number"]
    for i in closes(repo, p.get("body")):
        status, issue = call("GET", f"/repos/{repo}/issues/{i}")
        if status != 200:
            say(f"  #{i}, which #{n} closes, did not read ({status}); it stays open")
            continue
        if issue.get("pull_request"):
            say(f"  #{i}, which #{n} names, is a pull request; left as it is")
            continue
        if issue.get("state") != "open":
            continue
        status, body = call("PATCH", f"/repos/{repo}/issues/{i}", {"state": "closed", "state_reason": "completed"})
        if status != 200:
            say(f"  #{i}, which #{n} closes, was not closed ({status}): {body.get('message', '')}")
        else:
            say(f"  #{i} closed: #{n} merged")


def mergeable(repo, number, call, wait=time.sleep):
    # GitHub works mergeability out after it is asked; null means not yet.
    for _ in range(6):
        status, pr = call("GET", f"/repos/{repo}/pulls/{number}")
        if status != 200:
            return None, f"did not read ({status})"
        if pr.get("mergeable") is not None:
            return pr, ""
        wait(5)
    return None, "GitHub did not say whether it merges"


def one_pass(repo, call, say, wait=time.sleep):
    status, prs = call("GET", f"/repos/{repo}/pulls?state=open&base=main&per_page=100")
    if status != 200:
        say(f"the open pull requests did not read ({status})")
        return None
    merged = 0
    for p in sorted(prs, key=lambda p: p["number"]):
        n = p["number"]
        if p.get("draft"):
            continue
        if (p.get("head", {}).get("repo") or {}).get("full_name") != repo:
            say(f"#{n} is from another repository; not merged")
            continue
        sha = p["head"]["sha"]
        ok, why = green(repo, sha, call)
        if not ok:
            say(f"#{n} not yet: {why}")
            continue
        pr, why = mergeable(repo, n, call, wait)
        if pr is None:
            say(f"#{n} not yet: {why}")
            continue
        if not pr.get("mergeable") or pr["head"]["sha"] != sha:
            say(f"#{n} not yet: GitHub says it does not merge cleanly ({pr.get('mergeable_state')})")
            continue
        status, body = call("PUT", f"/repos/{repo}/pulls/{n}/merge", {"sha": sha, "merge_method": "merge"})
        if status != 200:
            say(f"#{n} was refused ({status}): {body.get('message', '')}")
            continue
        say(f"#{n} merged at {sha[:7]}")
        merged += 1
        close_what_it_names(repo, p, call, say)
        ref = p["head"]["ref"]
        # GitHub aims a stacked pull request at main only when its own
        # delete-on-merge removes the branch; deleting it through the API
        # closes them instead (2 Nov: #96 closed when #95's branch went).
        # So each one stacked on it is aimed at main first, and the branch
        # stays when any of them could not be.
        status, stacked = call("GET", f"/repos/{repo}/pulls?state=open&base={quote(ref, safe='')}&per_page=100")
        if status != 200:
            say(f"  its branch {ref} stays: the pull requests on it did not read ({status})")
            continue
        moved = True
        for sp in stacked:
            st, body = call("PATCH", f"/repos/{repo}/pulls/{sp['number']}", {"base": "main"})
            if st != 200:
                say(f"  #{sp['number']} was not aimed at main ({st}): {body.get('message', '')}")
                moved = False
            else:
                say(f"  #{sp['number']}, stacked on it, now aims at main")
        if not moved:
            say(f"  its branch {ref} stays, so what is stacked on it stays open")
            continue
        status, body = call("DELETE", f"/repos/{repo}/git/refs/heads/{ref}")
        if status not in (204, 422):
            say(f"  its branch {ref} was not deleted ({status}): {body.get('message', '')}")
    return merged


def run(repo, call=api, say=print, wait=time.sleep):
    # A merge aims what was stacked on it at main, so look again until a pass merges nothing.
    for _ in range(10):
        merged = one_pass(repo, call, say, wait)
        if merged is None:
            return 1
        if merged == 0:
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(run(os.environ["GITHUB_REPOSITORY"]))
