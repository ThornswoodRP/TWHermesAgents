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

The same lease carries the connector's other job: telling the Bot that asked a
card how Patrick answered it (`connector_outbox`, written by the answer's own
commit in `core.py`). Its outcome is an effect receipt, and a confirmed one is
the "Bot told" state.

A chat message is never an answer, an approval or authority of any kind (07
section 5). Text goes to Hermes and to the phone, never into the event chain
or a log.
"""

from __future__ import annotations

import json
from datetime import timedelta

from .auth import Principal
from .core import DecisionCore
from .feeds import Feeds, room_events
from .own import PAGE_TEXT, PAGES, _page_mid, clean_page, fetch_page, page_kind
from .store import CoreError, Store, canonical, new_id, parse, sha256_hex

MAX_TEXT = 8000
LEASE = timedelta(minutes=20)
ROOM = "room"  # the first room bound, by its first name
OUTCOMES = {"confirmed": "sent", "failed": "dead", "uncertain": "uncertain"}
MINE = 50
MAX_CLAIM = 7  # the six Bots and the rooms, at most this many at once
ROOM_PAGE = 50
ROOM_KINDS = ("message.user", "message.member")
PAGES_GW = "bots"   # the Bots' gateway, in the page store beside Patrick's own two


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

    def send(self, p: Principal, target: str, text, idempotency_key) -> dict:
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
        body_sha = sha256_hex(canonical({"target": target, "text": text}))
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
            receipt = {"message_id": mid, "target": target, "status": "queued", "created_at": now}
            db.execute("INSERT INTO commands(workspace_id, principal_id, idempotency_key, op, body_sha256, "
                       "result_json, created_at) VALUES(?,?,?,?,?,?,?)",
                       (p.workspace_id, p.id, idempotency_key, "chat.send", body_sha, canonical(receipt), now))
        return receipt

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
        return [dict(r) for r in reversed(rows)]

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
                "mine": self._mine(p.workspace_id, target), "can_send": self.s.flag(p.workspace_id, "mobile_chat_write")}

    def summary(self, p: Principal) -> dict:
        self._patrick(p)
        feeds = Feeds(self.s)
        snap = feeds.get(p.workspace_id, "bots")
        chats = {b["bot"]: b for b in (snap["bots"] or {}).get("bots", [])}
        out = []
        for bot in self.bots(p.workspace_id):
            msgs = chats.get(bot, {}).get("messages", [])
            out.append({"bot": bot, "last": msgs[-1] if msgs else None,
                        "unsettled": self._unsettled(p.workspace_id, bot)})
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
        return {"items": items}

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
                "The card in the decision core is the record; this message approves nothing by itself.")

    def result(self, workspace_id: str, token: str | None, item_id: str, outcome: str, ref: str = "",
               reason: str = "") -> dict:
        Feeds(self.s).check_token(workspace_id, "connector", token)
        if outcome not in OUTCOMES:
            raise CoreError(400, "bad_outcome", "an outcome is confirmed, failed or uncertain")
        ref, reason = str(ref)[:128], str(reason)[:200]
        state = OUTCOMES[outcome]
        row = self.s.db.execute("SELECT status FROM chat_messages WHERE id = ? AND workspace_id = ?",
                                (item_id, workspace_id)).fetchone()
        if row:
            if row["status"] == state:
                return {"id": item_id, "status": state, "replayed": True}
            if row["status"] != "leased":
                raise CoreError(409, "not_leased", f"that message is {row['status']}, not leased")
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
