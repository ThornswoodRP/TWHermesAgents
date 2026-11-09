"""Load the guard rails' policy and answer simple questions about it."""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from pathlib import Path

# policy.yaml is the source people read and edit. policy.json is the same
# data, written by scripts/render_profiles.py, so the guard needs nothing but
# the standard library inside Hermes (whose Python has no PyYAML). A test
# fails if the two differ.
POLICY_SOURCE = Path(__file__).with_name("policy.yaml")
POLICY_FILE = Path(__file__).with_name("policy.json")


# Who writes to GitHub: Patrick's Hermes until the cutover, then Bramble.
EXECUTORS = ("patrick-hermes", "bramble")


# A GitHub noreply address, with or without the account id in front:
# 187923600+evilpatrick06@users.noreply.github.com.
NOREPLY = re.compile(r"(?:\d+\+)?([a-z0-9][a-z0-9-]*(?:\[bot\])?)@users\.noreply\.github\.com")


class PolicyError(Exception):
    """The policy file itself is wrong. Nothing is allowed until it is fixed."""


@dataclass(frozen=True)
class Verdict:
    """The answer to one request. A refusal always says why."""

    allowed: bool
    reason: str

    @staticmethod
    def yes(reason: str = "allowed") -> "Verdict":
        return Verdict(True, reason)

    @staticmethod
    def no(reason: str) -> "Verdict":
        return Verdict(False, reason)


