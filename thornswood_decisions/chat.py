"""Patrick's chat with the six Bots and the rooms, from the phone (07 step 4).

A room is one of the group chats this workspace is bound to (`bind-room`):
Thornswood Workbench now, and any later one. Its chat target is its room id;
"room" is the first name for the first room bound, kept so nothing written
before the Rooms list stops working.

The phone writes a message here; the core keeps it in `chat_messages`, queued,
in the same commit as its command record, so a retry with the same key gets the
same receipt and never a second message. The connector (`connector.py`), which
runs on the VM as the Bots' `hermes` user, leases one message per Bot (or the
room) at a time, hands it to Hermes the way Hermes itself does, and says what
came of it:

- sent: Hermes took it;
- dead: it was not sent, and Patrick sees why and may send it again himself;
- uncertain: Hermes may have it. It is never sent again by itself (07 section
  11.1, "an unclear effect blocks automatic retry"). A room message is settled
  later from the room itself, because the room's event id comes from the key
  the core gave it; a Bot Chat message stays unclear and the phone says so.
- not_ready (an outcome, not a state): the gateway was not ready (starting,
  stopped, reloading), or the connector's own Hermes helper failed and said
  so (its reason begins with HELPER_FAILED), so nothing was sent. The
  message goes back to queued as it was, its lease and that attempt undone, and
  the phone says what it waits for. A gateway that is not ready yet is not a
  failure of the message, so it is never dead for that.

The same lease carries the connector's other job: telling the Bot that asked a
card how Patrick answered it (`connector_outbox`, written by the answer's own
commit in `core.py`). Its outcome is an effect receipt, and a confirmed one is
the "Bot told" state.

A chat message answers a card only when Patrick says so on the phone (Pixel
check, 2 Nov: "A chat is never an answer to a card" was wrong, "it should be").
A Bot's Bot Chat shows the cards that Bot asked and that wait on him. When his
words name one of a card's options (its label or its key, at the start of what
he wrote), the app shows that the message answers that card and with which
option, and he can take that off before sending. A message sent with it is that
answer: `DecisionCore.answer` takes it with every check a tap has (the exact
revision he saw, the card still waiting and not expired, the option on that
revision, and a passkey sign-in from the last minutes for a high-risk card), and
his words are the answer's note. The Bot is told the answer as for a tap, and
gets his words as a chat message as well. The core never reads an answer out
of a message by itself, and a Bot's words never answer anything. Text goes to
Hermes and to the phone, never into the event chain or a log.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .auth import Principal
from .core import DecisionCore
from .feeds import Feeds, room_events
from .own import DOING_STATES, PAGE_TEXT, PAGES, TOOL_NAME, _page_mid, clean_page, fetch_page, page_kind
from .reactions import Reactions
from .reactions import emoji as _emoji
from .tasks import Tasks
from .reactions import message_id as _message_id
from .store import CoreError, Store, canonical, new_id, parse, sha256_hex

MAX_TEXT = 8000
# The cards whose answer settles something, so DECIDED.md gets its line (plan line 39,
# q-p18-decided-auto). A review, a to-do or a board or weekly review approves one step
# and settles nothing that outlives it.
SETTLES = ("question", "change")
MOUNTAIN = ZoneInfo("America/Denver")  # Patrick's dates, as DECIDED.md writes them
LEASE = timedelta(minutes=20)
ROOM = "room"  # the first room bound, by its first name
OUTCOMES = {"confirmed": "sent", "failed": "dead", "uncertain": "uncertain", "not_ready": "queued"}
WAITS = "waiting for the Bots' gateway: "  # the reason a message put back by not_ready carries
# ...unless what it waits for is the connector's own Hermes helper, which failed (connector.py
# HELPER_FAILED): then the reason is the helper's failure as the connector said it, never the gateway.
HELPER_FAILED = "the connector's Hermes helper"
MINE = 50
MAX_CLAIM = 7  # the six Bots and the rooms, at most this many at once
ROOM_PAGE = 50
ROOM_KINDS = ("message.user", "message.member")
DOING_ID = re.compile(r"^msg_[A-Za-z0-9_-]{1,40}$")
DOING_STALE = timedelta(seconds=60)  # the connector says it at least every 20 s while a turn runs
PAGES_GW = "bots"   # the Bots' gateway, in the page store and the reactions beside Patrick's own two
# Each Bot's gateway state, as the Bots' connector last read it from Hermes (room_send.py op states), for the
# phone's online dots. Held in the core's memory only, like the pages: it is what is true now, and after a
# restart the phone says "not heard from" until the connector's next report, half a minute at most.
GATEWAY_STATES = ("absent", "starting", "ready", "draining", "incompatible", "inaccessible", "conflict",
                  "check_failed")  # Hermes's RuntimeState (gateway/runtime_contract.py), and a check that raised
LIVE_STALE = timedelta(seconds=120)  # four of the connector's reports missed: it is not heard from
_LIVE: dict[str, dict] = {}
_LIVE_LOCK = threading.Lock()


def clean_room_page(body) -> dict:
    """Only what the phone shows of one page of a room, oldest first by the room's own sequence."""
    if not isinstance(body, dict) or not isinstance(body.get("more"), bool):
        raise CoreError(400, "bad_page", "a page is a JSON object with more true or false")
    events = body.get("events")
    if not isinstance(events, list) or len(events) > ROOM_PAGE:
        raise CoreError(400, "bad_page", f"a page of a room has at most {ROOM_PAGE} events")
    out, last = [], None
    for e in events:
        seq = e.get("seq") if isinstance(e, dict) else None
        if not isinstance(seq, int) or isinstance(seq, bool) or not 0 < seq < 2 ** 63 or (last and seq <= last):
            raise CoreError(400, "bad_page", "each event has the room's own sequence number, oldest first")
        last = seq
        if e.get("kind") not in ROOM_KINDS:
            raise CoreError(400, "bad_page", "a page of a room holds only its messages")
        out.append({"seq": seq, "kind": e["kind"], "actor": str(e.get("actor") or "")[:80],
                    "text": str(e.get("text") or "")[:PAGE_TEXT], "at": str(e.get("at") or "")[:40]})
    return {"events": out, "more": body["more"]}


