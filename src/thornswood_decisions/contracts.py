"""The pinned Hermes contract the decision core is built on.

`contracts.json` holds what was read from the pinned Hermes source (07
section 11.2, step 0). This module loads it and checks it agrees with the
guard rails' own policy: the same six Bots, the same room, ids Hermes accepts.
A disagreement is a stop, never a guess.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

CONTRACT_FILE = Path(__file__).with_name("contracts.json")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class ContractError(Exception):
    """The pinned contract is wrong or disagrees with the policy."""


def load(path: Path | str = CONTRACT_FILE) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def profile_id_ok(contract: dict, profile_id: str) -> bool:
    return re.fullmatch(contract["profiles"]["id_pattern"], profile_id) is not None


def check(contract: dict, policy_data: dict) -> list[str]:
    """Every way the contract and the policy disagree. Empty means they agree."""
    problems: list[str] = []
    h = contract["hermes"]
    if not COMMIT_RE.match(h.get("commit", "")):
        problems.append(f"the pinned Hermes commit is not a full 40-character SHA: {h.get('commit')!r}")
    if h.get("tag") != f"v{h.get('version')}":
        problems.append(f"the tag {h.get('tag')!r} does not name version {h.get('version')!r}")

    ids = contract["profiles"]["ids"]
    bots = list(policy_data["bots"])
    if sorted(ids) != sorted(bots):
        problems.append(f"the contract's profiles {sorted(ids)} are not the policy's Bots {sorted(bots)}")
    if len(set(ids)) != len(ids):
        problems.append("a profile id is listed twice")
    for pid in ids:
        if not profile_id_ok(contract, pid):
            problems.append(f"Hermes would refuse the profile id {pid!r}")

    room = contract["room"]
    in_policy = sorted(b for b, cfg in policy_data["bots"].items() if room["name"] in cfg.get("rooms", []))
    if sorted(room["members"]) != in_policy:
        problems.append(f"the room's members {sorted(room['members'])} are not the Bots the policy seats in "
                        f"{room['name']!r}: {in_policy}")
    return problems
