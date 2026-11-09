"""Model and thinking for one of Patrick's own chats, from the app's details pane (plan Part 6, 2 Nov).

The details pane beside an open chat (on a phone, the chat's own details sheet) shows what Hermes holds for
that chat and changes its model or thinking level, that chat only. That gateway's own connector
(own_connector.py) does both with Hermes's own calls, the ones Desktop's pickers and `/model` and `/reasoning`
in a chat make (room_send.py "control", contracts.json "control"):

- the read: Hermes's session.info for the chat (its model, thinking level and whether approvals are skipped
  in it), config.get busy (what a message into a running turn does) and model.options (what can be picked);
- a model: Hermes's session.mutate model, fenced to the chat's revision and never global. A model Hermes's
  own cost guard holds for a confirmation is not switched: there is no "switch anyway" here;
- thinking: Hermes's config.set reasoning for that chat, never global.

Only models one of his sign-ins (his Claude login, ChatGPT), a free model or a local one covers are offered
(plan Part 6, decision 4 A: no extra spend). The connector lists only those; the core refuses a pick its last
read did not offer; and the connector checks the pick again against Hermes's own list just before switching,
so a hand-made request cannot move a chat to a billed model.

How full the chat's context is and the tokens it used are shown too, read as Desktop's context meter reads
them (session.context_breakdown and session.usage). Approvals and the busy setting are shown, not changed,
here. Nothing here is kept on disk: a read lives 30 seconds and a change at most 2 minutes, in memory. A core
restart drops a change in flight, and the pane then shows what Hermes really holds; a change is never shown
as made until the connector read it back. A change needs a passkey sign-in from the last 30 minutes, as a
message to his own Hermes does.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta

from .store import CoreError, new_id

FRESH = timedelta(seconds=30)       # a read answers from memory this long
AGAIN = timedelta(seconds=20)       # an ask not answered is handed out again after this
CHANGE_TTL = timedelta(minutes=2)   # a change the connector never answered is given up after this
MAX_WAIT = 25.0
MAX_OFFERED = 80
LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
BUSY = ("steer", "queue", "interrupt")
CTL_ID = re.compile(r"^ctl_[A-Za-z0-9_-]{8,40}$")
MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")
PROVIDER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


def _text(v, n: int) -> str | None:
    return " ".join(v.split())[:n] or None if isinstance(v, str) else None


def clean_state(raw) -> dict:
    """What the connector read, in the shape the app takes, with anything odd left out."""
    if not isinstance(raw, dict):
        raise CoreError(400, "bad_control", "a control read is a JSON object")
    model, provider = raw.get("model"), raw.get("provider")
    offered = []
    for o in raw.get("offered") or [] if isinstance(raw.get("offered"), list) else []:
        if (isinstance(o, dict) and isinstance(o.get("model"), str) and MODEL.match(o["model"])
                and isinstance(o.get("provider"), str) and PROVIDER.match(o["provider"])
                and len(offered) < MAX_OFFERED):
            offered.append({"provider": o["provider"], "model": o["model"],
                            "name": _text(o.get("name"), 40) or o["provider"],
                            "why": o.get("why") if o.get("why") in ("subscription", "free", "local") else None})
    levels = [lv for lv in LEVELS if lv in (raw.get("levels") or [])]
    return {"model": model if isinstance(model, str) and MODEL.match(model) else None,
            "provider": provider if isinstance(provider, str) and PROVIDER.match(provider) else None,
            "reasoning": raw.get("reasoning") if raw.get("reasoning") in LEVELS else None,
            "levels": levels, "yolo": raw.get("yolo") is True,
            "busy": raw.get("busy") if raw.get("busy") in BUSY else None, "offered": offered,
            **_use(raw)}


def _count(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 10 ** 12 else None


def _use(raw: dict) -> dict:
    """How full the chat's context is and the tokens it used, as Desktop's context meter shows them
    (room_send.py _usage_read): each part null, with Hermes's reason when it gave one, if it is not known."""
    out = {}
    c = raw.get("context")
    used, most = (_count(c.get("context_used")), _count(c.get("context_max"))) if isinstance(c, dict) else (None, None)
    if used is not None and most:
        pct = _count(c.get("context_percent"))
        out["context"] = {"used": used, "max": most, "percent": min(pct, 100) if pct is not None
                          else min(round(used * 100 / most), 100), "estimated": c.get("context_estimated") is True}
    else:
        out["context"] = None
    u = raw.get("usage")
    counts = {k: _count(u.get(k)) for k in ("input", "output", "total", "calls")} if isinstance(u, dict) else {}
    if counts and None not in counts.values():
        out["usage"] = counts | {"compressions": _count(u.get("compressions"))}
    else:
        out["usage"] = None
    for part in ("context", "usage"):
        why = _text(raw.get(part + "_why"), 160)
        if out[part] is None and why:
            out[part + "_why"] = why
    return out