class Policy:
    def __init__(self, data: dict):
        self.data = data
        self._check()

    @classmethod
    def load(cls, path: Path | str = POLICY_FILE) -> "Policy":
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f))

    @classmethod
    def load_source(cls, path: Path | str = POLICY_SOURCE) -> "Policy":
        import yaml

        with open(path, encoding="utf-8") as f:
            return cls(yaml.safe_load(f))

    def _check(self) -> None:
        bots = self.data.get("bots") or {}
        if len(bots) != 6:
            raise PolicyError(f"the policy names {len(bots)} Bots; there are exactly six")
        routers = [b for b, v in bots.items() if v.get("role") == "router"]
        if routers != ["bramble"]:
            raise PolicyError("Bramble must be the one and only router")
        for bot, v in bots.items():
            if v.get("claim_label") != f"claimed: {v.get('name')}":
                raise PolicyError(f"{bot}'s claim label must be 'claimed: {v.get('name')}'")
        known = set(self.data["repos"].values())
        for wf, spec in self.data["runs"]["workflows"].items():
            if (spec or {}).get("repo", self.data["runs"]["repo"]) not in known:
                raise PolicyError(f"runs.workflows.{wf}.repo must be one of the repos, not {spec.get('repo')!r}")
            named = (spec or {}).get("inputs") or {}
            for name, pattern in named.items():
                try:
                    re.compile(pattern)
                except (re.error, TypeError) as e:
                    raise PolicyError(f"runs.workflows.{wf}.inputs.{name} is not a pattern: {e}") from None
            if set((spec or {}).get("required") or []) - set(named):
                raise PolicyError(f"runs.workflows.{wf}.required names an input it does not take")
        if self.data.get("executor") not in EXECUTORS:
            raise PolicyError(f"executor must be one of {', '.join(EXECUTORS)}, not {self.data.get('executor')!r}")
        checks = self.data.get("merge", {}).get("required_checks") or {}
        for repo in self.data.get("merge", {}).get("base", {}):
            names = checks.get(repo)
            if (not isinstance(names, list) or not names
                    or not all(isinstance(n, str) and n for n in names)
                    or len(set(names)) != len(names)):
                raise PolicyError(f"the merge rule names no required checks for {repo}")
        apps = self.data.get("github_apps") or {}
        for app in ("bramble", "workers"):
            reach = (apps.get(app) or {}).get("repos")
            if not isinstance(reach, list) or not set(reach) <= set(self.data.get("merge", {}).get("base", {})):
                raise PolicyError(f"github_apps.{app}.repos lists the repos the App is installed on, each one the merge rule names")
        if set((apps.get("bramble") or {}).get("repos") or []) != set(self.data.get("merge", {}).get("base", {})):
            raise PolicyError("Bramble's App is installed on every repo the Bots work in")
        auto = self.data.get("merge", {}).get("auto_merge")
        if (not isinstance(auto, dict) or set(auto) != set(self.data.get("merge", {}).get("base", {}))
                or not all(isinstance(v, list) and all(isinstance(n, str) and n for n in v) for v in auto.values())):
            raise PolicyError("merge.auto_merge names, for each repo the merge rule names, the checks GitHub's "
                              "branch rules must require (empty where GitHub's auto-merge does not merge)")

    # Bots

    @property
    def bots(self) -> dict:
        return self.data["bots"]

    def is_bot(self, bot: str) -> bool:
        return bot in self.bots

    def bot_name(self, bot: str) -> str:
        return self.bots[bot]["name"]

    def is_router(self, bot: str) -> bool:
        return self.bots.get(bot, {}).get("role") == "router"

    def app_of(self, bot: str) -> str:
        return self.bots[bot]["app"]

    def app_reaches(self, app: str, repo: str) -> bool:
        """Whether the App is installed on the repo (github_apps.<app>.repos)."""
        return repo in self.data["github_apps"][app]["repos"]

    def reader_for(self, repo: str) -> str:
        """The App that reads a repo for any Bot: the workers' where it is
        installed, Bramble's where only Bramble's is (Ops)."""
        return "workers" if self.app_reaches("workers", repo) else "bramble"

    def claim_labels(self) -> dict[str, str]:
        return {v["claim_label"]: bot for bot, v in self.bots.items()}

    # Branches

    def own_branch(self, bot: str, branch: str) -> bool:
        pattern = self.data["branches"]["pattern"].replace("{bot}", re.escape(bot))
        return re.fullmatch(pattern, branch) is not None

    def branch_owner(self, branch: str) -> str | None:
        for bot in self.bots:
            if self.own_branch(bot, branch):
                return bot
        return None

    def lane_branch(self, branch: str) -> bool:
        return any(branch.startswith(p) for p in self.data["hands_off"]["lanes"]["branch_prefixes"])

    def required_checks(self, repo: str) -> list[str]:
        return list(self.data["merge"]["required_checks"][repo])

    def auto_merge(self, repo: str) -> bool:
        """Whether GitHub's auto-merge, not Bramble, merges in this repo."""
        return bool(self.data["merge"]["auto_merge"].get(repo))

    def robot_author(self, login: str) -> bool:
        """A robot's pull request (Dependabot, the pin robot): no person or
        thread owns it, so Bramble puts it on auto-merge like a Bot's."""
        return login in (self.data["merge"].get("robot_authors") or [])

    def _trusted_logins(self) -> set[str]:
        m = self.data["merge"]
        names = (m.get("auto_merge_people") or []) + (m.get("robot_authors") or []) + (m.get("workflow_authors") or [])
        return {n.lower() for n in names}

    def trusted_login(self, login: str | None) -> bool:
        """One of Patrick's list of people whose work may merge, a robot or
        one of our own workflows (merge.auto_merge_people, robot_authors,
        workflow_authors). GitHub logins ignore case."""
        return bool(login) and login.lower() in self._trusted_logins()

    def trusted_credit(self, login: str | None, email: str) -> bool:
        """Whether a commit's author or a Co-authored-by line names someone
        whose work may merge: by the GitHub account the commit is linked to,
        a Bot's own commit address, or a GitHub noreply address of a trusted
        login. Anything else is outside work (Patrick, 8 Nov 2026)."""
        if self.trusted_login(login):
            return True
        email = (email or "").strip().lower()
        pattern = self.data["brokers"]["files"]["commit_email"]
        if email in {pattern.format(bot=b).lower() for b in self.bots}:
            return True
        m = NOREPLY.fullmatch(email)
        return bool(m) and self.trusted_login(m.group(1))

    @staticmethod
    def credit_login(login: str | None, email: str) -> str | None:
        """The GitHub login a credit names: the account GitHub links the
        commit to, or the login in a GitHub noreply address."""
        if login:
            return login.lower()
        m = NOREPLY.fullmatch((email or "").strip().lower())
        return m.group(1) if m else None

    def rule_checks(self, repo: str) -> list[str]:
        """The checks GitHub's rules on the base branch must require before
        auto-merge is turned on there."""
        return list(self.data["merge"]["auto_merge"].get(repo) or [])


def name_matches(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name.upper(), p.upper()) for p in patterns)
