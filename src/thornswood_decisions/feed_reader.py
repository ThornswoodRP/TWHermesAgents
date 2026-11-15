"""Reads the rooms and the six Bot Chats, and sends the core a snapshot of each.

Runs on the VM as the Bots' `hermes` user, once a minute from
tw-decide-feed.timer:

    python3 -I -m thornswood_decisions.feed_reader --room <room id> [--room <another room id>]

It opens Hermes's own stores read-only (`shared-state.db` under ~/.hermes, and
each Bot's `profiles/<id>/state.db`; the tables named in contracts.json), keeps
only what the phone's Room and Bot Chat views show, and sends each to the core
on 127.0.0.1 with that feed's token (~/.config/thornswood/feed-<name>.token,
0600). It writes nothing to Hermes, holds no other credential and logs no
message text. The Board is live_reader.py's: what each Bot is doing now.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .own_connector import MESSAGES_SQL, role_of, said_text, with_marks

CORE = "http://127.0.0.1:9135/api/v1"
ROOM_KINDS = ("message.user", "message.member")
# A room's threads (TWHermesAgents #108): Hermes keeps each message's thread in its payload's thread_id
# (gateway/hosted_room_discussion.py), and a thread is the messages that share one. The newest this many,
# by their last message, go with each room's snapshot.
ROOM_THREADS = 20
BOT_CHAT_TITLE = "Bot Chat"
BOT_MESSAGES = 40


def _ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _iso(epoch) -> str:
    if epoch is None:
        return ""
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat()


def _event(r) -> dict | None:
    """One room message as the phone shows it, with its thread; None when Hermes's row cannot be read."""
    try:
        actor = json.loads(r["actor_json"])
        payload = json.loads(r["payload_json"])
    except json.JSONDecodeError:
        return None
    if not isinstance(actor, dict) or not isinstance(payload, dict):
        return None
    name = actor.get("display_name") or actor.get("profile") or actor.get("id") or ""
    text, thread = payload.get("text"), payload.get("thread_id")
    return {"seq": int(r["seq"]), "event_id": str(r["event_id"]), "kind": r["kind"], "actor": str(name),
            "text": text if isinstance(text, str) else "", "at": _iso(r["created_at"]),
            "thread": thread if isinstance(thread, str) else ""}


def room_threads(con: sqlite3.Connection, room_id: str, limit: int = ROOM_THREADS) -> list[dict]:
    """The room's newest threads by their last message: each one's id, how many messages it has, who started it
    and with what words (its first message), and when its last message came."""
    rows = con.execute(
        "SELECT json_extract(payload_json, '$.thread_id') AS thread, count(*) AS n, min(seq) AS first, "
        "max(seq) AS last FROM hosted_room_events WHERE room_id = ? AND kind IN (?, ?) "
        "AND json_valid(payload_json) AND json_type(payload_json, '$.thread_id') = 'text' "
        "GROUP BY thread ORDER BY last DESC LIMIT ?", (room_id, *ROOM_KINDS, limit)).fetchall()
    out = []
    for t in rows:
        ends = {r["seq"]: _event(r) for r in con.execute(
            "SELECT seq, event_id, kind, actor_json, payload_json, created_at FROM hosted_room_events "
            "WHERE room_id = ? AND seq IN (?, ?)", (room_id, t["first"], t["last"])).fetchall()}
        first, last = ends.get(t["first"]), ends.get(t["last"])
        if first is None or last is None:
            continue
        out.append({"thread": t["thread"], "count": int(t["n"]), "by": first["actor"], "title": first["text"][:200],
                    "last_seq": int(t["last"]), "at": last["at"]})
    return out


def room_snapshot(state_db: Path, room_id: str, now: datetime, limit: int = 200) -> dict:
    con = _ro(state_db)
    try:
        rows = con.execute("SELECT seq, event_id, kind, actor_json, payload_json, created_at FROM hosted_room_events "
                           "WHERE room_id = ? AND kind IN (?, ?) ORDER BY seq DESC LIMIT ?",
                           (room_id, *ROOM_KINDS, limit)).fetchall()
        threads = room_threads(con, room_id)
    finally:
        con.close()
    events = [e for e in (_event(r) for r in reversed(rows)) if e is not None]
    return {"source_time": now.isoformat(), "room_id": room_id, "events": events, "threads": threads}


def room_page(state_db: Path, room_id: str, before: int | None, n: int = 50, thread: str | None = None) -> dict:
    """One page of a room for the phone's full history: the newest `n` messages before the room's sequence
    number `before`, oldest first, the same events the room feed sends. With `thread`, only that thread's
    messages (#108), and the newest page says the thread's own count and start, as the room's list of threads
    does."""
    con = _ro(state_db)
    try:
        rows = con.execute("SELECT seq, event_id, kind, actor_json, payload_json, created_at FROM hosted_room_events "
                           "WHERE room_id = ? AND kind IN (?, ?) AND (? IS NULL OR seq < ?) "
                           "AND (? IS NULL OR (json_valid(payload_json) AND json_extract(payload_json, '$.thread_id') = ?)) "
                           "ORDER BY seq DESC LIMIT ?",
                           (room_id, *ROOM_KINDS, before, before, thread, thread, n + 1)).fetchall()
    finally:
        con.close()
    events = []
    for r in reversed(rows[:n]):
        e = _event(r)
        if e is not None:
            del e["event_id"]
            events.append(e)
    return {"events": events, "more": len(rows) > n}


