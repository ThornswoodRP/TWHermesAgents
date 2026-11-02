"""Thornswood guard: the Bots' guard rails, as a Hermes plugin.

Installed into each of the six profiles (plugins/thornswood_guard) by
scripts/apply.py and enabled in each profile's config. It registers one
pre_tool_call hook that refuses what a Bot may not do, and two system prompt
sections: rule zero (rule_zero.md, Patrick's "Fix the cause, no band-aids,
ever", word for word) and a short one that tells the Bot a refusal is a stop.
The sections are frozen into every new session's prompt, so a reload or a
hand edit of SOUL.md can never drop rule zero: only a PR here can change it.

If the policy cannot be read, every tool call is refused: a guard that cannot
read its rules fails closed.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

PLUGIN_NAME = "thornswood_guard"

RULE_ZERO_FILE = Path(__file__).with_name("rule_zero.md")

# Hermes caps a plugin's prompt section at 4,000 characters.
SECTION_MAX = 4000

ROUTER_RULE_ZERO = ("As Bramble you also reject any worker PR, brief or card that is a band-aid under rule zero, "
                    "or that does not state its cause in one sentence: send it back to its owner naming the cause "
                    "it skipped, and never merge it, file it or offer it to Patrick as an option.")


def rule_zero_section(policy, bot: str) -> str:
    """Rule zero, word for word, for every profile the guard runs in; Bramble's
    line on rejecting band-aids is added in Bramble's profile."""
    text = RULE_ZERO_FILE.read_text(encoding="utf-8").strip()
    if policy.is_bot(bot) and policy.is_router(bot):
        text += "\n" + ROUTER_RULE_ZERO
    return text


def _prompt_section(policy, bot: str) -> str:
    if not policy.is_bot(bot):
        return ""
    name = policy.bot_name(bot)
    lines = [
        f"You are {name}. The Thornswood guard runs before every tool you call, in this profile.",
        "When it refuses a call, that is a stop: say what was refused and why, file what is missing for its owner, and never route around it or ask another Bot to do it.",
        "No Bot holds a GitHub credential. GitHub writes go through the git broker, which checks the guard rails again.",
    ]
    if policy.is_router(bot):
        names = " and ".join(f"'{n}'" for n in policy.required_checks(policy.data["repos"]["app"]))
        lines.append(f"In Thornswood and the fork GitHub's auto-merge merges: you turn it on for a Bot's pull request "
                     f"at the exact head and GitHub merges once {names} succeed there, and a person's or a thread's "
                     f"pull request is its owner's to put on auto-merge. Ops has no auto-merge: you merge there once "
                     f"its check succeeded on the exact head.")
    else:
        lines.append("You never merge, start a release, deploy or Nexus run, or change a workflow file through your own App.")
    return "\n".join(lines)


def register(ctx):
    # register() must never raise: Hermes skips a plugin whose register()
    # fails, and a skipped guard would leave the Bot unguarded.
    bot = getattr(ctx, "profile_name", "") or ""
    try:
        from .policy import Policy
        from .tool_guard import check_tool

        policy = Policy.load()
        rule_zero = rule_zero_section(policy, bot)
    except Exception as e:  # the guard fails closed: every tool is refused
        reason = f"the Thornswood guard could not read its policy or rule zero ({type(e).__name__}: {e}); every tool is refused until Patrick fixes it"
        logger.error(reason)

        def refuse_all(tool_name, args=None, **kwargs):
            return {"action": "block", "message": reason}

        ctx.register_hook("pre_tool_call", refuse_all)
        return

    gate = None
    try:
        from . import brokers
        gate = brokers.register(ctx, policy, bot)
    except Exception as e:  # with the brokers on, a gate that fails to load refuses every tool
        if policy.data["brokers"].get("ready"):
            reason = f"the Thornswood turn gate could not load ({type(e).__name__}: {e}); every tool is refused until Patrick fixes it"
            logger.error(reason)

            def refuse_all(tool_name, args=None, **kwargs):
                return {"action": "block", "message": reason}

            ctx.register_hook("pre_tool_call", refuse_all)
            return

    def before_tool(tool_name, args=None, **kwargs):
        if gate is not None:
            message = gate.before_tool(tool_name, **kwargs)
            if message:
                return {"action": "block", "message": message}
        verdict = check_tool(policy, bot, tool_name, args or {})
        if verdict.allowed:
            return None
        logger.info("thornswood_guard refused %s for %s: %s", tool_name, bot, verdict.reason)
        return {"action": "block", "message": f"Refused by the Thornswood guard: {verdict.reason}."}

    ctx.register_hook("pre_tool_call", before_tool)

    if hasattr(ctx, "register_system_prompt_section"):
        ctx.register_system_prompt_section("thornswood.rule-zero", rule_zero, position="after_memory", max_chars=SECTION_MAX)
    section = _prompt_section(policy, bot)
    if section and hasattr(ctx, "register_system_prompt_section"):
        ctx.register_system_prompt_section("thornswood.guard", section, position="after_memory", max_chars=1500)
