"""Retry, undo, edit and resend, and branch, on one message of a chat (TWHermesAgents #80, 10 Nov).

Hermes Desktop offers four things on a message, and so does the app, in his own Hermes and One (`me`,
`one`) and in a Bot's Bot Chat or a Bot's session a Board row opened (`bots`). Each is done with Hermes's
own calls on the chat's running gateway, the ones `hermes chat`'s /retry, /undo and /branch make
(hermes_cli/gateway_chat_commands.py, hermes_cli/gateway_chat_view.py command; contracts.json "rewind"):

- undo: Hermes's session.mutate rewind to one of his turns: that turn and everything after it leave the
  chat. Hermes keeps them, out of the chat, and nothing else changes. The app puts the words of that turn
  back in the box, as Desktop and /undo do;
- retry, on a reply: the same rewind to the turn the reply answers, then that turn's own words sent again,
  as Hermes keeps them (agent.context_compressor.retryable_user_text, which refuses a turn of a photo as
  /retry does), so the reply is written again;
- edit and resend, on one of his turns: the same rewind to it, then his new words sent in its place;
- branch: Hermes's session.mutate branch through one message: a new chat with every message up to and with
  that one, the chat itself left as it is. The app opens the new chat.

A retry or an edit runs a turn, so it is a message (`own_messages` or `chat_messages`, migration 24) with
`rewind` and `rewind_to`, delivered the way any message is, with its live line, its alert and its honest
result; the connector takes the chat back first, inside the same turn (room_send.py --tw-rewind). An undo
or a branch runs none, so it is a row here, in `chat_rewinds`, done by that gateway's connector.

Hermes refuses a rewind while a turn runs in the chat, so the order is kept here: a rewind, a retry or an
edit goes only once nothing he sent to that chat before it is still on its way, and nothing he sends after
it goes until it is done. Each needs what a message to that chat needs: own_chat_write and a passkey sign-in
from the last 30 minutes on his own Hermes and One; mobile_chat_write for a Bot, and the Bots' connector
does it only while connector_effects is on. No words go into the event chain.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta

from .auth import Principal
from .reactions import message_id as _message_id
from .store import CoreError, Store, canonical, new_id, parse, sha256_hex

OPS = ("undo", "branch")
REWINDS = ("retry", "edit")   # the two that are a message
LEASE = timedelta(minutes=2)  # one session.mutate, through the gateway
SHOWN = 20
RW_ID = re.compile(r"^rw_[A-Za-z0-9_-]{8,40}$")
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")  # own.SESSION_ID


def rewind_of(rewind, rewind_to) -> tuple[str | None, int | None]:
    """A message's retry or edit, checked: (None, None) for a plain message."""
    if rewind is None and rewind_to is None:
        return None, None
    if rewind not in REWINDS:
        raise CoreError(400, "bad_rewind", "a message takes the chat back for a retry or an edit")
    try:
        return rewind, _message_id(rewind_to)
    except CoreError:
        raise CoreError(400, "bad_rewind", "a retry or an edit names his turn by Hermes's own id") from None


