"""The phone's Work, Room and Bot Chat views: read-only snapshots the Bots' Hermes sends in.

Hermes keeps the Kanban and the Workbench room; the core keeps no copy of
either beyond the latest snapshot of each, shown with the time it was read
("as of"). The snapshot comes from `feed_reader.py`, which runs as the Bots'
`hermes` user with a feed token. That token can do one thing: replace one
snapshot. It cannot read a card, answer, or reach anything else, and what it
sends is shown as text, never as markup. Because any Bot could read that token,
these views are what the Bots' Hermes reports, not authority: a decision is
only ever a card (07 section 5, "never authority").

The connector (`connector.py`, step 4) has a token of the same kind under the
name "connector". It is not a feed: it carries Patrick's chat to Hermes and
says what came of each message, and it can do nothing else. The push worker
(`push_worker.py`, step 5) has one too, under "push".
"""

from __future__ import annotations

import hmac
import json
import secrets

from .store import CoreError, Store, canonical, parse, sha256_hex

NAMES = ("work", "room", "bots")
TOKENS = NAMES + ("connector", "push")
MAX_FEED_BYTES = 512 * 1024
MAX_TASKS = 500
MAX_EVENTS = 200
MAX_BOT_MESSAGES = 40
TASK_FIELDS = {"id": 64, "title": 200, "assignee": 64, "status": 32, "updated_at": 40}
EVENT_FIELDS = {"event_id": 128, "kind": 32, "actor": 80, "text": 2000, "at": 40}
BOT_MESSAGE_FIELDS = {"role": 16, "text": 2000, "at": 40}
ROLES = ("user", "assistant")


