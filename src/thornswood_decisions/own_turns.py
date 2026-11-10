"""Hermes's questions and approvals in one of Patrick's own chats, and Stop, from the app (9 Nov 2026).

Patrick, 9 Nov: "so I can fully switch to that versus the official Hermes Desktop app". Desktop shows a
turn's question (Hermes's clarify tool) and an approval for a flagged command as a prompt he answers, and it
has Stop. The app's turns used to go in as one-shot turns, which Hermes takes as having nobody to ask: its
question tool answered itself "make the most reasonable assumption", and a flagged command was refused.
Now each turn is attended (room_send.py `attended-chat`, contracts.json own_chat "asks"):

- the turn's process says each question or approval Hermes opens and settles; the connector passes the open
  ones on with what the turn is doing (`turns[].asks` in each claim), and the app shows them under the live
  line with Hermes's own choices;
- an answer or a Stop from the app is an order here, in memory only, handed to that connector in its next
  claim (`orders`), which writes it to that turn's process; the process makes the call `hermes chat`'s own
  /answer, /approve or /stop makes, on its own gateway connection.

An order is taken only for a message of his that is running now in that Hermes, only with a passkey sign-in
from the last 30 minutes (as a message to his own Hermes needs: an approval runs a command with his
Hermes's power), and an answer only to a prompt the connector said is open, with a choice it offered. Hermes
checks it again: a prompt no longer open, or a choice it did not offer, is refused there. No words of a
question or an answer go into the event chain. An order not handed out in 2 minutes is dropped: by then the
app shows the turn as it really is.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta

from .store import new_id
from .wakes import WAKES

KINDS = ("approval", "clarify")
PROMPT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
MAX_ASKS = 3
MAX_CHOICES = 12
CHOICE = 300
TEXT = 4000
ANSWER = 4000
ORDER_TTL = timedelta(minutes=2)
ORDER_STATUS = ("sent", "gone", "refused")


def _text(v, n: int) -> str | None:
    return v[:n] if isinstance(v, str) and v.strip() else None


def clean_asks(raw) -> list[dict]:
    """The open questions and approvals a connector said a turn has, in the shape the app takes."""
    out = []
    for a in raw if isinstance(raw, list) else []:
        if not (isinstance(a, dict) and a.get("kind") in KINDS and isinstance(a.get("prompt_id"), str)
                and PROMPT_ID.match(a["prompt_id"])) or len(out) >= MAX_ASKS:
            continue
        choices = [c[:CHOICE] for c in a.get("choices") or [] if isinstance(c, str) and c.strip()] \
            if isinstance(a.get("choices"), list) else []
        ask = {"prompt_id": a["prompt_id"], "kind": a["kind"], "choices": choices[:MAX_CHOICES]}
        if a["kind"] == "clarify":
            ask["question"] = _text(a.get("question"), TEXT) or ""
            ask["multi_select"] = a.get("multi_select") is True and bool(choices)
        else:
            ask["description"] = _text(a.get("description"), TEXT) or ""
            ask["command"] = _text(a.get("command"), TEXT) or ""
        out.append(ask)
    return out


def clean_order(raw) -> dict | None:
    """How the turn said his last order went."""
    if not isinstance(raw, dict) or raw.get("op") not in ("stop", "answer") or raw.get("status") not in ORDER_STATUS:
        return None
    out = {"op": raw["op"], "status": raw["status"]}
    if isinstance(raw.get("prompt_id"), str) and PROMPT_ID.match(raw["prompt_id"]):
        out["prompt_id"] = raw["prompt_id"]
    if isinstance(raw.get("reason"), str):
        out["reason"] = raw["reason"][:200]
    return out


def answer_value(ask: dict, value) -> str | None:
    """The answer Hermes takes for that prompt, or None when it is not one: an approval's choice is one it
    offered; a question's answer is one line of text, or for a question that takes several of its choices,
    a list of them, sent as Hermes's own JSON list (apps/desktop/src/store/clarify.ts)."""
    if ask["kind"] == "approval":
        return value if isinstance(value, str) and value in ask["choices"] else None
    if isinstance(value, list):
        if not (ask.get("multi_select") and value and len(value) == len(set(value))
                and all(isinstance(v, str) and v in ask["choices"] for v in value)):
            return None
        return json.dumps(value)
    if isinstance(value, str) and value.strip() and len(value) <= ANSWER:
        return value
    return None


class _Orders:
    """Orders waiting for their connector's next claim, in memory only."""

    def __init__(self):
        self.lock = threading.Lock()
        self.waiting: dict[tuple, list[dict]] = {}

    def add(self, ws: str, gw: str, message_id: str, op: str, prompt_id: str | None, value: str | None,
            now: datetime) -> dict:
        order = {"id": new_id("ord"), "message_id": message_id, "op": op, "at": now}
        if op == "answer":
            order |= {"prompt_id": prompt_id, "value": value}
        with self.lock:
            rows = [o for o in self.waiting.get((ws, gw), []) if now - o["at"] < ORDER_TTL
                    and not (o["message_id"] == message_id and o["op"] == op and o.get("prompt_id") == prompt_id)]
            rows.append(order)
            self.waiting[(ws, gw)] = rows
        WAKES.bump(ws, gw)
        return {"order": order["id"], "status": "queued"}

    def hand(self, ws: str, gw: str, now: datetime) -> list[dict]:
        with self.lock:
            rows = self.waiting.pop((ws, gw), [])
        return [{k: v for k, v in o.items() if k != "at"} for o in rows if now - o["at"] < ORDER_TTL]


ORDERS = _Orders()
