"""The connector: carries Patrick's chat, and the "Bot told" messages, from the core into Hermes.

Runs on the VM as the Bots' `hermes` user (tw-decide-connector.service), with
the system Python and only this repository's code:

    python3 -I -m thornswood_decisions.connector --hermes /home/hermes/.local/bin/hermes

It does nothing while `connector_effects` is off. Otherwise, every few seconds
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
from shared-state.db, both read only, 50 messages at a time.
The same claim hands it Patrick's reactions on Bot Chat messages: each is set in
that Bot's own store with `room_send.py` op `react` (Hermes's SessionDB), and
what came of it goes to `POST /api/v1/connector/reacted`.
While a Bot Chat turn runs, what it is doing (thinking, writing, or a tool by its
plain name, read from Hermes's stream with own_connector.follow_turn, never the
words) goes to `POST /api/v1/connector/doing` when it changes, for the phone's
live line under the message.
For each message, it tells the core what came of it (`POST /api/v1/connector/result`):
confirmed only when Hermes said so, failed only when nothing was sent, and
uncertain for everything else. An uncertain message is never sent again by
itself. A gateway that is not ready (it is starting, stopped, being reloaded by
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
the claims: a Bot's turn holds its round for as long as it runs, up to 15
minutes, and the core calls a Bot "not heard from" after two minutes without a
report.
"""

from __future__ import annotations

import argparse
import json
import os
import re
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

from .own_connector import follow_turn, probe_env

CORE = "http://127.0.0.1:9135/api/v1"
POLL_SECONDS = 5
TURN_SECONDS = 15 * 60
CHECK_SECONDS = 60
LIVE_SECONDS = 30  # how often each Bot's gateway state goes to the core, for the phone's dots
TASKS_SECONDS = 30  # how often each Bot's subagents, background commands and scheduled jobs go to the core
# At most this many `hermes` turns at once: each is a whole Hermes process, and
# the unit's MemoryMax holds them all.
PARALLEL = 3
DOING_SECONDS = 2    # while turns run, what each is doing goes to the core this often when it changed
DOING_KEEP = 20      # and at least this often, so the core knows the line is current (chat.py DOING_STALE)
BOT_CHAT = "Bot Chat"
PAGE_BYTES = 480 * 1024  # under the core's 512 KB
HELPER = Path(__file__).with_name("room_send.py")
HELPER_FAILED = "the connector's Hermes helper"  # how a helper failure's reason begins (chat.py HELPER_FAILED)
# How the reason begins when a Bot Chat message waits for the fleet queue (tw-policy) instead of the
# gateway (chat.py FLEET). The Bots' turn gate turns away a turn the queue has no room for, and that turn
# runs nothing (src/thornswood_guard/brokers.py TURNED_AWAY_DIR). 2 Nov 2026: Patrick's answer on the #5327
# ad18c2c card went to Bramble while the queue was turning turns away, the turn ended saying it had not
# started, and the connector called it sent. Now a message goes only when the queue would admit its turn,
# and one whose turn was turned away all the same goes back in the queue, unspent, like a gateway that
# was not ready.
FLEET = "the fleet queue"
TURNED_AWAY_DIR = ("local", "thornswood-hermes", "turned-away")
ROOM_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")  # own.SESSION_ID


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


def room_page(shared_db: Path, room_id: str, before: int | None) -> dict:
    from .feed_reader import room_page as read
    n = 50
    while True:
        page = read(shared_db, room_id, before, n)
        if len(json.dumps(page, ensure_ascii=False).encode("utf-8")) <= PAGE_BYTES or n <= 1:
            return page
        n //= 2


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


