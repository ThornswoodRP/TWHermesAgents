"""What may sit on the Oracle box, checked by name. No value is ever read.

The one named exception is the live Discord bot's token, only in the
gateway's own .env (Patrick, 30 Oct). Never on Oracle: a box key, a bridge
token, a Nexus key, signing material, a GitHub token in a Bot's reach, or a
key for a provider that bills by use.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from .policy import Policy, name_matches

ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def env_names(path: Path) -> set[str]:
    """The variable names an env file sets. Reads up to the first '=' only."""
    names = set()
    if not path.exists():
        return names
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = ENV_LINE.match(line)
            if m:
                names.add(m.group(1))
    return names


def check_env(policy: Policy, names: set[str], where: str, *, later_allowed: bool = False) -> list[str]:
    """`where` is "root" for the gateway's own .env, or a profile name."""
    s = policy.data["secrets"]
    allowed = {e["name"]: e["where"] for e in s["oracle_allowed"]}
    if later_allowed:
        allowed.update({e["name"]: e["where"] for e in s["oracle_later"]})
    settings = set(s["oracle_settings"])
    problems = []
    for name in sorted(names):
        if name in settings:
            continue
        if name_matches(name, s["forbidden_names"]):
            problems.append(f"{name} in the {where} .env is never allowed on the Oracle box")
        elif name in allowed:
            if allowed[name] != where:
                problems.append(f"{name} may sit only in the gateway's own .env, not in the {where} .env")
        elif any(name == e["name"] for e in s["oracle_later"]):
            problems.append(f"{name} is set before its route exists; no real tokens go on the VM yet")
        elif secret_shaped(policy, name):
            problems.append(f"{name} in the {where} .env looks like a secret and is not on the list of what may sit on the Oracle box")
    return problems


def secret_shaped(policy: Policy, name: str) -> bool:
    parts = set(name.upper().split("_"))
    return bool(parts & set(policy.data["secrets"]["secret_name_parts"]))


def check_files(policy: Policy, root: Path, skip: set[str] = frozenset()) -> list[str]:
    """Files that must never be on the box, found by name under `root`,
    not going into folders named in `skip`."""
    patterns = policy.data["secrets"]["forbidden_files"]
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for name in filenames:
            if any(fnmatch.fnmatchcase(name, p) for p in patterns):
                found.append(f"{Path(dirpath) / name} is never allowed on the Oracle box")
    return found