class Rewinds:
    def __init__(self, store: Store):
        self.s = store

    def ask(self, p: Principal, gateway: str, chat: str, session: str | None, op, mid, idempotency_key) -> dict:
        """One undo or branch. The callers have checked who asks, the chat, and that it may be changed now."""
        if op not in OPS:
            raise CoreError(400, "bad_op", "a message is undone or branched from")
        try:
            mid = _message_id(mid)
        except CoreError:
            raise CoreError(400, "bad_message", "an undo or a branch names one message by Hermes's own id") from None
        if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 200:
            raise CoreError(400, "bad_key", "a request carries an idempotency key of 8 to 200 characters")
        body_sha = sha256_hex(canonical({"gateway": gateway, "chat": chat, "session": session, "op": op,
                                         "message_id": mid}))
        with self.s.tx() as db:
            cmd = db.execute("SELECT op, body_sha256, result_json FROM commands WHERE workspace_id = ? "
                             "AND principal_id = ? AND idempotency_key = ?",
                             (p.workspace_id, p.id, idempotency_key)).fetchone()
            if cmd:
                if cmd["op"] != "chat.rewind" or cmd["body_sha256"] != body_sha:
                    raise CoreError(409, "key_reused", "that idempotency key was used for a different request")
                return json.loads(cmd["result_json"]) | {"replayed": True}
            now = self.s.now()
            rid = new_id("rw")
            db.execute("INSERT INTO chat_rewinds(id, workspace_id, gateway, chat, session_id, op, message_id, status, "
                       "principal_id, device_id, created_at, updated_at) VALUES(?,?,?,?,?,?,?,'queued',?,?,?,?)",
                       (rid, p.workspace_id, gateway, chat, session, op, mid, p.id, p.device_id, now, now))
            self.s.wake(p.workspace_id, gateway)
            self.s.event(db, p.workspace_id, "chat.rewind_" + op, rid, "patrick",
                         {"gateway": gateway, "device": p.device_id})
            receipt = {"id": rid, "gateway": gateway, "chat": chat, "op": op, "message_id": mid, "status": "queued",
                       "created_at": now} | ({"session": session} if session else {})
            db.execute("INSERT INTO commands(workspace_id, principal_id, idempotency_key, op, body_sha256, "
                       "result_json, created_at) VALUES(?,?,?,?,?,?,?)",
                       (p.workspace_id, p.id, idempotency_key, "chat.rewind", body_sha, canonical(receipt), now))
        return receipt

    def shown(self, workspace_id: str, gateway: str, chat: str, session: str | None = None) -> list[dict]:
        """The newest undos and branches of one chat, and how each went."""
        rows = self.s.db.execute(
            "SELECT id, op, message_id, status, reason, made_session_id, created_at, updated_at FROM chat_rewinds "
            "WHERE workspace_id = ? AND gateway = ? AND chat = ? AND session_id IS ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?", (workspace_id, gateway, chat, session, SHOWN)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def waiting(self, db, workspace_id: str, gateway: str) -> list[dict]:
        """Every undo and branch of this gateway not yet done, oldest first, for its claim to order with the
        messages. One whose lease ran out means the connector stopped before it said what happened: it may be
        done in Hermes or not, so it is never done again by itself (a branch twice is two chats), and the
        phone says to look at the chat."""
        now = self.s.clock()
        for r in db.execute("SELECT id, lease_until FROM chat_rewinds WHERE workspace_id = ? AND gateway = ? "
                            "AND status = 'leased'", (workspace_id, gateway)).fetchall():
            if r["lease_until"] and now >= parse(r["lease_until"]):
                db.execute("UPDATE chat_rewinds SET status = 'uncertain', lease_until = NULL, reason = ?, "
                           "updated_at = ? WHERE id = ?",
                           ("the connector stopped before it said what happened", self.s.now(), r["id"]))
                self.s.event(db, workspace_id, "chat.rewind_uncertain", r["id"], "core", {"reason": "lease ran out"})
        return [dict(r) for r in db.execute(
            "SELECT id, chat, session_id, op, message_id, status, created_at, rowid AS n FROM chat_rewinds "
            "WHERE workspace_id = ? AND gateway = ? AND status IN ('queued', 'leased') ORDER BY created_at, rowid",
            (workspace_id, gateway)).fetchall()]

    def lease(self, db, row: dict) -> dict:
        db.execute("UPDATE chat_rewinds SET status = 'leased', lease_until = ?, updated_at = ? WHERE id = ?",
                   ((self.s.clock() + LEASE).isoformat(), self.s.now(), row["id"]))
        return {"id": row["id"], "chat": row["chat"], "op": row["op"], "message_id": row["message_id"]} \
            | ({"session": row["session_id"]} if row["session_id"] else {})

    def done(self, workspace_id: str, gateway: str, body, by: str) -> dict:
        """What came of one: done (with the new chat, for a branch), failed with Hermes's reason, uncertain
        (Hermes did not answer, so it may be done; never done again by itself), or not_ready (the gateway was
        not running, so nothing was done and it goes back in the queue). An answer about one that is not
        leased changes nothing."""
        if not isinstance(body, dict) or not isinstance(body.get("id"), str) or not RW_ID.match(body["id"]):
            raise CoreError(400, "bad_result", "a result names the request it answers")
        outcome = body.get("outcome")
        if outcome not in ("done", "failed", "uncertain", "not_ready"):
            raise CoreError(400, "bad_outcome", "an undo or a branch is done, failed, uncertain or not_ready")
        reason = str(body.get("reason") or "")[:200]
        made = body.get("session_id")
        with self.s.tx() as db:
            row = db.execute("SELECT op, status FROM chat_rewinds WHERE id = ? AND workspace_id = ? AND gateway = ?",
                             (body["id"], workspace_id, gateway)).fetchone()
            if not row:
                raise CoreError(404, "no_item", "no such request")
            if row["status"] != "leased":
                return {"status": row["status"], "stale": True}
            if outcome == "not_ready":
                db.execute("UPDATE chat_rewinds SET status = 'queued', lease_until = NULL, reason = ?, updated_at = ? "
                           "WHERE id = ?", (reason or None, self.s.now(), body["id"]))
                return {"status": "queued"}
            if outcome == "done" and row["op"] == "branch":
                if not isinstance(made, str) or not SESSION_ID.match(made):
                    raise CoreError(400, "bad_result", "a branch done names the new chat by Hermes's own id")
            else:
                made = None
            db.execute("UPDATE chat_rewinds SET status = ?, lease_until = NULL, reason = ?, made_session_id = ?, "
                       "updated_at = ? WHERE id = ?", (outcome, reason or None, made, self.s.now(), body["id"]))
            self.s.event(db, workspace_id, f"chat.rewind_{outcome}", body["id"], by, {"op": row["op"]})
        return {"status": outcome} | ({"session_id": made} if made else {})
