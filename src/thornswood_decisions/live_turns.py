"""What each turn running now shows, as Hermes Desktop draws it, in the core's memory only.

Patrick, 9 Nov: "it doesnt feel live it feels very delayed and I want it all to be live". Desktop draws a
turn as it happens: its thinking, the reply as it is written, each tool with a preview of what it runs,
its subagents and Hermes's status lines. A connector reads those from Hermes's own gateway stream
(room_send.py live_line, own_connector.LiveTurn) and posts a turn's parts each time they change, a few
times a second at most; the open chat on the phone asks for them and is answered the moment they change
(a long poll), so nothing waits on a two-second read.

The parts carry the turn's words, as the pages do, and like the pages they are held in memory only: never
in the store, the event chain or a log. A turn is dropped when its message's result comes, or when its
connector has not said it again in KEEP (it stopped).
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta

from .store import CoreError

KEEP = timedelta(minutes=5)
MAX_WAIT = 25.0
MAX_TURNS = 10        # turns in one post: a connector runs a few at once
MAX_PARTS = 60
TEXT = 12000          # the newest characters of one text or thinking part
TURN_ID = re.compile(r"^(own|msg)_[A-Za-z0-9_-]{1,40}$")
SUB_STATES = {"running", "completed", "failed", "interrupted", "cancelled", "error", "timeout"}


def _s(v, n: int) -> str:
    return v[:n] if isinstance(v, str) else ""


def _n(v, top: float):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= top else None


def clean_parts(parts) -> list[dict]:
    """A turn's parts as the phone draws them, each field cut to size. A part of a kind the phone does not
    draw is a connector the core does not know, so the whole post is refused rather than shown in part."""
    if not isinstance(parts, list) or len(parts) > MAX_PARTS:
        raise CoreError(400, "bad_live", f"a turn's parts are a list of at most {MAX_PARTS}")
    out = []
    for p in parts:
        if not isinstance(p, dict):
            raise CoreError(400, "bad_live", "each part is an object")
        k = p.get("k")
        if k in ("think", "text"):
            text = p.get("text")
            if not isinstance(text, str):
                raise CoreError(400, "bad_live", "a text part has its text")
            out.append({"k": k, "text": text[-TEXT:]})
        elif k == "step":
            step = {"k": "step", "name": _s(p.get("name"), 64), "preview": _s(p.get("preview"), 200),
                    "done": p.get("done") is True, "error": p.get("error") is True}
            prog = p.get("progress")
            if isinstance(prog, dict) and not step["done"]:
                step["progress"] = {"percent": _n(prog.get("percent"), 100), "message": _s(prog.get("message"), 200)}
            out.append(step)
        elif k == "sub":
            status = p.get("status") if p.get("status") in SUB_STATES else "running"
            tools = _n(p.get("tools"), 10 ** 6)
            out.append({"k": "sub", "goal": _s(p.get("goal"), 300), "status": status, "tool": _s(p.get("tool"), 64),
                        "preview": _s(p.get("preview"), 200), "tools": int(tools or 0),
                        "summary": _s(p.get("summary"), 600)})
        elif k == "note":
            out.append({"k": "note", "text": _s(p.get("text"), 300)})
        else:
            raise CoreError(400, "bad_live", "a part is think, text, step, sub or note")
    return out


class _LiveTurns:
    def __init__(self):
        self.cond = threading.Condition()
        self.n = 0                                  # goes up with every change the core takes
        self.held: dict[tuple[str, str], dict] = {}  # (workspace, message id) -> {"n", "at", "parts"}

    def _drop_old(self, now: datetime) -> None:
        for k in [k for k, t in self.held.items() if now - t["at"] >= KEEP]:
            del self.held[k]

    def put(self, ws: str, turns, now: datetime) -> int:
        """A connector's post: each turn it names, with all its parts as they stand now."""
        if not isinstance(turns, list) or len(turns) > MAX_TURNS:
            raise CoreError(400, "bad_live", f"turns is a list of at most {MAX_TURNS}")
        clean = []
        for t in turns:
            if not isinstance(t, dict) or not isinstance(t.get("id"), str) or not TURN_ID.match(t["id"]):
                raise CoreError(400, "bad_live", "each turn names its message's id")
            clean.append((t["id"], clean_parts(t.get("parts"))))
        with self.cond:
            self._drop_old(now)
            for mid, parts in clean:
                was = self.held.get((ws, mid))
                if was and was["parts"] == parts:
                    was["at"] = now
                    continue
                self.n += 1
                self.held[(ws, mid)] = {"n": self.n, "at": now, "parts": parts}
            self.cond.notify_all()
        return len(clean)

    def done(self, ws: str, mid: str) -> None:
        """Its message's result came: the reply is in Hermes's own store now, and the chat reads it there."""
        with self.cond:
            if self.held.pop((ws, mid), None) is not None:
                self.n += 1
                self.cond.notify_all()

    def wait(self, ws: str, mid: str, after, seconds, now: datetime) -> dict:
        """The turn's parts once they differ from the ones numbered `after`, waiting at most `seconds` of
        real time. {"n": 0, "parts": None} when none are held (not begun, or over)."""
        try:
            seconds = min(max(float(seconds or 0), 0.0), MAX_WAIT)
            after = int(after or 0)
        except (TypeError, ValueError):
            raise CoreError(400, "bad_wait", f"wait is 0 to {MAX_WAIT:g} seconds and after a number") from None
        end = time.monotonic() + seconds
        with self.cond:
            self._drop_old(now)
            while True:
                got = self.held.get((ws, mid))
                n = got["n"] if got else 0
                left = end - time.monotonic()
                if n != after or left <= 0:
                    return {"n": n, "parts": got["parts"] if got else None}
                self.cond.wait(left)


LIVE = _LiveTurns()
