"""What scripts/render_profiles.py would write, without writing it."""

from __future__ import annotations

import importlib.util

from helpers import REPO, policy


def rendered() -> dict[str, str]:
    spec = importlib.util.spec_from_file_location("render_profiles", REPO / "scripts" / "render_profiles.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    p = policy()
    return {bot: module.render(p, bot) for bot in p.bots}


def rendered_policy_json() -> str:
    spec = importlib.util.spec_from_file_location("render_profiles", REPO / "scripts" / "render_profiles.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from thornswood_guard.policy import Policy

    return module.policy_json(Policy.load_source())
