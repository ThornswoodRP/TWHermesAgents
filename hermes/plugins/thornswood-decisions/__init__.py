"""The agent half of the thornswood-decisions plugin: rule zero, and nothing else.

The plugin is a backend (dashboard/plugin_api.py) and a Desktop pane
(desktop/plugin.js). Hermes loads a plugin with a plugin.yaml through this
file, and plugin.yaml is what lets `hermes plugins enable thornswood-decisions`
find it.

The agent half gives Patrick's own Hermes (my-hermes on the VM, and his
Hermes on One where the plugin is linked) no tools, hooks or commands. It adds
one system prompt section: rule zero, Patrick's "Fix the cause, no band-aids,
ever", word for word from rule_zero.md, the same text the guard gives the six
Bots. Hermes freezes it into every new session's prompt, so a reload or a
SOUL.md edit cannot drop it; only a TWHermesAgents PR changes it, and a test
there fails if this copy and the guard's ever differ.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

RULE_ZERO_FILE = Path(__file__).with_name("rule_zero.md")
SECTION_MAX = 4000


def register(ctx) -> None:
    if not hasattr(ctx, "register_system_prompt_section"):
        logger.error("thornswood-decisions: this Hermes cannot add rule zero to the prompt "
                     "(no register_system_prompt_section); update Hermes")
        return
    try:
        text = RULE_ZERO_FILE.read_text(encoding="utf-8").strip()
    except OSError as e:
        logger.error("thornswood-decisions: rule zero is missing (%s); reinstall the plugin", e)
        return
    ctx.register_system_prompt_section("thornswood.rule-zero", text, position="after_memory", max_chars=SECTION_MAX)
