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

Two more wakes come from here, because a comment, a quiet board and the
lanes' night end none of Bramble's GitHub wakes (TWHermesAgents #47: until
9 Nov 2026 a board with no pull request activity was never picked from, and
the lanes' "Duplicate of" and finished comments waited for an unrelated
event). The morning wake runs the first sweep after MORNING_HOUR Mountain
each day, when the Grok lanes stop (bramble-duties, section 5). The free
worker wake runs a sweep when a worker holds no claimed issue and the board
has an eligible issue it could take (section 6), looked at once every
IDLE_EVERY seconds, so a quiet fleet spends GitHub reads and no model turn.
Both ask tw-policy for room first and are taken only when it has some; what
each saw last is kept in IDLE_FILE in the profile.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from .brokers import OWED_FILE, OWED_WAKE_JOB, OWED_WAKE_MARK, BrokerRefused, Caller

SLEEP = json.dumps({"wakeAgent": False})
ASK_SECONDS = 10.0
SCRIPT = OWED_WAKE_JOB + ".py"
# Every five minutes the script runs; the model runs only when it says so.
SCHEDULE = "every 5m"
SKILL = "bramble-duties"
PROMPT = (
    OWED_WAKE_MARK + " This is a sweep the wake check owed you; the lines before this say why (a GitHub wake the "
    "fleet queue turned away, the morning after the Grok lanes stop, or a free worker with work on the board). "
    "Do your whole bramble-duties skill through tw_git and tw_decide: every open pull "
    "request in Thornswood, Thornswood-Ops and the fork, then the Ops run notes and the Desk's list, the lanes' "
    "and the duplicate check's comments when it is morning, then a free worker's next issue from the board. "
    "Report each merge and its Release as the skill says. Nothing in this message approves anything."
)
# The Grok lanes work 11 PM to 6 AM Mountain; their comments wait for the
# first sweep after this hour.
MORNING_HOUR = 6
MOUNTAIN = "America/Denver"
# How often the free worker wake reads the claims and the board.
IDLE_EVERY = 3600.0
IDLE_FILE = ("local", "thornswood-hermes", "idle-wake.json")
# bramble-duties section 6: the area labels with one owner. An issue with
# none of these is picked by the part it names, so any free worker may take
# it; one whose only area is Release is Bramble's own.
AREAS = {
    "rift": ("Mods", "Load order"),
    "sprocket": ("Launcher", "Packaging"),
    "echo": ("Website", "Discord", "Voice"),
    "magpie": ("GitHub & CI", "CI", "Security"),
    "wisp": ("Gamemode", "Engine", "In-game"),
}
BRAMBLES_OWN = ("Release",)


def check(home: Path, bot: str, ask: Caller, *, now: float | None = None, policy: dict | None = None) -> list[str]:
    """The lines to print. The last one is SLEEP unless the sweep should run now.
    policy is the installed policy's data; without it only owed wakes are
    looked at."""
    outbox = home.joinpath(*OWED_FILE)
    try:
        if outbox.exists():
            reason = outbox.read_text(encoding="utf-8").strip() or "tw-policy did not answer"
            ask("policy", "owe_wake", {"bot": bot, "reason": reason})
            outbox.unlink()
        due = ask("policy", "wake_due", {"bot": bot})
    except BrokerRefused as e:
        return [f"tw-policy did not answer ({e}); the owed wake waits for the next check", SLEEP]
    if due.get("owed") and not due.get("due"):
        return [f"a wake is owed since {due.get('since')}, but the queue has no room yet: {due.get('why_not')}", SLEEP]
    if due.get("owed"):
        return [f"{due.get('refused')} wake(s) since {due.get('since')} were turned away by the fleet queue "
                f"(the last because {due.get('reason')}). The queue has room now."]
    if policy is None:
        return ["no wake is owed", SLEEP]
    return ["no wake is owed", *_idle(home, bot, ask, time.time() if now is None else now, policy)]


