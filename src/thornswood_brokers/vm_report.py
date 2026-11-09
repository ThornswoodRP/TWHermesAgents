"""The VM's install results, written to GitHub by tw-git (ThornswoodRP/Thornswood#5564).

The follower (follow_main.py) installs each new main by itself, as root. Until
8 Nov 2026 what it did was only in /var/lib/thornswood/follow-main.json, root's
alone, so an install that stopped went unseen from 4 Nov to 8 Nov. The VM's
own deploy key only reads, and no App is on this repository, so the follower
hands each result to tw-git, the one GitHub writer here, over its socket (op
vm_install, which only root may ask for), and tw-git writes it as the
workers' App (policy follow_main.report):

  - one comment per install on the ledger issue in the ops repository: the
    commit, the result, the step it stopped at, the reason, what was put back;
  - on the board, one open issue while the VM is not installing main: opened
    by the first stop, a comment for each stop after it, and closed by the next
    good install.

Only those fields go out, each held to its form, and the reason is one line
with any address taken out: every step's output stays in the VM's own log.
Every write is found before it is made (a marker in its text), so a report the
follower sends again after a lost answer writes nothing twice.
"""

from __future__ import annotations

import re
import urllib.parse

from .github_api import GitHub, GitHubError
from .wire import Refused

SHA = re.compile(r"^[0-9a-f]{40}$")
RESULTS = {
    "installed": "installed",
    "not installed": "not installed: the tests or the release stopped it before anything changed",
    "failed": "failed after it was installed",
    "fetch failed": "could not fetch main",
    "fetched": "fetches main again",
}
STOPS = ("not installed", "failed", "fetch failed")
STEPS = ("fetch", "release", "tests", "install", "apply", "reload", "canary", "put back")
# An IPv4 address, or an IPv6 one (a "::" in it, or four or more groups), so a
# time of day such as 12:55:00 is left alone.
ADDRESS = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"
                     r"|(?<![\w:])(?=[0-9a-f:]*::|(?:[0-9a-f]{1,4}:){4})[0-9a-f:]{2,39}(?![\w:])", re.IGNORECASE)
BOARD_MARK = "<!-- tw:vm-install-stop -->"
ISSUES = {"issues": "write", "metadata": "read"}


def one_line(text, cap: int = 300) -> str:
    """A reason as it may go out: one line, no address, at most `cap` characters."""
    s = " ".join(str(text or "").split())
    s = ADDRESS.sub("<address>", s)
    return s if len(s) <= cap else s[: cap - 3] + "..."