def wanted(state: dict | None, body) -> dict:
    """The change the phone asked for, checked against what the last read offered."""
    if not isinstance(body, dict):
        raise CoreError(400, "bad_control", "a change is a JSON object")
    pick, level = body.get("model"), body.get("reasoning")
    if pick is None and level is None:
        raise CoreError(400, "bad_control", "a change names a model, a thinking level or both")
    if state is None:
        raise CoreError(409, "control_unread", "this chat's settings have not been read yet; open its details again")
    out = {}
    if pick is not None:
        if not (isinstance(pick, dict) and isinstance(pick.get("model"), str) and isinstance(pick.get("provider"), str)):
            raise CoreError(400, "bad_model", "a model is its provider and its name")
        if not any(o["model"] == pick["model"] and o["provider"] == pick["provider"] for o in state["offered"]):
            raise CoreError(403, "model_not_offered", "that model is not one your sign-ins, a free model or a "
                            "local one covers, so this chat is not moved to it")
        out["model"] = {"provider": pick["provider"], "model": pick["model"]}
    if level is not None:
        if level not in LEVELS or (state["levels"] and level not in state["levels"]):
            raise CoreError(400, "bad_reasoning", "that thinking level is not one this chat's model takes")
        out["reasoning"] = level
    return out


class _Controls:
    """Per chat: the last read, the ask the connector has not answered, and the newest change and how it went."""

    def __init__(self):
        self.cond = threading.Condition()
        self.read: dict = {}      # (ws, gw, chat) -> (read_at, state)
        self.asked: dict = {}     # (ws, gw, chat) -> {"id", "set", "at", "handed"}
        self.changes: dict = {}   # (ws, gw, chat) -> {"id", "set", "status", "reason", "at", "key"}

    def _drop_old(self, now: datetime) -> None:
        for k in [k for k, a in self.asked.items() if now - a["at"] > CHANGE_TTL]:
            a = self.asked.pop(k)
            ch = self.changes.get(k)
            if ch and ch["id"] == a["id"] and ch["status"] == "queued":
                ch.update(status="lapsed", reason="the connector did not answer in 2 minutes; nothing was changed "
                          "that the pane did not read back")

    def state(self, key: tuple, now: datetime) -> dict:
        with self.cond:
            self._drop_old(now)
            got = self.read.get(key)
            ch = self.changes.get(key)
            return {"state": got[1] if got else None, "read_at": got[0].isoformat() if got else None,
                    "change": ({k: ch[k] for k in ("id", "set", "status", "reason")} if ch else None)}

    def ask(self, key: tuple, now: datetime) -> None:
        """A read, unless one is in hand or asked already (a change is a read too)."""
        with self.cond:
            self._drop_old(now)
            got = self.read.get(key)
            if key not in self.asked and not (got and now - got[0] < FRESH):
                self.asked[key] = {"id": new_id("ctl"), "set": None, "at": now, "handed": None}

    def wait(self, key: tuple, seconds: float, since) -> None:
        """Until a read newer than `since` is in, for at most `seconds` of real time."""
        end = time.monotonic() + seconds
        with self.cond:
            while True:
                got = self.read.get(key)
                if got and (since is None or got[0] > since):
                    return
                left = end - time.monotonic()
                if left <= 0:
                    return
                self.cond.wait(left)

    def change(self, key: tuple, want: dict, idem: str, now: datetime) -> dict:
        with self.cond:
            self._drop_old(now)
            ch = self.changes.get(key)
            if ch and ch["key"] == idem:
                if ch["set"] != want:
                    raise CoreError(409, "key_reused", "that idempotency key was used for a different change")
                return {"id": ch["id"], "status": ch["status"], "replayed": True}
            cid = new_id("ctl")
            # The newest change wins: an older one not handed out yet is replaced, never run after it.
            self.asked[key] = {"id": cid, "set": want, "at": now, "handed": None}
            self.changes[key] = {"id": cid, "set": want, "status": "queued", "reason": None, "at": now, "key": idem}
            return {"id": cid, "status": "queued"}

    def hand(self, ws: str, gw: str, now: datetime) -> list[dict]:
        with self.cond:
            self._drop_old(now)
            out = []
            for key, a in sorted(self.asked.items(), key=lambda kv: kv[1]["at"]):
                if key[0] != ws or key[1] != gw or len(out) >= 3 or (a["handed"] and now - a["handed"] < AGAIN):
                    continue
                a["handed"] = now
                out.append({"id": a["id"], "session_id": key[2], **({"set": a["set"]} if a["set"] else {})})
            return out

    def answer(self, ws: str, gw: str, body, now: datetime) -> dict:
        if not isinstance(body, dict) or not isinstance(body.get("id"), str) or not CTL_ID.match(body["id"]):
            raise CoreError(400, "bad_control", "a control answer names the ask it answers")
        state = clean_state(body.get("state")) if body.get("state") is not None else None
        with self.cond:
            key = next((k for k, a in self.asked.items() if a["id"] == body["id"] and k[0] == ws and k[1] == gw), None)
            if key is None:
                raise CoreError(409, "not_asked", "no control read or change with that id was asked of this gateway")
            a = self.asked.pop(key)
            if state is not None:
                self.read[key] = (now, state)
            ch = self.changes.get(key)
            if a["set"] and ch and ch["id"] == a["id"]:
                if body.get("applied") is True and state is not None:
                    # Taken; "deferred" when a running turn keeps its model and the switch lands after it.
                    why = _text(body.get("reason"), 200)
                    ch.update(status="deferred" if why else "done", reason=why)
                else:
                    ch.update(status="refused", reason=_text(body.get("reason"), 200) or "Hermes did not change it")
            self.cond.notify_all()
            return {"ok": True}


CONTROLS = _Controls()
