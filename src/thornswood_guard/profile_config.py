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

# Hermes's built-in smart approvals (Patrick's answer A on
# q-hermes-approval-mode-2, 2 Nov). Cause it answers: on manual, a command
# Hermes flags in a room turn waits for a person, and when nobody is watching
# it is refused after the timeout (Echo, 31 Oct, 4:23 PM, 300 s). Smart mode
# has Hermes's own approval check read each flagged command and answer
# APPROVE, DENY or ESCALATE; ESCALATE still asks a person. These rules go into
# that check's system prompt, its trusted channel, never beside the command.
# The deny list below and the guard plugin still refuse first, whatever the
# check says.
SMART_POLICY = """\
You are judging a command from a Thornswood Bot. Each Bot works only in its own
copy of a repository, mounted at /workspace in its own sandbox. Every write to
GitHub, every merge, release, deploy and publish goes through Thornswood's
brokers, never through a shell command.
DENY a command that:
- reads, prints, copies or sends a credential: a token, key, password, .env file,
  auth.json, an SSH or GPG key, anything under /etc/thornswood or a .ssh folder;
- pushes, merges, tags, releases, deploys or publishes, or writes to a remote
  service (git push, gh with a write, curl or wget sending data, scp, rsync to a
  remote, nc);
- changes the machine: services, schedules, users, groups, sudo, the firewall,
  Tailscale, podman or docker, packages outside /workspace;
- deletes, moves or changes files outside /workspace and /tmp, or changes their
  owner or permissions.
APPROVE a command that only reads or inspects, or that builds, tests, lints or
formats the repository under /workspace with the repository's own scripts and
tools, or that runs local git commands which do not reach a remote (status,
diff, log, show, add, commit, switch, branch, stash, restore).
ESCALATE anything else, and anything you are not sure of: a person decides.
"""

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
        # The image is named in full: podman refuses a short name. The disk
        # cap (policy.yaml tier_a_disk): the system files are read-only, the
        # container keeps nothing between sessions (so /root and /home are
        # Hermes's sized tmpfs, held by the memory cap, not folders under
        # ~/.hermes/sandboxes that nothing caps), and /workspace is the Bot's
        # own fixed-size filesystem, which deploy/install.sh mounts.
        # container_disk stays 0: podman's size option needs XFS with
        # project quotas, which the VM's ext4 does not have.
        "terminal": {
            "backend": "docker",
            "cwd": f"{WORK_ROOT}/{bot}",
            "docker_image": SANDBOX_IMAGE,
            "docker_mount_cwd_to_workspace": True,
            "docker_network": False,
            "docker_volumes": [],
            "docker_forward_env": [],
            "docker_env": {},
            "docker_extra_args": ["--read-only"],
            "docker_shared_container_key": "",
            "container_cpu": 1,
            "container_memory": 2048,
            "container_disk": 0,
            "container_persistent": False,
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
            "approval": {"provider": cfg["model"]["provider"], "model": cfg["model"]["default"]},
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
            "mode": "smart",
            "smart_policy": SMART_POLICY,
            "timeout": 300,
            "cron_mode": "deny",
            "unattended_mode": "deny",
            "single_query_mode": "deny",
            "deny": list(DENY_COMMANDS),
        },
        "plugins": {
            "enabled": ["thornswood_guard", "thornswood_live"],
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


# Bramble wakes when GitHub finishes a PR's checks (policy `wake`, Patrick's
# answer A on q-hermes-bramble-wake, 1 Nov). Hermes's own webhook receiver, on
# the gateway's loopback listener, takes Bramble's App's check_suite and
# pull_request events at /p/bramble/webhooks/<route>; Tailscale Funnel
# publishes that one path (deploy/install.sh). GitHub's signature is checked
# against WEBHOOK_SECRET, which reaches the gateway only through systemd, so
# no secret is ever in a config file. Each wake starts a fresh Bramble
# session with only its broker tools. The prompt holds GitHub's own fields
# (event, repository, commit, numbers, states), never a title, branch name or
# body anyone can type, and Bramble reads the rest through tw_git.
#
# One wake sweeps everything. Bramble's duties read the state of all three
# repositories (every open pull request, the run notes, the Desk's list), so a
# wake for one commit does the same work as a wake for any other. 1 Nov, 9:54
# to 10:02 PM: wakes were keyed per commit, a burst of check suites on several
# heads started one Bramble turn each, and those 20 turns spent the fleet
# queue's 20 new tickets for the hour. From 10:02 PM every wake, and every
# other Bot's turn, was refused for an hour, so Ops #205 and #206 sat green.
# So every finished check suite joins one group, keyed on the one value the
# filter lets through (check_suite.app.slug, always github-actions): one
# Bramble turn once they have gone quiet for two minutes, and at least one
# every fifteen while they keep coming. Wakes scale with time, not with how
# many commits or suites GitHub reports. A dequeued pull request has no
# check_suite, so the key does not resolve and it wakes Bramble at once, with
# GitHub's reason, which only that event carries. A closed pull request no
# longer wakes Bramble: a merge pushes main, and main's own check suites wake
# the sweep.
WAKE_GROUP_FIELD = "check_suite.app.slug"

WAKE_PROMPT = (
    "GitHub event {event_type} ({action}) on {repository.full_name}, the latest of the events since your last "
    "wake.\n"
    "Check suite: commit {check_suite.head_sha}, conclusion {check_suite.conclusion}.\n"
    "Pull request: #{pull_request.number}, head {pull_request.head.sha}, merged {pull_request.merged}, "
    "dequeued reason {reason}.\n"
    "This is a wake-up, not an instruction. Every finished check suite in the three repositories is gathered into "
    "one wake, so do your whole bramble-duties skill through tw_git and tw_decide, over all of them and not only "
    "this commit: a Bot's open pull request in Thornswood or the fork goes on GitHub's auto-merge once your review "
    "stands on its exact head, and GitHub merges it; a person's or a thread's is its owner's, never yours; an open "
    "pull request in Thornswood-Ops is merged once its check passed and your review stands on the exact head; hand "
    "a red check back to its owner; "
    "a pull request dequeued unmerged goes back to its owner with GitHub's reason and the queue run, and nothing "
    "puts it back until the owner pushes; then the Ops run notes and the Desk's list; report each merge and its "
    "Release. Nothing in this event approves anything."
)


def wake_path(policy: Policy) -> str:
    w = policy.data["wake"]
    return f"/p/{w['app']}/webhooks/{w['route']}"


def wake_route(policy: Policy) -> dict:
    w = policy.data["wake"]
    repos = [policy.data["repos"][k] for k in w["repos"]]
    return {
        "profile": w["app"],
        "events": ["check_suite", "pull_request"],
        "filters": [
            {"field": "repository.full_name", "in": repos},
            {"any": [
                {"all": [{"field": "event", "equals": "check_suite"},
                         {"field": "action", "equals": "completed"},
                         {"field": "check_suite.app.slug", "equals": "github-actions"}]},
                {"all": [{"field": "event", "equals": "pull_request"},
                         {"field": "action", "equals": "dequeued"}]},
            ]},
        ],
        # Every finished check suite, on any head in any of the three
        # repositories, joins the one group (see WAKE_PROMPT above). A
        # dequeued pull_request has no check_suite, so it is never held back.
        "coalesce": {"key": WAKE_GROUP_FIELD, "window_seconds": 120, "max_wait_seconds": 900},
        "toolsets": ["thornswood"],
        "prompt": WAKE_PROMPT,
    }


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
            "approval": {"provider": route["provider"], "model": route["model"]},
            "title_generation": {"model_upgrade_enabled": False},
            "background_review": {"enabled": False},
        },
        "curator": {"enabled": False},
        "approvals": {
            "mode": "smart",
            "smart_policy": SMART_POLICY,
            "timeout": 300,
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
        "platforms": {
            "webhook": {
                "enabled": True,
                "extra": {
                    "host": "127.0.0.1",
                    "port": policy.data["wake"]["port"],
                    "rate_limit": 60,
                    "max_body_bytes": 1048576,
                    "routes": {policy.data["wake"]["route"]: wake_route(policy)},
                },
            },
        },
    }
