"""The Bots' side of the brokers: the turn gate and the two broker tools.

Off until Patrick sets brokers.ready in the policy by a PR, after the install
and the canary attack tests pass on the VM. Before that the sockets do not
exist, and nothing here is registered.

The turn gate (08 section 7). Each model turn asks tw-policy for a ticket in
pre_llm_call. Hermes bounds that hook (30 seconds by default) and goes on
when it runs out, so the hook cannot hold a turn for long. What it can do is
fail closed: a turn without an admitted ticket has every tool refused in
pre_tool_call, and its one model answer is told to say the work is queued
and stop. Routes 2 and 4 need the ticket at the proxy as well. The ticket
goes back at the turn's end (on_session_end, which Hermes fires per turn) or
when the turn is stopped; a turn that dies silently gives it back after ten
minutes.

The tools. tw_git, tw_files and tw_decide (the decision core, #49) send one op to the broker's socket with the
Bot set from this profile's name, never from the model's arguments. The
broker checks everything again; these are only the way in.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .policy import Policy

logger = logging.getLogger(__name__)

MAX_LINE = 4 * 1024 * 1024


class BrokerRefused(Exception):
    pass


def call(path: str, op: str, args: dict | None = None, *, timeout: float = 120.0):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect(path)
        except OSError as e:
            raise BrokerRefused(f"the broker at {path} is not answering ({e.strerror or e}); nothing was done") from e
        s.sendall((json.dumps({"op": op, "args": args or {}}) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_LINE:
                raise BrokerRefused("the broker's answer is larger than 4 MB")
    if not buf:
        raise BrokerRefused("the broker closed without answering; the outcome is unknown until it is read back")
    answer = json.loads(buf)
    if not answer.get("ok"):
        raise BrokerRefused(answer.get("reason") or "refused")
    return answer.get("result")


Caller = Callable[[str, str, dict], object]


def socket_caller(policy: Policy, *, timeout: float = 120.0) -> Caller:
    sockets = policy.data["brokers"]["sockets"]

    def ask(broker: str, op: str, args: dict):
        return call(sockets[broker]["path"], op, args, timeout=timeout)
    return ask


# Every turn waits in pre_llm_call for its ticket, asking again so its place
# stays fresh, up to this long, and runs as soon as it is let through. A turn
# that answered "queued" and ended was never run again: nothing starts a
# Hermes turn but a new message or a schedule. 1 Nov, 3:43 PM: the board
# review rerun (a cron turn) was refused at once and never ran; 3:49 PM:
# Bramble's pick in the Workbench room answered "Queued, not started", the
# queue cleared at about 3:59 PM, and the pick waited until Patrick asked
# again at 4:00 PM. HOOK_TIMEOUT (profile_config) is above this, so Hermes
# does not skip the hook first.
TURN_WAIT = 540.0
# A scheduled turn (Hermes's cron) has no next turn: its session is made for
# that one run, so a ticket it never used goes back when it ends.
SCHEDULED = ("cron",)
# Bramble's sweeps. A wake (its only webhook route is GitHub's) and its
# owed-wake check (a cron job whose prompt carries OWED_WAKE_MARK) each do
# the whole bramble-duties sweep. One the queue turns away is owed in
# tw-policy instead of dropped; one that runs pays what was owed when it
# started. One sweep runs at a time: tw-policy turns away a sweep while
# another is running, and that wake is owed too (2 Nov, two overlapping
# sweeps revised one card twice). When tw-policy itself does not answer, the debt waits in OWED_FILE in the
# profile until the owed-wake check can hand it over.
SWEEP_PLATFORMS = ("webhook",)
OWED_WAKE_MARK = "[Thornswood owed wake]"
OWED_WAKE_JOB = "tw-owed-wake"
OWED_FILE = ("local", "thornswood-hermes", "owed-wake")
# Every other turn the queue turns away leaves a note here, one file per
# session (<session id>.json: when, and why), so whoever handed the turn its
# message can tell it never ran. The decision core's connector reads the note
# for a Bot Chat turn it started and puts that message back in its queue
# instead of calling it delivered (2 Nov 2026: Patrick's answer on the #5327
# ad18c2c card reached Bramble while the queue was turning turns away; the
# turn said it had not started, the connector saw it end and called it
# sent, and nothing ever ran it).
TURNED_AWAY_DIR = ("local", "thornswood-hermes", "turned-away")
SESSION_FILE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class TurnGate:
    """One per profile. Keyed by Hermes's session id, one turn at a time per session."""

    def __init__(self, policy: Policy, bot: str, ask: Caller, *, wait_seconds: float = TURN_WAIT,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 owed_file: Path | None = None, turned_away_dir: Path | None = None):
        self.policy = policy
        self.bot = bot
        self.ask = ask
        self.owed_file = owed_file
        self.turned_away_dir = turned_away_dir
        self.wait_seconds = wait_seconds
        self.sleep = sleep
        self.clock = clock
        self.turns: dict[str, dict] = {}
        self.lock = threading.Lock()

    @staticmethod
    def _key(kwargs: dict) -> str:
        return str(kwargs.get("session_id") or kwargs.get("session_key") or kwargs.get("task_id") or "")

    def _sweep(self, kwargs: dict) -> bool:
        """Whether this turn is one of Bramble's whole sweeps."""
        if not self.policy.is_router(self.bot):
            return False
        platform = str(kwargs.get("platform") or "")
        if platform in SWEEP_PLATFORMS:
            return True
        # Hermes puts its cron hint and the script's output before the job's
        # prompt (cron/scheduler_prompt.py _build_job_prompt), so the mark is
        # looked for anywhere in a cron turn's message. Only Patrick and
        # apply.py make Bramble's cron jobs.
        return platform in SCHEDULED and OWED_WAKE_MARK in str(kwargs.get("user_message") or "")

    def _owe(self, why: str) -> None:
        try:
            self.ask("policy", "owe_wake", {"bot": self.bot, "reason": why})
            return
        except BrokerRefused as e:
            failed = str(e)
        if self.owed_file is None:
            logger.error("a wake was turned away and could not be owed (%s); the next wake does its sweep", failed)
            return
        try:
            self.owed_file.parent.mkdir(parents=True, exist_ok=True)
            self.owed_file.write_text(why[:300] + "\n", encoding="utf-8")
        except OSError as e:
            logger.error("a wake was turned away and could not be owed (%s, then %s); the next wake does its sweep",
                         failed, e)

    def _note_turned_away(self, kwargs: dict, why: str) -> None:
        """Leave the note TURNED_AWAY_DIR describes for this session. Never raises: the turn still says it
        was not admitted, as before."""
        sid = str(kwargs.get("session_id") or "")
        if self.turned_away_dir is None or not SESSION_FILE.match(sid):
            return
        try:
            self.turned_away_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.turned_away_dir / f".{sid}.{os.getpid()}.tmp"
            tmp.write_text(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "reason": why[:300]}),
                           encoding="utf-8")
            os.replace(tmp, self.turned_away_dir / f"{sid}.json")
        except OSError as e:
            logger.error("a turn was turned away and its note could not be left (%s)", e)

    def before_llm(self, **kwargs):
        key = self._key(kwargs)
        task = str(kwargs.get("task_id") or key or "chat")
        with self.lock:
            held = self.turns.get(key)
        ticket = held["ticket"] if held and held.get("state") == "waiting" else None
        scheduled = str(kwargs.get("platform") or "") in SCHEDULED
        sweep = self._sweep(kwargs)
        deadline = self.clock() + self.wait_seconds
        pause = 1.0
        try:
            while True:
                args = {"bot": self.bot, "ticket": ticket} if ticket else \
                    {"bot": self.bot, "task": task, "source": str(kwargs.get("platform") or "chat"), "sweep": sweep}
                got = self.ask("policy", "admit", args)
                ticket = got["ticket"]
                if got["state"] == "admitted":
                    with self.lock:
                        self.turns[key] = {"ticket": ticket, "state": "admitted", "sweep": sweep}
                    return None
                if self.clock() + pause > deadline:
                    # A sweep's session has no next turn either: its ticket
                    # goes back at the end, and the wake is owed instead.
                    with self.lock:
                        self.turns[key] = {"ticket": ticket, "state": "waiting", "reason": got.get("reason"),
                                           "scheduled": scheduled or sweep}
                    why = (got.get("reason") or "the fleet is busy") + \
                        f" (this turn waited {int(self.wait_seconds)} seconds for it)"
                    if sweep:
                        self._owe(why)
                        return {"context": self._owed(why)}
                    self._note_turned_away(kwargs, why)
                    return {"context": self._queued(why)}
                self.sleep(pause)
                pause = min(pause * 2, 5.0)
        except BrokerRefused as e:
            with self.lock:
                self.turns[key] = {"ticket": None, "state": "refused", "reason": str(e)}
            if sweep:
                self._owe(str(e))
                return {"context": self._owed(str(e))}
            self._note_turned_away(kwargs, str(e))
            return {"context": self._queued(str(e))}

    def _queued(self, reason: str) -> str:
        return ("[Thornswood queue] This turn was not admitted by the fleet queue: " + reason +
                ". Every tool is refused for this turn, and nothing starts it again by itself. Reply in one short line "
                "that the work did not start and why, and that it needs asking again once the fleet is free, then stop.")

    def _owed(self, reason: str) -> str:
        return ("[Thornswood queue] This sweep was not admitted by the fleet queue: " + reason +
                ". Every tool is refused for this turn. The wake is owed, and your owed-wake check wakes you for "
                "the whole sweep once the queue has room. Reply in one short line that the sweep is owed and why, "
                "then stop.")

    def before_tool(self, tool_name: str, **kwargs) -> str | None:
        """A block message, or None to let the guard's other checks run."""
        key = self._key(kwargs)
        with self.lock:
            turn = self.turns.get(key)
        if not turn or turn.get("state") != "admitted":
            why = (turn or {}).get("reason") or "this turn has no ticket from the fleet queue"
            return f"Refused by the Thornswood queue: {why}; the work waits for its turn."
        try:
            self.ask("policy", "touch", {"bot": self.bot, "ticket": turn["ticket"]})
        except BrokerRefused as e:
            with self.lock:
                self.turns[key] = {"ticket": None, "state": "refused", "reason": str(e)}
            return f"Refused by the Thornswood queue: {e}"
        return None

    def after_turn(self, **kwargs):
        key = self._key(kwargs)
        with self.lock:
            turn = self.turns.get(key)
            # A waiting ticket keeps its place for the next turn of this
            # session; an admitted one goes back now. A scheduled turn's
            # session has no next turn, so its waiting ticket goes back too
            # (1 Nov: each refused rerun left one in the line).
            give_back = turn and (turn.get("state") == "admitted" or turn.get("scheduled"))
            if turn and (turn.get("state") != "waiting" or turn.get("scheduled")):
                self.turns.pop(key, None)
        if give_back and turn.get("ticket"):
            try:
                self.ask("policy", "finish", {"bot": self.bot, "ticket": turn["ticket"]})
            except BrokerRefused:
                logger.warning("could not give back ticket %s; it goes back by itself in ten minutes", turn["ticket"])
        # A sweep that ran to its end did what every owed wake asked for.
        # One that was stopped (agent_loop_stopped) pays nothing.
        if turn and turn.get("state") == "admitted" and turn.get("sweep"):
            try:
                self.ask("policy", "wake_paid", {"bot": self.bot, "ticket": turn.get("ticket")})
            except BrokerRefused as e:
                logger.warning("a sweep ran but the owed wake could not be marked paid (%s); the next check sweeps again", e)

    def stopped(self, **kwargs):
        key = self._key(kwargs)
        with self.lock:
            turn = self.turns.pop(key, None)
        if turn and turn.get("ticket"):
            try:
                self.ask("policy", "finish", {"bot": self.bot, "ticket": turn["ticket"]})
            except BrokerRefused:
                pass