def _text(value, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise CoreError(400, "bad_feed", "a feed field is text")
    return str(value)[:limit]


def _time(value) -> str:
    try:
        return parse(str(value)).isoformat()
    except (ValueError, TypeError):
        raise CoreError(400, "bad_feed", "source_time is an ISO time with a zone") from None


def clean(name: str, body: dict) -> dict:
    """Only the fields the views show, each cut to size. Anything else is dropped."""
    if not isinstance(body, dict):
        raise CoreError(400, "bad_feed", "a feed is a JSON object")
    out = {"source_time": _time(body.get("source_time"))}
    if name == "work":
        tasks = body.get("tasks")
        if not isinstance(tasks, list) or len(tasks) > MAX_TASKS:
            raise CoreError(400, "bad_feed", f"tasks is a list of at most {MAX_TASKS}")
        out["tasks"] = [{k: _text(t.get(k) if isinstance(t, dict) else None, n) for k, n in TASK_FIELDS.items()}
                        for t in tasks]
        for t, raw in zip(out["tasks"], tasks):
            p = raw.get("priority") if isinstance(raw, dict) else None
            t["priority"] = p if isinstance(p, int) and not isinstance(p, bool) and -1000 <= p <= 1000 else 0
    elif name == "bots":
        bots = body.get("bots")
        if not isinstance(bots, list) or len(bots) > 16:
            raise CoreError(400, "bad_feed", "bots is a list, one entry for each Bot")
        out["bots"] = []
        for b in bots:
            if not isinstance(b, dict):
                raise CoreError(400, "bad_feed", "each Bot is an object")
            msgs = b.get("messages")
            if not isinstance(msgs, list) or len(msgs) > MAX_BOT_MESSAGES:
                raise CoreError(400, "bad_feed", f"a Bot Chat sends at most its last {MAX_BOT_MESSAGES} messages")
            kept = []
            for m in msgs:
                row = {k: _text(m.get(k) if isinstance(m, dict) else None, n) for k, n in BOT_MESSAGE_FIELDS.items()}
                if row["role"] not in ROLES:
                    raise CoreError(400, "bad_feed", "a Bot Chat message is from the user or the assistant")
                kept.append(row)
            out["bots"].append({"bot": _text(b.get("bot"), 64), "session_id": _text(b.get("session_id"), 128),
                                "messages": kept})
    else:
        events = body.get("events")
        if not isinstance(events, list) or len(events) > MAX_EVENTS:
            raise CoreError(400, "bad_feed", f"events is a list of at most {MAX_EVENTS}")
        out["room_id"] = _text(body.get("room_id"), 64)
        out["events"] = []
        for e in events:
            seq = e.get("seq") if isinstance(e, dict) else None
            if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
                raise CoreError(400, "bad_feed", "each event has its room seq")
            out["events"].append({"seq": seq, **{k: _text(e.get(k), n) for k, n in EVENT_FIELDS.items()}})
    return out


class Feeds:
    def __init__(self, store: Store):
        self.s = store

    def new_token(self, workspace_id: str, name: str, *, by: str) -> str:
        """A new token for one feed (or the connector), replacing the old one. Shown once; only its hash is kept."""
        if name not in TOKENS:
            raise CoreError(400, "bad_feed", f"the tokens are {', '.join(TOKENS)}")
        token = secrets.token_urlsafe(32)
        with self.s.tx() as db:
            db.execute("INSERT INTO feed_sources(workspace_id, name, token_hash, created_at) VALUES(?,?,?,?) "
                       "ON CONFLICT(workspace_id, name) DO UPDATE SET token_hash = excluded.token_hash, "
                       "created_at = excluded.created_at", (workspace_id, name, sha256_hex(token), self.s.now()))
            self.s.event(db, workspace_id, "feed.token_made", name, by, {})
        return token

    def check_token(self, workspace_id: str, name: str, token: str | None) -> None:
        row = self.s.db.execute("SELECT token_hash FROM feed_sources WHERE workspace_id = ? AND name = ?",
                                (workspace_id, name)).fetchone()
        if not row or not token or not hmac.compare_digest(row["token_hash"], sha256_hex(token)):
            raise CoreError(401, "bad_feed_token", f"that is not the {name} token")

    def put(self, workspace_id: str, name: str, token: str | None, body: dict) -> dict:
        if name not in NAMES:
            raise CoreError(404, "no_route", "no such feed")
        self.check_token(workspace_id, name, token)
        snap = clean(name, body)
        if name == "room":
            bound = self.s.db.execute("SELECT room_id FROM room_bindings WHERE workspace_id = ?",
                                      (workspace_id,)).fetchall()
            if snap["room_id"] not in {r[0] for r in bound}:
                raise CoreError(409, "not_the_room", "the feed names a room this workspace is not bound to")
        if name == "bots":
            bound = {r[0] for r in self.s.db.execute("SELECT bot FROM bot_bindings WHERE workspace_id = ?",
                                                     (workspace_id,)).fetchall()}
            names = [b["bot"] for b in snap["bots"]]
            if len(set(names)) != len(names) or not set(names) <= bound:
                raise CoreError(409, "not_the_bots", "the feed names a Bot this workspace does not have, or one twice")
        text = canonical(snap)
        digest = sha256_hex(text)
        with self.s.tx() as db:
            if name == "room":
                # What the room shows settles any of Patrick's room messages whose send was unclear.
                from .chat import settle_from_room
                settle_from_room(self.s, db, workspace_id, snap["events"])
            db.execute("INSERT INTO feed_snapshots(workspace_id, name, body, sha256, source_time, received_at) "
                       "VALUES(?,?,?,?,?,?) ON CONFLICT(workspace_id, name) DO UPDATE SET body = excluded.body, "
                       "sha256 = excluded.sha256, source_time = excluded.source_time, "
                       "received_at = excluded.received_at", (workspace_id, name, text, digest,
                                                              snap["source_time"], self.s.now()))
        return {"sha256": digest}

    def get(self, workspace_id: str, name: str) -> dict:
        row = self.s.db.execute("SELECT body, source_time, received_at FROM feed_snapshots "
                                "WHERE workspace_id = ? AND name = ?", (workspace_id, name)).fetchone()
        if not row:
            return {"as_of": None, "received_at": None, name: None}
        body = json.loads(row["body"])
        return {"as_of": row["source_time"], "received_at": row["received_at"], name: body}
