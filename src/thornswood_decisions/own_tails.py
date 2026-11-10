"""A subagent's own transcript, opened from the app's Tasks tab (#111).

The Tasks tab listed each subagent of his own Hermes and One with its goal and last tool, and a tap did
nothing, while Hermes Desktop opens a subagent's transcript from its status stack. Now a tap opens it, read
only, with what Desktop shows there: Hermes's subagent.tail for that chat and subagent (the child's own
transcript as it runs, cut to its newest part, and whether it was cut), asked again every 2 seconds while it
is open, as Desktop polls it (apps/desktop .../status-stack/subagent-transcript.tsx).

A read is asked of that Hermes's own connector (own_connector.py), which makes Hermes's own call through
room_send.py ("tail", contracts.json "tasks" tail). Only a subagent that Hermes listed in its last report is
read. A Bot's subagents are not read here: each Bot's chats stay with its own Board session.

Nothing here is kept on disk: a read lives 3 seconds and an ask at most 30, in memory. The transcript is the
child's words, so it is never written to the event chain either.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta

from .store import CoreError, new_id
from .wakes import WAKES

FRESH = timedelta(seconds=3)       # a read answers from memory this long
AGAIN = timedelta(seconds=10)      # an ask not answered is handed out again after this
ASK_TTL = timedelta(seconds=30)    # an ask nobody answered is given up after this
KEEP = timedelta(minutes=2)        # a read nobody asked for again is dropped after this
MAX_TEXT = 16384                   # Desktop shows the newest 16384 characters
MAX_WAIT = 10.0
ASK_ID = re.compile(r"^tail_[A-Za-z0-9_-]{8,40}$")


def clean(body) -> dict:
    """What the connector read, as the app takes it."""
    text = body.get("text") if isinstance(body.get("text"), str) else ""
    out = {"available": body.get("available") is True, "text": text[-MAX_TEXT:],
           "truncated": body.get("truncated") is True or len(text) > MAX_TEXT}
    why = body.get("reason")
    if isinstance(why, str) and why.strip() and not out["available"]:
        out["why"] = " ".join(why.split())[:200]
    return out


class _Tails:
    def __init__(self):
        self.cond = threading.Condition()
        self.read: dict[tuple, tuple] = {}   # (ws, gw, chat, sub) -> (read_at, tail)
        self.asked: dict[tuple, dict] = {}   # (ws, gw, chat, sub) -> {"id", "at", "handed"}

    def _drop_old(self, now: datetime) -> None:
        for k in [k for k, a in self.asked.items() if now - a["at"] > ASK_TTL]:
            del self.asked[k]
        for k in [k for k, (at, _) in self.read.items() if now - at > KEEP]:
            del self.read[k]

    def ask(self, key: tuple, now: datetime) -> None:
        """A read, unless a fresh one is in hand or one is asked already."""
        with self.cond:
            self._drop_old(now)
            got = self.read.get(key)
            if key not in self.asked and not (got and now - got[0] < FRESH):
                self.asked[key] = {"id": new_id("tail"), "at": now, "handed": None}
                WAKES.bump(key[0], key[1])

    def wait(self, key: tuple, seconds: float) -> None:
        """Until a read is in, for at most `seconds` of real time."""
        end = time.monotonic() + seconds
        with self.cond:
            while key not in self.read:
                left = end - time.monotonic()
                if left <= 0:
                    return
                self.cond.wait(left)

    def hand(self, ws: str, gw: str, now: datetime) -> list[dict]:
        """The reads to make, oldest first, at most 3; a read is safe to ask again, so one not answered in
        10 seconds is handed out again."""
        with self.cond:
            self._drop_old(now)
            out = []
            for (w, g, chat, sub), a in sorted(self.asked.items(), key=lambda kv: kv[1]["at"]):
                if w != ws or g != gw or len(out) >= 3 or (a["handed"] and now - a["handed"] < AGAIN):
                    continue
                a["handed"] = now
                out.append({"id": a["id"], "session_id": chat, "subagent_id": sub})
            return out

    def answer(self, ws: str, gw: str, body, now: datetime) -> dict:
        if not isinstance(body, dict) or not isinstance(body.get("id"), str) or not ASK_ID.match(body["id"]):
            raise CoreError(400, "bad_tail", "a transcript answer names the read it answers")
        with self.cond:
            key = next((k for k, a in self.asked.items() if a["id"] == body["id"] and k[:2] == (ws, gw)), None)
            if key is None:
                raise CoreError(409, "not_asked", "no transcript read with that id was asked of this gateway")
            del self.asked[key]
            self.read[key] = (now, clean(body))
            self.cond.notify_all()
        return {"ok": True}

    def state(self, key: tuple) -> dict | None:
        with self.cond:
            got = self.read.get(key)
            return {"at": got[0].isoformat(), **got[1]} if got else None


TAILS = _Tails()