def room_event_id(send_key: str) -> str:
    """The id the gateway gives a room message sent with this key (hosted_rooms.py:250 user_event_id)."""
    return "user:" + sha256_hex(send_key.strip())


def room_targets(db, workspace_id: str, room_id: str) -> tuple[str, ...]:
    """The chat targets a room's messages were kept under: its id, and "room" too for the first room bound."""
    first = db.execute("SELECT room_id FROM room_bindings WHERE workspace_id = ? ORDER BY bound_at, rowid LIMIT 1",
                       (workspace_id,)).fetchone()
    return (room_id, ROOM) if first and first[0] == room_id else (room_id,)


def settle_from_room(store: Store, db, workspace_id: str, room_id: str, events: list[dict]) -> int:
    """Inside the room feed's commit: a room message the room shows was sent, whatever the connector said."""
    seen = {e.get("event_id") for e in events}
    targets = room_targets(db, workspace_id, room_id)
    rows = db.execute(f"SELECT id, send_key FROM chat_messages WHERE workspace_id = ? "
                      f"AND target IN ({','.join('?' * len(targets))}) "
                      "AND status IN ('leased', 'uncertain', 'dead')", (workspace_id, *targets)).fetchall()
    n = 0
    for r in rows:
        if room_event_id(r["send_key"]) in seen:
            db.execute("UPDATE chat_messages SET status = 'sent', lease_until = NULL, reason = ?, updated_at = ? "
                       "WHERE id = ?", ("seen in the room", store.now(), r["id"]))
            store.event(db, workspace_id, "chat.sent", r["id"], "room feed", {"seen_in_room": True})
            n += 1
    return n


class _Turns:
    """What each Bot Chat turn running now is doing, as the Bots' connector last said it (thinking, writing,
    or a tool by its plain name, from Hermes's own stream), in memory only, like the pages. Each post is
    the whole set of turns running now, so a turn that ended is gone with the next one; one not said
    again in DOING_STALE is gone too, so the phone never shows a line nobody is keeping current."""

    def __init__(self):
        self.lock = threading.Lock()
        self.said: dict[str, dict] = {}

    def put(self, ws: str, turns: list, now: datetime) -> int:
        kept = {}
        for t in turns[:10]:
            if (isinstance(t, dict) and isinstance(t.get("id"), str) and DOING_ID.match(t["id"])
                    and t.get("state") in DOING_STATES):
                tool = t.get("tool") if t["state"] == "tool" else None
                kept[t["id"]] = {"state": t["state"],
                                 "tool": tool if isinstance(tool, str) and TOOL_NAME.match(tool) else None}
        with self.lock:
            self.said[ws] = {"at": now, "turns": kept}
        return len(kept)

    def get(self, ws: str, item_id: str, now: datetime) -> dict | None:
        with self.lock:
            got = self.said.get(ws)
        if not got or now - got["at"] >= DOING_STALE:
            return None
        return got["turns"].get(item_id)

    def done(self, ws: str, item_id: str) -> None:
        with self.lock:
            if ws in self.said:
                self.said[ws]["turns"].pop(item_id, None)


TURNS = _Turns()


