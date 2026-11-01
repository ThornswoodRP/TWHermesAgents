"""Each Bot's Hermes config, built from the policy.

scripts/apply.py writes these keys into the existing profile's config.yaml
and leaves every other key as Hermes has it. `model` and `fallback_providers`
are replaced whole, so nothing of the old route (a base_url, a paid provider)
survives. profiles/<bot>.yaml in this repo is this output, committed so a
reviewer can read it; a test fails if the two ever differ.
"""

from __future__ import annotations

from .policy import Policy
from .routes import config_for, gateway_chain

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

# Hermes bounds each plugin hook by plugins.hook_callback_timeout (default 30,
# at most 600) and skips a pre_llm_call that runs past it. Every turn waits
# in that hook for its ticket (brokers.TURN_WAIT, 540 seconds), so
# the bound is Hermes's maximum. Every other hook here answers in well under
# a second; a pre_tool_call past the bound still fails closed.
HOOK_TIMEOUT = 600

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
        # The helper models. Text helpers ("auto") already take the Bot's own
        # main route, and Hermes refuses to guess another login once a main
        # provider is set. Vision is the one helper whose "auto" walks a fixed
        # list after the main route (OpenRouter, the Nous Portal, DeepInfra),
        # which is where the gateway's "Nous Portal not configured" lines came
        # from (31 Oct). It is pinned to the Bot's own first route, so a helper
        # call never looks past the Bot's routes. It names no endpoint or key
        # of its own (check_config refuses one), so it signs in the way the
        # main route does.
        "auxiliary": {
            "free_only": True,
            "openrouter_model": "",
            "vision": {"provider": cfg["model"]["provider"], "model": cfg["model"]["default"]},
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
            "hook_callback_timeout": HOOK_TIMEOUT,
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


GATEWAY_PROFILE = "default"

# Toolsets whose availability check makes a helper call. The gateway warms the
# tool list of its own profile when it starts, and the image and video tools'
# check asks the vision helper for a client, then for "auto", which walks the
# Nous Portal. The gateway's own profile holds no sign-in (it is not a Bot),
# so the pinned helper finds none and the walk goes on (31 Oct, 3:04 PM: the
# Nous count fell from 5 to 3). The guard already refuses every tool there;
# these toolsets are off so nothing asks for a helper the profile cannot have.
GATEWAY_HELPER_TOOLSETS = ["vision", "video"]


def build_gateway(policy: Policy) -> dict:
    """The gateway's own profile: /home/hermes/.hermes/config.yaml, which
    tw-hermes-gateway runs as. It is not a Bot, but it holds the Discord
    connection and starts every helper the gateway makes before a turn reaches
    a Bot, so it lives under the same rules (31 Oct: with no route and no
    helper of its own, its vision helper walked to the Nous Portal). One route,
    the Max login every Bot has first, with no fallback, and no sign-in for
    it: with a main provider set, Hermes refuses to guess another login for a
    text helper. The vision helper pinned to that route; the toolsets whose
    check asks for a helper turned off; nothing that bills; and the guard,
    which refuses every tool in a profile that is not one of the six Bots."""
    route = gateway_chain(policy)[0]
    never = list(policy.data["toolsets"]["never"]) + list(policy.data["toolsets"]["workers_never"])
    never += [t for t in GATEWAY_HELPER_TOOLSETS if t not in never]
    return {
        "model": {"provider": route["provider"], "default": route["model"]},
        "fallback_providers": [],
        "agent": {"service_tier": "", "disabled_toolsets": never},
        "auxiliary": {
            "free_only": True,
            "openrouter_model": "",
            "vision": {"provider": route["provider"], "model": route["model"]},
            "title_generation": {"model_upgrade_enabled": False},
            "background_review": {"enabled": False},
        },
        "curator": {"enabled": False},
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
            "hook_callback_timeout": HOOK_TIMEOUT,
        },
        "hooks_auto_accept": False,
        "security": {"redact_secrets": True},
    }
