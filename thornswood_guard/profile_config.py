"""Each Bot's Hermes config, built from the policy.

scripts/apply.py writes these keys into the existing profile's config.yaml
and leaves every other key as Hermes has it. `model` and `fallback_providers`
are replaced whole, so nothing of the old route (a base_url, a paid provider)
survives. profiles/<bot>.yaml in this repo is this output, committed so a
reviewer can read it; a test fails if the two ever differ.
"""

from __future__ import annotations

from .policy import Policy
from .routes import config_for

REPLACED_WHOLE = ("model", "fallback_providers")

# Terminal commands Hermes itself refuses, even under /yolo. A second lock
# beside the guard plugin, enforced by Hermes's own approval code.
DENY_COMMANDS = [
    "git push*",
    "*git push *",
    "gh pr merge*",
    "*gh pr merge*",
    "gh pr create*",
    "gh workflow run*",
    "*gh workflow run*",
    "gh api * -X *",
    "gh api * --method *",
    "gh label *",
    "gh issue edit*",
    "gh secret*",
    "gh variable*",
    "gh ruleset*",
    "gh auth login*",
    "gh auth token*",
    "*cat *auth.json*",
    "*printenv*",
    "*/proc/*/environ*",
    "sudo *",
]

WORK_ROOT = "/home/hermes/tw-work"

# Hermes's own sandbox image (amd64 and arm64), named in full for podman.
SANDBOX_IMAGE = "docker.io/nousresearch/hermes-sandbox:desktop"


def build(policy: Policy, bot: str) -> dict:
    never = list(policy.data["toolsets"]["never"])
    if not policy.is_router(bot):
        never += policy.data["toolsets"]["workers_never"]
    cfg = config_for(policy, bot)
    cfg.update({
        "timezone": "America/Denver",
        "agent": {
            "reasoning_effort": "medium",
            "service_tier": "",
            "disabled_toolsets": never,
        },
        # Tier A: every terminal, file and code call runs in the Bot's own
        # rootless podman container (Hermes's docker backend finds podman on
        # PATH). Only the Bot's working folder is mounted, at /workspace; no
        # network; one core and 2 GB, which Hermes applies when the cpu,
        # memory and pids controllers are delegated (they are on the VM).
        # The image is named in full: podman refuses a short name. No disk
        # cap here: the VM's ext4 store can't enforce one (Hermes needs XFS
        # with pquota), so the disk cap comes with the per-Bot users' quotas.
        "terminal": {
            "backend": "docker",
            "cwd": f"{WORK_ROOT}/{bot}",
            "docker_image": SANDBOX_IMAGE,
            "docker_mount_cwd_to_workspace": True,
            "docker_network": False,
            "docker_volumes": [],
            "docker_forward_env": [],
            "docker_env": {},
            "docker_extra_args": [],
            "docker_shared_container_key": "",
            "container_cpu": 1,
            "container_memory": 2048,
            "container_disk": 0,
            "container_persistent": True,
        },
        "auxiliary": {
            "free_only": True,
            "openrouter_model": "",
            "title_generation": {"model_upgrade_enabled": False},
            "background_review": {"enabled": False},
        },
        "curator": {"enabled": False},
        "skills": {
            "write_approval": True,
            "guard_agent_created": True,
            "inline_shell": False,
            "project_discovery": False,
        },
        "approvals": {
            "mode": "manual",
            "cron_mode": "deny",
            "unattended_mode": "deny",
            "single_query_mode": "deny",
            "deny": list(DENY_COMMANDS),
        },
        "plugins": {
            "enabled": ["thornswood_guard"],
            "auto_apply": False,
            "auto_update_check_hours": 0,
            "hook_callback_timeout": 30,
        },
        "hooks_auto_accept": False,
        "security": {"redact_secrets": True},
    })
    return cfg


def merge_into(existing: dict, ours: dict) -> dict:
    """Our keys over Hermes's config: leaf by leaf, except the keys replaced whole."""
    out = dict(existing)
    for key, value in ours.items():
        if key in REPLACED_WHOLE or not isinstance(value, dict) or not isinstance(out.get(key), dict):
            out[key] = value
        else:
            out[key] = merge_into(out[key], value)
    return out