class Hermes:
    """The two stock ways into the Bots' Hermes."""

    def __init__(self, hermes_cmd: list[str], helper_cmd: list[str] | str, home: Path, helper: Path = HELPER,
                 fleet=None):
        # A Python's path alone (the tests' stand-ins) runs the helper file straight with it.
        self.cmd, self.home = list(hermes_cmd), home
        self.helper_cmd = [helper_cmd, "-I", str(helper)] if isinstance(helper_cmd, str) else list(helper_cmd)
        # fleet(bot) -> (room, why not): whether the fleet queue would admit a turn of that Bot now
        # (fleet_room). None where the Bots have no queue (brokers.ready off): every turn runs.
        self.fleet = fleet

    def turned_away(self, bot: str, since: float, sid: str | None = None) -> str | None:
        """Why the queue turned away the turn that started at `since` (time.time()) in the Bot's Bot Chat,
        or in session `sid`, from the note the Bots' turn gate left for that session; None when it ran. The
        note is taken once read."""
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

    def to_room(self, room_id: str, send_key: str, text: str) -> tuple[str, str, str]:
        got = self._helper({"op": "send", "room_id": room_id, "event_id": send_key, "text": text}, CHECK_SECONDS)
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

    def to_bot(self, bot: str, text: str, doing=None) -> tuple[str, str, str]:
        """(outcome, ref, reason) for one message to a Bot's Bot Chat. `doing(state, tool)` hears what the
        turn is doing as Hermes's stream says it (own_connector.follow_turn): thinking, writing, or a tool."""
        state, why = self.check(bot)
        if state != "ready":
            # Nothing was sent: the message waits, with why (the gateway, or the helper's own failure).
            return "not_ready", "", why
        with tempfile.TemporaryDirectory(prefix="tw-chat-") as folder:
            os.chmod(folder, 0o700)
            path = Path(folder) / "message.txt"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            argv = [*self.cmd, "-p", bot, "chat", "--in", str(self.home), "-c", BOT_CHAT, "--create-if-missing",
                    "-Q", "--format", "stream-json", "--query-file", str(path)]
            started = time.time()
            try:
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, cwd=str(self.home), text=True,
                                        encoding="utf-8", errors="replace")
            except OSError:
                return "failed", "", "could not start the hermes command"
            reader = threading.Thread(target=follow_turn, args=(proc.stdout, {}, doing or (lambda s, t: None)),
                                      daemon=True)
            reader.start()
            try:
                code = proc.wait(timeout=TURN_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                return "uncertain", "", "no end to the Bot's turn in 15 minutes; it may have the message"
            finally:
                reader.join(30)
                proc.stdout.close()
        if code == 0:
            if self.fleet is not None:
                why = self.turned_away(bot, started)
                if why is not None:
                    # The turn ran nothing: the message goes back in the queue as it was, and goes again
                    # once the queue has room (check, above).
                    return "not_ready", "", f"{FLEET} turned the turn away: {why}"
            return "confirmed", "turn finished", ""
        return "uncertain", "", f"hermes stopped with exit code {code}; it may have the message"

    def to_session(self, bot: str, sid: str, text: str, doing=None) -> tuple[str, str, str]:
        """(outcome, ref, reason) for one message into one session of a Bot: the one a Board row opens,
        where the Bot may be working right now (Patrick, 9 Nov). It goes the way Desktop's message box
        sends into an open chat: room_send.py `chat --resume`, through the Bot's running gateway, which
        holds the session's turn. So a message into a session whose turn is running is steered into it,
        interrupts it, or waits behind it, as that session's busy setting says, and never runs beside it.
        The turn is the Bot's own, unattended, under its own guard rails, as a Bot Chat turn is.

        A steered or redirected message has no turn of its own: Hermes has it once it says so, which is
        the end of this message (the Board's live line follows the Bot's turn). Anything else is followed
        to its turn's end, as a Bot Chat turn is."""
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
        with tempfile.TemporaryDirectory(prefix="tw-chat-") as folder:
            os.chmod(folder, 0o700)
            path = Path(folder) / "message.txt"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            argv = [*self.helper_cmd, "chat", "-p", bot, "-Q", "--format", "stream-json", "--query-file", str(path),
                    "--resume", sid]
            # The Bot's own home, named before any of Hermes is imported (room_send.py loads Hermes's chat
            # view before Hermes reads -p), so every part of the turn is that profile's.
            env = dict(os.environ, HERMES_HOME=str(self.home / ".hermes" / "profiles" / bot))
            started = time.time()
            try:
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, cwd=str(self.home), text=True,
                                        encoding="utf-8", errors="replace", env=env)
            except OSError:
                return "failed", "", "could not start the hermes command"
            seen: dict = {}
            taken = threading.Event()

            def heard(st, tool):
                if seen.get("taken"):
                    taken.set()
                (doing or (lambda s, t: None))(st, tool)

            reader = threading.Thread(target=follow_turn, args=(proc.stdout, seen, heard), daemon=True)
            reader.start()
            ends = time.monotonic() + TURN_SECONDS
            code = None
            while code is None and not taken.is_set() and time.monotonic() < ends:
                try:
                    code = proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            if code is None and taken.is_set():
                # Hermes has the message in the running turn. What is left is only watching that turn, so the
                # view is let go of, as Desktop's /detach does, and ends without touching the turn.
                proc.kill()
                proc.wait()
                reader.join(30)
                proc.stdout.close()
                return "confirmed", f"{seen['taken']} into the running turn", ""
            if code is None:
                proc.kill()
                proc.wait()
                reader.join(30)
                proc.stdout.close()
                return "uncertain", "", "no end to the Bot's turn in 15 minutes; it may have the message"
            reader.join(30)
            proc.stdout.close()
        if seen.get("taken"):
            return "confirmed", f"{seen['taken']} into the running turn", ""
        if code == 0:
            if self.fleet is not None:
                why = self.turned_away(bot, started, sid)
                if why is not None:
                    return "not_ready", "", f"{FLEET} turned the turn away: {why}"
            return "confirmed", "turn finished", ""
        return "uncertain", "", f"hermes stopped with exit code {code}; it may have the message"