def rooms_snapshot(state_db: Path, room_ids: list[str], now: datetime) -> dict:
    """Every room in one snapshot; with several, fewer of each one's last events, so the feed stays small."""
    limit = max(25, 200 // max(1, len(room_ids)))
    rooms = [room_snapshot(state_db, r, now, limit) for r in room_ids]
    return {"source_time": now.isoformat(),
            "rooms": [{"room_id": r["room_id"], "events": r["events"], "threads": r["threads"]} for r in rooms]}


def _text(content) -> str:
    """A message's content as plain text: a string, or the text parts of a list of parts."""
    if isinstance(content, str):
        try:
            parts = json.loads(content) if content.startswith("[") else None
        except json.JSONDecodeError:
            parts = None
        if not isinstance(parts, list):
            return content
        content = parts
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def bot_chat(state_db: Path) -> dict | None:
    """The canonical Bot Chat of one profile: its id and its last messages from Patrick and the Bot."""
    if not state_db.exists():
        return None
    con = _ro(state_db)
    try:
        s = con.execute("SELECT id FROM sessions WHERE title = ? AND archived = 0 ORDER BY started_at DESC LIMIT 1",
                        (BOT_CHAT_TITLE,)).fetchone()
        if not s:
            return None
        # A row Hermes put in by itself (a background command's notice) is a notice, never his (own_connector
        # NOTICE_KINDS); one it never shows is not read.
        rows = con.execute(with_marks(con, MESSAGES_SQL), (s["id"], BOT_MESSAGES)).fetchall()
    finally:
        con.close()
    return {"session_id": s["id"], "messages": [
        {"role": role_of(r), "text": _text(r["content"]) if role_of(r) != "notice" else said_text(r),
         "at": _iso(r["timestamp"])} for r in reversed(rows)]}


# The model a Bot's newest turn ran on. After each turn the gateway writes the model it really used on that
# session's row, with the route it took in model_config.gateway_runtime: its provider and fallback_active,
# true while Hermes has moved the session off its main model (Hermes 0.23.0
# gateway/run_turn_prepare.py _sync_session_model_from_agent, called from gateway/run_turn_runner.py after a
# turn). So the newest such row says what the Bot is on now, a fallback included, in Hermes's own words.
ROUTE_SQL = """
SELECT model, json_extract(model_config, '$.gateway_runtime.provider') AS provider,
  json_extract(model_config, '$.gateway_runtime.fallback_active') AS fallback,
  coalesce(last_activity_at, started_at) AS at FROM sessions
WHERE model IS NOT NULL AND model != '' AND model_config IS NOT NULL AND json_valid(model_config)
  AND json_type(model_config, '$.gateway_runtime') = 'object'
ORDER BY at DESC LIMIT 1
"""


def bot_route(state_db: Path) -> dict | None:
    """The model, provider and fallback state of one profile's newest turn; None before its first turn."""
    if not state_db.exists():
        return None
    con = _ro(state_db)
    try:
        r = con.execute(ROUTE_SQL).fetchone()
    finally:
        con.close()
    if not r:
        return None
    return {"model": str(r["model"]), "provider": str(r["provider"] or ""), "fallback": r["fallback"] in (1, True),
            "at": _iso(r["at"])}


def bots_snapshot(hermes_home: Path, bots: list[str], now: datetime) -> dict:
    out = []
    for bot in bots:
        db = hermes_home / "profiles" / bot / "state.db"
        chat = bot_chat(db)
        out.append({"bot": bot, "session_id": chat["session_id"] if chat else "",
                    "messages": chat["messages"] if chat else [], "route": bot_route(db)})
    return {"source_time": now.isoformat(), "bots": out}


def read_token(path: Path) -> str:
    st = path.stat()
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise SystemExit(f"STOP: {path} is readable by others; it must be 0600")
    return path.read_text(encoding="utf-8").strip()


def send(core: str, name: str, token: str, body: dict) -> int:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"{core}/feeds/{name}", data=data, method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=15) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code
    except (urllib.error.URLError, OSError):
        return 0


def main(argv: list[str]) -> int:
    home = Path(os.environ.get("HOME", "~")).expanduser()
    ap = argparse.ArgumentParser(prog="thornswood_decisions.feed_reader")
    ap.add_argument("--room", required=True, action="append", help="a room's id; once for each room")
    ap.add_argument("--hermes-home", default=str(home / ".hermes"))
    ap.add_argument("--tokens", default=str(home / ".config" / "thornswood"))
    ap.add_argument("--core", default=CORE)
    args = ap.parse_args(argv)
    hh, tokens = Path(args.hermes_home), Path(args.tokens)
    now = datetime.now(timezone.utc)
    bad = 0
    from . import contracts
    bots = contracts.load()["profiles"]["ids"]
    feeds = [("room", lambda: rooms_snapshot(hh / "shared-state.db", args.room, now)),
             ("bots", lambda: bots_snapshot(hh, bots, now))]
    for name, make in feeds:
        try:
            body = make()
        except (sqlite3.Error, OSError) as exc:
            print(f"FAIL {name}: could not read Hermes's store ({type(exc).__name__})")
            bad += 1
            continue
        status = send(args.core, name, read_token(tokens / f"feed-{name}.token"), body)
        print(f"{'PASS' if status == 200 else 'FAIL'} {name}: the core answered {status}")
        bad += status != 200
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
