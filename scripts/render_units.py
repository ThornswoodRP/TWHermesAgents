#!/usr/bin/env python3
"""Write deploy/systemd/*.slice and the hermes user slice drop-in from the
policy's slices. A test fails if they are stale.

    python scripts/render_units.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from thornswood_guard.policy import Policy  # noqa: E402

OUT = REPO / "deploy" / "systemd"
WHAT = {
    "tw-hermes.slice": "the gateway (six profiles and the room) and hermes serve, as hermes",
    "tw-brokers.slice": "tw-policy, tw-files, tw-git and tw-proxy, each as its own user",
    "tw-sandbox.slice": "every Tier B check, as tw-<bot>",
    "hermes-user.slice": "the hermes user's own manager: the Tier A podman containers",
}


def render(name: str, limits: dict) -> str:
    lines = [f"# {WHAT[name]}.",
             "# Generated from thornswood_guard/policy.yaml by scripts/render_units.py; do not edit by hand.",
             "# A limit changes only by a PR to the policy, never on the box."]
    if name == "hermes-user.slice":
        lines.insert(1, "# Installed as /etc/systemd/system/user-<uid of hermes>.slice.d/50-thornswood.conf.")
        lines += ["[Slice]"]
    else:
        lines += ["[Unit]", f"Description=Thornswood: {WHAT[name]}", "Before=slices.target", "", "[Slice]"]
    lines += ["MemoryAccounting=yes", "CPUAccounting=yes", "TasksAccounting=yes"]
    lines += [f"{k}={v}" for k, v in limits.items()]
    return "\n".join(lines) + "\n"


def files(policy: Policy) -> dict[Path, str]:
    out = {}
    for name, limits in policy.data["slices"].items():
        path = OUT / ("user-hermes.slice.conf" if name == "hermes-user.slice" else name)
        out[path] = render(name, limits)
    return out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for path, text in files(Policy.load_source()).items():
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {path.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
