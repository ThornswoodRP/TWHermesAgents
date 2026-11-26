"""The connector: carries Patrick's chat, and the "Bot told" messages, from the core into Hermes.

Runs on the VM as the Bots' `hermes` user (tw-decide-connector.service), with
the system Python and only this repository's code:

    python3 -I -m thornswood_decisions.connector --hermes /home/hermes/.local/bin/hermes

It does nothing while `connector_effects` is off. Otherwise, the moment the
core has new work for it (it holds a wait open, `POST /api/v1/connector/wait`,
wakes.py), when one of its turns ends, and every few seconds whatever happens,
it leases from the core the oldest waiting message for each Bot and the room
that has nothing in flight (`POST /api/v1/connector/claim`, with its own token
in ~/.config/thornswood/connector.token, 0600) and hands each to Hermes the
way Hermes itself does:

- a Bot Chat: `hermes -p <bot> chat --in ~ -c "Bot Chat" --create-if-missing
  -Q --format stream-json --query-file <file>`, the local transport of Hermes's own message_agent
  (tools/bot_mode_dm.py:11), so the message lands in that Bot's one canonical
  Bot Chat, the same one Desktop shows. The text goes in a 0600 file in a 0700
  folder, never on a command line, and the file is gone when the turn ends.
  Before it, a readiness check finds the Bot's gateway without starting one.
- one session of a Bot, the one a Board row opens (Patrick, 9 Nov, #77): `room_send.py chat -p <bot> -Q
  --format stream-json --query-file <file> --resume <session>` with Hermes's own Python and that Bot's own
  HERMES_HOME, through the Bot's running gateway, so a turn running there takes it by the session's busy
  setting (to_session). Only a session in that Bot's own store.
- the room: `room_send.py` with Hermes's own Python, which calls `groups.send`
  on the running gateway with the core's key as the event id.

A message's files and photos (#81) are fetched from the core while it is
leased (`POST /api/v1/connector/attachment`), each checked against its size
and SHA-256 before anything reaches Hermes. For a Bot they are saved in that
Bot's own home, where its Hermes stages what Desktop drops into its box
(own_connector.fetch_files), and handed over as Desktop hands them: each photo
as the turn's image, every other file as an @file: reference (hand_over). For
a room they go into the room's own store, and the message names them
(room_send.py _send). A file that does not come whole fails the message, and
nothing was sent.

The helper (room_send.py: readiness, room, reactions) runs inside Hermes's own
runtime: its real Python, with Hermes's source on the path, found the way
own_connector.py finds it (`hermes --print-runtime-command`, else the launcher
followed by hand, never a shell). Since 30 Oct the Bots' `hermes` is a /bin/sh
wrapper, and taking its #! line for the Python ran the helper with /bin/sh:
every readiness check failed and read as "could not check the Bots' gateway"
until 2 Nov. A helper that does not start or does not answer in JSON is a
failure of the helper, said as one (a FAIL line in the journal, and the
message's reason, with the tail of what it wrote to stderr), never a gateway
that is not ready. Nothing was sent, so the message still goes back in the
queue as it was (an install that is replacing Hermes's Python is the same
case, 1 Nov), and the connector claims nothing more until the helper answers.

Every few seconds, whatever connector_effects says, it also answers the pages of
full history the phone asked for (`POST /api/v1/connector/pages`, then
`/connector/page`): a Bot's Bot Chat from that profile's state.db and a room
from the store that holds it (feed_reader.room_store: the gateway's own state.db
since Hermes 0.23 moved the rooms there, else shared-state.db), both read only,
50 messages at a time.
The same claim hands it Patrick's reactions on Bot Chat messages: each is set in
that Bot's own store with `room_send.py` op `react` (Hermes's SessionDB), and
what came of it goes to `POST /api/v1/connector/reacted`.
While a Bot Chat turn runs, what it is doing (thinking, writing, or a tool by its
plain name, read from Hermes's stream with own_connector.follow_turn, never the
words) goes to `POST /api/v1/connector/doing` when it changes, for the phone's
live line under the message. What the turn shows, as Hermes Desktop draws it (its
thinking, the reply as it is written, each tool with its preview, its subagents and
status lines, own_connector.LiveTurn), goes to `POST /api/v1/connector/turns` with
each change, a few times a second at most, held by the core in memory only.
For each message, it tells the core what came of it (`POST /api/v1/connector/result`):
confirmed only when Hermes said so, failed only when nothing was sent, and
uncertain for everything else. An uncertain message is never sent again by
itself. At the local viewer's deadline, an exact receipt for its own queued or
started admission confirms delivery only. The gateway owns that work and may
keep running after the viewer detaches; this is never reported as turn finished.
A gateway that is not ready (it is starting, stopped, being reloaded by
an install, or its check did not answer in time) is not a failure of the message:
nothing was sent, so the outcome is not_ready and the core puts the message back
in the queue as it was. The connector then claims nothing more until that
gateway's readiness check says ready, once each round, and the message goes
then. A result the core did not take is kept and offered again until it
lands; that resends the record, never the message. Nothing here logs message
text or a token, holds a GitHub credential or listens on any port.

Every half minute, whatever connector_effects says, it also tells the core each
Bot's gateway state for the phone's online dots (`POST /api/v1/connector/live`):
Hermes's own runtime state, from `room_send.py` op `states`, which finds each
gateway the way the readiness check does and never starts one. The core names
the Bots to check in its answer.

Every half minute too, it sends the core what each of those Bots has going
besides its chats, for the app's Tasks tab (`POST /api/v1/connector/tasks`):
its subagents, background commands and scheduled jobs, from `room_send.py` op
`tasks`, read the way Hermes Desktop reads them (contracts.json "tasks").

Those reports and the phone's pages run on a thread of their own, apart from
the claims, and the phone's pages go the moment it asks for one. Each turn runs
on its own thread and says its result the moment it ends; the next claim does
not wait for it, so a message to one Bot never waits for another Bot's turn
(Patrick, 9 Nov: "it doesnt feel live"), up to PARALLEL turns at once.

A turn outlives the connector (#163). Before 15 Nov each was a child writing into a pipe the connector read,
so an install that started the connector again for a change to its own code (connector.py, own_connector.py,
room_send.py or contracts.json, in about 15 to 22 merges from 5 to 12 Nov) ended every turn it ran. Now each
runs under turn_run.py in a session of its own, writing Hermes's stream into a file in its own folder
(TURNS_DIR), and the unit stops only the connector (KillMode=process), so the turn goes on in the same unit
with the same sandbox. The connector that starts next picks up every turn still there (Connector.adopt):
it follows each from the start of its stream to its end, as if it had started it, and says what came of it
while the core still holds the message's lease (chat.LEASE, longer than TURN_SECONDS).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from .own_connector import LiveTurn, Waker, fetch_files, follow_turn, hand_over, probe_env

CORE = "http://127.0.0.1:9135/api/v1"
POLL_SECONDS = 5
WAIT_SECONDS = 10  # one wait on the core for new work, held open (under the posts' 15-second timeout)
TURN_SECONDS = 15 * 60
CHECK_SECONDS = 60
LIVE_SECONDS = 30  # how often each Bot's gateway state goes to the core, for the phone's dots
TASKS_SECONDS = 30  # how often each Bot's subagents, background commands and scheduled jobs go to the core
# At most this many `hermes` turns at once: one for each Bot and the room,
# since the core leases one message per target at a time (chat.MAX_CLAIM).
# Each is a whole Hermes process, and the unit's MemoryMax (900M) holds them
# all. 12 Nov 2026: it was 3, so at 12:07 AM Mountain, with Bramble's five
# briefs queued, Echo, Sprocket and Rift ran and Magpie and Wisp waited for
# a slot, not for memory or the fleet queue. Measured then on the VM: each
# `hermes` child 74 to 76 MiB RSS, the connector 32 MiB, the unit's peak
# 237 MiB with three running, so seven fit in about 600 MiB. The fleet
# queue still decides how many model turns run (policy admission).
PARALLEL = 7
DOING_SECONDS = 0.25  # while turns run, what each is doing and shows goes to the core this often when it changed
DOING_KEEP = 20      # and at least this often, so the core knows the line is current (chat.py DOING_STALE)
BOT_CHAT = "Bot Chat"
PAGE_BYTES = 480 * 1024  # under the core's 512 KB
HELPER = Path(__file__).with_name("room_send.py")
# Each Bot Chat turn runs under this, apart from the connector, so that it outlives it (#163, turn_run.py).
RUNNER = Path(__file__).with_name("turn_run.py")
# Where each turn the connector hands to Hermes keeps its state, under the Bots' user's home: one folder a
# message (0700), with the message as the core leased it (item.json, the connector's), how its turn was
# started (turn.json) and its process (pid.json, Hermes's), the turn's stream as Hermes writes it (out), how
# it ended (exit, turn_run.py), the message's text while the turn runs (message.txt), and what came of it
# until the core takes that (result.json). A connector that starts while turns of the one before it still
# run follows each of them to its end from here (Connector.adopt).
TURNS_DIR = (".local", "state", "thornswood", "connector-turns")
ITEM, TURN, PID, OUT, EXIT, RESULT, MESSAGE = ("item.json", "turn.json", "pid.json", "out", "exit", "result.json",
                                               "message.txt")
# What came of a message whose turn the connector before this one never started: nothing was sent.
NOT_HANDED = "the connector stopped before it handed the message to Hermes"
NOT_STARTED = "not-started"  # turn_run.NOT_STARTED: the turn's command could not start, so nothing was sent
FOLLOW_SECONDS = 0.1  # how often a turn's stream file is read again while the turn runs
ITEM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")  # store.new_id
HELPER_FAILED = "the connector's Hermes helper"  # how a helper failure's reason begins (chat.py HELPER_FAILED)
# How the reason begins when a Bot Chat message waits for the fleet queue (tw-policy) instead of the
# gateway (chat.py FLEET). The Bots' turn gate turns away a turn the queue has no room for, and that turn
# runs nothing (src/thornswood_guard/brokers.py TURNED_AWAY_DIR). 2 Nov 2026: Patrick's answer on the #5327
# ad18c2c card went to Bramble while the queue was turning turns away, the turn ended saying it had not
# started, and the connector called it sent. Now a message goes only when the queue would admit its turn,
# and one whose turn was turned away all the same goes back in the queue, unspent, like a gateway that
# was not ready.
FLEET = "the fleet queue"
# ...unless the turn gate kept the message in the core (brokers.TurnGate._keep), which then delivers it
# once the queue has room: the message is handed over, and it is not put back a second time.
KEPT = object()
TURNED_AWAY_DIR = ("local", "thornswood-hermes", "turned-away")
ROOM_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")  # own.SESSION_ID
THREAD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")  # feeds.THREAD_ID: a room's thread, as Hermes takes one


def bot_chat_id(hermes_home: Path, bot: str) -> str | None:
    """The id of a Bot's one canonical Bot Chat, from that profile's state.db, read only; None when it has none."""
    from .own_connector import _ro
    db = hermes_home / "profiles" / bot / "state.db"
    if not db.exists():
        return None
    con = _ro(db)
    try:
        s = con.execute("SELECT id FROM sessions WHERE title = ? AND archived = 0 ORDER BY started_at DESC LIMIT 1",
                        (BOT_CHAT,)).fetchone()
    finally:
        con.close()
    return s["id"] if s else None


def bot_session(hermes_home: Path, bot: str, sid: str) -> bool:
    """Whether that session is one of this Bot's own, in its profile's state.db, read only."""
    from .own_connector import _ro
    db = hermes_home / "profiles" / bot / "state.db"
    if not db.exists():
        return False
    con = _ro(db)
    try:
        return con.execute("SELECT 1 FROM sessions WHERE id = ?", (sid,)).fetchone() is not None
    finally:
        con.close()


def bot_page(hermes_home: Path, bot: str, before: int | None, session: str | None = None) -> dict:
    """One page of a Bot's one canonical Bot Chat, from that profile's state.db, read only. With `session`,
    a page of that one session of the Bot instead (the one the Board shows it working in), when it is one
    of that Bot's own."""
    from .own_connector import messages_page
    sid = session if session is not None and bot_session(hermes_home, bot, session) else \
        None if session is not None else bot_chat_id(hermes_home, bot)
    if sid is None:
        return {"messages": [], "more": False, "gone": True, "continued_as": None}
    db = hermes_home / "profiles" / bot / "state.db"
    n = 50
    while True:
        page = messages_page(db, sid, before, n)
        page["continued_as"] = None  # the phone names a Bot's chat by the Bot (and a session by its id)
        if len(json.dumps(page, ensure_ascii=False).encode("utf-8")) <= PAGE_BYTES or n <= 1:
            return page
        n //= 2