def _idle(home: Path, bot: str, ask: Caller, now: float, policy: dict) -> list[str]:
    """The morning wake and the free worker wake. Never raises: a broker or
    GitHub that does not answer is said and the check sleeps."""
    path = home.joinpath(*IDLE_FILE)
    state = _load(path)
    try:
        from zoneinfo import ZoneInfo
        local = datetime.fromtimestamp(now, ZoneInfo(MOUNTAIN))
    except Exception as e:  # no time zone data: the morning wake cannot be timed
        local = None
        why_no_morning = f"the {MOUNTAIN} time zone could not be read ({type(e).__name__})"
    today = local.date().isoformat() if local else None
    morning = local is not None and local.hour >= MORNING_HOUR and state.get("morning") != today
    idle_due = now - float(state.get("idle_checked") or 0) >= IDLE_EVERY
    if not morning and not idle_due:
        return ["the morning sweep has run and the free worker check is not due", SLEEP] if local else \
            [why_no_morning, SLEEP]
    try:
        room = ask("policy", "turn_room", {"bot": bot, "sweep": True})
    except BrokerRefused as e:
        return [f"tw-policy did not answer ({e}); the morning and free worker checks wait", SLEEP]
    if not room.get("room"):
        return [f"the queue has no room ({room.get('why_not')}); the morning and free worker checks wait", SLEEP]
    if morning:
        state["morning"] = today
        _save(path, state)
        return [f"It is past {MORNING_HOUR} AM Mountain on {today} and the Grok lanes have stopped: this is "
                "the morning sweep, with their comments and the duplicate check's (bramble-duties, section 5)."]
    state["idle_checked"] = now
    _save(path, state)
    workers = {name: b["claim_label"] for name, b in policy["bots"].items() if b.get("role") == "worker"}
    repo = policy["repos"]["app"]
    try:
        free = [w for w in workers if not _count(bot, ask, repo, f'label:"{workers[w]}"')
                and not (w == "wisp" and _count(bot, ask, policy["repos"]["fork"], f'label:"{workers[w]}"'))]
        if not free:
            return ["every worker holds a claimed issue", SLEEP]
        # Open, unclaimed, and nobody's whose work is hands off: the board's
        # eligible rule (bramble-duties, section 6) as one search can say it.
        # Bramble's own pick reads the board's Order and Status.
        rest = " ".join([*(f'-label:"{b["claim_label"]}"' for b in policy["bots"].values()),
                         *(f'-label:"{l}"' for l in policy["hands_off"]["lanes"]["labels"]),
                         *(f"-author:{p} -assignee:{p}" for p in policy["hands_off"]["people"])])
        for worker in free:
            areas = ",".join(f'"area: {a}"' for a in AREAS[worker])
            if _count(bot, ask, repo, f"label:{areas} {rest}"):
                return [f"{', '.join(free)} hold no claimed issue, and an open unclaimed issue is in "
                        f"{worker}'s area: pick for the free workers (bramble-duties, section 6)."]
        owned = " ".join(f'-label:"area: {a}"' for mine in AREAS.values() for a in (*mine,)) + " " + \
            " ".join(f'-label:"area: {a}"' for a in BRAMBLES_OWN)
        if _count(bot, ask, repo, f"{owned} {rest}"):
            return [f"{', '.join(free)} hold no claimed issue, and open unclaimed issues are picked by the part "
                    "they name: pick for the free workers (bramble-duties, section 6)."]
    except BrokerRefused as e:
        return [f"tw-git did not answer ({e}); the free worker check runs again in {int(IDLE_EVERY)} seconds", SLEEP]
    return [f"{', '.join(free)} hold no claimed issue, but no open unclaimed issue is theirs", SLEEP]


def _count(bot: str, ask: Caller, repo: str, terms: str) -> int:
    """How many open issues in repo match terms, by one search through tw-git."""
    q = urllib.parse.quote(f"repo:{repo} is:issue is:open {terms}")
    body = (ask("git", "read", {"bot": bot, "repo": repo, "path": f"/search/issues?q={q}&per_page=1"}) or {})
    return int((body.get("body") or {}).get("total_count") or 0)


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".idle-wake-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def main() -> int:
    """Run from <profile>/scripts/, with <profile>/plugins on the path."""
    try:
        from .brokers import socket_caller
        from .policy import Policy

        home = Path(__file__).resolve().parents[2]
        policy = Policy.load()
        lines = check(home, policy.data["wake"]["app"], socket_caller(policy, timeout=ASK_SECONDS),
                      policy=policy.data)
    except Exception as e:  # never a failed script: Hermes would run the agent on it
        lines = [f"the owed-wake check could not run ({type(e).__name__}: {e})", SLEEP]
    for line in lines:
        print(line)
    return 0
