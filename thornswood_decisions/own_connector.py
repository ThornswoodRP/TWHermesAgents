"""Carries Patrick's chat from the phone into his own Hermes: my-hermes on the VM, or his Hermes on One.

One file, standard library only, so the same copy runs on the VM and on One
(Windows). It imports nothing from this repository. It runs as that Hermes's
own user and reaches out to the decision core; nothing listens on any port.

    python3 -I own_connector.py --gateway me  --hermes /home/patrick-hermes/.local/bin/hermes
    py -3 -I own_connector.py   --gateway one --hermes C:\\...\\hermes.exe --core https://hermes.<tailnet>.ts.net/api/v1

Every few seconds it:

1. checks that Hermes's gateway is running, without starting one (room_send.py's
   readiness check, with Hermes's own Python), and tells the core whether it is
   while it leases the oldest waiting message of each chat that has nothing in
   flight (`POST /own/<gateway>/claim`, with its token in
   ~/.config/thornswood/own-<gateway>.token on the VM, or
   %APPDATA%\\Thornswood\\own-one.token on One);
2. runs each message as a turn in that Hermes, the way Hermes runs a turn with
   no one at the keyboard (contracts.json "own_chat"):

       a new chat:      hermes chat -Q --format stream-json --query-file <file> --yolo --source cli --in <home>
       an existing one: hermes chat -Q --format stream-json --query-file <file> --resume <session id>

   A new chat is made with `--yolo`, Patrick's "full power behind the passkey"
   (the core takes a message only from a passkey sign-in of the last 30
   minutes), and listed like any chat he starts (`--source cli`); Hermes names
   it as it names any chat, and the turn's last line says its id. Hermes keeps
   a chat's launch settings for good and refuses a resume that would change
   them (gateway_chat.py check_resume_policy), so a chat started elsewhere runs
   the phone's turn with its own approvals: a step that needs an approval is
   refused, unless that chat has /yolo on. Nothing here turns /yolo on. The
   text goes in a file only this user can read, never on a command line, and
   the file is gone when the turn ends;
3. tells the core what came of it (`POST /own/<gateway>/result`): confirmed
   only when Hermes finished the turn and said so, failed only when nothing
   was sent, and uncertain for everything else. An uncertain message is never
   sent again by itself;
4. every 20 seconds, and after each turn, sends the core the chat list, the
   way Hermes's own pickers list it, with the last messages of the five most
   recent chats and of any the phone asked for (`POST /own/<gateway>/feed`).

While own_chat_write is off the core answers 423 to all of it; this waits and
copies nothing. It logs no message text, no chat title and no token.
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
from datetime import datetime, timezone
from pathlib import Path

LOCAL_CORE = "http://127.0.0.1:9135/api/v1"
GATEWAYS = {"me": "my-hermes", "one": "One's Hermes"}
POLL_SECONDS = 5
FEED_SECONDS = 20
READY_SECONDS = 60       # how often a running gateway is checked again
NOT_READY_SECONDS = 15   # and one that was not running
TURN_SECONDS = 15 * 60
PARALLEL = 3
CHATS = 50
OPEN_RECENT = 5
MESSAGES = 30
TEXT = 2000
FEED_BYTES = 480 * 1024  # under the core's 512 KB
PAGE_CHATS = 50
PAGE_MESSAGES = 50
PAGE_TEXT = 8000
PAGE_KINDS = ("chats", "archived", "messages")
# Sessions Hermes's own pickers leave out (hermes_state_sessions.py INTERNAL_LISTING_SOURCES), and sub-agent runs.
NOT_LISTED = ("kanban", "tool", "oneshot", "subagent")
WINDOWS = os.name == "nt"
# One's task runs the connector with pythonw, which has no console. Windows gives every console program it
# starts (Hermes's python.exe for the readiness check each minute, hermes.exe for a turn, git for an
# update) a console window of its own, which showed on Patrick's screen for as long as each one ran (2 Nov,
# windows flashing open and shut). CREATE_NO_WINDOW starts it with none. Every child goes through quiet().
CREATE_NO_WINDOW = 0x08000000


def quiet() -> dict:
    """What every child process of the connector is started with: on Windows, no console window."""
    return {"creationflags": CREATE_NO_WINDOW} if WINDOWS else {}
# The core's own rule for a chat id (own.py), checked again before it goes after --resume.
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def say(line: str) -> None:
    """One line to the journal now. Under pythonw (One's task, with no window) there is no stdout."""
    if sys.stdout is not None:
        print(line, flush=True)


def read_token(path: Path) -> str:
    if not WINDOWS and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise SystemExit(f"STOP: {path} is readable by others; it must be 0600")
    return path.read_text(encoding="utf-8").strip()


def hermes_home() -> Path:
    """Where Hermes keeps its state for this user (hermes_constants.py get_default_hermes_root)."""
    if os.environ.get("HERMES_HOME"):
        return Path(os.environ["HERMES_HOME"])
    if WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "hermes"
    return Path.home() / ".hermes"


# The entry that ends the command Hermes's launcher prints for --print-runtime-command (pinned
# Hermes, hermes_cli/_launchers.py runtime_command and print_runtime_command; contracts.json own_chat).
RUNTIME_ENTRY = "runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)"
HELPER = Path(__file__).with_name("room_send.py")


def helper_command(hermes_bin: str, helper: Path = HELPER, python: str | None = None) -> list[str]:
    """How to run the readiness helper inside this Hermes's own Python, with its packages.

    Hermes's launcher says how it starts its runtime (`hermes --print-runtime-command`): on One
    that is a Python kept in Hermes's own store, started with a bootstrap that puts Hermes on its
    path, and nothing lies beside hermes.exe. The helper runs in that same bootstrap, with the
    helper's file in place of Hermes's own entry. A launcher too old to answer is a plain script
    or a venv's hermes.exe, whose interpreter the command names (hermes_python)."""
    if python:
        return [python, "-I", str(helper)]
    try:
        out = subprocess.run([hermes_bin, "--print-runtime-command"], capture_output=True, timeout=60, **quiet(),
                             check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"STOP: {hermes_bin} --print-runtime-command did not run ({type(exc).__name__})")
    if out.returncode != 0:
        return [hermes_python(hermes_bin), "-I", str(helper)]
    try:
        cmd = json.loads(out.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        cmd = None
    if (not isinstance(cmd, list) or len(cmd) != 4 or not all(isinstance(c, str) for c in cmd)
            or cmd[1:3] != ["-I", "-c"] or not cmd[3].endswith(RUNTIME_ENTRY)
            or not os.path.isabs(cmd[0]) or not os.path.isfile(cmd[0])):
        raise SystemExit(f"STOP: {hermes_bin} --print-runtime-command answered a command this connector does "
                         "not know; check contracts.json own_chat against this Hermes")
    run_helper = f"runpy.run_path({json.dumps(str(helper))}, run_name='__main__')"
    return [cmd[0], "-I", "-c", cmd[3][:-len(RUNTIME_ENTRY)] + run_helper]


def hermes_python(hermes_bin: str) -> str:
    """The interpreter an older `hermes` command runs with: python.exe beside a venv's hermes.exe, or its #! line."""
    path = Path(hermes_bin)
    if path.suffix.lower() == ".exe":
        py = path.with_name("python.exe")
        if not py.exists():
            raise SystemExit(f"STOP: no python.exe beside {hermes_bin}; pass --hermes-python")
        return str(py)
    first = path.read_bytes()[:400].split(b"\n", 1)[0].decode("utf-8", "replace")
    if not first.startswith("#!"):
        raise SystemExit(f"STOP: {hermes_bin} does not start with #!; pass --hermes-python")
    exe = first[2:].strip().split()[0]
    if not exe.startswith("/") or exe.rsplit("/", 1)[-1] == "env":
        raise SystemExit(f"STOP: {hermes_bin} does not name its Python by path; pass --hermes-python")
    return exe


def _iso(epoch) -> str:
    if epoch is None:
        return ""
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat()


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


# ── reading Hermes's chats ────────────────────────────────────────────────

LIST_SQL = f"""
SELECT s.id, s.title, coalesce(s.last_activity_at, s.started_at) AS last_at FROM sessions s
WHERE s.archived = 0 AND s.hidden = 0
  AND s.source NOT IN ({",".join("?" * len(NOT_LISTED))})
  AND coalesce(s.end_reason, '') != 'compression'
  AND (s.model_config IS NULL OR NOT json_valid(s.model_config)
       OR (json_extract(s.model_config, '$._delegate_from') IS NULL))
ORDER BY last_at DESC LIMIT ?
"""
# A compressed chat goes on in a new session whose parent ended with 'compression'; only the newest
# is listed, as Hermes's pickers show one row for it (hermes_state_sessions.py _expand_compression_lineage_ids).

# His Bot Chat, the chat Bot Mode and every Bot's message to him land in. Hermes keeps it hidden from
# its pickers, and its name is its identity: the canonical one is the hidden, unarchived row titled
# exactly "Bot Chat" (hermes_state_titles.py CANONICAL_BOT_CHAT_TITLE; gateway/session_bot.py finds it
# by that title, then goes on to its compression tip). The phone lists it first, by the id it goes on as.
BOT_CHAT = "Bot Chat"
BOT_CHAT_SQL = "SELECT id FROM sessions WHERE title = ? AND hidden = 1 AND archived = 0 ORDER BY started_at DESC LIMIT 1"
MESSAGES_SQL = ("SELECT role, content, timestamp FROM messages WHERE session_id = ? AND active = 1 "
                "AND role IN ('user', 'assistant') AND content IS NOT NULL AND content != '' "
                "ORDER BY id DESC LIMIT ?")


def _ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def snapshot(state_db: Path, wanted: list[str], now: datetime, messages: int = MESSAGES) -> dict:
    """The chat list and the open chats' last messages, from Hermes's state.db, read only."""
    con = _ro(state_db)
    try:
        bot = bot_chat(con)
        rows = [r for r in con.execute(LIST_SQL, (*NOT_LISTED, CHATS)).fetchall() if not bot or r["id"] != bot["id"]]
        chats, previews = ([bot], {bot["id"]: bot["preview"]}) if bot else ([], {})
        for r in rows[:CHATS - len(chats)]:
            last = con.execute(MESSAGES_SQL, (r["id"], 1)).fetchone()
            text = _text(last["content"]) if last else ""
            chats.append({"id": r["id"], "title": r["title"] or "", "last_at": _iso(r["last_at"]),
                          "preview": text[:200]})
            previews[r["id"]] = text
        open_ids = [c["id"] for c in chats[:OPEN_RECENT]]
        open_ids += [w for w in wanted if w not in open_ids]
        opened = []
        for sid in open_ids:
            msgs = con.execute(MESSAGES_SQL, (sid, messages)).fetchall()
            opened.append({"id": sid, "messages": [{"role": m["role"], "text": _text(m["content"])[:TEXT],
                                                    "at": _iso(m["timestamp"])} for m in reversed(msgs)]})
    finally:
        con.close()
    return {"source_time": now.isoformat(), "chats": chats, "open": opened}


# A chat's compression lineage, as Hermes's own hermes_state_sessions.py _LINEAGE_CTE_SQL walks it: the
# chats it went on from (each ended with 'compression') and the chats it goes on as.
LINEAGE_SQL = """
WITH RECURSIVE
  ancestors(id) AS (
    SELECT ?
    UNION
    SELECT parent.id FROM ancestors a JOIN sessions child ON child.id = a.id
    JOIN sessions parent ON parent.id = child.parent_session_id WHERE parent.end_reason = 'compression'),
  descendants(id, depth) AS (
    SELECT ?, 0
    UNION
    SELECT child.id, d.depth + 1 FROM descendants d JOIN sessions parent ON parent.id = d.id
    JOIN sessions child ON child.parent_session_id = parent.id WHERE parent.end_reason = 'compression')
"""


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def bot_chat(con: sqlite3.Connection) -> dict | None:
    """His Bot Chat as the phone lists it: the id it goes on as now, or None when this Hermes has none."""
    row = con.execute(BOT_CHAT_SQL, (BOT_CHAT,)).fetchone()
    if row is None:
        return None
    tip = con.execute(LINEAGE_SQL + "SELECT s.id, coalesce(s.last_activity_at, s.started_at) AS last_at "
                      "FROM descendants d JOIN sessions s ON s.id = d.id ORDER BY d.depth DESC, s.started_at DESC "
                      "LIMIT 1", (row["id"], row["id"])).fetchone()
    last = con.execute(MESSAGES_SQL, (tip["id"], 1)).fetchone()
    text = _text(last["content"]) if last else ""
    return {"id": tip["id"], "title": BOT_CHAT, "last_at": _iso(tip["last_at"]), "preview": text[:200],
            "bot_chat": True}


def chat_exists(state_db: Path, session_id: str) -> bool:
    con = _ro(state_db)
    try:
        return con.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone() is not None
    finally:
        con.close()


def chats_page(state_db: Path, before: str | None, q: str, n: int = PAGE_CHATS, archived: bool = False) -> dict:
    """Chats Hermes's own pickers list, newest first, older than `before`, with `q` in the title; with
    `archived`, the ones Hermes hides instead (to bring one back)."""
    sql = LIST_SQL.replace("ORDER BY last_at DESC LIMIT ?", "")
    if archived:
        sql = sql.replace("WHERE s.archived = 0", "WHERE s.archived = 1")
    where, args = [], [*NOT_LISTED]
    if before:
        where.append("coalesce(s.last_activity_at, s.started_at) < ?")
        args.append(datetime.fromisoformat(before).timestamp())
    if q:
        where.append("lower(coalesce(s.title, '')) LIKE ? ESCAPE '\\'")
        args.append("%" + q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    sql = sql.rstrip() + "".join(f"\n  AND {w}" for w in where) + "\nORDER BY last_at DESC, s.id LIMIT ?"
    con = _ro(state_db)
    try:
        # It heads the first page of the chat list, and a search for its name finds it. It is never
        # archived (BOT_CHAT_SQL takes only the unarchived one), so a page of hidden chats never lists it.
        bot = None if archived else bot_chat(con)
        rows = [r for r in con.execute(sql, (*args, n + 1)).fetchall() if not bot or r["id"] != bot["id"]]
        chats = [bot] if bot and not before and q.lower() in BOT_CHAT.lower() else []
        for r in rows[:n - len(chats)]:
            last = con.execute(MESSAGES_SQL, (r["id"], 1)).fetchone()
            chats.append({"id": r["id"], "title": r["title"] or "", "last_at": _iso(r["last_at"]),
                          "preview": (_text(last["content"]) if last else "")[:200]})
    finally:
        con.close()
    return {"chats": chats, "more": len(rows) > n - (1 if chats and chats[0].get("bot_chat") else 0)}


def messages_page(state_db: Path, session_id: str, before: int | None, n: int = PAGE_MESSAGES) -> dict:
    """One page of a chat's messages, the newest `n` older than Hermes's message id `before`, oldest
    first, across its whole compression lineage, by Hermes's own message ids. `gone` when Hermes no
    longer has the chat; `continued_as` the chat it goes on as when Hermes compressed it."""
    con = _ro(state_db)
    try:
        if con.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone() is None:
            return {"messages": [], "more": False, "gone": True, "continued_as": None}
        tip = con.execute(LINEAGE_SQL + "SELECT d.id FROM descendants d JOIN sessions s ON s.id = d.id "
                          "ORDER BY d.depth DESC, s.started_at DESC LIMIT 1", (session_id, session_id)).fetchone()[0]
        cols = _columns(con, "messages")
        summary = "AND m._compressed_summary = 0 " if "_compressed_summary" in cols else ""
        meta = "m.display_metadata" if "display_metadata" in cols else "NULL"
        rows = con.execute(
            LINEAGE_SQL + f"SELECT m.id, m.role, m.content, m.timestamp, {meta} AS meta FROM messages m "
            "WHERE m.session_id IN (SELECT id FROM ancestors UNION SELECT id FROM descendants) AND m.active = 1 "
            "AND m.role IN ('user', 'assistant') AND m.content IS NOT NULL AND m.content != '' " + summary +
            "AND (? IS NULL OR m.id < ?) ORDER BY m.id DESC LIMIT ?",
            (session_id, session_id, before, before, n + 1)).fetchall()
    finally:
        con.close()
    msgs = [{"mid": r["id"], "role": r["role"], "text": _text(r["content"])[:PAGE_TEXT], "at": _iso(r["timestamp"]),
             "reactions": reactions(r["meta"])} for r in reversed(rows[:n])]
    return {"messages": msgs, "more": len(rows) > n, "gone": False,
            "continued_as": tip if tip != session_id else None, "read_at": datetime.now(timezone.utc).isoformat()}


REACTION_BY = {"user": "patrick", "agent": "hermes"}


def reactions(raw) -> list[dict]:
    """The reactions Hermes keeps on a message row, under display_metadata's "reactions": one per
    person, {emoji, author} with author "user" (Patrick) or "agent" (Hermes). Older rows carry the
    JSON as a string inside a string (hermes_state_messages.py _decode_display_metadata)."""
    meta = raw
    for _ in range(2):
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except json.JSONDecodeError:
                return []
    out = []
    for r in (meta.get("reactions") if isinstance(meta, dict) else None) or []:
        if isinstance(r, dict) and r.get("author") in REACTION_BY and isinstance(r.get("emoji"), str) and r["emoji"]:
            out.append({"emoji": r["emoji"][:16], "by": REACTION_BY[r["author"]]})
    return out[:8]


def fitted_page(state_db: Path, req: dict) -> bytes:
    """A page as the core takes it: under its size, with fewer rows if need be. The cursor is the oldest row
    sent, so a shorter page leaves no gap."""
    n = PAGE_CHATS if req["kind"] in ("chats", "archived") else PAGE_MESSAGES
    while True:
        if req["kind"] in ("chats", "archived"):
            page = chats_page(state_db, req.get("before"), req.get("q") or "", n, archived=req["kind"] == "archived")
        else:
            page = messages_page(state_db, req["chat"], req.get("before"), n)
        body = json.dumps({"id": req["id"], "page": page}, ensure_ascii=False).encode("utf-8")
        if len(body) <= FEED_BYTES or n <= 1:
            return body
        n //= 2


def fitted(state_db: Path, wanted: list[str], now: datetime) -> bytes:
    """The feed as the core takes it: under its size, with fewer messages in each open chat if need be."""
    n = MESSAGES
    while True:
        body = json.dumps(snapshot(state_db, wanted, now, n), ensure_ascii=False).encode("utf-8")
        if len(body) <= FEED_BYTES or n <= 1:
            return body
        n //= 2


# ── running a turn ────────────────────────────────────────────────────────

class Hermes:
    def __init__(self, hermes_cmd: list[str], ready_cmd: list[str], home_dir: Path, name: str):
        self.cmd, self.ready_cmd, self.home_dir, self.name = list(hermes_cmd), list(ready_cmd), home_dir, name

    def ready(self) -> tuple[str, str]:
        """(state, why): is this Hermes's gateway running? Asked without starting one.

        "ready", "gateway_down" (the check ran and found no gateway running) or "check_failed"
        (the check itself could not run), which the phone tells apart (plan Part 1)."""
        req = json.dumps({"op": "ready", "profile": "", "name": self.name}).encode("utf-8")
        try:
            out = subprocess.run(self.ready_cmd, input=req, capture_output=True, **quiet(),
                                 timeout=60, cwd=str(self.home_dir), check=False)
            got = json.loads(out.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
        except subprocess.TimeoutExpired:
            return "check_failed", "the readiness check did not answer in a minute"
        except (OSError, IndexError, json.JSONDecodeError):
            return "check_failed", "could not run the readiness check with Hermes's Python"
        if not isinstance(got, dict):
            return "check_failed", "the readiness check did not answer in its shape"
        if got.get("ready") is True:
            return "ready", ""
        return "gateway_down", str(got.get("reason") or f"{self.name} is not ready")[:200]

    def react(self, session_id: str, message_id: int, emoji: str | None) -> tuple[str, str]:
        """(outcome, reason): set Patrick's reaction on one message to exactly `emoji` (None takes it off),
        with Hermes's own store, through the same helper and Python as the readiness check."""
        req = json.dumps({"op": "react", "profile": "", "session_id": session_id, "message_id": message_id,
                          "emoji": emoji}).encode("utf-8")
        try:
            out = subprocess.run(self.ready_cmd, input=req, capture_output=True, **quiet(),
                                 timeout=60, cwd=str(self.home_dir), check=False)
            got = json.loads(out.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
        except subprocess.TimeoutExpired:
            return "failed", "Hermes's store did not answer in a minute"
        except (OSError, IndexError, json.JSONDecodeError):
            return "failed", "could not run the helper with Hermes's Python"
        if isinstance(got, dict) and got.get("set") is True:
            return "set", ""
        reason = got.get("reason") if isinstance(got, dict) else None
        return "failed", str(reason or "not set")[:200]

    def session(self, action: str, session_id: str, title: str | None) -> tuple[str, str]:
        """(outcome, reason): rename, hide, bring back or delete one chat with Hermes's own store, through
        the same helper and Python as the readiness check. The title goes on stdin, never a command line."""
        req = json.dumps({"op": "session", "profile": "", "action": action, "session_id": session_id,
                          "title": title}).encode("utf-8")
        try:
            out = subprocess.run(self.ready_cmd, input=req, capture_output=True, **quiet(),
                                 timeout=60, cwd=str(self.home_dir), check=False)
            got = json.loads(out.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
        except subprocess.TimeoutExpired:
            return "failed", "Hermes's store did not answer in a minute"
        except (OSError, IndexError, json.JSONDecodeError):
            return "failed", "could not run the helper with Hermes's Python"
        if isinstance(got, dict) and got.get("done") is True:
            return "done", ""
        reason = got.get("reason") if isinstance(got, dict) else None
        return "failed", str(reason or "not done")[:200]

    def turn(self, session_id: str | None, text: str) -> tuple[str, str, str, str | None]:
        """(outcome, ref, reason, session id) for one message."""
        if session_id is not None and not SESSION_ID.match(session_id):
            return "failed", "", "that is not a chat id Hermes gives", None
        with tempfile.TemporaryDirectory(prefix="tw-own-") as folder:
            if not WINDOWS:
                os.chmod(folder, 0o700)
            path = Path(folder) / "message.txt"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            argv = [*self.cmd, "chat", "-Q", "--format", "stream-json", "--query-file", str(path)]
            if session_id:
                argv += ["--resume", session_id]
            else:
                argv += ["--yolo", "--source", "cli", "--in", str(self.home_dir)]
            return self._run(argv, session_id)

    def _run(self, argv: list[str], session_id: str | None) -> tuple[str, str, str, str | None]:
        seen: dict = {}

        def read(stream):
            for line in stream:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(ev, dict) and ev.get("type") in ("system", "result"):
                    seen[ev["type"]] = ev  # only the session id and the end are kept, never the text
                    seen[ev["type"]].pop("text", None)

        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, **quiet(),
                                    stderr=subprocess.DEVNULL, cwd=str(self.home_dir), text=True,
                                    encoding="utf-8", errors="replace")
        except OSError:
            return "failed", "", "could not start the hermes command", None
        reader = threading.Thread(target=read, args=(proc.stdout,), daemon=True)
        reader.start()
        try:
            code = proc.wait(timeout=TURN_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            reader.join(5)
            proc.stdout.close()
            return "uncertain", "", "no end to the turn in 15 minutes; Hermes may have the message", \
                self._sid(seen, session_id)
        reader.join(30)
        proc.stdout.close()
        result = seen.get("result")
        sid = self._sid(seen, session_id)
        if code == 0 and result and result.get("exit_code") == 0:
            return "confirmed", "turn finished", "", sid
        why = (result or {}).get("error") or f"hermes stopped with exit code {code}"
        return "uncertain", "", f"{str(why)[:150]}; Hermes may have the message", sid

    @staticmethod
    def _sid(seen: dict, session_id: str | None) -> str | None:
        for kind in ("result", "system"):
            sid = (seen.get(kind) or {}).get("session_id")
            if isinstance(sid, str) and sid:
                return sid
        return session_id


# ── keeping One's copy current ────────────────────────────────────────────

# The release the VM runs: a commit of main that passed the whole suite there before it was installed
# (thornswood_brokers/follow_main.py). The core names it in every claim answer.
RELEASE = re.compile(r"^[0-9a-f]{40}$")
UPDATE_FILES = ("own_connector.py", "room_send.py")
UPDATE_EXIT = 75         # "updated, start me again at once", for --supervise
RESTART_SECONDS = 60     # how long --supervise waits after any other stop


def own_commit(here: Path | None = None) -> str | None:
    """The commit this copy came from: COMMIT beside it (One: written by its installer and by each update
    since), or the release folder it runs from (the VM: /opt/thornswood-hermes/releases/<commit>)."""
    here = here or Path(__file__).resolve().parent
    try:
        commit = (here / "COMMIT").read_text(encoding="ascii").strip()
    except OSError:
        commit = here.parent.name
    return commit if RELEASE.match(commit) else None


class Updater:
    """Moves One's copy to the release the VM runs, the way the VM follows main by itself (#2862).

    The VM names the commit; the code comes from GitHub, never from the VM: Patrick's own clone of
    Thornswood-Hermes on One fetches main, the commit has to be on main, and its two files are taken
    from that commit with `git show`, each checked to compile before either is put in place, then
    COMMIT. The connector then finishes the turns it is running, takes no new one, and stops with
    UPDATE_EXIT, which --supervise answers by starting the new copy at once. A failed update changes
    nothing, is told to the core in words, and is tried again only for a newer release (or at the next
    sign-in): never in a loop."""

    def __init__(self, clone: Path, git: str, folder: Path, run=subprocess.run):
        self.clone, self.git_bin, self.folder, self.run = clone, git, folder, run

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        # Never a prompt: nobody is there under Task Scheduler. The clone signs in with gh, set by
        # Install-OwnConnector.ps1 in its own config; credential.interactive=never stops Git
        # Credential Manager's own windows too. A fetch that cannot sign in fails, and says so.
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
        return self.run([self.git_bin, "-c", "credential.interactive=never", "-C", str(self.clone), *args],
                        capture_output=True, timeout=120, check=False, stdin=subprocess.DEVNULL, env=env, **quiet())

    def to(self, release: str) -> tuple[bool, str]:
        """(done, why not)."""
        if not RELEASE.match(release):
            return False, "that is not a commit"
        try:
            got = self._git("fetch", "--quiet", "origin", "main")
            if got.returncode != 0:
                return False, f"git fetch in {self.clone.name} failed (exit {got.returncode})"
            if self._git("merge-base", "--is-ancestor", release, "origin/main").returncode != 0:
                return False, f"{release[:12]} is not on main"
            files = {}
            for name in UPDATE_FILES:
                got = self._git("show", f"{release}:thornswood_decisions/{name}")
                if got.returncode != 0:
                    return False, f"{name} is not in {release[:12]}"
                compile(got.stdout, name, "exec")
                files[name] = got.stdout
        except subprocess.TimeoutExpired:
            return False, "git did not answer in two minutes"
        except (OSError, SyntaxError, ValueError) as exc:
            return False, f"the update stopped ({type(exc).__name__})"
        for name, data in files.items():
            part = self.folder / f"{name}.part"
            part.write_bytes(data)
            os.replace(part, self.folder / name)
        part = self.folder / "COMMIT.part"
        part.write_text(release, encoding="ascii")
        os.replace(part, self.folder / "COMMIT")
        return True, ""


def supervise(argv: list[str], run=subprocess.Popen, sleep=time.sleep, rounds: int | None = None,
              contain: bool = True) -> int:
    """--supervise, as One's task runs it: the connector in a process of its own, started again at once
    when it stops to load an update, and a minute after any other stop. On Windows the connector is in
    a job object that ends with this process, so stopping the task never leaves a connector behind."""
    child = [sys.executable, "-I", str(Path(__file__).resolve()), *[a for a in argv if a != "--supervise"]]
    job = _job() if WINDOWS and contain else None
    n = 0
    while rounds is None or n < rounds:
        n += 1
        proc = run(child, stdin=subprocess.DEVNULL, **quiet())
        if job is not None:
            _in_job(job, proc)
        if proc.wait() != UPDATE_EXIT:
            sleep(RESTART_SECONDS)
    return 0


def _job():
    """A job object whose processes end when its last handle closes (this process ending)."""
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    job = k32.CreateJobObjectW(None, None)
    info = Extended()
    # KILL_ON_JOB_CLOSE, and BREAKAWAY_OK so a process Hermes starts to outlive its caller still can.
    info.BasicLimitInformation.LimitFlags = 0x2000 | 0x800
    if not job or not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        raise SystemExit(f"STOP: could not make the connector's job object ({ctypes.get_last_error()})")
    return (k32, job)


def _in_job(job, proc) -> None:
    k32, handle = job
    if not k32.AssignProcessToJobObject(handle, int(proc._handle)):
        proc.kill()
        raise SystemExit("STOP: could not put the connector in its job object")


# ── the core ──────────────────────────────────────────────────────────────

class Core:
    def __init__(self, base: str, gateway: str, token: str):
        self.base, self.gateway, self.token = base.rstrip("/"), gateway, token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post(self, op: str, body: dict | bytes) -> tuple[int, dict]:
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(f"{self.base}/own/{self.gateway}/{op}", data=data, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.token}"})
        try:
            with self.opener.open(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                out = json.loads(exc.read() or b"{}")
            except json.JSONDecodeError:
                out = {}
            exc.close()
            return exc.code, out
        except (urllib.error.URLError, OSError, ValueError):
            return 0, {}


class OwnConnector:
    def __init__(self, core: Core, hermes: Hermes, state_db: Path, say=say, clock=time.monotonic,
                 commit: str | None = None, updater: Updater | None = None):
        self.core, self.hermes, self.state_db, self.say, self.clock = core, hermes, state_db, say, clock
        self.commit, self.updater = commit, updater
        self.tried: str | None = None       # the release an update was last tried for
        self.update_why = ""                # why that update did not happen
        self.leaving: str | None = None     # the release this copy was replaced with: finish, then stop
        self.unsaid: list[dict] = []
        self.running: list[threading.Thread] = []
        self.wanted: list[str] = []
        self._lock = threading.Lock()
        self._ready = ("check_failed", "not checked yet")
        self._ready_at = None
        self._fed_at = None
        self.feed_now = True
        self.last_claim = None

    def ready(self) -> tuple[str, str]:
        now = self.clock()
        every = READY_SECONDS if self._ready[0] == "ready" else NOT_READY_SECONDS
        if self._ready_at is None or now - self._ready_at >= every:
            self._ready, self._ready_at = self.hermes.ready(), now
        return self._ready

    def _report(self) -> None:
        with self._lock:
            pending, self.unsaid = self.unsaid, []
        keep = []
        for r in pending:
            if "op_id" in r:
                # A rename, hide or delete: the core changes nothing for one it already has an answer to.
                status, _ = self.core.post("session_done", {"id": r["op_id"], "outcome": r["outcome"],
                                                            "reason": r["reason"]})
                if status in (200, 404):
                    self.say(f"{'PASS' if r['outcome'] == 'done' else 'NOTE'} chat {r['action']}: {r['outcome']}")
                else:
                    keep.append(r)
                continue
            if "version" in r:
                # A reaction: the core drops an answer about an older wish by itself.
                status, _ = self.core.post("reacted", r)
                if status in (200, 404):
                    self.say(f"{'PASS' if r['outcome'] == 'set' else 'NOTE'} reaction: {r['outcome']}")
                else:
                    keep.append(r)
                continue
            status, body = self.core.post("result", r)
            if status == 200 or (status == 409 and body.get("error") == "not_leased"):
                self.say(f"{'PASS' if r['outcome'] == 'confirmed' else 'NOTE'} {r['id']}: {r['outcome']}")
            else:
                keep.append(r)
        with self._lock:
            self.unsaid = keep + self.unsaid

    def _deliver(self, item: dict) -> None:
        sid = item.get("session_id")
        try:
            # No silent new chats: a chat Hermes no longer has is refused here, and nothing starts in its place.
            gone = bool(sid) and SESSION_ID.match(sid) is not None and not chat_exists(self.state_db, sid)
        except (sqlite3.Error, OSError):
            gone = False  # Hermes's own resume refuses a missing chat too
        if gone:
            with self._lock:
                self.unsaid.append({"id": item["id"], "outcome": "failed", "ref": "", "reason": "that chat is gone"})
                self.feed_now = True
            return
        try:
            outcome, ref, reason, sid = self.hermes.turn(item.get("session_id"), item["text"])
        except Exception as exc:  # whatever it was, the message may have gone: say so, never resend
            outcome, ref, reason, sid = ("uncertain", "", f"the connector hit {type(exc).__name__} during the turn",
                                         item.get("session_id"))
        r = {"id": item["id"], "outcome": outcome, "ref": ref, "reason": reason}
        if not item.get("session_id") and sid:
            r["session_id"] = sid
        with self._lock:
            self.unsaid.append(r)
            self.feed_now = True

    def react(self, wanted: list) -> int:
        """The reactions the core handed over, each set in Hermes one after another (each is one quick
        write), and what came of each told to the core with the next round. Returns how many were set."""
        done = tried = 0
        for r in wanted[:10]:
            if not (isinstance(r, dict) and isinstance(r.get("session_id"), str) and SESSION_ID.match(r["session_id"])
                    and type(r.get("message_id")) is int and type(r.get("version")) is int
                    and (r.get("emoji") is None or isinstance(r.get("emoji"), str))):
                continue
            outcome, reason = self.hermes.react(r["session_id"], r["message_id"], r.get("emoji"))
            done += outcome == "set"
            tried += 1
            with self._lock:
                self.unsaid.append({"session_id": r["session_id"], "message_id": r["message_id"],
                                    "version": r["version"], "outcome": outcome, "reason": reason})
        if tried:
            self._report()
        return done

    def sessions(self, wanted: list) -> int:
        """The renames, hides and deletes the core handed over, done one after another, and what came of
        each told to the core. A delete that cannot be done says Hermes's reason. Returns how many were done."""
        done = tried = 0
        for r in wanted[:5]:
            if not (isinstance(r, dict) and isinstance(r.get("id"), str)
                    and r.get("op") in ("rename", "archive", "unarchive", "delete")
                    and isinstance(r.get("session_id"), str) and SESSION_ID.match(r["session_id"])
                    and (r.get("title") is None or isinstance(r.get("title"), str))):
                continue
            outcome, reason = self.hermes.session(r["op"], r["session_id"], r.get("title"))
            done += outcome == "done"
            tried += 1
            if outcome == "done":
                self.feed_now = True  # the chat list shows the new title, or loses the chat, at once
            with self._lock:
                self.unsaid.append({"op_id": r["id"], "action": r["op"], "outcome": outcome, "reason": reason})
        if tried:
            self._report()
        return done

    def pages(self, wanted: list) -> int:
        """The pages the core handed over: each read from state.db, read only, and posted. Returns how many."""
        sent = 0
        for req in wanted[:3]:
            if not (isinstance(req, dict) and req.get("kind") in PAGE_KINDS and isinstance(req.get("id"), str)):
                continue
            if req["kind"] == "messages" and not (isinstance(req.get("chat"), str) and SESSION_ID.match(req["chat"])):
                continue
            try:
                body = fitted_page(self.state_db, req)
            except (sqlite3.Error, OSError, ValueError) as exc:
                self.say(f"FAIL page: could not read Hermes's chats ({type(exc).__name__})")
                continue
            status, _ = self.core.post("page", body)
            if status == 200:
                sent += 1
            elif status not in (409, 423):
                self.say(f"FAIL page: the core answered {status}")
        return sent

    def feed(self) -> int:
        try:
            body = fitted(self.state_db, self.wanted, datetime.now(timezone.utc))
        except (sqlite3.Error, OSError) as exc:
            self.say(f"FAIL feed: could not read Hermes's chats ({type(exc).__name__})")
            return 0
        status, out = self.core.post("feed", body)
        if status == 200:
            self.wanted = [w for w in out.get("wanted", []) if isinstance(w, str)]
        elif status not in (423,):
            self.say(f"FAIL feed: the core answered {status}")
        return status

    def busy(self) -> dict:
        """What it is doing, for the phone's words about a message that waits: how many turns are running,
        of how many at once, since when the oldest runs, its commit, and an update it is on or could not do."""
        now = self.clock()
        started = [getattr(t, "started_at", now) for t in self.running]
        return {"commit": self.commit, "busy": len(self.running), "parallel": PARALLEL,
                "busy_for": int(now - min(started)) if started else 0,
                "updating": self.leaving, "update_why": self.update_why}

    def _update(self, release) -> None:
        if (self.updater is None or self.leaving or not isinstance(release, str) or not RELEASE.match(release)
                or release == self.commit or release == self.tried):
            return
        self.tried = release
        done, why = self.updater.to(release)
        if done:
            self.leaving, self.update_why = release, ""
            self.say(f"PASS updated to {release[:12]}; stopping once the running turns end")
        else:
            self.update_why = f"could not update to {release[:12]}: {why}"[:200]
            self.say(f"FAIL {self.update_why}")

    def once(self) -> int:
        """One round. Returns how many turns it started, or -1 when the core said to wait."""
        self._report()
        self.running = [t for t in self.running if t.is_alive()]
        if self.leaving and not self.running:
            raise SystemExit(UPDATE_EXIT)
        state, why = self.ready()
        ready = state == "ready"
        free = 0 if self.leaving else PARALLEL - len(self.running)
        status, body = self.core.post("claim", {"limit": free if ready else 0, "ready": ready, "why": why,
                                                "state": state, **self.busy()})
        self.last_claim = status
        if status == 401:
            raise SystemExit("STOP: the core refused this connector's token")
        if status == 423:
            return -1
        if status != 200:
            return 0
        self._update(body.get("release"))
        wanted = [w for w in body.get("wanted", []) if isinstance(w, str)]
        if wanted != self.wanted:
            self.wanted, self.feed_now = wanted, True
        pages = body.get("pages", [])
        if isinstance(pages, list) and pages:
            self.pages(pages)
        reacts = body.get("reactions", [])
        if isinstance(reacts, list):
            self.react(reacts)
        ops = body.get("sessions", [])
        if isinstance(ops, list):
            self.sessions(ops)
        items = body.get("items", [])
        for item in items:
            t = threading.Thread(target=self._deliver, args=(item,), daemon=True)
            t.started_at = self.clock()
            t.start()
            self.running.append(t)
        now = self.clock()
        if self.feed_now or self._fed_at is None or now - self._fed_at >= FEED_SECONDS:
            self.feed_now = False
            self._fed_at = now
            self.feed()
        return len(items)

    def forever(self) -> None:
        while True:
            n = self.once()
            time.sleep(POLL_SECONDS * (6 if n < 0 else 1))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="own_connector")
    ap.add_argument("--gateway", required=True, choices=sorted(GATEWAYS))
    ap.add_argument("--hermes", required=True, help="the hermes command this user runs")
    ap.add_argument("--hermes-python", help="a Python with Hermes's packages, in place of asking the hermes command")
    ap.add_argument("--hermes-home", help="Hermes's state folder for this user (default: as Hermes finds it)")
    ap.add_argument("--token", help="this connector's token file")
    ap.add_argument("--core", default=LOCAL_CORE, help="the core's API: loopback on the VM, the VM's ts.net on One")
    ap.add_argument("--once", action="store_true", help="one round, then stop")
    ap.add_argument("--clone", help="One: Patrick's clone of Thornswood-Hermes, which this copy updates itself from")
    ap.add_argument("--git", default="git", help="the git command for --clone")
    ap.add_argument("--supervise", action="store_true",
                    help="One's task: run the connector in its own process and start it again after an update")
    args = ap.parse_args(argv)
    if args.supervise:
        return supervise(argv)
    if not (args.core.startswith("https://") or args.core.startswith("http://127.0.0.1:")):
        raise SystemExit("STOP: the core is reached over https, or on 127.0.0.1 on the VM itself")
    if args.token:
        token = Path(args.token)
    elif WINDOWS:
        token = Path(os.environ["APPDATA"]) / "Thornswood" / f"own-{args.gateway}.token"
    else:
        token = Path.home() / ".config" / "thornswood" / f"own-{args.gateway}.token"
    home = Path(args.hermes_home) if args.hermes_home else hermes_home()
    hermes = Hermes([args.hermes], helper_command(args.hermes, python=args.hermes_python), Path.home(),
                    GATEWAYS[args.gateway])
    here = Path(__file__).resolve().parent
    updater = Updater(Path(args.clone), args.git, here) if args.clone else None
    conn = OwnConnector(Core(args.core, args.gateway, read_token(token)), hermes, home / "state.db",
                        commit=own_commit(here), updater=updater)
    if args.once:
        n = conn.once()
        for t in conn.running:
            t.join()
        conn._report()
        if conn.last_claim not in (200, 423):
            print(f"FAIL the core at {args.core} answered {conn.last_claim or 'nothing'}")
            return 1
        state, why = conn._ready
        said = {"gateway_down": f" (its gateway is not running: {why})",
                "check_failed": f" (its readiness check failed: {why})"}
        print(("WAIT own_chat_write is off" if n < 0 else f"PASS {n} handed to {GATEWAYS[args.gateway]}")
              + said.get(state, ""))
        return 1 if conn.unsaid else 0
    conn.forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