class VmReport:
    def __init__(self, policy, github: GitHub):
        self.policy = policy
        self.gh = github
        r = policy.data["follow_main"]["report"]
        repos = policy.data["repos"]
        self.app = r["app"]
        self.login = r["app_login"]
        self.ledger_repo = repos[r["ledger_repo"]]
        self.ledger_issue = int(r["ledger_issue"])
        self.board_repo = repos[r["board_repo"]]

    # -- what the follower may say
    @staticmethod
    def _commit(v, name: str, *, required: bool = False) -> str | None:
        if v in (None, ""):
            if required:
                raise Refused(f"{name} is a full commit")
            return None
        if not isinstance(v, str) or not SHA.match(v):
            raise Refused(f"{name} is a full commit, 40 hex characters")
        return v

    def _read(self, args: dict) -> dict:
        result = args.get("result")
        if result not in RESULTS:
            raise Refused(f"result is one of {', '.join(RESULTS)}")
        step = args.get("step")
        if step not in (None, *STEPS):
            raise Refused(f"step is one of {', '.join(STEPS)}")
        at = str(args.get("at") or "")
        if not re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", at):
            raise Refused("at is a UTC time, YYYY-MM-DDTHH:MM:SSZ")
        out = {
            "result": result,
            "commit": self._commit(args.get("commit"), "commit", required=result not in ("fetch failed", "fetched")),
            "installed": self._commit(args.get("installed"), "installed"),
            "before": self._commit(args.get("before"), "before"),
            "step": step,
            "reason": one_line(args.get("reason")),
            "put_back": one_line(args.get("put_back"), 200),
            "at": at,
        }
        if result in STOPS and not out["reason"]:
            raise Refused("a stop says why")
        return out

    # -- the words
    @staticmethod
    def _short(c: str | None) -> str:
        return c[:12] if c else "nothing"

    def _line(self, r: dict) -> str:
        what = f"`{self._short(r['commit'])}`" if r["commit"] else "main"
        line = f"{r['at']}: {what} {RESULTS[r['result']]}"
        if r["step"]:
            line += f", at the {r['step']} step"
        line += "."
        if r["reason"]:
            line += f" {r['reason']}."
        if r["put_back"]:
            line += f" {r['put_back'][0].upper()}{r['put_back'][1:]}."
        line += f" The VM runs `{self._short(r['installed'])}`."
        return line

    def _marker(self, r: dict) -> str:
        return f"<!-- tw:vm-install:{r['commit'] or 'main'}:{r['result'].replace(' ', '-')}:{r['at']} -->"

    # -- the writes, each found first
    def _comment(self, repo: str, number: int, body: str, marker: str) -> int:
        for c in self.gh.get_all(self.app, repo, f"/repos/{repo}/issues/{number}/comments"):
            if marker in (c.get("body") or ""):
                return int(c["id"])
        r = self.gh.call(self.app, repo, ISSUES, "POST", f"/repos/{repo}/issues/{number}/comments",
                         {"body": f"{body}\n\n{marker}"})
        if r.status != 201:
            raise GitHubError(r.status, str(r.body))
        return int(r.body["id"])

    def _open_stop(self) -> int | None:
        q = urllib.parse.quote(self.login, safe="")
        for i in self.gh.get_all(self.app, self.board_repo, f"/repos/{self.board_repo}/issues?state=open&creator={q}"):
            if not i.get("pull_request") and BOARD_MARK in (i.get("body") or "") and (i.get("user") or {}).get("login") == self.login:
                return int(i["number"])
        return None

    def install(self, caller: str, args: dict) -> dict:
        r = self._read(args)
        line = self._line(r)
        marker = self._marker(r)
        out = {"ledger": self._comment(self.ledger_repo, self.ledger_issue, line, marker), "board": None}
        board = self._open_stop()
        if r["result"] in STOPS:
            if board is None:
                body = (f"The Hermes VM's follower is not installing main of TWHermesAgents. {line}\n"
                        f"Every step's output is on the VM, in its follow-main log for that commit.\n"
                        f"Done when: the VM installs main again; its follower closes this issue then.\n\n"
                        f"{BOARD_MARK}\n{marker}")
                made = self.gh.call(self.app, self.board_repo, ISSUES, "POST", f"/repos/{self.board_repo}/issues",
                                    {"title": "The Hermes VM stopped installing TWHermesAgents main", "body": body})
                if made.status != 201:
                    raise GitHubError(made.status, str(made.body))
                out["board"] = int(made.body["number"])
            else:
                # The issue's own text may already carry this stop (a lost answer).
                issue = self.gh.get(self.app, self.board_repo, f"/repos/{self.board_repo}/issues/{board}")
                if marker not in (issue.get("body") or ""):
                    self._comment(self.board_repo, board, line, marker)
                out["board"] = board
        elif board is not None:
            self._comment(self.board_repo, board, line + " So the VM runs main again.", marker)
            shut = self.gh.call(self.app, self.board_repo, ISSUES, "PATCH", f"/repos/{self.board_repo}/issues/{board}",
                                {"state": "closed", "state_reason": "completed"})
            if shut.status != 200:
                raise GitHubError(shut.status, str(shut.body))
            out["closed"] = board
        return out
