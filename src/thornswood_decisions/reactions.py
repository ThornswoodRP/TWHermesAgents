"""Patrick's reactions on messages, for Hermes, One and the Bots' Bot Chats (redesign step 2, plan Part 11).

Hermes keeps one reaction per person on a message row (contracts.json "reactions"). A row in
`chat_reactions` says which one Patrick wants there now, or none (emoji NULL), never a change to
make, so setting it twice is the same as once and a lease that ran out is simply handed out again.
`version` tells a connector's late answer from the newest wish.

The gateway is `me` or `one` (his own Hermes, the chat is a session id) or `bots` (the Bots'
gateway, the chat is the Bot's profile id: each Bot has one Bot Chat). Each gateway's own
connector sets them. The callers check who may ask and whether that gateway is answering; this
module keeps the rows. No message text goes into the event chain.
"""

from __future__ import annotations

from datetime import timedelta

from .auth import Principal
from .store import CoreError, Store, parse

REACTIONS = ("\U0001F44D", "\u2764\ufe0f", "\U0001F602", "\U0001F62E", "\U0001F622", "\U0001F64F", "\u2705", "\U0001F440")
LEASE = timedelta(minutes=2)   # setting one is a single write in Hermes's store
MAX_CLAIM = 10
SHOWN = 200                    # a chat's wishes the phone is shown


def message_id(value) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 < value < 2 ** 63:
        raise CoreError(400, "bad_message", "a reaction goes on one message, by Hermes's own id")
    return value


def emoji(value) -> str | None:
    if value is not None and value not in REACTIONS:
        raise CoreError(400, "bad_emoji", "a reaction is one of the phone's emoji, or null to take it off")
    return value


class Reactions:
    def __init__(self, store: Store):
        self.s = store

    def want(self, p: Principal, gateway: str, chat: str, mid, wanted) -> dict:
        """Patrick wants exactly this emoji on that message, or none. The same wish twice is one."""
        mid, wanted = message_id(mid), emoji(wanted)
        now = self.s.now()
        with self.s.tx() as db:
            row = db.execute("SELECT emoji, status FROM chat_reactions WHERE workspace_id = ? AND gateway = ? "
                             "AND chat = ? AND message_id = ?", (p.workspace_id, gateway, chat, mid)).fetchone()
            if row and row["emoji"] == wanted and row["status"] in ("queued", "leased", "set"):
                return {"message_id": mid, "emoji": wanted, "status": row["status"], "replayed": True}
            db.execute("INSERT INTO chat_reactions(workspace_id, gateway, chat, message_id, emoji, version, status, "
                       "principal_id, device_id, created_at, updated_at) VALUES(?,?,?,?,?,1,'queued',?,?,?,?) "
                       "ON CONFLICT(workspace_id, gateway, chat, message_id) DO UPDATE SET "
                       "emoji = excluded.emoji, version = version + 1, status = 'queued', lease_until = NULL, "
                       "reason = NULL, principal_id = excluded.principal_id, device_id = excluded.device_id, "
                       "updated_at = excluded.updated_at",
                       (p.workspace_id, gateway, chat, mid, wanted, p.id, p.device_id, now, now))
            self.s.event(db, p.workspace_id, "reaction.wanted", f"{gateway}:{chat}:{mid}", "patrick",
                         {"gateway": gateway, "emoji": wanted, "device": p.device_id})
        return {"message_id": mid, "emoji": wanted, "status": "queued"}

    def shown(self, workspace_id: str, gateway: str, chat: str) -> list[dict]:
        """His wishes in one chat and how each went, so the phone shows one at once, until the chat's own
        page shows it."""
        rows = self.s.db.execute("SELECT message_id, emoji, status, reason, updated_at FROM chat_reactions "
                                 "WHERE workspace_id = ? AND gateway = ? AND chat = ? ORDER BY updated_at DESC "
                                 "LIMIT ?", (workspace_id, gateway, chat, SHOWN)).fetchall()
        return [dict(r) for r in rows]

    def hand(self, db, workspace_id: str, gateway: str) -> list[dict]:
        """The reactions to set now, leased. One whose lease ran out goes out again: setting it is the same
        whether or not the last try got there."""
        now = self.s.clock()
        until = (now + LEASE).isoformat()
        out = []
        for r in db.execute("SELECT chat, message_id, emoji, version, status, lease_until FROM chat_reactions "
                            "WHERE workspace_id = ? AND gateway = ? AND status IN ('queued', 'leased') "
                            "ORDER BY updated_at", (workspace_id, gateway)).fetchall():
            if len(out) >= MAX_CLAIM:
                break
            if r["status"] == "leased" and r["lease_until"] and now < parse(r["lease_until"]):
                continue
            db.execute("UPDATE chat_reactions SET status = 'leased', lease_until = ? WHERE workspace_id = ? "
                       "AND gateway = ? AND chat = ? AND message_id = ?",
                       (until, workspace_id, gateway, r["chat"], r["message_id"]))
            out.append({"chat": r["chat"], "message_id": r["message_id"], "emoji": r["emoji"], "version": r["version"]})
        return out

    def answered(self, workspace_id: str, gateway: str, chat: str, body: dict, by: str) -> dict:
        """What came of setting one. An answer about an older wish than the newest is dropped, and the newest
        goes out on the next claim."""
        mid, version, outcome = body.get("message_id"), body.get("version"), body.get("outcome")
        if not isinstance(mid, int) or isinstance(mid, bool) or not isinstance(version, int) or isinstance(version, bool):
            raise CoreError(400, "bad_result", "a result names the message and the version it set")
        if outcome not in ("set", "failed"):
            raise CoreError(400, "bad_outcome", "a reaction's outcome is set or failed")
        reason = str(body.get("reason", ""))[:200]
        with self.s.tx() as db:
            row = db.execute("SELECT version, status FROM chat_reactions WHERE workspace_id = ? AND gateway = ? "
                             "AND chat = ? AND message_id = ?", (workspace_id, gateway, chat, mid)).fetchone()
            if not row:
                raise CoreError(404, "no_item", "no such reaction")
            if row["version"] != version or row["status"] != "leased":
                return {"status": row["status"], "stale": True}
            db.execute("UPDATE chat_reactions SET status = ?, lease_until = NULL, reason = ?, updated_at = ? "
                       "WHERE workspace_id = ? AND gateway = ? AND chat = ? AND message_id = ?",
                       (outcome, (reason or None) if outcome == "failed" else None, self.s.now(), workspace_id,
                        gateway, chat, mid))
            self.s.event(db, workspace_id, f"reaction.{outcome}", f"{gateway}:{chat}:{mid}", by, {})
        return {"status": outcome}