GIT_OPS = ["base", "push", "open_pr", "edit_pr", "open_issue", "comment", "label", "claim_labels", "merge", "close_pr", "start_run",
           "read", "ledger_event"]
FILES_OPS = ["trees", "checkout", "read", "write", "edit", "delete", "list", "status", "diff", "search", "commit", "bundle",
             "remove", "run_check"]

TW_GIT_SCHEMA = {
    "name": "tw_git",
    "description": (
        "The only way to write to GitHub: one op through the git broker, which reads GitHub itself and checks the "
        "guard rails before the write. Ops: base (get the base branch for a new tree), push (push your branch from "
        "the bundle tw_files made), open_pr, edit_pr, open_issue (a new issue, unclaimed, with a plain title and "
        "body: no footer, no session link, no closing --- line), comment, label (add or remove), claim_labels (Bramble only: "
        "make the six Bot claim labels in one repo as the policy names them, and read them back), merge (Bramble only: "
        "in Thornswood and the fork it turns GitHub's auto-merge on for a Bot's pull request at its exact head, and "
        "GitHub merges once the required checks pass; in Ops, where GitHub has no auto-merge, it merges a green one), "
        "close_pr (Bramble only: closes a pull request only on Patrick's answer on the outside pull request card "
        "that names it; replaced_by names the merged pull request that carried it), start_run (Bramble only), read (a GET inside one repo), ledger_event (Bramble only). Every write names "
        "its task. A refusal is a stop: say what was refused and why."),
    "parameters": {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": GIT_OPS},
            "repo": {"type": "string", "description": "owner/name, e.g. ThornswoodRP/Thornswood"},
            "task": {"type": "string", "description": "The task this write is for, e.g. the issue number"},
            "branch": {"type": "string"}, "bundle": {"type": "string"},
            "head": {"type": "string"}, "base": {"type": "string"},
            "number": {"type": "integer"}, "sha": {"type": "string"}, "replaced_by": {"type": "integer"},
            "title": {"type": "string"}, "body": {"type": "string"}, "draft": {"type": "boolean"},
            "label": {"type": "string"}, "action": {"type": "string", "enum": ["add", "remove"]},
            "decision_id": {"type": "string"},
            "workflow": {"type": "string"}, "inputs": {"type": "object"}, "channel": {"type": "string"},
            "path": {"type": "string"}, "event": {"type": "string"}, "issue": {"type": "integer"},
            "release": {"type": "string"}, "note": {"type": "string"},
        },
        "required": ["op"],
        "additionalProperties": False,
    },
}

