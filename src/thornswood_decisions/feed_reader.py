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

CORE = "http://127.0.0.1:9135/api/v1"
ROOM_KINDS = ("message.user", "message.member")
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


def room_snapshot(state_db: Path, room_id: str, now: datetime, limit: int = 200) -> dict:
    con = _ro(state_db)
    try:
        rows = con.execute("SELECT seq, event_id, kind, actor_json, payload_json, created_at FROM hosted_room_events "
                           "WHERE room_id = ? AND kind IN (?, ?) ORDER BY seq DESC LIMIT ?",
                           (room_id, *ROOM_KINDS, limit)).fetchall()
    finally:
        con.close()
    events = []
    for r in reversed(rows):
        try:
            actor = json.loads(r["actor_json"])
            payload = json.loads(r["payload_json"])
        except json.JSONDecodeError:
            continue
        name = actor.get("display_name") or actor.get("profile") or actor.get("id") or ""
        text = payload.get("text") if isinstance(payload, dict) else ""
        events.append({"seq": int(r["seq"]), "event_id": str(r["event_id"]), "kind": r["kind"], "actor": str(name),
                       "text": text if isinstance(text, str) else "", "at": _iso(r["created_at"])})
    return {"source_time": now.isoformat(), "room_id": room_id, "events": events}


def room_page(state_db: Path, room_id: str, before: int | None, n: int = 50) -> dict:
    """One page of a room for the phone's full history: the newest `n` messages before the room's sequence
    number `before`, oldest first, the same events the room feed sends."""
    con = _ro(state_db)
    try:
        rows = con.execute("SELECT seq, event_id, kind, actor_json, payload_json, created_at FROM hosted_room_events "
                           "WHERE room_id = ? AND kind IN (?, ?) AND (? IS NULL OR seq < ?) ORDER BY seq DESC LIMIT ?",
                           (room_id, *ROOM_KINDS, before, before, n + 1)).fetchall()
    finally:
        con.close()
    events = []
    for r in reversed(rows[:n]):
        try:
            actor = json.loads(r["actor_json"])
            payload = json.loads(r["payload_json"])
        except json.JSONDecodeError:
            continue
        name = actor.get("display_name") or actor.get("profile") or actor.get("id") or ""
        text = payload.get("text") if isinstance(payload, dict) else ""
        events.append({"seq": int(r["seq"]), "kind": r["kind"], "actor": str(name),
                       "text": text if isinstance(text, str) else "", "at": _iso(r["created_at"])})
    return {"events": events, "more": len(rows) > n}


def rooms_snapshot(state_db: Path, room_ids: list[str], now: datetime) -> dict:
    """Every room in one snapshot; with several, fewer of each one's last events, so the feed stays small."""
    limit = max(25, 200 // max(1, len(room_ids)))
    rooms = [room_snapshot(state_db, r, now, limit) for r in room_ids]
    return {"source_time": now.isoformat(),
            "rooms": [{"room_id": r["room_id"], "events": r["events"]} for r in rooms]}


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
        rows = con.execute("SELECT role, content, timestamp FROM messages WHERE session_id = ? AND active = 1 "
                           "AND role IN ('user', 'assistant') AND content IS NOT NULL AND content != '' "
                           "ORDER BY id DESC LIMIT ?", (s["id"], BOT_MESSAGES)).fetchall()
    finally:
        con.close()
    return {"session_id": s["id"], "messages": [
        {"role": r["role"], "text": _text(r["content"]), "at": _iso(r["timestamp"])} for r in reversed(rows)]}


def bots_snapshot(hermes_home: Path, bots: list[str], now: datetime) -> dict:
    out = []
    for bot in bots:
        chat = bot_chat(hermes_home / "profiles" / bot / "state.db")
        out.append({"bot": bot, "session_id": chat["session_id"] if chat else "",
                    "messages": chat["messages"] if chat else []})
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