def room_store(hermes_home: Path, room_id: str) -> Path:
    from .feed_reader import room_store as where
    return where(hermes_home, room_id)


def room_page(shared_db: Path, room_id: str, before: int | None, thread: str | None = None) -> dict:
    from .feed_reader import room_page as read
    n = 50
    while True:
        page = read(shared_db, room_id, before, n, thread=thread)
        if len(json.dumps(page, ensure_ascii=False).encode("utf-8")) <= PAGE_BYTES or n <= 1:
            return page
        n //= 2


def _rewind_args(rewind) -> list[str]:
    """room_send.py's own arguments for a retry or an edit, before `hermes chat`'s (rewinds.py)."""
    if not rewind:
        return []
    return ["--tw-rewind", rewind[0], str(rewind[1])]


def read_token(path: Path) -> str:
    st = path.stat()
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise SystemExit(f"STOP: {path} is readable by others; it must be 0600")
    return path.read_text(encoding="utf-8").strip()


def hermes_python(hermes_bin: str) -> str:
    """The real Python the `hermes` command runs Hermes with (own_connector.hermes_runtime), never a shell."""
    from .own_connector import hermes_python as find
    return find(hermes_bin)


def helper_command(hermes_bin: str, python: str | None = None, root: str | None = None,
                   helper: Path = HELPER) -> list[str]:
    """How the helper runs inside Hermes's own runtime (own_connector.helper_command)."""
    from .own_connector import helper_command as find
    return find(hermes_bin, helper, python=python, root=root)


def _tail(data: bytes, n: int = 160) -> str:
    text = " ".join(data.decode("utf-8", "replace").split())
    return text[-n:] if text else "nothing"


def _write_json(path: Path, value: dict) -> None:
    """Whole or not at all, 0600: a connector that starts after this one reads it as it was written."""
    part = path.with_name(path.name + ".part")
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(value, f)
    os.replace(part, path)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def proc_start(pid: int) -> str | None:
    """When a process started (field 22 of /proc/<pid>/stat), so a pid used again later is never taken for it
    (thornswood_brokers.policy_service.holder reads it the same way); None when it is not running (or only a
    zombie), or where there is no /proc."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None
    if not fields or fields[0] in ("Z", "X"):
        return None
    return fields[19] if len(fields) > 19 else None


class Turn:
    """One Bot Chat turn in its folder (TURNS_DIR): run by turn_run.py in a session of its own, writing Hermes's
    stream into a file, so that it goes on when the connector that started it stops, and the connector after
    it can follow it to its end. `popen` is there only for the connector that started it."""

    def __init__(self, folder: Path, rec: dict, pid: int | None = None, start: str | None = None,
                 popen: subprocess.Popen | None = None):
        self.folder, self.rec, self.pid, self.start, self.popen = folder, rec, pid, start, popen

    @property
    def started(self) -> float:
        return float(self.rec["started"])

    @classmethod
    def load(cls, folder: Path) -> "Turn | None":
        """The turn a connector before this one started in that folder; None when none was started there (no
        turn.json: Hermes was never asked)."""
        rec = _read_json(folder / TURN)
        if not (isinstance(rec, dict) and isinstance(rec.get("target"), str) and PROFILE_ID.match(rec["target"])
                and isinstance(rec.get("started"), (int, float))
                and (rec.get("session") is None or (isinstance(rec["session"], str) and SESSION_ID.match(rec["session"])))):
            return None
        pid = _read_json(folder / PID) or {}
        return cls(folder, rec, pid.get("pid") if type(pid.get("pid")) is int else None,
                   pid.get("start") if isinstance(pid.get("start"), str) else None)

    def alive(self) -> bool:
        """Whether its turn_run.py still runs: the very process, never another that got its pid later."""
        if self.popen is not None:
            return self.popen.poll() is None
        return self.pid is not None and self.start is not None and proc_start(self.pid) == self.start

    def code(self) -> str | None:
        """How it ended, as turn_run.py wrote it: the exit code, or NOT_STARTED; None when nothing says."""
        try:
            return (self.folder / EXIT).read_text(encoding="utf-8").strip()
        except OSError:
            return None

    def finished(self) -> bool:
        return (self.folder / EXIT).exists() or not self.alive()

    def lines(self, stop: threading.Event):
        """Hermes's stream, line by line as it is written, from its start, until the turn has finished and
        all of it is read (or `stop`)."""
        with open(self.folder / OUT, "rb") as f:
            part = b""
            while True:
                chunk = f.readline()
                if chunk:
                    part += chunk
                    if part.endswith(b"\n"):
                        yield part.decode("utf-8", "replace")
                        part = b""
                    continue
                if stop.is_set():
                    return
                if self.finished():
                    # It wrote all it will: whatever is left, then the end.
                    for line in (part + f.read()).splitlines(keepends=True):
                        yield line.decode("utf-8", "replace")
                    return
                time.sleep(FOLLOW_SECONDS)

    def kill(self) -> None:
        """Ends the turn and everything it started: its session's process group, which is turn_run.py's own."""
        if not self.alive():
            return
        if hasattr(os, "killpg"):
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                return  # it ended between the check above and the kill: there is nothing left to end
        elif self.popen is not None:
            # Windows, where only the tests run a turn: the runner and the turn under it.
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.pid)], capture_output=True, check=False)
            self.popen.kill()

    def reap(self) -> None:
        """The connector that started it waits for its runner, which has ended or been killed, so none is
        left behind; a turn it picked up is its own parent's to reap."""
        if self.popen is not None:
            # It has written how the turn ended and is returning, or it was killed: either way it ends now.
            self.popen.wait()


