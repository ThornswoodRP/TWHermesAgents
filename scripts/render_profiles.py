#!/usr/bin/env python3
"""Write thornswood_guard/policy.json from policy.yaml, profiles/<bot>.yaml:
each Bot's config as apply.py will set it, and profiles/gateway.yaml: the
gateway's own profile.

Run it after any change to policy.yaml. The guard reads policy.json (Hermes's
Python has no PyYAML); the profile files are for reading in review. A test
fails if either is stale.

    python scripts/render_profiles.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import json  # noqa: E402

import yaml  # noqa: E402

from thornswood_guard.policy import POLICY_FILE, Policy  # noqa: E402
from thornswood_guard.profile_config import build, build_gateway  # noqa: E402

HEADER = """# {name}'s Hermes config, as scripts/apply.py sets it in the existing
# profile /home/hermes/.hermes/profiles/{bot}/config.yaml. Generated from
# thornswood_guard/policy.yaml by scripts/render_profiles.py; do not edit by hand.
# Every key not listed here stays as Hermes has it. `model` and
# `fallback_providers` are replaced whole.
#
# Rooms: {rooms}.
# Skills: thornswood-ops and thornswood-guide, already in the profile;
# apply.py checks they are there.
"""


def render(policy: Policy, bot: str) -> str:
    rooms = ", ".join(policy.bots[bot].get("rooms", []))
    if policy.bots[bot].get("opens_working_rooms"):
        rooms += ", and the working rooms it opens"
    body = yaml.safe_dump(build(policy, bot), sort_keys=False, allow_unicode=True)
    return HEADER.format(name=policy.bot_name(bot), bot=bot, rooms=rooms) + body


GATEWAY_HEADER = """# The gateway's own profile, as scripts/apply.py sets it in
# /home/hermes/.hermes/config.yaml: the hermes user's default profile, which
# tw-hermes-gateway runs as. It is not a Bot. It holds the Discord connection
# and makes the gateway's own helper calls, so it gets one route (the Max
# login every Bot has first, no fallback), the helpers on it, and the guard,
# which refuses every tool in a profile that is not one of the six Bots.
# Generated from thornswood_guard/policy.yaml by scripts/render_profiles.py;
# do not edit by hand. Every key not listed here stays as Hermes has it.
# `model` and `fallback_providers` are replaced whole.
"""


def render_gateway(policy: Policy) -> str:
    return GATEWAY_HEADER + yaml.safe_dump(build_gateway(policy), sort_keys=False, allow_unicode=True)


def policy_json(policy: Policy) -> str:
    return json.dumps(policy.data, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    policy = Policy.load_source()
    POLICY_FILE.write_text(policy_json(policy), encoding="utf-8", newline="\n")
    print("wrote thornswood_guard/policy.json")
    out = REPO / "profiles"
    out.mkdir(exist_ok=True)
    for bot in policy.bots:
        (out / f"{bot}.yaml").write_text(render(policy, bot), encoding="utf-8", newline="\n")
        print(f"wrote profiles/{bot}.yaml")
    (out / "gateway.yaml").write_text(render_gateway(policy), encoding="utf-8", newline="\n")
    print("wrote profiles/gateway.yaml")
    return 0


if __name__ == "__main__":
    sys.exit(main())