def fleet_room(policy_file: Path | None = None):
    """fleet(bot) for Hermes: tw-policy's turn_room, asked over its socket as the Bots' own user, or None
    when the Bots have no queue (brokers.ready off). A queue that does not answer has no room."""
    from thornswood_guard.brokers import BrokerRefused, call
    from thornswood_guard.policy import Policy
    policy = Policy.load(policy_file) if policy_file else Policy.load()
    if not policy.data["brokers"].get("ready"):
        return None
    sock = policy.data["brokers"]["sockets"]["policy"]["path"]

    def fleet(bot: str) -> tuple[bool, str]:
        try:
            got = call(sock, "turn_room", {"bot": bot}, timeout=CHECK_SECONDS)
        except BrokerRefused as e:
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


def say(line: str) -> None:
    """One line to the journal now: under systemd stdout is a pipe, and `-I` ignores PYTHONUNBUFFERED."""
    print(line, flush=True)


class Connector:
    def __init__(self, core: Core, hermes: Hermes, say=say, hermes_home: Path | None = None):
        self.core, self.hermes, self.say = core, hermes, say
        self.hermes_home = hermes_home or Path.home() / ".hermes"
        self.unsaid: list[dict] = []  # results the core has not taken yet
        self.waiting: dict[str, str] = {}  # gateways (by profile, "" for the room) found not ready: why
        self._lock = threading.Lock()
        self.doing: dict[str, dict] = {}    # each Bot Chat turn running now: what it is doing
        self.live_bots: list[str] = []      # the Bots the core asked about last time
        self.live_at: float | None = None
        self.tasks_at: float | None = None
        self._tasks_run: threading.Thread | None = None

    def deliver(self, item: dict) -> dict:
        if item["target"] == "room":
            outcome, ref, reason = self.hermes.to_room(item["room_id"], item["send_key"], item["text"])
        else:
            def doing(state, tool):
                with self._lock:
                    self.doing[item["id"]] = {"id": item["id"], "state": state, "tool": tool}
            try:
                if item.get("session"):
                    outcome, ref, reason = self.hermes.to_session(item["target"], item["session"], item["text"], doing)
                else:
                    outcome, ref, reason = self.hermes.to_bot(item["target"], item["text"], doing)
            finally:
                with self._lock:
                    self.doing.pop(item["id"], None)
        return {"id": item["id"], "outcome": outcome, "ref": ref, "reason": reason}

    def say_doing(self, threads: list, clock=time.monotonic, wait=DOING_SECONDS) -> None:
        """While the round's turns run: what each Bot Chat turn is doing, to the core every `wait` seconds
        when it changed and at least every DOING_KEEP, for the phone's live line. Reading only: the core
        takes it while mobile_read is on, whatever connector_effects says."""
        said, said_at = None, None
        while any(t.is_alive() for t in threads):
            with self._lock:
                now_doing = sorted(self.doing.values(), key=lambda d: d["id"])
            if now_doing and (now_doing != said or clock() - said_at >= DOING_KEEP):
                # Said once per change whatever the answer: a refusal (mobile_read off) is not asked again
                # every two seconds, only with the next change or after DOING_KEEP.
                self.core.post("/connector/doing", {"turns": now_doing})
                said, said_at = now_doing, clock()
            for t in threads:
                t.join(wait / max(len(threads), 1))

    def _report(self) -> None:
        keep = []
        for r in self.unsaid:
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
            else:
                keep.append(r)
        self.unsaid = keep

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
            self.unsaid.append({"bot": r["bot"], "message_id": r["message_id"], "version": r["version"],
                                "outcome": outcome, "reason": reason})
        return done

    def _still_waiting(self) -> bool:
        """After a message came back not_ready: check each such gateway once this round. True while any is
        still not ready, so nothing more is claimed (and leased, and put back) until it is."""
        for profile in list(self.waiting):
            state, why = self.hermes.check(profile)
            if state == "ready":
                del self.waiting[profile]
                self.say(f"PASS {profile or 'the room'}'s gateway is ready; its messages go now")
            else:
                if state == "failed" and why != self.waiting[profile]:
                    self.say(f"FAIL {why}")
                self.waiting[profile] = why
        return bool(self.waiting)

    def once(self) -> int:
        """One round. Returns how many messages went to Hermes, or -1 when the core said to wait."""
        self._report()
        if self.waiting and self._still_waiting():
            return 0
        status, body = self.core.post("/connector/claim", {"limit": PARALLEL})
        if status == 423:
            return -1
        if status == 401:
            raise SystemExit("STOP: the core refused the connector token")
        if status != 200:
            return 0
        items = body.get("items", [])
        self.react(body.get("reactions", []))
        results: list[dict] = []

        def run(item):
            try:
                r = self.deliver(item)
            except Exception as exc:  # whatever it was, the message may have gone: say so, never resend
                r = {"id": item["id"], "outcome": "uncertain", "ref": "",
                     "reason": f"the connector hit {type(exc).__name__} while sending"}
            with self._lock:
                results.append(r)

        threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in items]
        for t in threads:
            t.start()
        self.say_doing(threads)
        for t in threads:
            t.join()
        gateway = {i["id"]: "" if i["target"] == "room" else i["target"] for i in items}
        for r in results:
            helper = r["reason"].startswith(HELPER_FAILED)
            if helper and r["outcome"] != "not_ready":
                self.say(f"FAIL {r['id']}: {r['reason']}")
            if r["outcome"] == "not_ready" and r["id"] in gateway:
                if gateway[r["id"]] not in self.waiting:
                    self.say(f"FAIL {r['reason']}; {gateway[r['id']] or 'the room'}'s messages wait in the queue"
                             if helper else
                             f"NOTE {r['reason']}; {gateway[r['id']]}'s messages wait in the queue"
                             if r["reason"].startswith(FLEET) else
                             f"NOTE {gateway[r['id']] or 'the room'}'s gateway is not ready ({r['reason']}); "
                             "its messages wait in the queue")
                self.waiting[gateway[r["id"]]] = r["reason"]
        self.unsaid.extend(results)
        self._report()
        return len(items)

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
                    page = room_page(self.hermes_home / "shared-state.db", target, before)
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

    def reads(self, sleep=time.sleep) -> None:
        """The phone's pages, each Bot's state and its tasks, every few seconds, whatever connector_effects says. Reading
        only, so it never waits on a claim round: a round waits for its turns, and on 8 Nov a three-minute turn
        to Bramble left all six Bots "not heard from" on the phone while it ran."""
        while True:
            self.pages()
            self.live()
            self.tasks()
            sleep(POLL_SECONDS)

    def forever(self) -> None:
        # The reads on their own thread; the claim every few seconds, or every half minute while the core says
        # connector_effects is off. A reads thread that raised stops the connector, as it did when both shared
        # this loop, with its traceback in the journal.
        reader = threading.Thread(target=self.reads, name="reads", daemon=True)
        reader.start()
        claim_at = 0.0
        while True:
            if not reader.is_alive():
                raise SystemExit("STOP: the thread for pages and Bot states stopped")
            if time.monotonic() >= claim_at:
                n = self.once()
                claim_at = time.monotonic() + (POLL_SECONDS * 6 if n < 0 else 0)
            time.sleep(POLL_SECONDS)


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
    ap.add_argument("--once", action="store_true", help="one round, then stop")
    args = ap.parse_args(argv)
    hermes = Hermes([args.hermes], helper_command(args.hermes, args.hermes_python, args.hermes_root), home,
                    fleet=fleet_room())
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