class Hermes:
    """The two stock ways into the Bots' Hermes."""

    def __init__(self, hermes_cmd: list[str], helper_cmd: list[str] | str, home: Path, helper: Path = HELPER,
                 fleet=None, policy_socket: str = ""):
        # A Python's path alone (the tests' stand-ins) runs the helper file straight with it.
        self.cmd, self.home = list(hermes_cmd), home
        self.helper_cmd = [helper_cmd, "-I", str(helper)] if isinstance(helper_cmd, str) else list(helper_cmd)
        # fleet(bot) -> (room, why not): whether the fleet queue would admit a turn of that Bot now
        # (fleet_room). None where the Bots have no queue (brokers.ready off): every turn runs.
        self.fleet = fleet
        # tw-policy's socket, which answers the models a Bot's chat may be moved to (bot_models).
        self.policy_socket = policy_socket

    def turned_away(self, bot: str, since: float, sid: str | None = None):
        """Why the queue turned away the turn that started at `since` (time.time()) in the Bot's Bot Chat,
        or in session `sid`, from the note the Bots' turn gate left for that session; None when it ran, and
        KEPT when the gate kept the message in the core. The note is taken once read."""
        sid = sid or bot_chat_id(self.home / ".hermes", bot)
        if sid is None:
            return None
        note = self.home.joinpath(".hermes", "profiles", bot, *TURNED_AWAY_DIR, f"{sid}.json")
        try:
            got = json.loads(note.read_text(encoding="utf-8"))
            at = datetime.fromisoformat(got["at"]).timestamp()
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if at < since:
            return None
        note.unlink(missing_ok=True)
        if got.get("kept") is True:
            return KEPT
        return str(got.get("reason") or "no reason given")[:150]

    def _helper(self, req: dict, timeout: float) -> dict | None:
        """The helper's one JSON answer; None when it did not answer in time. A helper that could not start,
        or did not answer in JSON, answers {"helper_failed": True, "reason": ...} with its stderr's tail."""
        try:
            # Read only, so with Hermes's own switch that keeps a start from finishing an update first
            # (own_connector.PROBE_ENV).
            out = subprocess.run(self.helper_cmd, input=json.dumps(req).encode("utf-8"),
                                 capture_output=True, timeout=timeout, cwd=str(self.home), check=False,
                                 env=probe_env())
        except subprocess.TimeoutExpired:
            return None
        except OSError as exc:
            return {"helper_failed": True, "started": False,
                    "reason": f"{HELPER_FAILED} did not start ({type(exc).__name__}: {self.helper_cmd[0]})"}
        try:
            got = json.loads(out.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            got = None
        if not isinstance(got, dict):
            return {"helper_failed": True, "started": True,
                    "reason": f"{HELPER_FAILED} failed (exit {out.returncode}): {_tail(out.stderr)}"}
        return got

    def check(self, profile: str) -> tuple[str, str]:
        """(state, why) of that profile's gateway, found without starting one: "ready", "not_ready" (the check
        ran and found it not running or not taking connections, or did not answer in time), or "failed"
        (the helper itself failed: nothing about the gateway is known)."""
        got = self._helper({"op": "ready", "profile": profile}, CHECK_SECONDS)
        if got is None:
            return "not_ready", "the gateway's readiness check did not answer in a minute"
        if got.get("helper_failed"):
            return "failed", str(got.get("reason"))[:200]
        if got.get("ready") is True:
            if profile and self.fleet is not None:
                room, why = self.fleet(profile)
                if not room:
                    return "not_ready", f"{FLEET} has no room for a turn of {profile}'s: {why}"[:200]
            return "ready", ""
        return "not_ready", str(got.get("reason") or "the Bots' gateway is not ready")[:200]

    def ready(self, profile: str) -> tuple[bool, str]:
        """(ready, why not), from check."""
        state, why = self.check(profile)
        return state == "ready", why

    def states(self, profiles: list[str]) -> dict[str, str] | None:
        """Each Bot's gateway state as Hermes names it (ready, starting, absent, ...), in one helper run, or
        None when the helper could not run or answer."""
        got = self._helper({"op": "states", "profiles": profiles}, CHECK_SECONDS)
        states = (got or {}).get("states")
        if not isinstance(states, dict):
            return None
        return {k: v for k, v in states.items() if k in profiles and isinstance(v, str)}

    def tasks(self, profiles: list[str]) -> dict:
        """Each Bot's subagents, background commands and scheduled jobs (room_send.py op `tasks`), in one
        helper run; {"why": ...} when the helper itself could not say."""
        got = self._helper({"op": "tasks", "profiles": profiles}, CHECK_SECONDS)
        if got is None:
            return {"why": "the tasks helper did not answer in a minute"}
        if got.get("helper_failed"):
            return {"why": str(got.get("reason"))[:200]}
        tasks = got.get("tasks")
        if not isinstance(tasks, dict):
            return {"why": str(got.get("reason") or "the tasks helper gave no tasks")[:200]}
        return {"tasks": {k: v for k, v in tasks.items() if k in profiles and isinstance(v, dict)}}

    def to_room(self, room_id: str, send_key: str, text: str, files: list | None = None,
                thread: str | None = None) -> tuple[str, str, str]:
        """(outcome, ref, reason) for one message to a room. `files` are (file, bytes) for each file on it,
        uploaded into the room's own store by the helper before the message goes (room_send.py _send). With
        `thread`, it goes into that thread of the room (#108); without, it starts a thread of its own."""
        req = {"op": "send", "room_id": room_id, "event_id": send_key, "text": text} | (
            {"thread_id": thread} if thread else {})
        if files:
            req["files"] = [{"id": f["id"], "name": f["name"], "mime": f["mime"],
                             "data_base64": base64.b64encode(body).decode("ascii")} for f, body in files]
        got = self._helper(req, CHECK_SECONDS)
        if got is None:
            return "uncertain", "", "no answer from the room helper in a minute"
        if got.get("helper_failed"):
            # Never started: nothing was sent, so it waits for the helper. Started and said nothing readable:
            # groups.send may have gone, and the room settles it from its own feed.
            return ("uncertain" if got.get("started") else "not_ready"), "", str(got.get("reason"))[:200]
        if got.get("sent") is True:
            return "confirmed", f"seq {got.get('seq')}", ""
        if got.get("sent") is False and got.get("ready") is False:
            return "not_ready", "", str(got.get("reason") or "the Bots' gateway is not ready")
        if got.get("sent") is False:
            return "failed", "", str(got.get("reason", "not sent"))
        return "uncertain", "", str(got.get("reason") or "the room helper stopped without saying")

    def control(self, bot: str, session_id: str, want: dict | None) -> tuple[dict | None, bool, str]:
        """(what Hermes holds for the Bot's chat, whether a change was made, why not): its model, whether that
        is a fallback, its thinking level, and the models it may be moved to, through that Bot's own gateway
        (room_send.py op `control` with the profile). The models are the Bot's own route list's that cost
        nothing per message (bot_models), so a pick can never move it off its policy."""
        got = self._helper({"op": "control", "profile": bot, "name": bot.capitalize(), "session_id": session_id,
                            "allowed": bot_models(bot, self.policy_socket), **({"set": want} if want else {})}, CHECK_SECONDS)
        if got is None:
            return None, False, "the Bot's gateway did not answer in a minute"
        if got.get("helper_failed"):
            return None, False, str(got.get("reason"))[:200]
        state = got.get("state") if isinstance(got.get("state"), dict) else None
        return state, got.get("applied") is True, str(got.get("reason") or "")[:200]

    def react(self, bot: str, session_id: str, message_id: int, emoji: str | None) -> tuple[str, str]:
        """(outcome, reason): set Patrick's reaction on one message of a Bot's Bot Chat to exactly `emoji`
        (None takes it off), with that profile's own store."""
        got = self._helper({"op": "react", "profile": bot, "session_id": session_id, "message_id": message_id,
                            "emoji": emoji}, CHECK_SECONDS)
        if got is None:
            return "failed", "Hermes's store did not answer in a minute"
        if got.get("set") is True:
            return "set", ""
        return "failed", str(got.get("reason") or "not set")[:200]

    def rewind(self, bot: str, session: str | None, action: str, message_id: int) -> tuple[str, str, str | None]:
        """(outcome, reason, new chat) for an undo or a branch (rewinds.py) in a Bot's Bot Chat, or in the session
        of it a Board row opened when it is one of that Bot's own: room_send.py op `rewind`, Hermes's own
        session.mutate on that Bot's running gateway. The outcome is done, failed, uncertain (Hermes did not
        answer; it may be done) or not_ready (the gateway is not running, or the helper failed: nothing done)."""
        try:
            if session is None:
                sid = bot_chat_id(self.home / ".hermes", bot)
                if sid is None:
                    return "failed", f"{bot} has no Bot Chat", None
            elif SESSION_ID.match(session) and bot_session(self.home / ".hermes", bot, session):
                sid = session
            else:
                return "failed", f"that session is not one of {bot}'s", None
        except (sqlite3.Error, OSError) as exc:
            return "not_ready", f"could not read {bot}'s store ({type(exc).__name__})", None
        got = self._helper({"op": "rewind", "profile": bot, "action": action, "session_id": sid,
                            "message_id": message_id}, CHECK_SECONDS + 30)
        if got is None:
            return "uncertain", "Hermes did not answer in 90 seconds; it may be done", None
        reason = str(got.get("reason") or "")[:200]
        if got.get("helper_failed"):
            return "not_ready", reason, None
        if got.get("done") is True:
            child = got.get("session_id")
            return "done", "", child if isinstance(child, str) and SESSION_ID.match(child) else None
        if got.get("done") is None and "done" in got:
            return "uncertain", reason, None
        return ("not_ready" if got.get("ready") is False else "failed"), reason or "not done", None

    @property
    def turns(self) -> Path:
        """Where each turn keeps its state (TURNS_DIR)."""
        return self.home.joinpath(*TURNS_DIR)

    def to_bot(self, bot: str, text: str, doing=None, live=None, files: list[str] | None = None,
               rewind=None, folder: Path | None = None) -> tuple[str, str, str]:
        """(outcome, ref, reason) for one message to a Bot's Bot Chat. `doing(state, tool)` hears what the
        turn is doing as Hermes's stream says it (own_connector.follow_turn): thinking, writing, or a tool.
        `live` (own_connector.LiveTurn) keeps the turn as Desktop draws it.

        It runs as room_send.py `chat`, Hermes's own `hermes chat` with the gateway's live events said too
        (room_send.py live_line): `hermes chat`'s own stream-json carries only the reply and the tools, so a
        Bot's thinking, its subagents and its status lines never reached the phone (Patrick, 9 Nov).

        `rewind` ("retry" or "edit", and his turn's id) takes the Bot Chat back to before that turn first
        (room_send.py --tw-rewind, rewinds.py).

        The turn keeps its state in `folder` (the connector's, for its message) and outlives this process
        (start_turn); without one, it gets a folder of its own, gone once it ends."""
        state, why = self.check(bot)
        if state != "ready":
            # Nothing was sent: the message waits, with why (the gateway, or the helper's own failure).
            return "not_ready", "", why
        return self._run(bot, None, text, folder, doing, live, lambda path: [
            *self.helper_cmd, "chat", *_rewind_args(rewind), "-p", bot, "--in", str(self.home), "-c", BOT_CHAT,
            "--create-if-missing", "-Q", "--format", "stream-json", "--query-file", str(path), *(files or [])])

    def _run(self, bot: str, sid: str | None, text: str, folder: Path | None, doing, live, argv) -> tuple[str, str, str]:
        """One turn of `bot` (in session `sid`, or its Bot Chat), started and followed to its end."""
        own = folder is None
        if own:
            self.turns.mkdir(parents=True, exist_ok=True)
            folder = Path(tempfile.mkdtemp(prefix="turn-", dir=self.turns))
        try:
            try:
                turn = self.start_turn(folder, bot, sid, text, argv)
            except OSError as exc:
                return "failed", "", f"could not keep the turn's state on disk ({type(exc).__name__}); nothing was sent"
            if turn is None:
                return "failed", "", "could not start the hermes command"
            return self.finish(turn, doing, live)
        finally:
            if own:
                shutil.rmtree(folder, ignore_errors=True)

    def start_turn(self, folder: Path, bot: str, sid: str | None, text: str, argv) -> Turn | None:
        """Starts one turn under turn_run.py, in a session of its own, with Hermes's stream going into a file
        in `folder`, never a pipe: a pipe broke when the connector stopped, and its turns stopped with it
        (#163). The text goes in a 0600 file in that 0700 folder, never on a command line, and is gone when
        the turn ends. `argv(path)` is the turn's command for the text's file. None when the runner could not
        start (nothing was sent)."""
        os.makedirs(folder, mode=0o700, exist_ok=True)
        os.chmod(folder, 0o700)
        # Before anything starts: a connector that finds no turn.json knows Hermes was never asked.
        rec = {"target": bot, "session": sid, "started": time.time()}
        _write_json(folder / TURN, rec)
        path = folder / MESSAGE
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        out = os.open(folder / OUT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        # The Bot's own home, named before any of Hermes is imported (room_send.py loads Hermes's chat view
        # before Hermes reads -p), so every part of the turn is that profile's.
        env = dict(os.environ, HERMES_HOME=str(self.home / ".hermes" / "profiles" / bot))
        try:
            proc = subprocess.Popen([sys.executable, "-I", str(RUNNER), str(folder), "--", *argv(path)],
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.DEVNULL,
                                    cwd=str(self.home), env=env, start_new_session=True)
        except OSError:
            path.unlink(missing_ok=True)
            return None
        finally:
            os.close(out)
        start = proc_start(proc.pid)
        try:
            _write_json(folder / PID, {"pid": proc.pid, "start": start})
        except OSError:
            pass  # the turn runs all the same; only a connector after this one could not follow it (resume)
        return Turn(folder, rec, proc.pid, start, popen=proc)

    def resume(self, folder: Path, doing=None, live=None) -> tuple[str, str, str]:
        """(outcome, ref, reason) for the turn a connector before this one started in `folder` and did not see
        to its end (Connector.adopt): followed from its stream's start to its end, as if this connector had
        started it."""
        turn = Turn.load(folder)
        if turn is None:
            # turn.json is written before anything starts, so Hermes was never asked: nothing was sent, and
            # the message goes back in the queue as it was.
            return "not_ready", "", NOT_HANDED
        if turn.pid is None and not (folder / EXIT).exists():
            return "uncertain", "", "the connector stopped as the turn started; it may have the message"
        return self.finish(turn, doing, live)

    def finish(self, turn: Turn, doing=None, live=None) -> tuple[str, str, str]:
        """Follows a turn to its end and says what came of it. Into a session, the message is Hermes's the
        moment it says it steered it into that session's running turn: what is left is only watching that
        turn, so the view is let go of, as Desktop's /detach does, and ends without touching the turn."""
        sid = turn.rec.get("session")
        seen: dict = {}
        taken = threading.Event()

        def heard(st, tool):
            if seen.get("taken"):
                taken.set()
            (doing or (lambda s, t: None))(st, tool)

        stop = threading.Event()
        reader = threading.Thread(target=follow_turn, args=(turn.lines(stop), seen, heard, live), daemon=True)
        reader.start()
        how = "ended"
        while reader.is_alive():
            if sid is not None and taken.is_set():
                how = "taken"
                break
            if time.time() >= turn.started + TURN_SECONDS:
                how = "timeout"
                break
            reader.join(0.5)
        if how != "ended":
            turn.kill()
            stop.set()
            reader.join(30)
        turn.reap()
        (turn.folder / MESSAGE).unlink(missing_ok=True)
        code = turn.code()
        if sid is not None and seen.get("taken"):
            return "confirmed", f"{seen['taken']} into the running turn", ""
        if how == "timeout":
            if seen.get("delivery_receipt"):
                # The deadline bounds this local viewer. Hermes owns the exact
                # accepted admission and can keep working after it detaches.
                # Confirm delivery, never task completion or a reason to replay.
                return "confirmed", "handed to Hermes; its turn may still be running", ""
            return "uncertain", "", "no end to the Bot's turn in 15 minutes; it may have the message"
        if code == NOT_STARTED:
            return "failed", "", "could not start the hermes command"
        try:
            n = int(code)
        except (TypeError, ValueError):
            # Its runner ended without saying (killed, or the VM stopped): the turn may have the message.
            return "uncertain", "", "the turn stopped without saying how it ended; it may have the message"
        return self._ended(turn.rec["target"], sid, n, turn.started, seen)

    def _ended(self, bot: str, sid: str | None, code: int, started: float, seen: dict) -> tuple[str, str, str]:
        """What came of a turn that ran to its end, from its exit code, how a retry's or an edit's rewind went,
        and the turn gate's note."""
        rewound = seen.get("rewind") or {}
        if rewound.get("status") == "refused":
            # Hermes kept the chat as it was, and the message never went in.
            return "failed", "", rewound.get("reason") or "Hermes did not take the chat back"
        if code == 0:
            if self.fleet is not None:
                why = self.turned_away(bot, started, sid)
                if why is KEPT:
                    return "confirmed", "kept by the turn gate; it goes once the queue has room", ""
                if why is not None and rewound.get("status") == "done":
                    # The chat was taken back, but the turn after it ran nothing: sending it again as it was
                    # would take back a turn that is gone, so it is not put back in the queue.
                    return "failed", "", (f"the chat was taken back, but {FLEET} turned the turn away: {why}; "
                                          "send it again")[:200]
                if why is not None:
                    # The turn ran nothing: the message goes back in the queue as it was, and goes again
                    # once the queue has room (check, above).
                    return "not_ready", "", f"{FLEET} turned the turn away: {why}"
            return "confirmed", "turn finished", ""
        return "uncertain", "", f"hermes stopped with exit code {code}; it may have the message"

    def to_session(self, bot: str, sid: str, text: str, doing=None, live=None,
                   files: list[str] | None = None, rewind=None, folder: Path | None = None) -> tuple[str, str, str]:
        """(outcome, ref, reason) for one message into one session of a Bot: the one a Board row opens,
        where the Bot may be working right now (Patrick, 9 Nov). It goes the way Desktop's message box
        sends into an open chat: room_send.py `chat --resume`, through the Bot's running gateway, which
        holds the session's turn. So a message into a session whose turn is running is steered into it,
        interrupts it, or waits behind it, as that session's busy setting says, and never runs beside it.
        The turn is the Bot's own, unattended, under its own guard rails, as a Bot Chat turn is.

        A steered or redirected message has no turn of its own: Hermes has it once it says so, which is
        the end of this message (the Board's live line follows the Bot's turn). Anything else is followed
        to its turn's end, as a Bot Chat turn is, and outlives this process as one does (start_turn)."""
        if not SESSION_ID.match(sid):
            return "failed", "", "that is not a session id Hermes gives"
        state, why = self.check(bot)
        if state != "ready":
            return "not_ready", "", why
        try:
            mine = bot_session(self.home / ".hermes", bot, sid)
        except (sqlite3.Error, OSError) as exc:
            return "not_ready", "", f"could not read {bot}'s store ({type(exc).__name__})"
        if not mine:
            return "failed", "", f"that session is not one of {bot}'s"
        return self._run(bot, sid, text, folder, doing, live, lambda path: [
            *self.helper_cmd, "chat", *_rewind_args(rewind), "-p", bot, "-Q", "--format", "stream-json",
            "--query-file", str(path), "--resume", sid, *(files or [])])


class PolicyUnanswered(Exception):
    """tw-policy refused an op of the connector's, or did not answer it."""


def _ask_policy(sock: str, op: str, args: dict):
    """One op of tw-policy's, over its socket as the Bots' own user. The
    connector reads nothing of the policy or the guard itself (15 Nov 2026):
    a process holds the code and data it loaded at its start, so while it
    imported the guard, every change to policy.json or to the guard's code
    restarted the connector at the install. A connector restart ends every
    Bot Chat turn it runs, so tw-follow-main held all new turns for up to 20
    minutes first, and the Board showed two Bots working, then one: 20 of the
    23 installs from 11 to 15 Nov that held the Bots changed nothing the
    connector runs (thornswood_brokers.restart_plan). tw-policy, which owns
    the policy, answers for it, and starts again by itself behind its socket.
    Raises PolicyUnanswered with tw-policy's reason when it refuses or does
    not answer."""
    from thornswood_brokers.wire import Refused, call
    try:
        return call(sock, op, args, timeout=CHECK_SECONDS)
    except Refused as e:
        raise PolicyUnanswered(str(e)) from e


def bot_models(bot: str, sock: str) -> list[list[str]]:
    """[provider, model] of each route in the Bot's own list that it reaches with its own sign-in, from
    tw-policy (its op bot_models): the only models its chat may be moved to from the app. Empty where the
    Bots have no queue, or tw-policy does not answer: the app may then move the chat nowhere."""
    if not sock:
        return []
    try:
        got = _ask_policy(sock, "bot_models", {"bot": bot}) or {}
    except PolicyUnanswered:
        return []
    models = got.get("models") if isinstance(got, dict) else None
    return [[str(m[0]), str(m[1])] for m in models or [] if isinstance(m, list) and len(m) == 2]


def fleet_room(sock: str | None):
    """fleet(bot) for Hermes: tw-policy's turn_room, asked over its socket (--policy-socket, which the
    unit gives) as the Bots' own user, or None when the connector was given no socket (the Bots have no
    queue: brokers.ready off). A queue that does not answer has no room."""
    if not sock:
        return None

    def fleet(bot: str) -> tuple[bool, str]:
        try:
            got = _ask_policy(sock, "turn_room", {"bot": bot})
        except PolicyUnanswered as e:
            return False, f"tw-policy did not answer ({e})"
        return bool(got.get("room")), str(got.get("why_not") or "")
    return fleet


class Core:
    def __init__(self, base: str, token: str):
        self.base, self.token = base, token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.token}"})
        try:
            with self.opener.open(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                out = json.loads(exc.read() or b"{}")
            except json.JSONDecodeError:
                out = {}
            exc.close()
            return exc.code, out
        except (urllib.error.URLError, OSError):
            return 0, {}

    def fetch(self, op: str, body: dict) -> tuple[int, bytes]:
        """An answer that is bytes (a file Patrick attached), not JSON: `POST /connector/<op>`."""
        req = urllib.request.Request(f"{self.base}/connector/{op}", data=json.dumps(body).encode("utf-8"),
                                     method="POST", headers={"Content-Type": "application/json",
                                                             "Authorization": f"Bearer {self.token}"})
        try:
            with self.opener.open(req, timeout=120) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            exc.close()
            return exc.code, b""
        except (urllib.error.URLError, OSError, ValueError):
            return 0, b""


def say(line: str) -> None:
    """One line to the journal now: under systemd stdout is a pipe, and `-I` ignores PYTHONUNBUFFERED."""
    print(line, flush=True)


class Connector:
    def __init__(self, core: Core, hermes: Hermes, say=say, hermes_home: Path | None = None,
                 turns: Path | None = None):
        self.core, self.hermes, self.say = core, hermes, say
        self.hermes_home = hermes_home or Path.home() / ".hermes"
        # Where each Bot Chat turn keeps its state (TURNS_DIR), so that it outlives this process; None (a
        # stand-in Hermes in the tests) keeps nothing on disk.
        self.turns = turns if turns is not None else getattr(hermes, "turns", None)
        self.folders: dict[str, Path] = {}  # each message's turn folder, until the core takes what came of it
        self._adopted = False
        self.unsaid: list[dict] = []  # results the core has not taken yet
        self.waiting: dict[str, str] = {}  # gateways (by profile, "" for the room) found not ready: why
        self._lock = threading.Lock()
        self.doing: dict[str, dict] = {}    # each Bot Chat turn running now: what it is doing
        self.lives: dict[str, LiveTurn] = {}  # each turn of the round: what it shows, as Desktop draws it
        self.lives_said: dict[str, int] = {}  # and the change of it the core has
        self.live_bots: list[str] = []      # the Bots the core asked about last time
        self.live_at: float | None = None
        self.tasks_at: float | None = None
        self._tasks_run: threading.Thread | None = None
        self.running: list[threading.Thread] = []  # the turns going now, each its own thread
        self.wakeup = threading.Event()     # the claim loop: the core has new work, or a turn ended
        self.read_wake = threading.Event()  # the reads thread: the core has new work (a page the phone asked for)
        self._doing_on = False              # a thread is saying what the turns do
        self._said = (None, None)           # the doing line the core has, and when it got it
        self._say_lock = threading.Lock()
        self._report_lock = threading.Lock()

    def deliver(self, item: dict) -> dict:
        files = item.get("attachments") or []
        try:
            if item["target"] == "room":
                got = self._fetched(files)
            else:
                # Into the Bot's own home, where its Hermes stages what Desktop drops into its box.
                text, extra = hand_over(item["text"], fetch_files(
                    self.core, self.hermes.home / ".hermes" / "profiles" / item["target"], item["id"], files)) \
                    if files else (item["text"], [])
        except ValueError as exc:
            # Nothing was handed to Hermes, so the message did not go: Patrick sees why and can send again.
            return {"id": item["id"], "outcome": "failed", "ref": "", "reason": str(exc)[:200]}
        if item["target"] == "room":
            outcome, ref, reason = self.hermes.to_room(item["room_id"], item["send_key"], item["text"], got,
                                                       thread=item.get("thread"))
        else:
            def doing(state, tool):
                with self._lock:
                    self.doing[item["id"]] = {"id": item["id"], "state": state, "tool": tool}
            live = LiveTurn()
            with self._lock:
                self.lives[item["id"]] = live
            rewind = ((item["rewind"], item["rewind_to"]) if item.get("rewind") in ("retry", "edit")
                      and type(item.get("rewind_to")) is int else None)
            try:
                folder = self._folder(item)
            except OSError as exc:
                return {"id": item["id"], "outcome": "failed", "ref": "",
                        "reason": f"could not keep the turn's state on disk ({type(exc).__name__}); nothing was sent"}
            more = ({"files": extra} if extra else {}) | ({"rewind": rewind} if rewind else {}) | (
                {"folder": folder} if folder else {})
            try:
                if item.get("session"):
                    outcome, ref, reason = self.hermes.to_session(item["target"], item["session"], text,
                                                                  doing, live, **more)
                else:
                    outcome, ref, reason = self.hermes.to_bot(item["target"], text, doing, live, **more)
            finally:
                with self._lock:
                    self.doing.pop(item["id"], None)
        return {"id": item["id"], "outcome": outcome, "ref": ref, "reason": reason}

    def _folder(self, item: dict) -> Path | None:
        """The folder this message's turn keeps its state in (TURNS_DIR), new, with the message as the core
        leased it, so that a connector after this one can see its turn to the end and say what came of it."""
        if self.turns is None or not ITEM_ID.fullmatch(item["id"]):
            return None
        folder = self.turns / f"m-{item['id']}"
        # One left from an earlier lease of this message, whose result the core took: never this turn's.
        shutil.rmtree(folder, ignore_errors=True)
        os.makedirs(folder, mode=0o700)
        os.chmod(self.turns, 0o700)
        _write_json(folder / ITEM, {"id": item["id"], "target": item["target"]})
        with self._lock:
            self.folders[item["id"]] = folder
        return folder

    def _forget(self, mid: str) -> None:
        """The core has what came of the message: its turn's folder goes."""
        with self._lock:
            folder = self.folders.pop(mid, None)
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)

    def adopt(self) -> int:
        """The turns the connector before this one started and did not see to their end, picked up once, when
        this one starts (#163): an install that changed the connector's code stopped it, and its turns went
        on (the unit's KillMode=process, turn_run.py). Each still running is followed to its end on a thread
        of its own, as if this connector had started it; each that ended meanwhile is read to its end at once;
        and a result the core had not taken is offered again. Returns how many turns it picked up."""
        self._adopted = True
        try:
            folders = sorted(p for p in self.turns.iterdir() if p.is_dir()) if self.turns is not None else []
        except OSError:
            folders = []
        picked = 0
        for folder in folders:
            item = _read_json(folder / ITEM)
            if not (isinstance(item, dict) and isinstance(item.get("id"), str) and ITEM_ID.fullmatch(item["id"])
                    and isinstance(item.get("target"), str) and PROFILE_ID.match(item["target"])):
                # Made before its message was written into it: no turn ever ran there.
                shutil.rmtree(folder, ignore_errors=True)
                continue
            with self._lock:
                self.folders[item["id"]] = folder
            result = _read_json(folder / RESULT)
            if isinstance(result, dict) and result.get("id") == item["id"]:
                with self._lock:
                    self.unsaid.append(result)
                continue
            t = threading.Thread(target=self._resume, args=(item, folder), name=f"turn {item['id']}", daemon=True)
            t.start()
            with self._lock:
                self.running.append(t)
            picked += 1
        if picked:
            self.say(f"NOTE picked up {picked} turn{'s' if picked != 1 else ''} the connector before this one started")
            self._say_doing_while_running()
        return picked

    def _resume(self, item: dict, folder: Path) -> None:
        def doing(state, tool):
            with self._lock:
                self.doing[item["id"]] = {"id": item["id"], "state": state, "tool": tool}
        live = LiveTurn()
        with self._lock:
            self.lives[item["id"]] = live
        try:
            outcome, ref, reason = self.hermes.resume(folder, doing, live)
            r = {"id": item["id"], "outcome": outcome, "ref": ref, "reason": reason}
        except Exception as exc:  # whatever it was, the message may have gone: say so, never resend
            r = {"id": item["id"], "outcome": "uncertain", "ref": "",
                 "reason": f"the connector hit {type(exc).__name__} while following the turn"}
        finally:
            with self._lock:
                self.doing.pop(item["id"], None)
        self._ended(item, r)

    def _fetched(self, files: list) -> list[tuple[dict, bytes]]:
        """Each file on a room message, fetched from the core and checked against its size and SHA-256:
        (file, bytes). The room keeps its files in its own store, so nothing is saved here."""
        out = []
        for f in files[:10]:
            if not isinstance(f, dict) or not isinstance(f.get("id"), str):
                raise ValueError("an attachment the core named is not well formed")
            status, body = self.core.fetch("attachment", {"id": f["id"]})
            if status != 200:
                raise ValueError(f"could not fetch {f.get('name')} from the core (it answered {status or 'nothing'})")
            if len(body) != f.get("size") or hashlib.sha256(body).hexdigest() != f.get("sha256"):
                raise ValueError(f"{f.get('name')} came from the core changed or cut short")
            out.append((f, body))
        return out

    def _say_doing_while_running(self) -> None:
        """While any turn runs: what each Bot Chat turn is doing, to the core every DOING_SECONDS when it
        changed and at least every DOING_KEEP, for the phone's live line, and what each shows (say_live).
        Reading only: the core takes it while mobile_read is on, whatever connector_effects says. One such
        thread at a time, started by the round that starts a turn, ending when none runs (decided under the
        lock that round adds its turns under, so a turn never starts without one)."""
        with self._lock:
            if self._doing_on:
                return
            self._doing_on = True

        def loop():
            while True:
                with self._lock:
                    if not any(t.is_alive() for t in self.running):
                        self._doing_on = False
                        return
                    now_doing = sorted(self.doing.values(), key=lambda d: d["id"])
                said, said_at = self._said
                if now_doing and (now_doing != said or time.monotonic() - said_at >= DOING_KEEP):
                    # Said once per change whatever the answer: a refusal (mobile_read off) is not asked again
                    # every quarter second, only with the next change or after DOING_KEEP.
                    self.core.post("/connector/doing", {"turns": now_doing})
                    self._said = (now_doing, time.monotonic())
                self.say_live()
                time.sleep(DOING_SECONDS)
        threading.Thread(target=loop, name="doing", daemon=True).start()

    def say_live(self) -> None:
        """Each turn that changed since the core last had it, as Desktop draws it (own_connector.LiveTurn):
        said once per change whatever the answer, as the doing line is."""
        with self._say_lock:
            with self._lock:
                lives = list(self.lives.items())
            changed = []
            for mid, live in lives:
                v, parts = live.seen()
                if v and self.lives_said.get(mid) != v:
                    changed.append((mid, v, parts))
            for i in range(0, len(changed), 10):
                self.core.post("/connector/turns",
                               {"turns": [{"id": mid, "parts": parts} for mid, _, parts in changed[i:i + 10]]})
            with self._lock:
                self.lives_said.update({mid: v for mid, v, _ in changed})

    def _report(self) -> None:
        """Each result the core has not taken yet, offered again until it lands: from the round, and from
        each turn the moment it ends. One report at a time."""
        with self._report_lock:
            with self._lock:
                pending, self.unsaid = self.unsaid, []
            keep = self._offer(pending)
            with self._lock:
                self.unsaid[:0] = keep

    def _offer(self, pending: list[dict]) -> list[dict]:
        keep = []
        for r in pending:
            if "rewind_id" in r:
                # An undo or a branch: the core changes nothing for one it already has an answer to.
                status, _ = self.core.post("/connector/rewound", {
                    "id": r["rewind_id"], "outcome": r["outcome"], "reason": r["reason"],
                    **({"session_id": r["session_id"]} if "session_id" in r else {})})
                if status in (200, 404):
                    self.say(f"{'PASS' if r['outcome'] == 'done' else 'NOTE'} {r['op']} in {r['bot']}'s chat: "
                             f"{r['outcome']}")
                else:
                    keep.append(r)
                continue
            if "version" in r:
                # A reaction: the core drops an answer about an older wish by itself.
                status, _ = self.core.post("/connector/reacted", r)
                if status in (200, 404):
                    self.say(f"{'PASS' if r['outcome'] == 'set' else 'NOTE'} reaction for {r['bot']}: {r['outcome']}")
                else:
                    keep.append(r)
                continue
            status, body = self.core.post("/connector/result", r)
            if status == 200 or (status == 409 and body.get("error") == "not_leased"):
                self.say(f"{'PASS' if r['outcome'] == 'confirmed' else 'NOTE'} {r['id']}: {r['outcome']}")
                self._forget(r["id"])
            else:
                keep.append(r)
        return keep

    def react(self, wanted: list) -> int:
        """The reactions the core handed over, each set in that Bot's Bot Chat one after another (each is one
        quick write). Returns how many were set; what came of each goes to the core with the report."""
        done = 0
        for r in wanted[:10]:
            if not (isinstance(r, dict) and isinstance(r.get("bot"), str) and PROFILE_ID.match(r["bot"])
                    and type(r.get("message_id")) is int and type(r.get("version")) is int
                    and (r.get("emoji") is None or isinstance(r.get("emoji"), str))):
                continue
            try:
                sid = bot_chat_id(self.hermes_home, r["bot"])
            except (sqlite3.Error, OSError) as exc:
                outcome, reason = "failed", f"could not read the Bot's store ({type(exc).__name__})"
            else:
                if sid is None:
                    outcome, reason = "failed", "that Bot has no Bot Chat"
                else:
                    outcome, reason = self.hermes.react(r["bot"], sid, r["message_id"], r.get("emoji"))
            done += outcome == "set"
            with self._lock:
                self.unsaid.append({"bot": r["bot"], "message_id": r["message_id"], "version": r["version"],
                                    "outcome": outcome, "reason": reason})
        return done

    def controls(self, wanted: list) -> int:
        """The reads and changes of a Bot's model and thinking the core handed over (own_control.py
        BOT_CONTROLS), each through that Bot's gateway and posted back at once. Returns how many were posted."""
        done = 0
        for r in wanted[:3]:
            if not (isinstance(r, dict) and isinstance(r.get("id"), str) and isinstance(r.get("bot"), str)
                    and PROFILE_ID.match(r["bot"]) and isinstance(r.get("session_id"), str)
                    and SESSION_ID.match(r["session_id"])):
                continue
            state, applied, reason = self.hermes.control(r["bot"], r["session_id"], r.get("set"))
            status, _ = self.core.post("/connector/control", {"id": r["id"], "state": state, "applied": applied,
                                                              "reason": reason})
            if status == 200:
                done += 1
            else:
                self.say(f"FAIL control: the core answered {status}")
        return done

    def rewinds(self, wanted: list) -> int:
        """The undos and branches the core handed over (rewinds.py), each done in that Bot's chat one after
        another (each is one session.mutate). Returns how many were done; what came of each goes to the core
        with the report."""
        done = 0
        for r in wanted[:5]:
            if not (isinstance(r, dict) and isinstance(r.get("id"), str) and isinstance(r.get("bot"), str)
                    and PROFILE_ID.match(r["bot"]) and r.get("op") in ("undo", "branch")
                    and type(r.get("message_id")) is int
                    and (r.get("session") is None or isinstance(r.get("session"), str))):
                continue
            outcome, reason, child = self.hermes.rewind(r["bot"], r.get("session"), r["op"], r["message_id"])
            done += outcome == "done"
            with self._lock:
                self.unsaid.append({"rewind_id": r["id"], "op": r["op"], "bot": r["bot"], "outcome": outcome,
                                    "reason": reason, **({"session_id": child} if child else {})})
        return done

    def _still_waiting(self) -> bool:
        """After a message came back not_ready: check each such gateway once this round. True while any is
        still not ready, so nothing more is claimed (and leased, and put back) until it is."""
        for profile in list(self.waiting):
            state, why = self.hermes.check(profile)
            with self._lock:
                was = self.waiting.get(profile)
                if state == "ready":
                    self.waiting.pop(profile, None)
                else:
                    self.waiting[profile] = why
            if state == "ready":
                self.say(f"PASS {profile or 'the room'}'s gateway is ready; its messages go now")
            elif state == "failed" and why != was:
                self.say(f"FAIL {why}")
        with self._lock:
            return bool(self.waiting)

    def round(self) -> int:
        """One claim, without waiting for the turns it starts (Patrick, 9 Nov: "it doesnt feel live"): each
        turn tells the core what came of it the moment it ends, and the next claim takes what came in
        meanwhile, up to PARALLEL turns at once. Before, a round waited for all its turns, so a message to
        one Bot waited for another Bot's three-minute turn. Returns how many messages went to Hermes, or -1
        when the core said to wait."""
        if not self._adopted:
            self.adopt()
        self._report()
        if self.waiting and self._still_waiting():
            return 0
        with self._lock:
            self.running = [t for t in self.running if t.is_alive()]
            free = PARALLEL - len(self.running)
        status, body = self.core.post("/connector/claim", {"limit": free})
        if status == 423:
            return -1
        if status == 401:
            raise SystemExit("STOP: the core refused the connector token")
        if status != 200:
            return 0
        items = body.get("items", [])[:free]
        self.react(body.get("reactions", []))
        self.rewinds(body.get("rewinds", []))
        self.controls(body.get("controls", []))
        for item in items:
            t = threading.Thread(target=self._turn, args=(item,), name=f"turn {item['id']}", daemon=True)
            t.start()
            with self._lock:
                self.running.append(t)
        if items:
            self._say_doing_while_running()
        self._report()
        return len(items)

    def _turn(self, item: dict) -> None:
        try:
            r = self.deliver(item)
        except Exception as exc:  # whatever it was, the message may have gone: say so, never resend
            r = {"id": item["id"], "outcome": "uncertain", "ref": "",
                 "reason": f"the connector hit {type(exc).__name__} while sending"}
        self._ended(item, r)

    def _ended(self, item: dict, r: dict) -> None:
        """What came of one message, at once: the turn's last parts, then its result. The claim loop is woken,
        since that Bot's next message can go now."""
        self.say_live()
        with self._lock:
            self.lives.pop(item["id"], None)
            self.lives_said.pop(item["id"], None)
        gateway = "" if item["target"] == "room" else item["target"]
        helper = r["reason"].startswith(HELPER_FAILED)
        if helper and r["outcome"] != "not_ready":
            self.say(f"FAIL {r['id']}: {r['reason']}")
        if r["outcome"] == "not_ready" and r["reason"] != NOT_HANDED:
            with self._lock:
                new = gateway not in self.waiting
                self.waiting[gateway] = r["reason"]
            if new:
                self.say(f"FAIL {r['reason']}; {gateway or 'the room'}'s messages wait in the queue"
                         if helper else
                         f"NOTE {r['reason']}; {gateway}'s messages wait in the queue"
                         if r["reason"].startswith(FLEET) else
                         f"NOTE {gateway or 'the room'}'s gateway is not ready ({r['reason']}); "
                         "its messages wait in the queue")
        with self._lock:
            self.unsaid.append(r)
            folder = self.folders.get(r["id"])
        if folder is not None:
            # Kept with the turn until the core takes it, so a connector that starts before it does offers it.
            try:
                _write_json(folder / RESULT, r)
            except OSError as exc:
                self.say(f"FAIL {r['id']}: could not keep its result on disk ({type(exc).__name__})")
        self._report()
        self.wakeup.set()

    def once(self) -> int:
        """One round and its turns to the end, with their results: `--once`, and the tests."""
        n = self.round()
        with self._lock:
            running = list(self.running)
        for t in running:
            t.join()
        self._report()
        return n

    def live(self, clock=time.monotonic) -> bool:
        """Each Bot's gateway state to the core, every LIVE_SECONDS. The first post carries none: the core's
        answer names the Bots to check. Returns whether it posted."""
        now = clock()
        if self.live_at is not None and now - self.live_at < LIVE_SECONDS:
            return False
        self.live_at = now
        asked = list(self.live_bots)
        states = self.hermes.states(asked) if asked else {}
        body = {"states": states} if states is not None else {"states": {}, "why": "could not run Hermes's check"}
        status, out = self.core.post("/connector/live", body)
        if status == 200 and isinstance(out.get("bots"), list):
            self.live_bots = [b for b in out["bots"] if isinstance(b, str) and PROFILE_ID.match(b)]
            if not asked and self.live_bots:
                # The first answer named the Bots: their states go at once, not half a minute later.
                self.live_at = None
                return self.live(clock)
            return True
        if status not in (200, 423):
            self.say(f"FAIL live: the core answered {status}")
        return status == 200

    def tasks(self, clock=time.monotonic) -> bool:
        """Every TASKS_SECONDS, in a thread of its own so a slow read holds up no message or page: the Bots'
        subagents, background commands and scheduled jobs, for the app's Tasks tab
        (`POST /api/v1/connector/tasks`). The Bots are the ones the core named for the dots. Returns whether a
        round started."""
        now = clock()
        if (self._tasks_run is not None and self._tasks_run.is_alive()) or not self.live_bots or (
                self.tasks_at is not None and now - self.tasks_at < TASKS_SECONDS):
            return False
        self.tasks_at = now
        bots = list(self.live_bots)

        def report():
            body = self.hermes.tasks(bots)
            status, _ = self.core.post("/connector/tasks", body)
            if "why" in body:
                self.say(f"FAIL tasks: {body['why']}")
            elif status not in (200, 423):
                self.say(f"FAIL tasks: the core answered {status}")

        self._tasks_run = threading.Thread(target=report, daemon=True)
        self._tasks_run.start()
        return True

    def pages(self) -> int:
        """The pages the phone asked for: each Bot Chat or room read from Hermes's own stores, read only, and
        posted. Reading only, so it runs whatever connector_effects says. Returns how many were posted."""
        status, body = self.core.post("/connector/pages", {})
        if status != 200:
            return 0
        sent = 0
        for req in body.get("pages", [])[:3]:
            if not isinstance(req, dict) or not isinstance(req.get("id"), str):
                continue
            before = req.get("before") if isinstance(req.get("before"), int) else None
            target = req.get("chat")
            try:
                if req.get("kind") == "bot" and isinstance(target, str) and PROFILE_ID.match(target):
                    page = bot_page(self.hermes_home, target, before)
                elif (req.get("kind") == "bot_session" and isinstance(target, str) and PROFILE_ID.match(target)
                      and isinstance(req.get("q"), str) and SESSION_ID.match(req["q"])):
                    page = bot_page(self.hermes_home, target, before, session=req["q"])
                elif req.get("kind") == "room" and isinstance(target, str) and ROOM_ID.match(target):
                    page = room_page(room_store(self.hermes_home, target), target, before)
                elif (req.get("kind") == "room_thread" and isinstance(target, str) and ROOM_ID.match(target)
                      and isinstance(req.get("q"), str) and THREAD_ID.fullmatch(req["q"])):
                    page = room_page(room_store(self.hermes_home, target), target, before, thread=req["q"])
                else:
                    continue
            except (sqlite3.Error, OSError) as exc:
                self.say(f"FAIL page: could not read Hermes's store ({type(exc).__name__})")
                continue
            status, _ = self.core.post("/connector/page", {"id": req["id"], "target": target, "page": page})
            if status == 200:
                sent += 1
            elif status not in (409, 423):
                self.say(f"FAIL page: the core answered {status}")
        return sent

    def reads(self) -> None:
        """The phone's pages, each Bot's state and its tasks, every few seconds and the moment the core asks
        for a page, whatever connector_effects says. Reading only, so it never waits on a claim: on 8 Nov a
        three-minute turn to Bramble left all six Bots "not heard from" on the phone while it ran."""
        while True:
            self.pages()
            self.live()
            self.tasks()
            self.read_wake.wait(POLL_SECONDS)
            self.read_wake.clear()

    def forever(self) -> None:
        # The reads on their own thread; a claim the moment the core has new work for it (wakes.py) or a turn
        # ended, and every few seconds whatever happens, or every half minute while the core says
        # connector_effects is off. A reads thread that raised stops the connector, with its traceback in
        # the journal.
        reader = threading.Thread(target=self.reads, name="reads", daemon=True)
        reader.start()
        Waker(lambda body: self.core.post("/connector/wait", body), [self.wakeup, self.read_wake],
              WAIT_SECONDS).start()
        claim_at = 0.0
        while True:
            if not reader.is_alive():
                raise SystemExit("STOP: the thread for pages and Bot states stopped")
            if time.monotonic() >= claim_at:
                n = self.round()
                claim_at = time.monotonic() + (POLL_SECONDS * 6 if n < 0 else 0)
            self.wakeup.wait(POLL_SECONDS)
            self.wakeup.clear()


def main(argv: list[str]) -> int:
    home = Path(os.environ.get("HOME", "~")).expanduser()
    ap = argparse.ArgumentParser(prog="thornswood_decisions.connector")
    ap.add_argument("--hermes", required=True, help="the hermes command the Bots' user runs")
    ap.add_argument("--hermes-python", help="Hermes's own Python, in place of asking the hermes command")
    ap.add_argument("--hermes-root", help="with --hermes-python: Hermes's source folder, when it is not installed "
                                          "into that Python")
    ap.add_argument("--check-ready", metavar="BOT",
                    help="run the helper's readiness check for one Bot, print its answer and stop: exit 0 only "
                         "when it says ready (tw-canary)")
    ap.add_argument("--token", default=str(home / ".config" / "thornswood" / "connector.token"))
    ap.add_argument("--core", default=CORE)
    ap.add_argument("--policy-socket", default="",
                    help="tw-policy's socket (policy.yaml brokers.sockets.policy.path), which the unit gives while "
                         "the Bots have a queue (brokers.ready); with none, every turn runs")
    ap.add_argument("--once", action="store_true", help="one round, then stop")
    args = ap.parse_args(argv)
    hermes = Hermes([args.hermes], helper_command(args.hermes, args.hermes_python, args.hermes_root), home,
                    fleet=fleet_room(args.policy_socket), policy_socket=args.policy_socket)
    if args.check_ready is not None:
        if not PROFILE_ID.match(args.check_ready):
            raise SystemExit("STOP: --check-ready takes a Bot's profile name")
        got = hermes._helper({"op": "ready", "profile": args.check_ready}, CHECK_SECONDS)
        print(json.dumps(got if got is not None else {"ready": False, "reason": "no answer in a minute"}))
        return 0 if got and got.get("ready") is True else 1
    conn = Connector(Core(args.core, read_token(Path(args.token))), hermes, hermes_home=home / ".hermes")
    if args.once:
        n = conn.once()
        print("WAIT connector_effects is off" if n < 0 else f"PASS {n} handed to Hermes")
        return 1 if conn.unsaid else 0
    conn.forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