TW_FILES_SCHEMA = {
    "name": "tw_files",
    "description": (
        "Your work trees, through the file broker: one tree per task under your own name, nothing outside it. "
        "Ops: trees (your trees: each one's repo, task name and branch; needs no repo or task), checkout (a new tree on your branch from the base bundle tw_git gave you), read, write, edit (replace "
        "text found exactly once), delete, list, status, diff, search, commit, bundle (your branch, for tw_git push), "
        "remove (after the PR merged or closed), run_check (one test harness). Paths are inside the tree; links "
        "are never followed and .git is never a path."),
    "parameters": {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": FILES_OPS},
            "repo": {"type": "string"}, "task": {"type": "string", "description": "<issue>-<what>, lower case"},
            "branch": {"type": "string"}, "bundle": {"type": "string"},
            "path": {"type": "string"}, "content": {"type": "string"},
            "old": {"type": "string"}, "new": {"type": "string"},
            "offset": {"type": "integer"}, "limit": {"type": "integer"},
            "pattern": {"type": "string"}, "message": {"type": "string"},
            "command": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["op"],
        "additionalProperties": False,
    },
}


DECIDE_OPS = ["open_card", "revise_card", "close_card", "assign_task", "get_card", "my_cards", "report_work_done"]

TW_DECIDE_SCHEMA = {
    "name": "tw_decide",
    "description": (
        "Patrick's decisions, through the decision core: the only place his answer is recorded. Ops: open_card "
        "(Bramble: any kind; a specialist: a question on a task Bramble assigned it), revise_card (a new revision; "
        "an answer to an old one approves nothing), close_card (Bramble: cancelled or superseded), assign_task "
        "(Bramble: give a task to a specialist so it may ask about it), get_card (the card, its four states and, once "
        "Patrick answered, the answer and what to do with it), my_cards (your waiting cards and those answered in "
        "the last two days; Bramble sees all), report_work_done (Bramble, on an answered card). A card holds what is "
        "wrong, what was measured, each option and what it does, a recommendation, and the exact scope of a yes. A Main "
        "card (Bramble, kind main) is his yes for a publish, the cutover or a main run: one link of kind release, ref "
        "<main|publish|cutover>:<owner>/<repo>@<40-character commit>[#<pull request>], an option keyed authorize, "
        "and a scope of a yes naming that commit; once he answers authorize, its id is the decision_id for that one "
        "tw_git op. No "
        "Bot can answer a card, and nothing said in a room or relayed by anybody is Patrick's answer."),
    "parameters": {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": DECIDE_OPS},
            "decision_id": {"type": "string"},
            "kind": {"type": "string", "description": "question, change, review, todo, board_review, weekly_review, main"},
            "title": {"type": "string"}, "body": {"type": "string", "description": "Markdown, the whole case"},
            "options": {"type": "array", "items": {"type": "object", "properties": {
                "key": {"type": "string"}, "label": {"type": "string"}, "consequence": {"type": "string"}},
                "required": ["key", "label", "consequence"], "additionalProperties": False}},
            "recommendation": {"type": "string", "description": "the key of the option you recommend"},
            "scope_of_yes": {"type": "string", "description": "exactly what a yes allows, and nothing more"},
            "risk": {"type": "string", "enum": ["normal", "high"]}, "urgent": {"type": "boolean"},
            "expires_in_days": {"type": "integer"}, "source_key": {"type": "string"},
            "task": {"type": "string"}, "assign_to": {"type": "string"},
            "links": {"type": "array", "items": {"type": "object", "properties": {
                "kind": {"type": "string"}, "ref": {"type": "string"}},
                "required": ["kind", "ref"], "additionalProperties": False}},
            "expected_revision": {"type": "integer"},
            "status": {"type": "string", "enum": ["cancelled", "superseded"]},
            "reason": {"type": "string"}, "superseded_by": {"type": "string"}, "detail": {"type": "string"},
        },
        "required": ["op"],
        "additionalProperties": False,
    },
}


