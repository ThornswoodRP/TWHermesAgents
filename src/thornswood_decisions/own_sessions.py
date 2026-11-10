"""Renaming, hiding, bringing back, pinning and deleting one of Patrick's own chats (redesign step 3, plan Part 11).

The phone asks; the core keeps the request in `own_session_ops` (migration 10), in the same commit as
its command record, so a retry with the same key gets the same receipt and never a second request. That
gateway's own connector (own_connector.py) is handed it with its claim and does it with Hermes's own
calls, as `hermes sessions rename` and `hermes sessions delete` and Hermes's session.archive do
(contracts.json "sessions"):

- rename: Hermes's set_session_title, which cleans the title and refuses one another chat holds;
- archive and unarchive: Hermes's set_session_archived, which hides or brings back the whole chat
  (every compressed part of it) and keeps its messages;
- pin and unpin: Hermes's set_session_pinned, as Desktop pins a chat (#101), for the whole chat; a
  pinned chat heads the chat list here and in Desktop;
- delete: Hermes's delete_session, refused while a turn is running or unfinished (in the chat or a
  side task of it); it removes that chat with its messages, the earlier parts Hermes compressed it
  from and the side tasks (delegate chats) Hermes ran for it. A chat branched off it is kept, and
  nothing else changes.

His Bot Chat is never one of them: Bot Mode and every Bot's message find it by its exact title while it is
hidden, so the core refuses it as listed, and the helper refuses it again by Hermes's own record.

Each ends in the same state however often it runs, so a lease that ran out is handed out again, and a
chat already gone when its delete runs is done. One request at a time in each chat. All of it needs
session_manage_write, off until Patrick turns it on. A delete cannot be undone, so it also needs a
passkey sign-in from the last 30 minutes, like a message with full power. No title goes into the event
chain or a log.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta

from .auth import Principal
from .store import CoreError, Store, canonical, new_id, parse, sha256_hex

OPS = ("rename", "archive", "unarchive", "pin", "unpin", "delete")
MAX_TITLE = 100            # Hermes's own SessionDB.MAX_TITLE_LENGTH
LEASE = timedelta(minutes=2)
MAX_CLAIM = 5
SHOWN = 20
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
OP_ID = re.compile(r"^sop_[A-Za-z0-9_-]{8,40}$")


def _title(value) -> str:
    if not isinstance(value, str) or CONTROL.search(value) or not value.strip():
        raise CoreError(400, "bad_title", "a title is some text on one line")
    value = " ".join(value.split())
    if len(value) > MAX_TITLE:
        raise CoreError(400, "bad_title", f"a title is at most {MAX_TITLE} characters")
    return value


class OwnSessions:
    def __init__(self, store: Store):
        self.s = store

    def ask(self, own, p: Principal, gw: str, sid: str, op, title, idempotency_key) -> dict:
        """One request from the phone. `own` is the OwnChat that has already checked Patrick, the gateway
        and the chat id."""
        if not self.s.flag(p.workspace_id, "session_manage_write"):
            raise CoreError(423, "session_manage_off", "renaming, hiding and deleting chats from the phone is off for now")
        if op not in OPS:
            raise CoreError(400, "bad_op", "a chat is renamed, archived, unarchived, pinned, unpinned or deleted")
        title = _title(title) if op == "rename" else None
        if op != "rename" and title is not None:
            raise CoreError(400, "bad_title", "only a rename carries a title")
        if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 200:
            raise CoreError(400, "bad_key", "a request carries an idempotency key of 8 to 200 characters")
        body_sha = sha256_hex(canonical({"gateway": gw, "session_id": sid, "op": op, "title": title}))
        with self.s.tx() as db:
            cmd = db.execute("SELECT op, body_sha256, result_json FROM commands WHERE workspace_id = ? "
                             "AND principal_id = ? AND idempotency_key = ?",
                             (p.workspace_id, p.id, idempotency_key)).fetchone()
            if cmd:
                if cmd["op"] != "own.session" or cmd["body_sha256"] != body_sha:
                    raise CoreError(409, "key_reused", "that idempotency key was used for a different request")
                return json.loads(cmd["result_json"]) | {"replayed": True}
            if not own.listed_or_hidden(p.workspace_id, gw, sid, hidden=op == "unarchive"):
                raise CoreError(404, "not_listed", f"{own.name(gw)} has not listed that chat")
            if own.is_bot_chat(p.workspace_id, gw, sid):
                raise CoreError(409, "bot_chat_kept", "your Bot Chat is kept as it is: Bot Mode and every Bot's "
                                "message to you find it by its name, so it is not renamed, hidden, pinned or "
                                "deleted; it is always first in the list")
            if op == "delete" and own.sign_in_until(p) is None:
                raise CoreError(403, "passkey_needed", "deleting a chat cannot be undone, so it needs a passkey "
                                "sign-in from the last 30 minutes; sign in with your passkey again")
            own.must_be_online(p.workspace_id, gw)
            now = self.s.now()
            oid = new_id("sop")
            db.execute("INSERT INTO own_session_ops(id, workspace_id, gateway, session_id, op, title, status, "
                       "principal_id, device_id, created_at, updated_at) VALUES(?,?,?,?,?,?,'queued',?,?,?,?)",
                       (oid, p.workspace_id, gw, sid, op, title, p.id, p.device_id, now, now))
            self.s.wake(p.workspace_id, gw)
            self.s.event(db, p.workspace_id, "own.session_" + op, oid, "patrick", {"gateway": gw, "device": p.device_id})
            receipt = {"id": oid, "gateway": gw, "session_id": sid, "op": op, "status": "queued", "created_at": now}
            db.execute("INSERT INTO commands(workspace_id, principal_id, idempotency_key, op, body_sha256, "
                       "result_json, created_at) VALUES(?,?,?,?,?,?,?)",
                       (p.workspace_id, p.id, idempotency_key, "own.session", body_sha, canonical(receipt), now))
        return receipt

    def shown(self, workspace_id: str, gw: str, sid: str | None = None) -> list[dict]:
        """The newest requests, of one chat or of the whole gateway, and how each went."""
        rows = self.s.db.execute(
            "SELECT id, session_id, op, title, status, reason, created_at, updated_at FROM own_session_ops "
            "WHERE workspace_id = ? AND gateway = ? AND (? IS NULL OR session_id = ?) "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?", (workspace_id, gw, sid, sid, SHOWN)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def hand(self, db, workspace_id: str, gw: str) -> list[dict]:
        """The requests to do now, leased: the oldest of each chat, while nothing else of that chat is in
        flight. One whose lease ran out goes out again; doing it twice ends in the same state."""
        now = self.s.clock()
        until = (now + LEASE).isoformat()
        rows = db.execute("SELECT id, session_id, op, title, status, lease_until FROM own_session_ops "
                          "WHERE workspace_id = ? AND gateway = ? AND status IN ('queued', 'leased') "
                          "ORDER BY created_at, rowid", (workspace_id, gw)).fetchall()
        busy = {r["session_id"] for r in rows
                if r["status"] == "leased" and r["lease_until"] and now < parse(r["lease_until"])}
        out = []
        for r in rows:
            if r["session_id"] in busy or len(out) >= MAX_CLAIM:
                continue
            busy.add(r["session_id"])
            db.execute("UPDATE own_session_ops SET status = 'leased', lease_until = ?, updated_at = ? WHERE id = ?",
                       (until, self.s.now(), r["id"]))
            out.append({"id": r["id"], "session_id": r["session_id"], "op": r["op"], "title": r["title"]})
        return out

    def done(self, workspace_id: str, gw: str, body) -> dict:
        """What came of one request: done, or failed with Hermes's reason. An answer about a request that
        is not leased (already answered) changes nothing."""
        if not isinstance(body, dict) or not isinstance(body.get("id"), str) or not OP_ID.match(body["id"]):
            raise CoreError(400, "bad_result", "a result names the request it answers")
        outcome = body.get("outcome")
        if outcome not in ("done", "failed"):
            raise CoreError(400, "bad_outcome", "a request's outcome is done or failed")
        reason = str(body.get("reason", ""))[:200]
        with self.s.tx() as db:
            row = db.execute("SELECT op, status FROM own_session_ops WHERE id = ? AND workspace_id = ? AND gateway = ?",
                             (body["id"], workspace_id, gw)).fetchone()
            if not row:
                raise CoreError(404, "no_item", "no such request")
            if row["status"] != "leased":
                return {"status": row["status"], "stale": True}
            db.execute("UPDATE own_session_ops SET status = ?, lease_until = NULL, reason = ?, updated_at = ? "
                       "WHERE id = ?", (outcome, reason or None, self.s.now(), body["id"]))
            self.s.event(db, workspace_id, f"own.session_{outcome}", body["id"], f"own connector ({gw})",
                         {"op": row["op"]})
        return {"status": outcome}
