"""Reads the Bots' Kanban and the Workbench room and sends the core a snapshot of each.

Runs on the VM as the Bots' `hermes` user, once a minute from
tw-decide-feed.timer:

    python3 -I -m thornswood_decisions.feed_reader --room <room id>

It opens Hermes's own stores read-only (`kanban.db` and `shared-state.db`
under ~/.hermes, the tables named in contracts.json), keeps only what the
phone's Work and Room views show, and sends each to the core on 127.0.0.1 with
that feed's token (~/.config/thornswood/feed-<name>.token, 0600). It writes
nothing to Hermes, holds no other credential and logs no message text.
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


def _ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _iso(epoch) -> str:
    if epoch is None:
        return ""
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat()


def work_snapshot(kanban_db: Path, now: datetime) -> dict:
    con = _ro(kanban_db)
    try:
        rows = con.execute("SELECT id, title, assignee, status, priority, created_at, started_at, completed_at "
                           "FROM tasks WHERE status != 'archived' ORDER BY priority DESC, created_at DESC "
                           "LIMIT 500").fetchall()
    finally:
        con.close()
    return {"source_time": now.isoformat(), "tasks": [
        {"id": r["id"], "title": r["title"], "assignee": r["assignee"] or "", "status": r["status"],
         "priority": int(r["priority"] or 0),
         "updated_at": _iso(r["completed_at"] or r["started_at"] or r["created_at"])} for r in rows]}


def room_snapshot(state_db: Path, room_id: str, now: datetime) -> dict:
    con = _ro(state_db)
    try:
        rows = con.execute("SELECT seq, kind, actor_json, payload_json, created_at FROM hosted_room_events "
                           "WHERE room_id = ? AND kind IN (?, ?) ORDER BY seq DESC LIMIT 200",
                           (room_id, *ROOM_KINDS)).fetchall()
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
        events.append({"seq": int(r["seq"]), "kind": r["kind"], "actor": str(name),
                       "text": text if isinstance(text, str) else "", "at": _iso(r["created_at"])})
    return {"source_time": now.isoformat(), "room_id": room_id, "events": events}


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
    ap.add_argument("--room", required=True, help="the Workbench room id")
    ap.add_argument("--hermes-home", default=str(home / ".hermes"))
    ap.add_argument("--tokens", default=str(home / ".config" / "thornswood"))
    ap.add_argument("--core", default=CORE)
    args = ap.parse_args(argv)
    hh, tokens = Path(args.hermes_home), Path(args.tokens)
    now = datetime.now(timezone.utc)
    bad = 0
    for name, make in (("work", lambda: work_snapshot(hh / "kanban.db", now)),
                       ("room", lambda: room_snapshot(hh / "shared-state.db", args.room, now))):
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
