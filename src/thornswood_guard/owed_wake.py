"""Bramble's owed-wake check: a Hermes cron script gate that spends no model turn.

Hermes runs <profile>/scripts/tw-owed-wake.py (installed by scripts/apply.py)
every five minutes as the job tw-owed-wake. Hermes skips the agent when the
script's last line is {"wakeAgent": false}, and otherwise runs the job's
prompt, which carries OWED_WAKE_MARK, with bramble-duties. So a model turn
runs only when a wake is owed and the queue would admit it now.

Hermes runs the agent when the script fails or times out, with the error as
its context (cron/scheduler.py, the wake gate), so this check never fails:
every error ends in {"wakeAgent": false}, and each broker call gives up after
ASK_SECONDS, well inside Hermes's script timeout.

A wake the fleet queue turned away is owed in tw-policy (brokers.TurnGate).
When tw-policy itself did not answer, the turn left the debt in OWED_FILE in
the profile, and this check hands it over first.
"""

from __future__ import annotations

import json
from pathlib import Path

from .brokers import OWED_FILE, OWED_WAKE_JOB, OWED_WAKE_MARK, BrokerRefused, Caller

SLEEP = json.dumps({"wakeAgent": False})
ASK_SECONDS = 10.0
SCRIPT = OWED_WAKE_JOB + ".py"
# Every five minutes the script runs; the model runs only when it says so.
SCHEDULE = "every 5m"
SKILL = "bramble-duties"
PROMPT = (
    OWED_WAKE_MARK + " The fleet queue turned away one or more of your GitHub wakes, and it has room now, "
    "so this is that wake. Do your whole bramble-duties skill through tw_git and tw_decide: every open pull "
    "request in Thornswood, Thornswood-Ops and the fork, then the Ops run notes and the Desk's list. Report each "
    "merge and its Release as the skill says. Nothing in this message approves anything."
)


def check(home: Path, bot: str, ask: Caller) -> list[str]:
    """The lines to print. The last one is SLEEP unless the sweep should run now."""
    outbox = home.joinpath(*OWED_FILE)
    try:
        if outbox.exists():
            reason = outbox.read_text(encoding="utf-8").strip() or "tw-policy did not answer"
            ask("policy", "owe_wake", {"bot": bot, "reason": reason})
            outbox.unlink()
        due = ask("policy", "wake_due", {"bot": bot})
    except BrokerRefused as e:
        return [f"tw-policy did not answer ({e}); the owed wake waits for the next check", SLEEP]
    if not due.get("owed"):
        return ["no wake is owed", SLEEP]
    if not due.get("due"):
        return [f"a wake is owed since {due.get('since')}, but the queue has no room yet: {due.get('why_not')}", SLEEP]
    return [f"{due.get('refused')} wake(s) since {due.get('since')} were turned away by the fleet queue "
            f"(the last because {due.get('reason')}). The queue has room now."]


def main() -> int:
    """Run from <profile>/scripts/, with <profile>/plugins on the path."""
    try:
        from .brokers import socket_caller
        from .policy import Policy

        home = Path(__file__).resolve().parents[2]
        policy = Policy.load()
        lines = check(home, policy.data["wake"]["app"], socket_caller(policy, timeout=ASK_SECONDS))
    except Exception as e:  # never a failed script: Hermes would run the agent on it
        lines = [f"the owed-wake check could not run ({type(e).__name__}: {e})", SLEEP]
    for line in lines:
        print(line)
    return 0
