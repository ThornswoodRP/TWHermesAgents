"""The phone's Work and Room views: read-only snapshots the Bots' Hermes sends in.

Hermes keeps the Kanban and the Workbench room; the core keeps no copy of
either beyond the latest snapshot of each, shown with the time it was read
("as of"). The snapshot comes from `feed_reader.py`, which runs as the Bots'
`hermes` user with a feed token. That token can do one thing: replace one
snapshot. It cannot read a card, answer, or reach anything else, and what it
sends is shown as text, never as markup. Because any Bot could read that token,
these views are what the Bots' Hermes reports, not authority: a decision is
only ever a card (07 section 5, "never authority").
"""

from __future__ import annotations

import hmac
import json
import secrets

from .store import CoreError, Store, canonical, parse, sha256_hex

NAMES = ("work", "room")
MAX_FEED_BYTES = 512 * 1024
MAX_TASKS = 500
MAX_EVENTS = 200
TASK_FIELDS = {"id": 64, "title": 200, "assignee": 64, "status": 32, "updated_at": 40}
EVENT_FIELDS = {"kind": 32, "actor": 80, "text": 2000, "at": 40}


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
        """A new token for one feed, replacing the old one. Shown once; only its hash is kept."""
        if name not in NAMES:
            raise CoreError(400, "bad_feed", f"the feeds are {', '.join(NAMES)}")
        token = secrets.token_urlsafe(32)
        with self.s.tx() as db:
            db.execute("INSERT INTO feed_sources(workspace_id, name, token_hash, created_at) VALUES(?,?,?,?) "
                       "ON CONFLICT(workspace_id, name) DO UPDATE SET token_hash = excluded.token_hash, "
                       "created_at = excluded.created_at", (workspace_id, name, sha256_hex(token), self.s.now()))
            self.s.event(db, workspace_id, "feed.token_made", name, by, {})
        return token

    def put(self, workspace_id: str, name: str, token: str | None, body: dict) -> dict:
        if name not in NAMES:
            raise CoreError(404, "no_route", "no such feed")
        row = self.s.db.execute("SELECT token_hash FROM feed_sources WHERE workspace_id = ? AND name = ?",
                                (workspace_id, name)).fetchone()
        if not row or not token or not hmac.compare_digest(row["token_hash"], sha256_hex(token)):
            raise CoreError(401, "bad_feed_token", "that is not this feed's token")
        snap = clean(name, body)
        if name == "room":
            bound = self.s.db.execute("SELECT room_id FROM room_bindings WHERE workspace_id = ?",
                                      (workspace_id,)).fetchall()
            if snap["room_id"] not in {r[0] for r in bound}:
                raise CoreError(409, "not_the_room", "the feed names a room this workspace is not bound to")
        text = canonical(snap)
        digest = sha256_hex(text)
        with self.s.tx() as db:
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
