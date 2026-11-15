"""The phone's Board, live: what each Bot is doing right now, sent to the core as the "work" feed.

Patrick, 2 Nov: "Is supposed to show only what each is currently doing ... it's supposed to be live what
they are doing". Runs on the VM as the Bots' `hermes` user, all the time (tw-decide-live.service):

    python3 -I -m thornswood_decisions.live_reader

Each Bot's Hermes writes what its running turns are doing through the thornswood_live plugin, one small file
per process in `~/.hermes/profiles/<bot>/thornswood-live/<pid>.json`. Every 2 seconds this reads those
files, keeps the ones whose process is still the one that wrote them (its pid and its start time), and
sends the core one snapshot: one row for each turn running now, with the Bot, the session's title, thinking
or the tool it runs, and since when. A Bot with no turn running has no row. It sends when the snapshot
changes, and otherwise once in each 30 seconds, so the core's "as of" says the Board is being read.

It reads each Bot's `state.db` read-only for a running session's title, and nothing else; it writes nothing
to Hermes, holds only the work feed's token (~/.config/thornswood/feed-work.token, 0600) and logs no words
of any turn.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .feed_reader import CORE, _ro, read_token, send

FOLDER = "thornswood-live"   # thornswood_live.FOLDER
EVERY = 2.0
QUIET_SEND = 30.0
MAX_ROWS = 60
STATES = ("thinking", "tool")
TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
SESSION_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
PLATFORM = re.compile(r"^[a-z0-9_-]{1,32}$")
PID_FILE = re.compile(r"^[0-9]{1,10}\.json$")


def process_start(pid: int) -> str | None:
    """/proc/<pid>/stat field 22, as thornswood_live.process_start reads it for its own process."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
        return stat[stat.rindex(")") + 2:].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def _turns(folder: Path, alive) -> list[dict]:
    """The turns in one profile's files whose writer is still running."""
    out = []
    try:
        names = sorted(n for n in os.listdir(folder) if PID_FILE.match(n))
    except OSError:
        return out
    for name in names:
        try:
            body = json.loads((folder / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # gone between the listing and the read, or not ours
        if not isinstance(body, dict) or not isinstance(body.get("turns"), list):
            continue
        pid = body.get("pid")
        if not (isinstance(pid, int) and str(pid) + ".json" == name and alive(pid, body.get("start"))):
            continue  # its process has ended: what it said is no longer true
        for t in body["turns"]:
            if not isinstance(t, dict) or t.get("state") not in STATES:
                continue
            sid = t.get("session_id")
            if not (isinstance(sid, str) and SESSION_ID.match(sid)):
                continue
            tool = t.get("tool") if t.get("state") == "tool" else None
            if tool is not None and not (isinstance(tool, str) and TOOL_NAME.match(tool)):
                tool = None
            platform = t.get("platform")
            out.append({"session_id": sid, "state": "tool" if tool else "thinking",
                        "tool": tool, "since": str(t.get("since") or "")[:40],
                        "step_since": str(t.get("step_since") or "")[:40],
                        "platform": platform if isinstance(platform, str) and PLATFORM.match(platform) else ""})
    return out


def _titles(state_db: Path, ids: list[str]) -> dict[str, str]:
    if not ids or not state_db.exists():
        return {}
    try:
        con = _ro(state_db)
        try:
            rows = con.execute(f"SELECT id, title FROM sessions WHERE id IN ({','.join('?' * len(ids))})",
                               ids).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return {}  # the title is a help, never the reason a Bot's row is missing
    return {r["id"]: (r["title"] or "")[:200] for r in rows}


def _alive(pid: int, start) -> bool:
    return start is not None and process_start(pid) == start


def snapshot(hermes_home: Path, bots: list[str], now: datetime, alive=_alive) -> dict:
    """One row for each turn running now in each Bot's profile, in the Bots' order; a Bot with none, no row."""
    rows = []
    for bot in bots:
        pdir = hermes_home / "profiles" / bot
        turns = _turns(pdir / FOLDER, alive)
        titles = _titles(pdir / "state.db", sorted({t["session_id"] for t in turns}))
        seen = set()
        for t in sorted(turns, key=lambda t: t["since"]):
            if t["session_id"] in seen:
                continue  # one turn at a time in a session: two processes never both run it
            seen.add(t["session_id"])
            rows.append({"bot": bot, "title": titles.get(t["session_id"], ""), **t})
    return {"source_time": now.isoformat(), "doing": rows[:MAX_ROWS]}


def _same(a: dict | None, b: dict) -> bool:
    return a is not None and a["doing"] == b["doing"]


def main(argv: list[str]) -> int:
    home = Path(os.environ.get("HOME", "~")).expanduser()
    ap = argparse.ArgumentParser(prog="thornswood_decisions.live_reader")
    ap.add_argument("--hermes-home", default=str(home / ".hermes"))
    ap.add_argument("--tokens", default=str(home / ".config" / "thornswood"))
    ap.add_argument("--core", default=CORE)
    ap.add_argument("--once", action="store_true", help="read and send once, then stop (the install's check)")
    args = ap.parse_args(argv)
    from . import contracts
    bots = contracts.load()["profiles"]["ids"]
    hh, token_path = Path(args.hermes_home), Path(args.tokens) / "feed-work.token"
    last, sent_at, said = None, 0.0, None
    while True:
        body = snapshot(hh, bots, datetime.now(timezone.utc))
        due = time.monotonic() - sent_at >= QUIET_SEND
        if not _same(last, body) or due or args.once:
            status = send(args.core, "work", read_token(token_path), body)
            if status == 200:
                last, sent_at = body, time.monotonic()
            # One line for each change of how the core answers, not one every 2 seconds.
            line = f"{'PASS' if status == 200 else 'FAIL'} work: the core answered {status}"
            if line != said or args.once:
                print(line, flush=True)
                said = line
            if args.once:
                return 0 if status == 200 else 1
        time.sleep(EVERY)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