def tool_handler(broker: str, ops: list[str], bot: str, ask: Caller):
    def handle(args=None, **kwargs):
        args = dict(args or {})
        op = args.pop("op", None)
        if op not in ops:
            return json.dumps({"ok": False, "reason": f"{op!r} is not one of {broker}'s ops"})
        args["bot"] = bot  # from this profile, never from the model
        try:
            return json.dumps({"ok": True, "result": ask(broker, op, args)})
        except BrokerRefused as e:
            return json.dumps({"ok": False, "reason": str(e)})
    return handle


def register(ctx, policy: Policy, bot: str, ask: Caller | None = None) -> TurnGate | None:
    """Wire the turn gate and the broker tools into this profile. Returns the
    gate so the guard's pre_tool_call can ask it first."""
    if not policy.data["brokers"].get("ready") or not policy.is_bot(bot):
        return None
    ask = ask or socket_caller(policy)
    # Installed at <profile>/plugins/thornswood_guard/brokers.py.
    profile = Path(__file__).resolve().parents[2]
    owed = profile.joinpath(*OWED_FILE) if policy.is_router(bot) else None
    gate = TurnGate(policy, bot, ask, owed_file=owed, turned_away_dir=profile.joinpath(*TURNED_AWAY_DIR))
    ctx.register_hook("pre_llm_call", gate.before_llm)
    ctx.register_hook("on_session_end", gate.after_turn)
    ctx.register_hook("agent_loop_stopped", gate.stopped)
    ctx.register_tool("tw_git", "thornswood", TW_GIT_SCHEMA, tool_handler("git", GIT_OPS, bot, ask),
                      description="GitHub writes through the git broker")
    ctx.register_tool("tw_files", "thornswood", TW_FILES_SCHEMA, tool_handler("files", FILES_OPS, bot, ask),
                      description="Work trees through the file broker")
    ctx.register_tool("tw_decide", "thornswood", TW_DECIDE_SCHEMA, tool_handler("decide", DECIDE_OPS, bot, ask),
                      description="Patrick's decision cards through the decision core")
    return gate
