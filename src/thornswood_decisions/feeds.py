"""The phone's Board, Rooms and Bot Chat views: read-only snapshots the Bots' Hermes sends in.

The work feed is the live Board (Patrick, 2 Nov: "only what each is currently
doing"): `doing`, one row for each turn a Bot is running now, with the Bot, the
session's title, thinking or the tool it runs, and since when. A Bot with no
turn running has no row, and nothing that ended is kept. It comes from
`live_reader.py` every 2 seconds while it changes. Hermes keeps the rooms; the
core keeps no copy of any feed beyond its latest snapshot. The
room feed carries every bound room at once, `{source_time, rooms: [{room_id,
events}]}`; the first shape, one room as `{source_time, room_id, events}`, is
still taken and kept as a list of one. The room and Bot Chat snapshots come from `feed_reader.py`, which runs as
the Bots' `hermes` user with a feed token, and the work feed from
`live_reader.py`, the same way. That token can do one thing: replace one
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
import re
import secrets

from .store import CoreError, Store, canonical, parse, sha256_hex

NAMES = ("work", "room", "bots")
TOKENS = NAMES + ("connector", "push", "own-me", "own-one")
MAX_FEED_BYTES = 512 * 1024
MAX_EVENTS = 200
MAX_ROOMS = 8
MAX_BOT_MESSAGES = 40
EVENT_FIELDS = {"event_id": 128, "kind": 32, "actor": 80, "text": 2000, "at": 40, "thread": 128}
# A room's newest threads (feed_reader.room_threads, TWHermesAgents #108).
MAX_THREADS = 20
THREAD_FIELDS = {"thread": 128, "by": 80, "title": 200, "at": 40}
BOT_MESSAGE_FIELDS = {"role": 16, "text": 2000, "at": 40}
ROLES = ("user", "assistant")
# The live Board (live_reader.py): one row for each turn running now.
MAX_DOING = 60
DOING_FIELDS = {"bot": 64, "title": 200, "since": 40, "platform": 32}
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
# A room thread's id, as Hermes takes one (gateway/hosted_rooms_common.py IDENTIFIER_RE, 128 characters at most).
THREAD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")  # own.SESSION_ID: the row opens that session
LIVE_FOR = 90
TOOL = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


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
        doing = body.get("doing")
        if not isinstance(doing, list) or len(doing) > MAX_DOING:
            raise CoreError(400, "bad_feed", f"doing is a list of at most {MAX_DOING}")
        out["doing"] = []
        for d in doing:
            if not isinstance(d, dict):
                raise CoreError(400, "bad_feed", "each row of the Board is an object")
            row = {k: _text(d.get(k), n) for k, n in DOING_FIELDS.items()}
            tool = d.get("tool")
            row["tool"] = tool if isinstance(tool, str) and TOOL.fullmatch(tool) else ""
            sid = d.get("session_id")
            row["session_id"] = sid if isinstance(sid, str) and SESSION_ID.fullmatch(sid) else ""
            row["state"] = "tool" if row["tool"] and d.get("state") == "tool" else "thinking"
            out["doing"].append(row)
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
        rooms = body.get("rooms") if "rooms" in body else [{"room_id": body.get("room_id"), "events": body.get("events")}]
        if not isinstance(rooms, list) or not 1 <= len(rooms) <= MAX_ROOMS:
            raise CoreError(400, "bad_feed", f"rooms is a list of 1 to {MAX_ROOMS}")
        out["rooms"] = [_room(r) for r in rooms]
        ids = [r["room_id"] for r in out["rooms"]]
        if len(set(ids)) != len(ids):
            raise CoreError(400, "bad_feed", "the feed names a room twice")
    return out


def _room(r) -> dict:
    events = r.get("events") if isinstance(r, dict) else None
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise CoreError(400, "bad_feed", f"each room's events is a list of at most {MAX_EVENTS}")
    kept = []
    for e in events:
        seq = e.get("seq") if isinstance(e, dict) else None
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            raise CoreError(400, "bad_feed", "each event has its room seq")
        kept.append({"seq": seq, **{k: _text(e.get(k), n) for k, n in EVENT_FIELDS.items()}})
    # A feed reader from before #108 sends no threads: the room then shows none.
    threads = r.get("threads", [])
    if not isinstance(threads, list) or len(threads) > MAX_THREADS:
        raise CoreError(400, "bad_feed", f"each room's threads is a list of at most {MAX_THREADS}")
    listed = []
    for t in threads:
        if not isinstance(t, dict):
            raise CoreError(400, "bad_feed", "each thread is a JSON object")
        row = {k: _text(t.get(k), n) for k, n in THREAD_FIELDS.items()}
        count, last = t.get("count"), t.get("last_seq")
        if not THREAD_ID.fullmatch(row["thread"]) or any(
                not isinstance(v, int) or isinstance(v, bool) or v < 1 for v in (count, last)):
            raise CoreError(400, "bad_feed", "each thread has Hermes's thread id, its count and its last seq")
        listed.append(row | {"count": count, "last_seq": last})
    return {"room_id": _text(r.get("room_id"), 64), "events": kept, "threads": listed}


def room_threads(body: dict | None, room_id: str) -> list[dict]:
    """One room's newest threads from a stored room snapshot; none from a snapshot that has them not."""
    if not body:
        return []
    rooms = body.get("rooms") if "rooms" in body else [body]
    return next((r.get("threads", []) for r in rooms if r.get("room_id") == room_id), [])


def room_events(body: dict | None, room_id: str) -> list[dict]:
    """One room's events from a stored room snapshot, of either shape."""
    if not body:
        return []
    rooms = body.get("rooms") if "rooms" in body else [body]
    return next((r.get("events", []) for r in rooms if r.get("room_id") == room_id), [])


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
            if not {r["room_id"] for r in snap["rooms"]} <= {r[0] for r in bound}:
                raise CoreError(409, "not_the_room", "the feed names a room this workspace is not bound to")
        if name in ("bots", "work"):
            bound = {r[0] for r in self.s.db.execute("SELECT bot FROM bot_bindings WHERE workspace_id = ?",
                                                     (workspace_id,)).fetchall()}
            names = [b["bot"] for b in snap["bots" if name == "bots" else "doing"]]
            # A Bot has one Bot Chat, and may run several turns at once.
            if (name == "bots" and len(set(names)) != len(names)) or not set(names) <= bound:
                raise CoreError(409, "not_the_bots", "the feed names a Bot this workspace does not have, or one twice")
        text = canonical(snap)
        digest = sha256_hex(text)
        with self.s.tx() as db:
            if name == "room":
                # What the room shows settles any of Patrick's room messages whose send was unclear.
                from .chat import settle_from_room
                for r in snap["rooms"]:
                    settle_from_room(self.s, db, workspace_id, r["room_id"], r["events"])
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
        out = {"as_of": row["source_time"], "received_at": row["received_at"], name: body}
        if name == "work":
            # The live Board is true only while its reader is sending: it sends at least once in each 30
            # seconds, so a snapshot older than LIVE_FOR is not shown as what the Bots are doing now.
            age = (parse(self.s.now()) - parse(row["received_at"])).total_seconds()
            out["live"] = age <= LIVE_FOR
        return out