class Chat:
    def __init__(self, store: Store):
        self.s = store

    # ── who can be talked to ──────────────────────────────────────────────

    def bots(self, workspace_id: str) -> list[str]:
        from . import contracts
        order = contracts.load()["profiles"]["ids"]
        bound = {r[0] for r in self.s.db.execute("SELECT bot FROM bot_bindings WHERE workspace_id = ?",
                                                 (workspace_id,)).fetchall()}
        return [b for b in order if b in bound]

    def rooms(self, workspace_id: str) -> list[dict]:
        """The rooms this workspace is bound to, in the order they were bound."""
        rows = self.s.db.execute("SELECT room_id, name FROM room_bindings WHERE workspace_id = ? "
                                 "ORDER BY bound_at, rowid", (workspace_id,)).fetchall()
        return [{"room_id": r["room_id"], "name": r["name"]} for r in rows]

    def room_id(self, workspace_id: str) -> str | None:
        rooms = self.rooms(workspace_id)
        return rooms[0]["room_id"] if rooms else None

    def _target(self, workspace_id: str, target: str) -> str:
        rooms = [r["room_id"] for r in self.rooms(workspace_id)]
        if target == ROOM and rooms:
            return rooms[0]
        if target in rooms or target in self.bots(workspace_id):
            return target
        raise CoreError(404, "no_chat", "there is no such chat")

    @staticmethod
    def _patrick(p: Principal) -> None:
        if p.kind != "patrick":
            raise CoreError(403, "not_patrick", f"only Patrick chats from the app; {p.label} cannot")

    # ── Patrick's side ────────────────────────────────────────────────────

    def send(self, p: Principal, target: str, text, idempotency_key, answer=None) -> dict:
        """One message to a Bot or a room. With `answer` (a card this Bot asked, the option, and the
        revision, attention revision and artifact SHA-256 the phone showed), the message is also
        Patrick's answer to that card: it is taken first, by DecisionCore.answer with all its checks, and
        nothing is sent when it is refused."""
        self._patrick(p)
        if not self.s.flag(p.workspace_id, "mobile_chat_write"):
            raise CoreError(423, "chat_write_off", "chat from the phone is off for now")
        target = self._target(p.workspace_id, target)
        if not isinstance(text, str) or not text.strip():
            raise CoreError(400, "bad_text", "a message has some text")
        text = text.strip()
        if len(text) > MAX_TEXT:
            raise CoreError(400, "bad_text", f"a message is at most {MAX_TEXT} characters")
        if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 200:
            raise CoreError(400, "bad_key", "a message carries an idempotency key of 8 to 200 characters")
        receipt = None
        if answer is not None:
            if len(idempotency_key) > 192:
                raise CoreError(400, "bad_key", "a message that answers a card carries a key of at most 192 characters")
            receipt = self._answer(p, target, text, idempotency_key, answer)
        body_sha = sha256_hex(canonical({"target": target, "text": text}
                                        | ({"answer": receipt["response_id"]} if receipt else {})))
        with self.s.tx() as db:
            cmd = db.execute("SELECT op, body_sha256, result_json FROM commands WHERE workspace_id = ? "
                             "AND principal_id = ? AND idempotency_key = ?",
                             (p.workspace_id, p.id, idempotency_key)).fetchone()
            if cmd:
                if cmd["op"] != "chat.send" or cmd["body_sha256"] != body_sha:
                    raise CoreError(409, "key_reused", "that idempotency key was used for a different request")
                return json.loads(cmd["result_json"]) | {"replayed": True}
            now = self.s.now()
            mid = new_id("msg")
            db.execute("INSERT INTO chat_messages(id, workspace_id, target, text, send_key, principal_id, device_id, "
                       "status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,'queued',?,?)",
                       (mid, p.workspace_id, target, text, f"tw-chat.{mid}", p.id, p.device_id, now, now))
            self.s.event(db, p.workspace_id, "chat.queued", mid, "patrick", {"target": target, "device": p.device_id})
            out = {"message_id": mid, "target": target, "status": "queued", "created_at": now}
            if receipt:
                out["answered"] = receipt
            db.execute("INSERT INTO commands(workspace_id, principal_id, idempotency_key, op, body_sha256, "
                       "result_json, created_at) VALUES(?,?,?,?,?,?,?)",
                       (p.workspace_id, p.id, idempotency_key, "chat.send", body_sha, canonical(out), now))
        return out

    def _answer(self, p: Principal, target: str, text: str, idempotency_key: str, answer) -> dict:
        """The card answer a message carries: only a card the Bot of this chat asked, through the one
        answer path, with his words as its note. Its own key is the message's, so the same send again
        is the same answer again, never a second one."""
        if target not in self.bots(p.workspace_id):
            raise CoreError(400, "no_cards_here", "only a Bot's Bot Chat answers a card")
        if not isinstance(answer, dict) or not isinstance(answer.get("decision_id"), str):
            raise CoreError(400, "bad_answer", "an answer names the card, the option and the revision shown")
        core = DecisionCore(self.s)
        if core.asker(p.workspace_id, answer["decision_id"]) != target:
            raise CoreError(409, "not_this_bots_card", f"that card is not one {target.capitalize()} asked")
        ints = [answer.get(k) for k in ("revision", "attention_revision")]
        if not all(isinstance(i, int) and not isinstance(i, bool) for i in ints):
            raise CoreError(400, "bad_answer", "an answer names the revision shown")
        return core.answer(p, decision_id=answer["decision_id"], option_key=str(answer.get("option_key", "")),
                           revision=ints[0], attention_revision=ints[1],
                           artifact_sha256=str(answer.get("artifact_sha256", "")),
                           idempotency_key=idempotency_key + ".answer", note=text[:2000])

    def react(self, p: Principal, target: str, message_id, emoji) -> dict:
        """Patrick's reaction on one message of a Bot's Bot Chat: exactly this emoji, or none (null).

        It rides mobile_chat_write like a message to that Bot, and the connector sets it only while
        connector_effects is on, in that Bot's own store. Hermes keeps it on the message and tells
        the Bot about it on a later turn only when that profile has display.message_reactions on
        (contracts.json "reactions"). A room has no reactions in Hermes, so a room takes none."""
        self._patrick(p)
        if not self.s.flag(p.workspace_id, "mobile_chat_write"):
            raise CoreError(423, "chat_write_off", "chat from the phone is off for now")
        target = self._target(p.workspace_id, target)
        if target in {r["room_id"] for r in self.rooms(p.workspace_id)}:
            raise CoreError(400, "no_room_reactions", "a room has no reactions in Hermes")
        mid, wanted = _message_id(message_id), _emoji(emoji)
        return {"target": target} | Reactions(self.s).want(p, PAGES_GW, target, mid, wanted)

    def _targets(self, workspace_id: str, target: str) -> tuple[str, ...]:
        if target in {r["room_id"] for r in self.rooms(workspace_id)}:
            return room_targets(self.s.db, workspace_id, target)
        return (target,)

    def _mine(self, workspace_id: str, target: str) -> list[dict]:
        targets = self._targets(workspace_id, target)
        rows = self.s.db.execute("SELECT id, text, status, reason, created_at, updated_at FROM chat_messages "
                                 f"WHERE workspace_id = ? AND target IN ({','.join('?' * len(targets))}) "
                                 "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                                 (workspace_id, *targets, MINE)).fetchall()
        mine = [dict(r) for r in reversed(rows)]
        now = self.s.clock()
        for m in mine:
            if m["status"] == "queued" and (m["reason"] or "").startswith((WAITS, HELPER_FAILED)):
                m["waits"] = m["reason"]
            if m["status"] == "leased" and (doing := TURNS.get(workspace_id, m["id"], now)):
                m["doing"] = doing  # the live line under a message being answered
        return mine

    def _unsettled(self, workspace_id: str, target: str) -> int:
        targets = self._targets(workspace_id, target)
        return self.s.db.execute("SELECT count(*) FROM chat_messages WHERE workspace_id = ? "
                                 f"AND target IN ({','.join('?' * len(targets))}) "
                                 "AND status IN ('queued', 'leased', 'uncertain')",
                                 (workspace_id, *targets)).fetchone()[0]

    @staticmethod
    def _room_history(events: list[dict]) -> list[dict]:
        return [{"who": e["actor"] or "someone", "role": "user" if e["kind"] == "message.user" else "member",
                 "text": e["text"], "at": e["at"]} for e in events]

    def view(self, p: Principal, target: str) -> dict:
        self._patrick(p)
        target = self._target(p.workspace_id, target)
        feeds = Feeds(self.s)
        rooms = {r["room_id"]: r["name"] for r in self.rooms(p.workspace_id)}
        if target in rooms:
            snap = feeds.get(p.workspace_id, "room")
            history = self._room_history(room_events(snap["room"], target))
            kind, name = "room", rooms[target]
        else:
            snap = feeds.get(p.workspace_id, "bots")
            chat = next((b for b in (snap["bots"] or {}).get("bots", []) if b["bot"] == target), None)
            history = [{"who": "Patrick" if m["role"] == "user" else target.capitalize(), "role": m["role"],
                        "text": m["text"], "at": m["at"]} for m in (chat or {}).get("messages", [])]
            kind, name = "bot", target.capitalize()
        return {"target": target, "kind": kind, "name": name, "as_of": snap["as_of"], "history": history,
                "cards": DecisionCore(self.s).asked_by(p, target) if kind == "bot" else [],
                "mine": self._mine(p.workspace_id, target), "can_send": self.s.flag(p.workspace_id, "mobile_chat_write"),
                "reactions": Reactions(self.s).shown(p.workspace_id, PAGES_GW, target) if kind == "bot" else [],
                "live": self.live_states(p.workspace_id).get(target) if kind == "bot" else None}

    def summary(self, p: Principal) -> dict:
        self._patrick(p)
        feeds = Feeds(self.s)
        snap = feeds.get(p.workspace_id, "bots")
        chats = {b["bot"]: b for b in (snap["bots"] or {}).get("bots", [])}
        out = []
        live = self.live_states(p.workspace_id)
        for bot in self.bots(p.workspace_id):
            msgs = chats.get(bot, {}).get("messages", [])
            out.append({"bot": bot, "last": msgs[-1] if msgs else None,
                        "unsettled": self._unsettled(p.workspace_id, bot), "live": live.get(bot)})
        room_snap = feeds.get(p.workspace_id, "room")
        rooms = []
        for r in self.rooms(p.workspace_id):
            events = room_events(room_snap["room"], r["room_id"])
            last = events[-1] if events else None
            rooms.append({"room_id": r["room_id"], "name": r["name"],
                          "last": {"who": last["actor"] or "someone", "text": last["text"], "at": last["at"]}
                          if last else None, "unsettled": self._unsettled(p.workspace_id, r["room_id"])})
        return {"as_of": snap["as_of"], "bots": out, "rooms_as_of": room_snap["as_of"], "rooms": rooms,
                "room": bool(rooms)}

    # ── each Bot's gateway, for the phone's dots ──────────────────────────

    def live(self, workspace_id: str, token: str | None, body) -> dict:
        """The Bots' connector's report of each Bot's gateway state. Reading only, so it is taken while
        mobile_read is on, whatever connector_effects says. Answers with the Bots to check next time."""
        Feeds(self.s).check_token(workspace_id, "connector", token)
        if not self.s.flag(workspace_id, "mobile_read"):
            raise CoreError(423, "mobile_read_off", "the phone reads nothing while mobile_read is off")
        bots = self.bots(workspace_id)
        states = body.get("states") if isinstance(body, dict) else None
        if not isinstance(states, dict) or any(k not in bots or v not in GATEWAY_STATES for k, v in states.items()):
            raise CoreError(400, "bad_states", "states names bound Bots, each with one of Hermes's gateway states")
        why = body.get("why")
        if states or why is not None:
            if why is not None and not isinstance(why, str):
                raise CoreError(400, "bad_states", "why is words")
            with _LIVE_LOCK:
                _LIVE[workspace_id] = {"at": self.s.clock(), "states": dict(states), "why": (why or "")[:200]}
        return {"bots": bots}

    def tasks(self, workspace_id: str, token: str | None, body) -> dict:
        """The Bots' connector's report of what each Bot has going besides its chats (tasks.py). Reading only,
        so it is taken while mobile_read is on, whatever connector_effects says."""
        Feeds(self.s).check_token(workspace_id, "connector", token)
        if not self.s.flag(workspace_id, "mobile_read"):
            raise CoreError(423, "mobile_read_off", "the phone reads nothing while mobile_read is off")
        bots = self.bots(workspace_id)
        if not isinstance(body, dict):
            raise CoreError(400, "bad_tasks", "a report is a JSON object")
        if "why" in body:
            for bot in bots:
                Tasks(self.s).put(workspace_id, bot, {"why": body.get("why")})
            return {"bots": bots}
        reports = body.get("tasks")
        if not isinstance(reports, dict) or any(k not in bots for k in reports):
            raise CoreError(400, "bad_tasks", "tasks names bound Bots")
        for bot, report in reports.items():
            Tasks(self.s).put(workspace_id, bot, {"tasks": report})
        return {"bots": bots}

    def tasks_view(self, p: Principal) -> dict:
        """The app's Tasks tab: Hermes, One and each bound Bot, in the rail's order (tasks.py)."""
        self._patrick(p)
        from .schema import OWN_GATEWAYS
        return Tasks(self.s).view(p.workspace_id, [*OWN_GATEWAYS, *self.bots(p.workspace_id)])

    def live_states(self, workspace_id: str) -> dict:
        """Each Bot's state for the phone: Hermes's own word for its gateway and when it was read; "not_heard"
        when the connector has not reported in LIVE_STALE, and "check_failed" when it could not run the check."""
        with _LIVE_LOCK:
            got = _LIVE.get(workspace_id)
        if got is None or self.s.clock() - got["at"] > LIVE_STALE:
            return {b: {"state": "not_heard", "since": got["at"].isoformat() if got else None}
                    for b in self.bots(workspace_id)}
        at = got["at"].isoformat()
        if got["why"] and not got["states"]:
            return {b: {"state": "check_failed", "since": at, "why": got["why"]} for b in self.bots(workspace_id)}
        return {b: {"state": got["states"].get(b, "not_heard"), "since": at} for b in self.bots(workspace_id)}

    # ── the connector's side ──────────────────────────────────────────────

    # ── full history: pages (step 2, plan Part 4) ─────────────────────────

    def page(self, p: Principal, target: str, before=None, wait=0) -> dict:
        """One page of a Bot's Bot Chat (`before` Hermes's own message id) or of a room (`before` the room's
        sequence number), read by the Bots' connector and held in the core's memory only."""
        self._patrick(p)
        target = self._target(p.workspace_id, target)
        kind = "room" if target in {r["room_id"] for r in self.rooms(p.workspace_id)} else "bot"
        return fetch_page((p.workspace_id, PAGES_GW, kind, target, _page_mid(before), ""), wait, self.s.clock())

    def page_requests(self, workspace_id: str, token: str | None) -> dict:
        """The pages the phone asked for, for the Bots' connector. Reading only: it asks while mobile_read is
        on, whatever connector_effects says."""
        Feeds(self.s).check_token(workspace_id, "connector", token)
        if not self.s.flag(workspace_id, "mobile_read"):
            raise CoreError(423, "mobile_read_off", "the phone reads nothing while mobile_read is off")
        return {"pages": PAGES.hand(workspace_id, PAGES_GW, self.s.clock())}

    def page_post(self, workspace_id: str, token: str | None, body) -> dict:
        """A page the Bots' connector read. It names the request, and the Bot or room it read, which must be
        the one asked for."""
        Feeds(self.s).check_token(workspace_id, "connector", token)
        if not self.s.flag(workspace_id, "mobile_read"):
            raise CoreError(423, "mobile_read_off", "the phone reads nothing while mobile_read is off")
        key = page_kind(body.get("id") if isinstance(body, dict) else None)
        if key is None or key[0] != workspace_id or key[1] != PAGES_GW:
            raise CoreError(409, "not_asked", "no page with that id was asked of the Bots' connector")
        if body.get("target") != key[3]:
            raise CoreError(409, "not_that_chat", "the page is of another Bot or room than the one asked for")
        page = body.get("page")
        clean = clean_room_page(page) if key[2] == "room" else clean_page("messages", page)
        PAGES.answer(workspace_id, PAGES_GW, body["id"], clean, self.s.clock())
        return {"ok": True}

    def claim(self, workspace_id: str, token: str | None, limit: int = MAX_CLAIM) -> dict:
        """Lease the oldest waiting message for each Bot (and the room) that has nothing in flight, at most `limit`."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_CLAIM:
            raise CoreError(400, "bad_limit", f"the limit is 1 to {MAX_CLAIM}")
        Feeds(self.s).check_token(workspace_id, "connector", token)
        if not self.s.flag(workspace_id, "connector_effects"):
            raise CoreError(423, "connector_effects_off", "the connector does nothing while connector_effects is off")
        rooms = {r["room_id"] for r in self.rooms(workspace_id)}
        first = self.room_id(workspace_id)
        items = []
        with self.s.tx() as db:
            self._lapse(db, workspace_id)
            busy = {r[0] for r in db.execute(
                "SELECT target FROM chat_messages WHERE workspace_id = ? AND status = 'leased' UNION "
                "SELECT target FROM connector_outbox WHERE workspace_id = ? AND status = 'leased'",
                (workspace_id, workspace_id)).fetchall()}
            waiting = db.execute(
                "SELECT 'chat' AS kind, id, target, created_at, rowid AS n FROM chat_messages WHERE workspace_id = ? "
                "AND status = 'queued' UNION ALL "
                "SELECT kind, id, target, created_at, rowid AS n FROM connector_outbox WHERE workspace_id = ? "
                "AND status = 'queued' AND kind = 'decision.answered' ORDER BY created_at, n",
                (workspace_id, workspace_id)).fetchall()
            until = (self.s.clock() + LEASE).isoformat()
            now = self.s.now()
            for w in waiting:
                if w["target"] in busy or len(items) >= limit:
                    continue
                busy.add(w["target"])
                if w["kind"] == "chat":
                    row = db.execute("SELECT text, send_key FROM chat_messages WHERE id = ?", (w["id"],)).fetchone()
                    db.execute("UPDATE chat_messages SET status = 'leased', lease_until = ?, attempts = attempts + 1, "
                               "updated_at = ? WHERE id = ?", (until, now, w["id"]))
                    item = {"id": w["id"], "kind": "chat", "target": w["target"], "text": row["text"],
                            "send_key": row["send_key"]}
                else:
                    db.execute("UPDATE connector_outbox SET status = 'leased', lease_until = ?, "
                               "attempts = attempts + 1, updated_at = ? WHERE id = ?", (until, now, w["id"]))
                    item = {"id": w["id"], "kind": "decision.answered", "target": w["target"],
                            "text": self._told_text(db, w["id"]), "send_key": f"tw-told.{w['id']}"}
                # The connector sends to a room by its id; to it every room is "room".
                if item["target"] == ROOM:
                    item["room_id"] = first
                elif item["target"] in rooms:
                    item["room_id"], item["target"] = item["target"], ROOM
                item["lease_until"] = until
                self.s.event(db, workspace_id, "connector.leased", w["id"], "connector", {"target": w["target"]})
                items.append(item)
            reactions = [{"bot": r.pop("chat"), **r} for r in Reactions(self.s).hand(db, workspace_id, PAGES_GW)]
        return {"items": items, "reactions": reactions}

    def _lapse(self, db, workspace_id: str) -> None:
        """A lease that ran out means the connector stopped mid-send: unclear, so never sent again by itself."""
        now = self.s.clock()
        for table in ("chat_messages", "connector_outbox"):
            rows = db.execute(f"SELECT id, lease_until FROM {table} WHERE workspace_id = ? AND status = 'leased'",
                              (workspace_id,)).fetchall()
            for r in rows:
                if r["lease_until"] and now >= parse(r["lease_until"]):
                    db.execute(f"UPDATE {table} SET status = 'uncertain', lease_until = NULL, reason = ?, "
                               f"updated_at = ? WHERE id = ?",
                               ("the connector stopped before it said what happened", self.s.now(), r["id"]))
                    if table == "connector_outbox":
                        db.execute("INSERT INTO effect_receipts(id, workspace_id, outbox, outbox_id, outcome, "
                                   "detail_json, at) VALUES(?,?,?,?,?,?,?)",
                                   (new_id("fx"), workspace_id, "connector", r["id"], "uncertain",
                                    canonical({"reason": "lease ran out"}), self.s.now()))
                    self.s.event(db, workspace_id, "chat.uncertain" if table == "chat_messages" else "effect.uncertain",
                                 r["id"], "core", {"reason": "lease ran out"})

    @staticmethod
    def _told_text(db, outbox_id: str) -> str:
        cx = db.execute("SELECT body_json FROM connector_outbox WHERE id = ?", (outbox_id,)).fetchone()
        body = json.loads(cx["body_json"])
        d = db.execute("SELECT title FROM decisions WHERE id = ?", (body["decision_id"],)).fetchone()
        o = db.execute("SELECT label FROM decision_options WHERE decision_id = ? AND revision = ? AND key = ?",
                       (body["decision_id"], body["revision"], body["option"])).fetchone()
        return (f"Patrick answered the card \"{d['title']}\" ({body['decision_id']}, revision {body['revision']}): "
                f"{o['label'] if o else body['option']} (option {body['option']}). Response {body['response']}. "
                "The card in the decision core is the record; this message approves nothing by itself."
                + Chat._decided_text(db, body["decision_id"], body["response"]))

    @staticmethod
    def _decided_text(db, decision_id: str, response_id: str) -> str:
        """What the Bot that asked does so DECIDED.md carries this answer.

        The core is private and on the VM, and DECIDED.md links where each decision was
        made, so the Bot first puts the answer on the card's public issue and the line
        links that comment, with the card id beside it so the record can be found. The
        format and the checks that read the file are DECIDED.md's own (PR #5339)."""
        d = db.execute("SELECT kind FROM decisions WHERE id = ?", (decision_id,)).fetchone()
        if d["kind"] not in SETTLES:
            return ""
        at = db.execute("SELECT answered_at FROM responses WHERE id = ?", (response_id,)).fetchone()["answered_at"]
        day = parse(at).astimezone(MOUNTAIN)
        date = f"{day.day} {day:%b %Y}"
        issues = [r["ref"] for r in db.execute("SELECT ref FROM task_links WHERE decision_id = ? AND kind = 'issue' "
                                               "ORDER BY rowid", (decision_id,))]
        where = (f"issue {issues[0]}" if issues else
                 "a new issue for it (file it on the board first, unclaimed, in Todo)")
        return ("\n\nRecord the answer in docs/DECIDED.md in ThornswoodRP/Thornswood; every answered question "
                "or change card gets its line:\n"
                f"1. Comment on {where}: what was asked, Patrick's answer and the date, {date}. Plain, as the "
                "guide's \"How an issue and a comment are written\" says: no byline and no footer.\n"
                "2. On a branch of your own, add one bullet under the topic heading it is about, or under "
                "\"### After the plan\" when it is a design card:\n"
                f"   - **The decision as one plain sentence.** ({decision_id}) {date}, [#N](the link to that "
                "comment).\n"
                "   Wrap it near 78 columns with a two-space continuation. If it changes a line already there, "
                "that line goes and this one takes its place; never reorder or rewrite any other line.\n"
                "3. Run pwsh scripts/dev/Check-Words.ps1, node tests/test-docs-index.js and "
                "node tests/test-3283-docs-word-number.js, then open the pull request through tw_git.")

    def reacted(self, workspace_id: str, token: str | None, body) -> dict:
        """What came of setting one reaction in a Bot's Bot Chat."""
        Feeds(self.s).check_token(workspace_id, "connector", token)
        if not isinstance(body, dict) or not isinstance(body.get("bot"), str):
            raise CoreError(400, "bad_result", "a result names the Bot, the message and the version it set")
        return Reactions(self.s).answered(workspace_id, PAGES_GW, body["bot"], body, "connector")

    def doing(self, workspace_id: str, token: str | None, body) -> dict:
        """What each Bot Chat turn running now is doing, from the Bots' connector, for the live line under
        the message it answers. Reading only (nothing reaches Hermes), so it is taken while mobile_read is
        on, whatever connector_effects says."""
        Feeds(self.s).check_token(workspace_id, "connector", token)
        if not self.s.flag(workspace_id, "mobile_read"):
            raise CoreError(423, "mobile_read_off", "the phone reads nothing while mobile_read is off")
        turns = body.get("turns") if isinstance(body, dict) else None
        if not isinstance(turns, list):
            raise CoreError(400, "bad_turns", "turns is a list of what each running turn is doing")
        return {"kept": TURNS.put(workspace_id, turns, self.s.clock())}

    def result(self, workspace_id: str, token: str | None, item_id: str, outcome: str, ref: str = "",
               reason: str = "") -> dict:
        Feeds(self.s).check_token(workspace_id, "connector", token)
        TURNS.done(workspace_id, item_id)
        if outcome not in OUTCOMES:
            raise CoreError(400, "bad_outcome", "an outcome is confirmed, failed, uncertain or not_ready")
        ref, reason = str(ref)[:128], str(reason)[:200]
        state = OUTCOMES[outcome]
        if outcome == "not_ready":
            return self._not_ready(workspace_id, item_id, reason)
        row = self.s.db.execute("SELECT status, target FROM chat_messages WHERE id = ? AND workspace_id = ?",
                                (item_id, workspace_id)).fetchone()
        if row:
            if row["status"] == state:
                return {"id": item_id, "status": state, "replayed": True}
            if row["status"] != "leased":
                raise CoreError(409, "not_leased", f"that message is {row['status']}, not leased")
            # The turn is over and the Bot's answer is in its Bot Chat (or the message in the room): that
            # chat's newest page is read again, not shown from memory for up to PAGE_FRESH without it.
            PAGES.forget(workspace_id, PAGES_GW, ("bot", "room"), row["target"])
            with self.s.tx() as db:
                db.execute("UPDATE chat_messages SET status = ?, lease_until = NULL, reason = ?, updated_at = ? "
                           "WHERE id = ?", (state, reason or None, self.s.now(), item_id))
                self.s.event(db, workspace_id, f"chat.{state}", item_id, "connector", {"ref": ref})
            return {"id": item_id, "status": state}
        cx = self.s.db.execute("SELECT status FROM connector_outbox WHERE id = ? AND workspace_id = ?",
                               (item_id, workspace_id)).fetchone()
        if not cx:
            raise CoreError(404, "no_item", "no such message")
        if cx["status"] == state:
            return {"id": item_id, "status": state, "replayed": True}
        if cx["status"] != "leased":
            raise CoreError(409, "not_leased", f"that message is {cx['status']}, not leased")
        DecisionCore(self.s).confirm_effect(item_id, outbox="connector", outcome=outcome,
                                            detail={"ref": ref, "reason": reason})
        with self.s.tx() as db:
            db.execute("UPDATE connector_outbox SET lease_until = NULL, reason = ? WHERE id = ?",
                       (reason or None, item_id))
        return {"id": item_id, "status": state}

    def _not_ready(self, workspace_id: str, item_id: str, reason: str) -> dict:
        """The gateway was not ready, so nothing was sent: back in the queue as it was, with no effect
        receipt (there was no effect) and that attempt not counted. It goes once the gateway is ready."""
        why = (reason if reason.startswith(HELPER_FAILED) else WAITS + (reason or "it is not ready"))[:200]
        with self.s.tx() as db:
            for table, kind in (("chat_messages", "chat.not_ready"), ("connector_outbox", "connector.not_ready")):
                row = db.execute(f"SELECT status FROM {table} WHERE id = ? AND workspace_id = ?",
                                 (item_id, workspace_id)).fetchone()
                if not row:
                    continue
                if row["status"] == "queued":
                    return {"id": item_id, "status": "queued", "replayed": True}
                if row["status"] != "leased":
                    raise CoreError(409, "not_leased", f"that message is {row['status']}, not leased")
                db.execute(f"UPDATE {table} SET status = 'queued', lease_until = NULL, reason = ?, "
                           f"attempts = max(attempts - 1, 0), updated_at = ? WHERE id = ?",
                           (why, self.s.now(), item_id))
                self.s.event(db, workspace_id, kind, item_id, "connector", {"reason": why})
                return {"id": item_id, "status": "queued"}
        raise CoreError(404, "no_item", "no such message")
