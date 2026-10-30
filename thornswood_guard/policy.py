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
        checks = self.data.get("merge", {}).get("required_check") or {}
        for repo in self.data.get("merge", {}).get("base", {}):
            if not isinstance(checks.get(repo), str) or not checks[repo]:
                raise PolicyError(f"the merge rule names no required check for {repo}")

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

    # Paths

    def patrick_path(self, path: str, verb_scripts: set[str] = frozenset()) -> bool:
        path = path.lstrip("/")
        if path in verb_scripts:
            return True
        return any(glob_match(path, pat) for pat in self.data["merge"]["patrick_paths"])

    def required_check(self, repo: str) -> str:
        return self.data["merge"]["required_check"][repo]


def glob_match(path: str, pattern: str) -> bool:
    """Match a repo path against a pattern where ** crosses folders and * does not."""
    regex = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            regex += ".*"
            i += 2
        elif pattern[i] == "*":
            regex += "[^/]*"
            i += 1
        else:
            regex += re.escape(pattern[i])
            i += 1
    return re.fullmatch(regex, path) is not None


def name_matches(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name.upper(), p.upper()) for p in patterns)


def verb_scripts(verb_lists: list[dict]) -> set[str]:
    """Every script a gate or Nexus verb list names, read from main's copy."""
    scripts = set()
    for listing in verb_lists:
        for verb in (listing.get("verbs") or {}).values():
            if verb.get("script"):
                scripts.add(verb["script"].lstrip("/"))
    return scripts
