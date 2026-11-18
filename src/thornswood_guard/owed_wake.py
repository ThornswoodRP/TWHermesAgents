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
each day, at the requested audit handoff (bramble-duties, section 5). The free
worker wake runs a sweep when a worker holds no claimed issue and the board
has an eligible issue it could take (section 6), looked at once every
IDLE_EVERY seconds, so a quiet fleet spends GitHub reads and no model turn.
The same look finds a worker that holds an active issue but has run no
model turn since the last look (tw-policy's bot_turns): its turn was turned
away or ended mid-issue, and nothing else wakes it (9 Nov 2026: Sprocket
from 9:23 AM, Magpie from 11:27 AM). It wakes Bramble to tell the worker to
carry on, and says so again at every look while the worker stays stopped:
the stop is what GitHub and tw-policy show now, not a note that it was said.
Until 9 Nov 2026 each stop was said once, and kept as told when the line was
printed; at 12:59 PM the queue turned that turn away (another sweep was
running), the owed wake it left was paid by a sweep that never saw the line,
and Sprocket and Magpie stayed stopped with nothing left to say so.
Both ask tw-policy for room first and are taken only when it has some; what
each saw last is kept in IDLE_FILE in the profile.

The free worker look also runs at the first check after a worker's turn
ends, not only on the hour: every check reads tw-policy's bot_turns, which
spends no GitHub read, and a worker with no turn running or in line whose
last turn ended after the last look is a hand-back, a stop or a finish that
Bramble acts on now. And such a worker, holding a claimed issue with no
pull request, is named at once, not after an hour of quiet: a worker runs
an issue only inside a turn, so one with no turn running, none in line and
no message queued for it in the decision core (tw-decide queued_for) is
not working anything. 11 Nov 2026: Rift handed back #4980 blocked at 4:00
PM and Magpie #5765 at 3:59 PM; the 4:52 PM look counted both busy on
their claims, quiet just under the hour it then needed, and the next look
was an hour away, so four of the five workers sat with no turn from 4 PM
while Wisp alone worked. The look waits while Bramble has a turn running,
in line or queued, because a hand-back is answered in that turn and the
look must read what it did; the hourly look runs whatever Bramble is doing.

A worker opportunity stays in IDLE_FILE until its exact assignment is sent,
or a fresh canonical board read proves no eligible work is available. A
completed coordinator sweep alone cannot acknowledge it. Pending idle
workers share one board snapshot at the next five-minute check while
Bramble has no turn running or queued. Source failure retains observations
and defers passive reads for an hour. Cached assignment hints never ride an
unrelated wake. Legacy stopped-claim guidance still can, until a scheduled
sweep carries it. 11 Nov 2026: the 6:31 PM look named Rift and Magpie stopped and
Sprocket, Echo and Wisp free; its sweep was turned away because another was
running, a GitHub sweep paid the wake it owed without its lines, the 6:44 PM
sweep carried only an Ops issue, and no worker got work for the next hour.

Bramble's two weekly reviews (scripts/apply.py REVIEW_JOBS) are Hermes cron
jobs of its own profile, and nothing told anybody when one failed: Bramble
has no cronjob tool in its turns, and the guard keeps its file tools out of
Hermes's home. 15 Nov 2026: the board review fired at 8:37 AM Mountain while
the Claude limit was spent, its turn ended "Your request was not processed",
and what Hermes recorded about it could be read only on the VM
(TWHermesAgents #181, Thornswood-Ops #336). So the alert look also reads
those two jobs' records in <profile>/cron/jobs.json, the file Hermes keeps
them in (contracts.json, profiles.jobs), and a run that ended with an error
wakes Bramble once with the job, when it ran and Hermes's own error line,
for Bramble to put on the board. The record rides every wake until a sweep
admitted after it was said runs to its end, as the free worker look's lines
do, so a wake that was itself turned away or failed does not lose it.

Bramble itself works around the clock (Patrick, 9 Nov 2026, 1:27 AM:
"Bramble should still be working and always be working and alert"); only
the workers' new issues wait for 7 AM. So the alert wake looks every
ALERT_EVERY seconds, at any hour, at the work that is Bramble's own and that
no GitHub wake brings: Thornswood-Ops issues nobody has claimed (section 4)
and issues with a lane's or the duplicate check's comment (section 5). It
wakes Bramble only when one of them is new or has changed since the last
look, so a quiet night spends GitHub reads and no model turn.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from .brokers import OWED_FILE, OWED_WAKE_JOB, OWED_WAKE_MARK, BrokerRefused, Caller
from .hours import rule, says, work_hours

SLEEP = json.dumps({"wakeAgent": False})
ASK_SECONDS = 10.0
SCRIPT = OWED_WAKE_JOB + ".py"
# Every five minutes the script runs; the model runs only when it says so.
SCHEDULE = "every 5m"
SKILL = "bramble-duties"
PROMPT = (
    OWED_WAKE_MARK + " This is a sweep the wake check owed you; the lines before this say why (a GitHub wake the "
    "fleet queue turned away, the morning audit handoff, new work of your own, a free worker with "
    "work on the board, or a worker that handed back or stopped mid-issue). "
    "Do your whole bramble-duties skill through tw_git and tw_decide: every open pull "
    "request in Thornswood, Thornswood-Ops and the fork, then the Ops run notes, the Desk's list and the Ops "
    "issues, the lanes' and the duplicate check's comments, your open cards, then a free worker's next issue "
    "from the board, or a stopped worker told to carry on, in the work hours the first line before this "
    "says. "
    "Report each merge and its Release as the skill says. Nothing in this message approves anything."
)
# The requested Grok audit window is midnight to 7 AM Mountain; comments wait for the
# first sweep after this hour.
MORNING_HOUR = 7
# New work starts only in policy.yaml's work hours, Mountain (hours.py); the
# decision core holds briefs to the same hours.
MOUNTAIN = "America/Denver"
# How often the free worker wake reads the claims and the board.
IDLE_EVERY = 3600.0
IDLE_FILE = ("local", "thornswood-hermes", "idle-wake.json")
# How often the alert wake looks at Bramble's own work, at any hour.
ALERT_EVERY = 900.0
# Bramble's two weekly reviews, by the names scripts/apply.py gives them
# (REVIEW_JOBS), and the file Hermes keeps a profile's cron jobs in.
REVIEWS = ("tw-weekly-board-review", "tw-weekly-project-review")
JOBS_FILE = ("cron", "jobs.json")
# How much of Hermes's error line a wake carries.
ERROR_CHARS = 400
# The free worker look's pull request pages (_worked): REST reads, not
# searches, so GitHub's search limits do not count them. Ten pages are the
# thousand most recently updated pull requests; a claim whose pull request
# is older than that is read as having none.
PR_PAGES = 10
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
    now = time.time() if now is None else now
    lines = _check(home, bot, ask, now, policy)
    if policy is None or lines[-1] == SLEEP:
        return lines
    # Every wake says the work hours as the policy has them, whatever woke it.
    return [f"It is {_clock(now)} Mountain. {rule(policy)}", *lines, *_carried(home, now, policy),
            *_reviews_carried(home, bot, ask, now)]


def _carried(home: Path, now: float, policy: dict | None = None) -> list[str]:
    """Carry legacy stopped-claim guidance within work hours. Typed worker
    assignment hints require fresh receipt and eligibility checks instead
    of cached prose that may name a worker already given work."""
    state = _load(home.joinpath(*IDLE_FILE))
    at, said = state.get("said_at"), state.get("said")
    if not at or not said or float(at) == now or not _working(now, policy):
        return []
    # Typed assignment hints are never replayed from cached prose: receipts
    # and current eligibility may have changed for just one named worker.
    if state.get("pending_workers"):
        return []
    said = [line for line in said if isinstance(line, str) and "hold no active issue" not in line]
    if not said:
        return []
    return [f"What the free worker look at {_clock(float(at))} Mountain said, which no sweep has carried to its end yet "
            "(bramble-duties, section 6). Respect Patrick's Stop; never replay an uncertain or cancelled assignment:", *said]


def _check(home: Path, bot: str, ask: Caller, now: float, policy: dict | None) -> list[str]:
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
        owed = [f"{due.get('refused')} wake(s) since {due.get('since')} were turned away by the fleet queue "
                f"(the last because {due.get('reason')}). The queue has room now."]
        if policy is None:
            return owed
        # The free worker look and the alert look ride an owed wake too.
        # 12 Nov 2026: a wake was owed nearly all evening (GitHub's wakes
        # turned away while another sweep ran), this returned before the
        # look, and no look ran from 7:32 PM Mountain on, so Rift, Sprocket
        # and Echo sat with no issue from 9:15 PM.
        looked = _idle(home, bot, ask, now, policy)
        return owed if looked[-1] == SLEEP else [*owed, *looked]
    if policy is None:
        return ["no wake is owed", SLEEP]
    return ["no wake is owed", *_idle(home, bot, ask, now, policy)]


def _local(now: float):
    """now as Mountain time, or None when the time zone data cannot be read."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(now, ZoneInfo(MOUNTAIN))
    except Exception:
        return None


def _working(now: float, policy: dict | None = None) -> bool:
    local = _local(now)
    start, end = work_hours(policy)
    return local is not None and start <= local.hour < end


def _clock(t: float) -> str:
    local = _local(t)
    return local.strftime("%I:%M %p").lstrip("0") if local else datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M UTC")


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
    start, end = work_hours(policy)
    working = local is not None and start <= local.hour < end
    idle_due = working and (now - float(state.get("idle_checked") or 0) >= IDLE_EVERY
                            or state.get("receipt_scan_pending", False))
    alert_due = now - float(state.get("alert_checked") or 0) >= ALERT_EVERY
    night = None if local is None else (f"it is {local.strftime('%I:%M %p').lstrip('0')} Mountain: no worker starts "
                                        f"new work outside {says((start, end))}; overnight is the requested audit window")
    ended_said = None
    if state.get("said_at"):
        unheard, ended_said = _unheard(bot, ask, state, now, policy)
        _save(path, state)
        idle_due = idle_due or (working and unheard)
    if working and (not idle_due or state.get("progress_workers")):
        ended, why_ended = _turns_ended(bot, ask, policy, float(state.get("idle_checked") or 0))
        ended_said = why_ended or ended_said
        idle_due = idle_due or bool(ended)
        if ended and state.get("progress_workers"):
            state["progress_workers"] = sorted(set(state["progress_workers"]) | set(ended))
    if now < float(state.get("progress_retry_after") or 0):
        idle_due = False
    if not morning and not idle_due and not alert_due:
        if local is None:
            return [why_no_morning, SLEEP]
        if not working:
            return [night, SLEEP]
        return [*([ended_said] if ended_said else []),
                "the morning sweep has run, and neither the alert check nor the free worker check is due", SLEEP]
    try:
        room = ask("policy", "turn_room", {"bot": bot, "sweep": True})
    except BrokerRefused as e:
        return [f"tw-policy did not answer ({e}); the morning, alert and free worker checks wait", SLEEP]
    if not room.get("room"):
        return [f"the queue has no room ({room.get('why_not')}); the morning, alert and free worker checks wait", SLEEP]
    if morning:
        state["morning"] = today
        _save(path, state)
        return [f"It is past {MORNING_HOUR} AM Mountain on {today}, the requested audit handoff: this is "
                "the morning sweep, with their comments and the duplicate check's (bramble-duties, section 5)."]
    said = []
    if alert_due:
        state["alert_checked"] = now
        failed = _new_failed_reviews(home, state, now)
        try:
            seen = _own_work(bot, ask, policy)
        except BrokerRefused as e:
            seen = None
            said.append(f"tw-git did not answer ({e}); the alert check runs again in {int(ALERT_EVERY)} seconds")
        if seen is not None:
            new = sorted(set(seen) - set(state.get("seen") or []))
            state["seen"] = sorted(seen)
            _save(path, state)
            if new or failed:
                return [*failed, *([f"New or changed since the last look, and yours at any hour: {', '.join(new[:8])}"
                                    + (f" and {len(new) - 8} more" if len(new) > 8 else "")
                                    + " (bramble-duties, sections 4, 5 and 7)."] if new else [])]
            said.append("nothing of your own is new since the last look")
        else:
            _save(path, state)
            if failed:
                return [*failed, *said]
    if not idle_due:
        return [*said, night if not working and night else "the free worker check is not due", SLEEP]
    if alert_due:
        # One look per run (10 Nov 2026): the alert look's searches and the
        # free worker look's, sent back to back on the workers' App, passed
        # GitHub's secondary rate limit at every hourly look from 11:59 AM
        # (a 403 on the tenth search in seven seconds), and no worker was
        # given an issue from 12:27 PM. The free worker look stays due and
        # runs at the next check, five minutes on.
        return [*said, "the free worker check is due and runs at the next check, apart from this one's searches",
                SLEEP]
    return [*said, *([ended_said] if ended_said else []), *_free_workers(bot, ask, now, policy, path, state)]


def _failed_reviews(home: Path) -> dict[str, str]:
    """{"<job>@<last run>": the line that says it} for each of Bramble's weekly
    reviews whose last run ended with an error, read from Hermes's own record
    (cron.jobs mark_job_run sets last_error only on a run that failed). An
    unreadable file is no record: the next look reads it again."""
    try:
        data = json.loads(home.joinpath(*JOBS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    jobs = data.get("jobs") if isinstance(data, dict) else data
    out = {}
    for job in jobs if isinstance(jobs, list) else []:
        if not isinstance(job, dict) or job.get("name") not in REVIEWS:
            continue
        at, error = job.get("last_run_at"), " ".join(str(job.get("last_error") or "").split())
        if not at or not error:
            continue
        if len(error) > ERROR_CHARS:
            error = error[: ERROR_CHARS - 3] + "..."
        out[f"{job['name']}@{at}"] = (
            f"Your weekly job {job['name']} ran {_day_and_clock(str(at))} and did not do its work. "
            f"Hermes recorded: \"{error}\". Put this on Thornswood-Ops as it is, on the open issue about that "
            "run or a new one titled with the job and the day (bramble-duties, section 4), so its cause is "
            "fixed where it is. Change no job.")
    return out


def _day_and_clock(iso: str) -> str:
    """When a run was, as Patrick reads it ("on 15 Nov at 8:37 AM Mountain"),
    or Hermes's own time when it cannot be read."""
    t = _when(iso)
    local = _local(t) if t else None
    if local is None:
        return f"at {iso}"
    return f"on {local.day} {local.strftime('%b')} at {_clock(t)} Mountain"


def _new_failed_reviews(home: Path, state: dict, now: float) -> list[str]:
    """The failed review runs no wake has said yet, each now kept in state
    as said at now. Records Hermes no longer holds (the job ran again) leave
    state."""
    failed = _failed_reviews(home)
    kept = {k: v for k, v in (state.get("reviews") or {}).items() if k in failed}
    new = [k for k in failed if k not in kept]
    for k in new:
        kept[k] = {"at": now}
    state["reviews"] = kept
    return [failed[k] for k in new]


def _reviews_carried(home: Path, bot: str, ask: Caller, now: float) -> list[str]:
    """Failed review runs already said, that no sweep admitted after the
    saying has carried to its end (tw-policy's sweep_done): added to every
    wake until one has. tw-policy not answering keeps them."""
    path = home.joinpath(*IDLE_FILE)
    state = _load(path)
    pending = {k: v for k, v in (state.get("reviews") or {}).items() if not v.get("carried")}
    if not pending:
        return []
    try:
        mine = ((ask("policy", "bot_turns", {"bot": bot}) or {}).get("bots") or {}).get(bot) or {}
        done = _when(str(mine.get("sweep_done") or ""))
    except BrokerRefused:
        done = 0.0
    failed = _failed_reviews(home)
    lines = []
    for k, v in pending.items():
        if done and done >= float(v["at"]):
            v["carried"] = True
        elif float(v["at"]) != now and k in failed:
            lines.append(failed[k])
    _save(path, state)
    return lines


def _pending_workers(state: dict, policy: dict) -> dict[str, float] | None:
    raw = state.get("pending_workers")
    if (not isinstance(raw, dict) or set(raw) - set(_workers(policy))
            or any(isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at) or at <= 0
                   for at in raw.values())):
        return None  # old or malformed state needs a fresh, guarded look
    return dict(raw)


def _forget_workers(state: dict) -> None:
    for key in ("said_at", "said", "pending_workers", "pending_reviews", "progress_workers",
                "progress_retry_after", "review_retry_after"):
        state.pop(key, None)


def _receipt_after(row, at: float, *, accepted=False) -> bool:
    if row is None:
        return False
    try:
        if not isinstance(row, dict) or not re.fullmatch(r"cx_[A-Za-z0-9_-]{1,128}", row.get("id", "")):
            raise ValueError()
        when = datetime.fromisoformat(row["at"])
        if (when.tzinfo is None or row.get("status") not in
                ("queued", "leased", "sent", "dead", "uncertain", "cancelled", "quarantined")
                or (accepted and row.get("status") not in ("queued", "leased", "sent"))):
            raise ValueError()
        return when.timestamp() > at
    except (TypeError, ValueError, KeyError, OverflowError):
        raise BrokerRefused("the assignment receipt metadata is unavailable or malformed") from None


def _refresh_receipts(bot: str, ask: Caller, state: dict, policy: dict) -> None:
    """Reconcile at most 32 exact tracked receipts per check. The rotating
    batch can recover oversized old state without dropping any task hold."""
    held = state.get("review_receipts") or {}
    if not held:
        return
    keys = sorted(held)
    start = int(state.get("receipt_cursor") or 0) % len(keys)
    batch = (keys[start:] + keys[:start])[:32]
    reply = ask("decide", "queued_for", {"bot": bot, "receipts": batch})
    current = reply.get("receipts") if isinstance(reply, dict) else None
    if not isinstance(current, dict) or set(current) != set(batch):
        raise BrokerRefused("the exact held assignment receipts are unavailable")
    for key, row in current.items():
        _receipt_after(row, 0)
        if row["id"] != key or row.get("target") not in _workers(policy) or not isinstance(row.get("superseded"), bool):
            raise BrokerRefused("the held assignment receipt metadata is malformed")
        successor = row.get("successor")
        if row["superseded"]:
            _receipt_after(successor, 0)
            if not isinstance(successor, dict) or successor.get("target") not in _workers(policy):
                raise BrokerRefused("the successor assignment receipt is unavailable")
            row = successor
        held.pop(key, None)
        if row["status"] != "sent":
            held[row["id"]] = row
    state["review_receipts"] = held
    state["receipt_cursor"] = (start + len(batch)) % max(1, len(held))


def _scan_receipts(bot: str, ask: Caller, state: dict, policy: dict, pages: int) -> tuple[bool, int]:
    """Bounded unseen-outcome catch-up, with a durable monotonic cursor."""
    count = 0
    for _ in range(pages):
        cursor = state.get("receipt_scan_after", 0)
        scan = ask("decide", "queued_for", {"bot": bot, "scan_after": cursor})
        if (not isinstance(scan, dict) or not isinstance(scan.get("outcomes"), list)
                or len(scan["outcomes"]) > 32 or not isinstance(scan.get("scan_more"), bool)
                or not isinstance(scan.get("scan_after"), int) or isinstance(scan["scan_after"], bool)
                or scan["scan_after"] < cursor or (scan["outcomes"] and scan["scan_after"] <= cursor)
                or (scan["scan_more"] and (not scan["outcomes"] or scan["scan_after"] <= cursor))):
            raise BrokerRefused("the assignment outcome scan is unavailable or malformed")
        for row in scan["outcomes"]:
            _receipt_after(row, 0)
            if row.get("target") not in _workers(policy):
                raise BrokerRefused("the assignment outcome scan target is malformed")
            state.setdefault("review_receipts", {})[row["id"]] = row
        count += len(scan["outcomes"])
        state["receipt_scan_after"] = scan["scan_after"]
        state["receipt_scan_pending"] = scan["scan_more"]
        if not scan["scan_more"]:
            return True, count
    return False, count


def _unheard(bot: str, ask: Caller, state: dict, now: float, policy: dict) -> tuple[bool, str | None]:
    """Keep an observed opportunity until a newer accepted assignment receipt
    or a fresh canonical no-work result settles that worker. A completed
    coordinator sweep alone cannot settle it. Busy/queued coordinators
    coalesce checks; unavailable metadata retains state with bounded retry."""
    try:
        turns = (ask("policy", "bot_turns", {"bot": bot}) or {}).get("bots") or {}
        mine = turns.get(bot) or {}
    except BrokerRefused:
        return False, None
    at = float(state["said_at"])
    done = mine.get("sweep_done")
    try:
        carried = bool(done) and datetime.fromisoformat(done).timestamp() >= at
    except (TypeError, ValueError):
        carried = False
    if mine.get("running") or mine.get("waiting"):
        return False, None
    pending = _pending_workers(state, policy)
    if pending == {} and carried:
        _forget_workers(state)
        return False, None
    try:
        args = {"bot": bot}
        if pending:
            args["since"] = datetime.fromtimestamp(min(pending.values()), timezone.utc).isoformat()
        queued = ask("decide", "queued_for", args)
        if not isinstance(queued, dict) or not isinstance(queued.get("bots"), dict):
            raise BrokerRefused("the assignment queue metadata is unavailable")
        if queued["bots"].get(bot):
            return False, None
        if pending:
            if not all(isinstance(queued.get(k), dict) for k in ("dispatches", "attempts")):
                raise BrokerRefused("the assignment receipts are unavailable")
            held = state.setdefault("review_receipts", {})
            # Record outcomes before settling workers, including a later
            # different cancellation beside an earlier confirmed delivery.
            for rows in (queued["dispatches"], queued["attempts"]):
                for w, row in rows.items():
                    if w in pending and _receipt_after(row, pending[w]) and row.get("status") != "sent":
                        held[row["id"]] = dict(held.get(row["id"], {}), **row, target=w)
            _refresh_receipts(bot, ask, state, policy)
            pending = {w: at for w, at in pending.items()
                       if not (_receipt_after(queued["dispatches"].get(w), at, accepted=True)
                               and queued["dispatches"][w]["status"] == "sent")}
            state["pending_workers"] = pending
            if not pending:
                _forget_workers(state)
                return False, None
            state["pending_reviews"] = {w: queued["attempts"][w] for w, at in pending.items()
                                        if _receipt_after(queued["attempts"].get(w), at)
                                        and queued["attempts"][w].get("status") in
                                        ("dead", "uncertain", "cancelled", "quarantined")}
            if any(not isinstance(turns.get(w), dict) for w in pending):
                raise BrokerRefused("the worker activity metadata is unavailable")
            available = [w for w in pending if not queued["bots"].get(w)
                         and not (turns.get(w) or {}).get("running") and not (turns.get(w) or {}).get("waiting")]
            if not available:
                state.pop("progress_workers", None)
                return False, None
            state["progress_workers"] = available
    except BrokerRefused:
        state["progress_retry_after"] = now + IDLE_EVERY
        return False, None
    if now < float(state.get("progress_retry_after") or 0):
        return False, None
    if pending is not None and pending:
        return True, ("the observed idle workers still have no assignment receipt; "
                      + ("no sweep carried its lines to the end; " if not carried else "")
                      + "refresh eligibility before "
                      "routing work, respecting Patrick's Stop and never replaying an uncertain or cancelled assignment")
    if carried and pending is None:
        return True, "the earlier worker observation has no typed dispatch proof; refresh eligibility once"
    return True, (f"the free worker look at {_clock(at)} Mountain woke you, but no sweep carried its lines to the "
                  "end (it was turned away, or a sweep that never saw them paid its wake): it looks again now")


def _workers(policy: dict) -> list[str]:
    return [name for name, b in policy["bots"].items() if b.get("role") == "worker"]


def _turns_ended(bot: str, ask: Caller, policy: dict, since: float) -> tuple[list[str], str | None]:
    """The workers whose last turn ended after since (the last free worker
    look) and that have no turn running, none in line and no message queued
    for them: each handed back, stopped or finished, and the look runs now.
    Empty, with why, while Bramble has a turn running, in line or queued (it
    is answering a hand-back, and the look reads what that turn did), and
    empty when tw-policy or tw-decide does not answer (the hourly look still
    runs). Spends no GitHub read."""
    try:
        turns = (ask("policy", "bot_turns", {"bot": bot}) or {}).get("bots") or {}
    except BrokerRefused:
        return [], None
    ended = []
    for w in _workers(policy):
        t = turns.get(w)
        if not isinstance(t, dict) or t.get("running") or t.get("waiting") or not t.get("last"):
            continue
        try:
            if datetime.fromisoformat(t["last"]).timestamp() > since:
                ended.append(w)
        except (TypeError, ValueError):
            continue
    if not ended:
        return [], None
    try:
        queued = (ask("decide", "queued_for", {"bot": bot}) or {}).get("bots") or {}
    except BrokerRefused as e:
        return [], f"{', '.join(ended)} ended a turn, but tw-decide did not answer ({e}); the free worker look waits"
    ended = [w for w in ended if not queued.get(w)]
    mine = turns.get(bot) if isinstance(turns.get(bot), dict) else {}
    if ended and (mine.get("running") or mine.get("waiting") or queued.get(bot)):
        return [], (f"{', '.join(ended)} ended a turn since the last look; the free worker look runs once your own "
                    "turn that is running, in line or queued has ended, so it reads what that turn did")
    if not ended:
        return [], None
    return ended, (f"{', '.join(ended)} ended a turn since the last look and has no turn running, in line or queued: "
                   "the free worker look runs now, not on the hour")


def _own_work(bot: str, ask: Caller, policy: dict) -> list[str]:
    """What is Bramble's own and comes with no GitHub wake, as
    "<repo>#<number> at <updated>" so a change shows: Ops issues nobody has
    claimed, and open issues with a lane's or the duplicate check's comment."""
    repos = policy["repos"]
    searches = ((repos["ops"], 'is:issue is:open -label:"claimed: Bramble"'),
                (repos["app"], 'is:issue is:open "duplicate of" in:comments'),
                (repos["app"], 'is:issue is:open "may repeat an open issue" in:comments'))
    out = []
    for repo, terms in searches:
        q = urllib.parse.quote(f"repo:{repo} {terms}")
        body = (ask("git", "read", {"bot": bot, "repo": repo, "path": f"/search/issues?q={q}&sort=updated&per_page=50"})
                or {}).get("body") or {}
        for i in body.get("items") or []:
            out.append(f"{repo.split('/')[-1]}#{int(i.get('number') or 0)} at {i.get('updated_at') or '?'}")
    return out


def _free_workers(bot: str, ask: Caller, now: float, policy: dict, path: Path, state: dict) -> list[str]:
    """The free worker wake, 7 AM to midnight: its lines, the last SLEEP unless a free
    worker has work on the board."""
    progress = state.pop("progress_workers", None)
    all_due = now - float(state.get("all_workers_checked") or 0) >= IDLE_EVERY
    names = _workers(policy) if all_due or not progress else [w for w in progress if w in _workers(policy)]
    if all_due:
        state["all_workers_checked"] = now
    state["idle_checked"] = now
    state.pop("stopped", None)
    _save(path, state)
    workers = {name: policy["bots"][name]["claim_label"] for name in names}
    repo = policy["repos"]["app"]
    asked = _asked(bot, ask)
    observed = {"workers": set(), "no_work": set()}
    try:
        # Initialize from structured current receipts too: after an upgrade,
        # a historical uncertain assignment must not become a carry-on hint
        # merely because it predates this observation's timestamp.
        # Scan every unseen non-sent receipt, not just the latest per worker:
        # two failures between scheduler ticks must retain both exact tasks.
        complete, _ = _scan_receipts(bot, ask, state, policy, 4)
        if not complete:
            _save(path, state)
            return ["the bounded assignment receipt scan continues at the next check; no uncertain task is replayed", SLEEP]
        latest = ask("decide", "queued_for", {"bot": bot, "since": "1970-01-01T00:00:00+00:00"})
        if (not isinstance(latest, dict) or not isinstance(latest.get("bots"), dict)
                or not isinstance(latest.get("attempts"), dict)):
            raise BrokerRefused("the current assignment metadata is unavailable")
        for w, row in latest["attempts"].items():
            if w in _workers(policy) and _receipt_after(row, 0) and row.get("status") != "sent":
                state.setdefault("review_receipts", {})[row["id"]] = dict(row, target=w)
        _refresh_receipts(bot, ask, state, policy)
        workers = {w: label for w, label in workers.items() if not latest["bots"].get(w)}
        if not workers:
            _save(path, state)
            return ["the workers already have deliveries in flight; this check coalesces with them", SLEEP]
        lines = _look(bot, ask, now, policy, workers, repo, asked, observed,
                      state.get("review_receipts") or {})
        _, arrived = _scan_receipts(bot, ask, state, policy, 1)
        if arrived:
            state["receipt_scan_pending"] = True
            _save(path, state)
            return ["new assignment outcomes arrived during the board read; refresh the checked candidates next tick", SLEEP]
        if observed.get("closed"):
            state["review_receipts"] = {key: row for key, row in (state.get("review_receipts") or {}).items()
                                         if not isinstance(row.get("issue"), dict)
                                         or (row["issue"].get("repo"), row["issue"].get("number"))
                                         not in observed["closed"]}
    except BrokerRefused as e:
        state["progress_retry_after"] = now + IDLE_EVERY
        _save(path, state)
        return [f"tw-git did not answer ({e}); the free worker check runs again in {int(IDLE_EVERY)} seconds", SLEEP]
    state.pop("progress_retry_after", None)
    pending = _pending_workers(state, policy) or {}
    pending = {w: at for w, at in pending.items() if w not in observed["no_work"]}
    for worker in observed["workers"]:
        pending.setdefault(worker, now)
    if lines[-1] == SLEEP:
        if not pending:
            _forget_workers(state)
    else:
        # check() puts the work hours line first on every wake.
        state["said_at"], state["said"] = now, lines
    if pending or lines[-1] != SLEEP:
        state["pending_workers"] = pending
    _save(path, state)
    return lines


def _look(bot: str, ask: Caller, now: float, policy: dict, workers: dict[str, str], repo: str,
          asked: set[str], observed: dict | None = None, reviews: dict | None = None) -> list[str]:
    """Read claims and PRs in bounded batches, then one complete canonical
    board snapshot for all free fields. Exact failed/cancelled receipts
    require review of that task, never replay or a worker-wide stop."""
    reviews = reviews or {}
    excluded, review_lines, ambiguous = set(), [], set()
    allowed_repos = {repo, policy["repos"]["fork"]}
    for receipt in reviews.values():
        worker = receipt.get("target")
        issue = receipt.get("issue")
        if (isinstance(issue, dict) and isinstance(issue.get("repo"), str) and issue["repo"] in allowed_repos
                and isinstance(issue.get("number"), int) and not isinstance(issue["number"], bool)
                and 0 < issue["number"] <= 9999999999):
            excluded.add((issue["repo"], issue["number"]))
            task = f'{issue["repo"]}#{issue["number"]}'
            asked = asked | {task}  # only this claim waits for review
        else:
            task = "an unidentified legacy task"
            ambiguous.add(worker)
            if observed is not None:
                observed.setdefault("ambiguous", set()).add(worker)
        if worker not in workers:
            continue  # holds apply globally; prose is scoped to this scan
        review_lines.append(f"{worker}'s receipt {receipt['id']} is {receipt['status']} for {task}.")
    # A worker's pull request for a Thornswood issue may be in the fork:
    # Wisp's engine fixes are (10 Nov 2026: #4513's fix merged as fork
    # #148 at 5:49 AM, and Wisp, seen as still working it, got nothing
    # from 9:44 AM on, with 179 issues open in its field).
    claims = _claims(bot, ask, repo, list(workers.values()))
    fork_claims = _claims(bot, ask, policy["repos"]["fork"], [workers["wisp"]]) if "wisp" in workers else {}
    # The pull requests are read back to the oldest claimed issue's creation,
    # since a pull request for an issue is opened after it.
    since = min((str(i["created_at"]) for items in (*claims.values(), *fork_claims.values())
                 for i in items if i.get("created_at")), default="")
    prs = {**_worked(bot, ask, policy["repos"]["fork"], since), **_worked(bot, ask, repo, since)}
    parked: dict[str, list[int]] = {}
    free = []
    for w in workers:
        busy = _active(repo, w, claims.get(workers[w], []), prs, asked, parked)
        if w == "wisp":
            busy = _active(policy["repos"]["fork"], w, fork_claims.get(workers[w], []), prs, asked,
                           parked) or busy
        if not busy:
            free.append(w)
    stopped = _stopped(bot, ask, [w for w in workers if w not in free and w not in ambiguous], now)
    said = review_lines[:5]
    if len(review_lines) > 5:
        said.append(f"{len(review_lines) - 5} more exact receipt outcomes remain held for review.")
    if review_lines:
        said.append("Review these outcomes without replaying an old brief. Historical ambiguity is not a worker pause; "
                    "only fresh checked issue refs below may be assigned, subject to Patrick's explicit pause or Stop.")
    if parked:
        said.append(f"{'; '.join(w + ' ' + ', '.join(f'#{n}' for n in sorted(parked[w])) for w in sorted(parked))}: "
                    "each holds a claimed issue with no pull request that it has moved on from (it opened a "
                    "pull request for another issue since the issue last changed), so it is not working it: "
                    "comment what it waits on and take the claim off, or send it back to the worker as its "
                    "next issue (bramble-duties, section 6, items 1 and 5). The worker counts as free.")
    if stopped:
        said.append(f"{', '.join(sorted(stopped))} hold an active issue but have no turn running, none in line and "
                    "nothing queued for them, the last ended "
                    f"{', '.join(w + ' ' + (stopped[w] or 'never') for w in sorted(stopped))}: each handed back "
                    "with no pull request or stopped mid-issue, and nothing wakes it by itself. If its hand-back "
                    "says it is blocked, comment what it waits on, take the claim off and give it its next issue; "
                    "otherwise tell it to carry on (bramble-duties, section 6, items 5 and 6). This is said at "
                    "every look until it runs a turn or its claim comes off. This applies only to its other "
                    "active claims, never to an exact receipt task held for review above.")
    if not free:
        return said or ["every worker is on an active issue", SLEEP]
    # Filter each field before truncation, from the same complete snapshot.
    groups = {w: {"areas": list(AREAS.get(w, ()))} for w in free}
    groups["shared"] = {"not_areas": [*(a for mine in AREAS.values() for a in mine), *BRAMBLES_OWN]}
    board = ask("git", "board", {"bot": bot, "eligible": True, "limit": 1, "groups": groups,
                                  **({"excluded_issues": [{"repo": repo, "number": n} for repo, n in sorted(excluded)]}
                                     if excluded else {})})
    results = board.get("groups") if isinstance(board, dict) else None
    if not isinstance(results, dict) or set(results) != set(groups):
        raise BrokerRefused("the grouped eligible board snapshot is unavailable")
    for group in results.values():
        if (not isinstance(group, dict) or not isinstance(group.get("issues"), list)
                or not isinstance(group.get("open"), int) or isinstance(group.get("open"), bool)
                or group["open"] < 0 or (not group["issues"] and group["open"] != 0)):
            raise BrokerRefused("the grouped eligible board snapshot is malformed or incomplete")
    if observed is not None:
        closed = board.get("closed_exclusions", [])
        if not isinstance(closed, list) or any(not isinstance(i, dict) or set(i) != {"repo", "number"}
                or not isinstance(i["repo"], str) or not isinstance(i["number"], int)
                or (i["repo"], i["number"]) not in excluded for i in closed):
            raise BrokerRefused("the closed receipt task proof is malformed")
        observed["closed"] = {(i["repo"], i["number"]) for i in closed}
    shared = bool(results["shared"]["issues"])
    fed = [w for w in free if results[w]["issues"]]
    if observed is not None:
        observed["workers"].update(w for w in free if w in fed or shared)
        observed["no_work"].update(w for w in free if w not in fed and not shared)
    candidates = []
    for worker in free:
        rows = results[worker]["issues"] or results["shared"]["issues"]
        if rows:
            item = rows[0]
            if (not isinstance(item, dict) or item.get("repo") not in allowed_repos
                    or not isinstance(item.get("number"), int) or isinstance(item["number"], bool)):
                raise BrokerRefused("the checked candidate issue reference is malformed")
            candidates.append(f"Checked next issue for {worker}: {item['repo']}#{item['number']}.")
    direction = " Only pick these checked issue refs; recheck claim eligibility before assigning. Never replay a held receipt."
    if fed:
        return [*said, *candidates, f"{', '.join(free)} hold no active issue (none claimed, or each claimed one waits), and "
                f"open unclaimed issues are in the field of {', '.join(fed)}"
                + ": pick for each free worker from the checked refs (bramble-duties, section 6)." + direction]
    if shared:
        return [*said, *candidates, f"{', '.join(free)} hold no active issue, and open unclaimed issues are picked by the "
                "part they name: pick for the free workers (bramble-duties, section 6)." + direction]
    if said:
        return said
    return [f"{', '.join(free)} hold no active issue, but no open unclaimed issue is theirs", SLEEP]


def _labels(item: dict) -> set[str]:
    return {(x.get("name") if isinstance(x, dict) else x) for x in item.get("labels") or []}


def _search(bot: str, ask: Caller, repo: str, terms: str, per_page: int, page: int = 1) -> dict:
    """One issue search in repo through tw-git: its body (total_count, items)."""
    q = urllib.parse.quote(f"repo:{repo} is:issue is:open {terms}")
    more = f"&page={page}" if page > 1 else ""
    return (ask("git", "read", {"bot": bot, "repo": repo,
                                "path": f"/search/issues?q={q}&per_page={per_page}{more}"}) or {}).get("body") or {}


def _claims(bot: str, ask: Caller, repo: str, labels: list[str]) -> dict[str, list[dict]]:
    """The open issues in repo carrying each claim label, from one search for
    all of them (a label: term with commas is any of them), a page of 100 at
    a time: on 10 Nov 2026 the five workers held 35 between them."""
    terms = "label:" + ",".join(f'"{l}"' for l in labels)
    body = _search(bot, ask, repo, terms, 100)
    items = list(body.get("items") or [])
    total = min(int(body.get("total_count") or 0), 1000)  # search serves its first 1000
    page = 1
    while len(items) < total and body.get("items"):
        page += 1
        body = _search(bot, ask, repo, terms, 100, page)
        items.extend(body.get("items") or [])
    out: dict[str, list[dict]] = {}
    for i in items:
        for l in _labels(i) & set(labels):
            out.setdefault(l, []).append(i)
    return out


def _worked(bot: str, ask: Caller, repo: str, since: str = "") -> dict[tuple[str, int], str]:
    """(worker, issue) for each of the repo's pull requests that is open or
    merged, from its <bot>/<issue>-<what> branch, with when the pull request
    was opened (newest when there are several). Read newest updated first, a
    page of 100 at a time, back to since (the oldest claimed issue's
    creation), at most PR_PAGES pages. 12 Nov 2026: it read one page, and
    Echo's #4330, #4333 and #4327, whose fixes merged on 9 Nov and wait for
    the main Ship, fell off it and were called parked."""
    out: dict[tuple[str, int], str] = {}
    for page in range(1, PR_PAGES + 1):
        pulls = (ask("git", "read", {"bot": bot, "repo": repo, "path": f"/repos/{repo}/pulls?state=all&sort=updated"
                                     f"&direction=desc&per_page=100&page={page}"}) or {}).get("body") or []
        pulls = pulls if isinstance(pulls, list) else []
        for p in pulls:
            m = re.match(r"^([a-z]+)/(\d+)-", ((p.get("head") or {}).get("ref") or ""))
            if m and (p.get("state") == "open" or p.get("merged_at")):
                key = (m.group(1), int(m.group(2)))
                out[key] = max(out.get(key, ""), str(p.get("created_at") or ""))
        oldest = min((str(p.get("updated_at") or "") for p in pulls), default="")
        if len(pulls) < 100 or not since or not oldest or _when(oldest) < _when(since):
            break
    return out


def _asked(bot: str, ask: Caller) -> set[str]:
    """The tasks ("<owner>/<repo>#<number>") a pending card waits on Patrick
    for. tw-decide not answering leaves them looking active, which costs
    only a free worker check."""
    try:
        cards = ask("decide", "my_cards", {"bot": bot}) or []
    except BrokerRefused:
        return set()
    return {c["task"] for c in cards if isinstance(c, dict) and c.get("status") == "pending" and c.get("task")}


def _active(repo: str, worker: str, items: list[dict], prs: dict[tuple[str, int], str],
            asked: set[str] = frozenset(), parked: dict[str, list[int]] | None = None) -> bool:
    """Whether the worker holds a claimed issue in repo that is not waiting.
    Waiting is never a reason to sit idle (Patrick, 9 Nov 2026: "It should
    auto continue to the next issue in their field and start a new one when
    they are waiting on a build, push, merge, release, or a decision"). An
    issue waits when the worker's branch has an open or merged pull request
    in either repo (a build, a check, a review, a merge or a release), when
    it carries `needs: patrick`, or when a pending card asks Patrick about it.

    A claimed issue with no pull request is parked, not active, when the
    worker opened a pull request for another issue after the issue last
    changed: a worker works one issue at a time, so it moved on and is not
    working this one. Each is added to parked. 10 Nov 2026: Rift's #4511
    (last changed 7:26 PM on 9 Nov, its note saying the game files were not
    read yet) held Rift as busy while it opened #5731 and #5756 for other
    issues, so from 10:33 AM, with 61 issues open in its field, Rift got
    nothing new and was never seen as stopped either."""
    newest = max((at for (w, _), at in prs.items() if w == worker), default="")
    active = False
    for i in items:
        n = int(i.get("number") or 0)
        labels = _labels(i)
        if (worker, n) in prs or "needs: patrick" in labels or f"{repo}#{n}" in asked:
            continue
        changed = str(i.get("updated_at") or "")
        if changed and newest and _when(newest) > _when(changed):
            if parked is not None:
                parked.setdefault(worker, []).append(n)
            continue
        active = True
    return active


def _when(iso: str) -> float:
    """GitHub's time (2026-11-10T02:26:14Z) as seconds; an unreadable one is 0."""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _stopped(bot: str, ask: Caller, busy: list[str], now: float) -> dict[str, str | None]:
    """Each busy worker with no model turn running, none in line and no
    message queued for it in the decision core, with when its last turn was
    heard from (None when it never ran one). A worker works an issue only
    inside a turn, so such a worker is not working it, however recently its
    turn ended (until 11 Nov 2026 it also had to be quiet for IDLE_EVERY,
    and a worker that handed back blocked waited up to two hours for its
    next issue). tw-policy or tw-decide not answering finds none: that costs
    one look."""
    if not busy:
        return {}
    try:
        turns = (ask("policy", "bot_turns", {"bot": bot}) or {}).get("bots") or {}
        queued = (ask("decide", "queued_for", {"bot": bot}) or {}).get("bots") or {}
    except BrokerRefused:
        return {}
    out = {}
    for w in busy:
        t = turns.get(w)
        if not isinstance(t, dict) or t.get("running") or t.get("waiting") or queued.get(w):
            continue
        out[w] = t.get("last")
    return out


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
