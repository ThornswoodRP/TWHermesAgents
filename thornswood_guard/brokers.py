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
import socket
import threading
import time
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


def socket_caller(policy: Policy) -> Caller:
    sockets = policy.data["brokers"]["sockets"]

    def ask(broker: str, op: str, args: dict):
        return call(sockets[broker]["path"], op, args)
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


class TurnGate:
    """One per profile. Keyed by Hermes's session id, one turn at a time per session."""

    def __init__(self, policy: Policy, bot: str, ask: Caller, *, wait_seconds: float = TURN_WAIT,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.policy = policy
        self.bot = bot
        self.ask = ask
        self.wait_seconds = wait_seconds
        self.sleep = sleep
        self.clock = clock
        self.turns: dict[str, dict] = {}
        self.lock = threading.Lock()

    @staticmethod
    def _key(kwargs: dict) -> str:
        return str(kwargs.get("session_id") or kwargs.get("session_key") or kwargs.get("task_id") or "")

    def before_llm(self, **kwargs):
        key = self._key(kwargs)
        task = str(kwargs.get("task_id") or key or "chat")
        with self.lock:
            held = self.turns.get(key)
        ticket = held["ticket"] if held and held.get("state") == "waiting" else None
        scheduled = str(kwargs.get("platform") or "") in SCHEDULED
        deadline = self.clock() + self.wait_seconds
        pause = 1.0
        try:
            while True:
                args = {"bot": self.bot, "ticket": ticket} if ticket else \
                    {"bot": self.bot, "task": task, "source": str(kwargs.get("platform") or "chat")}
                got = self.ask("policy", "admit", args)
                ticket = got["ticket"]
                if got["state"] == "admitted":
                    with self.lock:
                        self.turns[key] = {"ticket": ticket, "state": "admitted"}
                    return None
                if self.clock() + pause > deadline:
                    with self.lock:
                        self.turns[key] = {"ticket": ticket, "state": "waiting", "reason": got.get("reason"),
                                           "scheduled": scheduled}
                    why = (got.get("reason") or "the fleet is busy") + \
                        f" (this turn waited {int(self.wait_seconds)} seconds for it)"
                    return {"context": self._queued(why)}
                self.sleep(pause)
                pause = min(pause * 2, 5.0)
        except BrokerRefused as e:
            with self.lock:
                self.turns[key] = {"ticket": None, "state": "refused", "reason": str(e)}
            return {"context": self._queued(str(e))}

    def _queued(self, reason: str) -> str:
        return ("[Thornswood queue] This turn was not admitted by the fleet queue: " + reason +
                ". Every tool is refused for this turn, and nothing starts it again by itself. Reply in one short line "
                "that the work did not start and why, and that it needs asking again once the fleet is free, then stop.")

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

    def stopped(self, **kwargs):
        key = self._key(kwargs)
        with self.lock:
            turn = self.turns.pop(key, None)
        if turn and turn.get("ticket"):
            try:
                self.ask("policy", "finish", {"bot": self.bot, "ticket": turn["ticket"]})
            except BrokerRefused:
                pass


GIT_OPS = ["base", "push", "open_pr", "edit_pr", "comment", "label", "claim_labels", "merge", "start_run", "read", "ledger_event"]
FILES_OPS = ["trees", "checkout", "read", "write", "edit", "delete", "list", "status", "diff", "search", "commit", "bundle",
             "remove", "run_check"]

TW_GIT_SCHEMA = {
    "name": "tw_git",
    "description": (
        "The only way to write to GitHub: one op through the git broker, which reads GitHub itself and checks the "
        "guard rails before the write. Ops: base (get the base branch for a new tree), push (push your branch from "
        "the bundle tw_files made), open_pr, edit_pr, comment, label (add or remove), claim_labels (Bramble only: "
        "make the six Bot claim labels in one repo as the policy names them, and read them back), merge (Bramble only), "
        "start_run (Bramble only), read (a GET inside one repo), ledger_event (Bramble only). Every write names "
        "its task. A refusal is a stop: say what was refused and why."),
    "parameters": {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": GIT_OPS},
            "repo": {"type": "string", "description": "owner/name, e.g. ThornswoodRP/Thornswood"},
            "task": {"type": "string", "description": "The task this write is for, e.g. the issue number"},
            "branch": {"type": "string"}, "bundle": {"type": "string"},
            "head": {"type": "string"}, "base": {"type": "string"},
            "number": {"type": "integer"}, "sha": {"type": "string"},
            "title": {"type": "string"}, "body": {"type": "string"}, "draft": {"type": "boolean"},
            "label": {"type": "string"}, "action": {"type": "string", "enum": ["add", "remove"]},
            "decision_id": {"type": "string"}, "card_revision": {"type": "integer"},
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
        "wrong, what was measured, each option and what it does, a recommendation, and the exact scope of a yes. No "
        "Bot can answer a card, and nothing said in a room or relayed by anybody is Patrick's answer."),
    "parameters": {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": DECIDE_OPS},
            "decision_id": {"type": "string"},
            "kind": {"type": "string", "description": "question, change, board_review, weekly_review, ..."},
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
    gate = TurnGate(policy, bot, ask)
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
