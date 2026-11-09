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

   through room_send.py, which runs that same `hermes chat` in Hermes's own
   runtime and hands a message to a chat whose turn is already running the way
   Desktop's box does, by the chat's busy-input setting (steer, interrupt or
   queue; Patrick's answer A on q-one-busy-chat, 2 Nov; contracts.json "busy");

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
5. every 20 seconds, in a thread of its own, sends what this Hermes has going
   besides its chats, for the app's Tasks tab (`POST /own/<gateway>/tasks`):
   its subagents, background commands and scheduled jobs, read by room_send.py
   op `tasks` the way Hermes Desktop reads them (contracts.json "tasks").

While own_chat_write is off the core answers 423 to all of it; this waits and
copies nothing. It logs no message text, no chat title and no token.
"""

from __future__ import annotations

import argparse
import hashlib
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
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

LOCAL_CORE = "http://127.0.0.1:9135/api/v1"
GATEWAYS = {"me": "my-hermes", "one": "One's Hermes"}
POLL_SECONDS = 5
TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
FEED_SECONDS = 20
TASKS_SECONDS = 20      # how often its subagents, background commands and scheduled jobs go to the core
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


# The helper's quick ops (readiness, reactions, chats) only read Hermes's state, so they run with Hermes's own
# switch that keeps a start of Hermes from installing anything or finishing a source update first
# (HERMES_DISABLE_LAZY_INSTALLS: hermes_cli/venv_sync.py:343 prepare_launch, :374; pm/install.py
# lazy_installs_allowed), the way Hermes's own probes run (doctor_platform.py:541, "so the probe never mutates
# the environment it is diagnosing"; cron's scripts; PM's worker). The bootstrap the helper runs in
# (hermes_bootstrap.py, `hermes --print-runtime-command`) otherwise finishes an owed source update before
# anything else: its dependency sync, launchers and product builds, which take minutes (venv_sync.py:371,
# why Hermes's own `gateway status` is spared it). A check started then would be stopped at its minute,
# every minute, mid-update, and say "the readiness check did not answer in a minute" (One's bar, 2 Nov
# 5:06 PM). The update is finished by Hermes's own next start or `hermes update`, never by a check. A turn
# (`hermes chat`) is not a check and starts as Hermes does.
PROBE_ENV = {"HERMES_DISABLE_LAZY_INSTALLS": "1"}
CHECK_SECONDS = 60  # how long a quick op of the helper may take


def probe_env() -> dict:
    return {**os.environ, **PROBE_ENV}


def printed(data: bytes | None, n: int = 120) -> str:
    """The tail of what a child printed, on one line."""
    text = " ".join((data or b"").decode("utf-8", "replace").split())
    return text[-n:] if text else "nothing"


# The core's own rule for a chat id (own.py), checked again before it goes after --resume.
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


LOG = "connector.log"     # One's record, beside this file, for a connector that has no stdout
LOG_BYTES = 1_000_000    # kept to about this size, with the one before it as connector.log.1


def say(line: str) -> None:
    """One line to the journal now. Under pythonw (One's task, with no window) there is no stdout, so the line
    goes to connector.log beside this file, with the time: on 7 Nov One's connector stopped at 7:17 PM and
    nothing on One said why."""
    if sys.stdout is not None:
        print(line, flush=True)
        return
    path = Path(__file__).resolve().parent / LOG
    if path.is_file() and path.stat().st_size > LOG_BYTES:
        os.replace(path, path.with_name(LOG + ".1"))
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now().astimezone().isoformat(timespec='seconds')} {line}\n")


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


def helper_command(hermes_bin: str, helper: Path = HELPER, python: str | None = None,
                   root: str | None = None) -> list[str]:
    """How to run the helper (room_send.py) inside this Hermes's own runtime: its real Python, with Hermes's
    source on the path.

    Hermes's launcher says how it starts its runtime (`hermes --print-runtime-command`): a Python kept in
    Hermes's own store, started with a bootstrap that puts Hermes's source folder on its path (on One, and
    on the VM since its launcher became a /bin/sh wrapper on 30 Oct). The helper runs in that same
    bootstrap, with the helper's file in place of Hermes's own entry. A launcher too old to answer is
    followed by hand (hermes_runtime): a venv's hermes.exe or #! script, or a /bin/sh wrapper that execs
    Hermes's Python. A given --hermes-python (and --hermes-root) is used as it is, never a shell."""
    if python:
        if is_shell(python):
            raise SystemExit(f"STOP: --hermes-python {python} is a shell, not a Python; give Hermes's own Python")
        return _direct(python, root, helper)
    try:
        out = subprocess.run([hermes_bin, "--print-runtime-command"], capture_output=True, timeout=60, **quiet(),
                             check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"STOP: {hermes_bin} --print-runtime-command did not run ({type(exc).__name__})")
    if out.returncode != 0:
        exe, found_root = hermes_runtime(hermes_bin)
        return _direct(exe, root or found_root, helper)
    try:
        cmd = json.loads(out.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        cmd = None
    if (not isinstance(cmd, list) or len(cmd) != 4 or not all(isinstance(c, str) for c in cmd)
            or cmd[1:3] != ["-I", "-c"] or not cmd[3].endswith(RUNTIME_ENTRY)
            or not os.path.isabs(cmd[0]) or not os.path.isfile(cmd[0]) or is_shell(cmd[0])):
        raise SystemExit(f"STOP: {hermes_bin} --print-runtime-command answered a command this connector does "
                         "not know; check contracts.json own_chat against this Hermes")
    run_helper = f"runpy.run_path({json.dumps(str(helper))}, run_name='__main__')"
    return [cmd[0], "-I", "-c", cmd[3][:-len(RUNTIME_ENTRY)] + run_helper]


def _direct(python: str, root: str | None, helper: Path) -> list[str]:
    """The helper run straight with a Python; with Hermes's source folder, when Hermes is not installed
    into that Python, given to the helper, which puts it first on its path (room_send.py --hermes-root)."""
    return [python, "-I", str(helper), *(["--hermes-root", root] if root else [])]


# The shells a launcher may be written for. A shell is never Hermes's Python: run as `<shell> -I
# room_send.py` it fails at the first parenthesis (30 Oct to 2 Nov, every readiness check of the Bots'
# connector, read as "could not check the Bots' gateway").
SHELLS = {"sh", "bash", "dash", "ash", "ksh", "mksh", "zsh", "busybox", "fish", "csh", "tcsh"}
_PYTHON = re.compile(r"^(python|pypy)(\d+(\.\d+)*)?(\.exe)?$", re.I)
_EXEC = re.compile(r"^[ \t]*exec[ \t]+(?=\S)", re.M)
_ROOT = re.compile(r"""sys\.path\.insert\(\s*0\s*,\s*(['"])([^'"]+)\1\s*\)""")


def is_shell(exe: str) -> bool:
    name = re.split(r"[\\/]", exe)[-1].lower()
    return name.removesuffix(".exe") in SHELLS


def hermes_python(hermes_bin: str) -> str:
    """The real Python an older `hermes` command runs with (hermes_runtime), never a shell."""
    return hermes_runtime(hermes_bin)[0]


def hermes_runtime(hermes_bin: str, _depth: int = 0) -> tuple[str, str | None]:
    """(Python, Hermes's source folder or None) the `hermes` command runs Hermes with, read from the files.

    python.exe beside a venv's hermes.exe; a #! script's interpreter (Hermes installed into it, so no
    folder); or, for a #! shell wrapper, the command its `exec` line runs: another wrapper is followed in
    turn, and a Python's bootstrap names the folder it puts first on the path
    (`sys.path.insert(0, '<folder>')`, the shape Hermes's launcher writes). A shell is never the answer."""
    path = Path(hermes_bin)
    if path.suffix.lower() == ".exe":
        py = path.with_name("python.exe")
        if not py.exists():
            raise SystemExit(f"STOP: no python.exe beside {hermes_bin}; pass --hermes-python")
        return str(py), None
    text = path.read_bytes()[:256 * 1024].decode("utf-8", "replace")
    first = text.split("\n", 1)[0]
    if not first.startswith("#!"):
        raise SystemExit(f"STOP: {hermes_bin} does not start with #!; pass --hermes-python and --hermes-root")
    exe = first[2:].strip().split()[0] if first[2:].strip() else ""
    if not exe.startswith("/") or exe.rsplit("/", 1)[-1] == "env":
        raise SystemExit(f"STOP: {hermes_bin} does not name its Python by path; pass --hermes-python")
    if not is_shell(exe):
        return exe, None
    if _depth >= 5:
        raise SystemExit(f"STOP: {hermes_bin} is one shell wrapper too many deep; pass --hermes-python and "
                         "--hermes-root")
    import shlex
    m = _EXEC.search(text)
    try:
        argv = shlex.split(text[m.end():], comments=False, posix=True) if m else []
    except ValueError:
        argv = []
    target = argv[0] if argv else ""
    if not target.startswith("/"):
        raise SystemExit(f"STOP: {hermes_bin} is a {exe} script, not a Python, and this connector cannot "
                         "follow its exec line; pass --hermes-python and --hermes-root")
    name = target.rsplit("/", 1)[-1]
    if _PYTHON.match(name):
        found = next((_ROOT.search(a) for a in argv[1:] if _ROOT.search(a)), None)
        return target, (found.group(2) if found else None)
    if is_shell(target):
        raise SystemExit(f"STOP: {hermes_bin} runs a shell ({target}), not Hermes's Python; pass --hermes-python "
                         "and --hermes-root")
    return hermes_runtime(target, _depth + 1)


def _iso(epoch) -> str:
    if epoch is None:
        return ""
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat()


def _text(content) -> str:
    """A message's content as plain text: a string, or the text parts of a list of parts, with a skill's
    loaded instructions shown as Patrick typed them (as_typed)."""
    if isinstance(content, str):
        try:
            parts = json.loads(content) if content.startswith("[") else None
        except json.JSONDecodeError:
            parts = None
        if not isinstance(parts, list):
            return as_typed(content)
        content = parts
    if isinstance(content, list):
        return as_typed("\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"))
    return ""


# A /skill message, as Hermes keeps it, opens with the skill's whole text ahead of what Patrick typed. Hermes
# shows it as typed in its own titles, previews and transcripts (agent/skill_commands.py
# describe_skill_invocation with separator " ", at the pinned commit); untitled,
# the phone named a chat by the raw text instead (Patrick's screenshot, 2 Nov 3:13 PM: "[IMPORTANT: The user
# has invoked the ..."). The markers are Hermes's own, byte for byte (skill_commands.py:49-81 and :336-338);
# test_decisions_lines.py SkillMessages holds the answers Hermes's own describer gave on the same messages.
_SKILL_PREFIX = "[IMPORTANT: The user has invoked the "
_SKILL_NAME = re.compile(re.escape(_SKILL_PREFIX) + r'"([^"]*)"')
_SKILL_MARKER = "The full skill content is loaded below.]"
_SKILL_INSTRUCTION = "The user has provided the following instruction alongside the skill invocation: "
_SKILL_RUNTIME_NOTE = "\n\n[Runtime note:"
_BUNDLE_MARKER = " skill bundle,"
_BUNDLE_INSTRUCTION = "\nUser instruction: "
_BUNDLE_FIRST_BLOCK = "\n\n[Loaded as part of the "
_AUTO_PREFIX = '[IMPORTANT: The "'
_AUTO_DIR_NOTE_END = "then run them with the terminal tool using the absolute path."
_AUTO_TRAILING = (("[Skill config (", "\n]"), ("[Skill setup note: ", "]\n\n"),
                  ("[This skill has supporting files", "/scripts/foo.js`)."))
_EXCERPT_JOINT = "\x1e"


def _cut_after(text: str, marker: str, stop: str, find) -> str | None:
    at = find(marker)
    return (text[at + len(marker):].split(stop, 1)[0].strip() or None) if at >= 0 else None


def as_typed(text: str) -> str:
    """A /skill message as Patrick typed it ("/work fix the title"), or the text unchanged."""
    if text.startswith(_AUTO_PREFIX):
        name = (re.match(re.escape(_AUTO_PREFIX) + r'([^"]*)"', text) or [None, ""])[1]
        end = text.rfind(_AUTO_DIR_NOTE_END)
        if end == -1:
            return f"/{name}" if name else text
        tail = text[end + len(_AUTO_DIR_NOTE_END):].lstrip()
        while stop := next((e for b, e in _AUTO_TRAILING if tail.startswith(b)), None):
            cut = tail.find(stop)
            tail = tail[cut + len(stop):].lstrip() if cut >= 0 else ""
        return " ".join(tail.split()) or (f"/{name}" if name else text)
    if not text.startswith(_SKILL_PREFIX):
        return text
    m = _SKILL_NAME.match(text)
    name = (m.group(1) if m else "").strip()
    label = name if name.startswith("/") else f"/{name}"
    if _BUNDLE_MARKER in text:
        said = _cut_after(text, _BUNDLE_INSTRUCTION, _BUNDLE_FIRST_BLOCK, text.find)
    elif _SKILL_MARKER in text:
        said = _cut_after(text, _SKILL_INSTRUCTION, _SKILL_RUNTIME_NOTE, text.rfind)
    else:
        said = None
    if said:
        said = " ".join(said.split(_EXCERPT_JOINT)[0].split())
    if said:
        return f"{label} {said}" if name else said
    return label if name else text


# What Patrick wrote, apart from what Hermes attached to it: Hermes keeps an @file: reference where it was
# typed and puts the file's text under "--- Attached Context ---" (agent/context_references.py); a long
# paste in Hermes Desktop is such a file, composer-pastes/pasted_content_<stamp>.txt. A list line shows the
# words, or "Pasted text" or the file's name, never the reference or the attached text (Hermes Desktop's
# own rule, chat-messages/hydration.ts). The phone shows the whole message the same way (app.js authored).
_ATTACHED = re.compile(r"(?:^|\n)--- Attached Context ---[ \t]*\n")
_WARNINGS = re.compile(r"(?:^|\n)--- Context Warnings ---.*\Z", re.S)
_REF = re.compile(r"""@(file|folder|url|image|tool|terminal):("[^"\n]+"|'[^'\n]+'|`[^`\n]+`|\S+)""")
_PASTE = re.compile(r"(?:^|[\\/])pasted_content_[\w-]+\.txt$")


def _ref_label(kind: str, target: str) -> str:
    target = target[1:-1] if target[:1] in "\"'`" and target[-1:] == target[:1] else target
    if kind == "file" and _PASTE.search(target):
        return "Pasted text"
    return [p for p in re.split(r"[\\/]", target) if p][-1] if target.strip("\\/") else target


def line_of(text: str) -> str:
    """One line for a list: what was written, else what was attached."""
    cut = _ATTACHED.search(text)
    words = _WARNINGS.sub("", text[:cut.start()] if cut else text)
    labels = [_ref_label(m.group(1), m.group(2)) for m in _REF.finditer(words)]
    words = _REF.sub("", words)
    # Its line breaks stay: the phone reads Markdown's marks (a list item, a heading) at the start of a
    # line, so it can show plain words. Flattened first, "- **Why:**" showed as it is (2 Nov).
    line = "\n".join(" ".join(part.split()) for part in words.splitlines() if part.strip())
    return line or ", ".join(dict.fromkeys(labels))


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
# A list line comes from the newest message that has words: a turn whose newest row is a tool call or
# an image alone once left his Hermes's Bot Chat with an empty line (2 Nov).
LINE_LOOK = 20
FIRST_SQL = ("SELECT content FROM messages WHERE session_id = ? AND active = 1 AND role = 'user' "
             "AND content IS NOT NULL AND content != '' ORDER BY id LIMIT ?")
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
        chats = [bot] if bot else []
        chats += [_row(con, r) for r in rows[:CHATS - len(chats)]]
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
    # Its line comes from its whole lineage: the chat it goes on as now may hold no message with words yet.
    lineage = [r["id"] for r in con.execute(LINEAGE_SQL + "SELECT id FROM ancestors UNION SELECT id FROM descendants",
                                            (row["id"], row["id"])).fetchall()]
    return {"id": tip["id"], "title": BOT_CHAT, "last_at": _iso(tip["last_at"]), "preview": _line(con, lineage),
            "bot_chat": True}


def _line(con: sqlite3.Connection, ids: list[str]) -> str:
    """A chat's list line: the newest of its last LINE_LOOK messages that has words (line_of)."""
    marks = ",".join("?" * len(ids))
    for m in con.execute(MESSAGES_SQL.replace("session_id = ?", f"session_id IN ({marks})"), (*ids, LINE_LOOK)):
        line = line_of(_text(m["content"]))
        if line:
            return line[:200]
    return ""


def _row(con: sqlite3.Connection, r: sqlite3.Row) -> dict:
    """One chat of the list. A chat Hermes has not titled is named by its first message, as Hermes Desktop
    names it (chat-runtime.ts sessionTitle: the title, else the preview, the first message Patrick wrote);
    `first` goes only with an untitled chat, so a rename still starts from Hermes's own title."""
    row = {"id": r["id"], "title": r["title"] or "", "last_at": _iso(r["last_at"]), "preview": _line(con, [r["id"]])}
    if not row["title"]:
        for m in con.execute(FIRST_SQL, (r["id"], 5)):
            first = line_of(_text(m["content"]))
            if first:
                row["first"] = first[:120]
                break
    return row


def chat_exists(state_db: Path, session_id: str) -> bool:
    con = _ro(state_db)
    try:
        return con.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone() is not None
    finally:
        con.close()


def _fork_child(row) -> bool:
    """Hermes's _is_explicit_fork_child_row with include_reset (hermes_state_messages.py:1878): a branch,
    delegate, tool or reset child of its parent is a conversation of its own."""
    if row["source"] == "tool":
        return True
    try:
        cfg = json.loads(row["model_config"]) if isinstance(row["model_config"], str) else row["model_config"]
    except json.JSONDecodeError:
        return False
    if not isinstance(cfg, dict):
        return False
    markers = (cfg.get("_branched_from"), cfg.get("_delegate_from"), cfg.get("_reset_from"))
    parent = row["parent_session_id"]
    return parent in markers if parent else any(m is not None for m in markers)


def turn_held(state_db: Path, session_id: str, since: float) -> bool:
    """Whether another Hermes process holds this chat's turn, taken before `since` (when this connector's
    own turn started), so the turn waits for it. Hermes runs one turn at a time in a conversation: a new
    one waits for the turn lease (agent/turn_facade_lease.py:297, up to 30 minutes), keyed by the chat's
    first compression ancestor (hermes_state_compression.py:683, mirrored here) in session_turn_leases.
    Read only; any error is "not known to wait", never a guess."""
    try:
        con = _ro(state_db)
    except (sqlite3.Error, OSError):
        return False
    try:
        def row(sid):
            return con.execute("SELECT id, parent_session_id, source, model_config, end_reason FROM sessions "
                               "WHERE id = ?", (sid,)).fetchone()
        current, seen = row(session_id), {session_id}
        while current:
            parent_id = current["parent_session_id"]
            if not parent_id or parent_id in seen or _fork_child(current):
                break
            parent = row(parent_id)
            if not parent or parent["end_reason"] != "compression":
                break
            seen.add(parent_id)
            current = parent
        key = current["id"] if current else session_id
        held = con.execute("SELECT acquired_at, expires_at FROM session_turn_leases WHERE conversation_id = ?",
                           (key,)).fetchone()
        return bool(held) and held["acquired_at"] < since and held["expires_at"] > time.time()
    except sqlite3.Error:
        return False
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
        chats += [_row(con, r) for r in rows[:n - len(chats)]]
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

# How room_send.py's turn says Hermes took a message, and the live line's state for it.
ADMISSIONS = {"started": "thinking", "queued": "queued", "steered": "steered", "redirected": "redirected"}


def follow_turn(stream, seen: dict, tell) -> None:
    """Hermes's stream-json events of one turn, read as they come (hermes_cli/stream_json.py). The start and
    the end are kept in `seen` without their text (the session id and the exit code are all that is read
    from them), and `tell(state, tool)` hears each change of what the turn is doing, once: "thinking" (the
    start, and after each tool's result), "writing" (the reply's text), or "tool" with the tool's name
    when it is a plain name (else None). Never the words, a tool's input or its output. The Bots'
    connector reads its Bot Chat turns with this too (connector.py).

    room_send.py's turn adds one line saying how Hermes took the message ({"type": "admission"}): "queued" (behind
    the turn already running, in Hermes's own queue), "steered" or "redirected" (into the running turn), or
    "started" (also said when a queued one leaves the queue to run). From then on `seen["admitted"]` is true:
    Hermes has the message; `seen["taken"]` keeps a steer or redirect for the rest of the turn."""
    now = None
    for line in stream:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        kind = ev.get("type")
        seen["events"] = seen.get("events", 0) + 1
        if kind in ("system", "result"):
            ev.pop("text", None)
            seen[kind] = ev
        state = {"system": ("thinking", None), "tool_result": ("thinking", None),
                 "text": ("writing", None)}.get(kind)
        if kind == "admission" and ev.get("status") in ADMISSIONS:
            seen["admitted"] = True
            if ev["status"] in ("steered", "redirected"):
                seen["taken"] = ev["status"]
            state = (ADMISSIONS[ev["status"]], None)
        if kind == "tool_use":
            name = ev.get("name")
            state = ("tool", name if isinstance(name, str) and TOOL_NAME.match(name) else None)
        if state and state != now:
            now = state
            tell(*state)


class Hermes:
    def __init__(self, hermes_cmd: list[str], ready_cmd: list[str], home_dir: Path, name: str,
                 state_db: Path | None = None, turn_cmd: list[str] | None = None):
        self.cmd, self.ready_cmd, self.home_dir, self.name = list(hermes_cmd), list(ready_cmd), home_dir, name
        # room_send.py in Hermes's runtime (helper_command), which takes `hermes chat`'s own arguments.
        self.turn_cmd = list(turn_cmd) if turn_cmd else list(hermes_cmd)
        self.state_db = state_db  # Hermes's own state.db, read only, to see a turn wait for the chat's turn

    def ready(self) -> tuple[str, str]:
        """(state, why): is this Hermes's gateway running? Asked without starting one.

        "ready", "gateway_down" (the check ran and found no gateway running) or "check_failed"
        (the check itself could not run), which the phone tells apart (plan Part 1)."""
        req = json.dumps({"op": "ready", "profile": "", "name": self.name}).encode("utf-8")
        try:
            out = subprocess.run(self.ready_cmd, input=req, capture_output=True, **quiet(),
                                 timeout=CHECK_SECONDS, cwd=str(self.home_dir), check=False, env=probe_env())
        except subprocess.TimeoutExpired as exc:
            return "check_failed", f"the readiness check did not answer in a minute; it printed: {printed(exc.stderr)}"[:200]
        except OSError:
            return "check_failed", "could not run the readiness check with Hermes's Python"
        try:
            got = json.loads(out.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            got = None
        if not isinstance(got, dict):
            # The helper itself failed (a shell for a Python, Hermes not on its path): never "gateway down".
            return "check_failed", f"the readiness helper failed (exit {out.returncode}): {printed(out.stderr, 160)}"[:200]
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
                                 timeout=CHECK_SECONDS, cwd=str(self.home_dir), check=False, env=probe_env())
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
                                 timeout=CHECK_SECONDS, cwd=str(self.home_dir), check=False, env=probe_env())
            got = json.loads(out.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
        except subprocess.TimeoutExpired:
            return "failed", "Hermes's store did not answer in a minute"
        except (OSError, IndexError, json.JSONDecodeError):
            return "failed", "could not run the helper with Hermes's Python"
        if isinstance(got, dict) and got.get("done") is True:
            return "done", ""
        reason = got.get("reason") if isinstance(got, dict) else None
        return "failed", str(reason or "not done")[:200]

    def tasks(self) -> dict:
        """This Hermes's subagents, background commands and scheduled jobs (room_send.py op `tasks`), as the
        helper read them; {"why": ...} when the helper itself could not say."""
        req = json.dumps({"op": "tasks", "profiles": [""]}).encode("utf-8")
        try:
            out = subprocess.run(self.ready_cmd, input=req, capture_output=True, **quiet(),
                                 timeout=CHECK_SECONDS, cwd=str(self.home_dir), check=False, env=probe_env())
        except subprocess.TimeoutExpired as exc:
            return {"why": f"the tasks helper did not answer in a minute; it printed: {printed(exc.stderr)}"[:200]}
        except OSError:
            return {"why": "could not run the tasks helper with Hermes's Python"}
        try:
            got = json.loads(out.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            got = None
        mine = got.get("tasks", {}).get("") if isinstance(got, dict) and isinstance(got.get("tasks"), dict) else None
        if not isinstance(mine, dict):
            return {"why": f"the tasks helper failed (exit {out.returncode}): {printed(out.stderr, 160)}"[:200]}
        return mine

    def control(self, session_id: str, want: dict | None) -> tuple[dict | None, bool, str]:
        """(what Hermes holds for the chat, whether a change was made, why not): one chat's model, thinking
        level, approvals and busy setting, and the models one of his sign-ins, a free model or a local one
        covers, through the same helper and Python as the readiness check (room_send.py "control"). With
        `want`, the helper changes that chat's model or thinking level first, with Hermes's own calls."""
        req = json.dumps({"op": "control", "profile": "", "name": self.name, "session_id": session_id,
                          **({"set": want} if want else {})}).encode("utf-8")
        try:
            out = subprocess.run(self.ready_cmd, input=req, capture_output=True, **quiet(),
                                 timeout=90, cwd=str(self.home_dir), check=False, env=probe_env())
            got = json.loads(out.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
        except subprocess.TimeoutExpired:
            return None, False, "Hermes did not answer in 90 seconds"
        except (OSError, IndexError, json.JSONDecodeError):
            return None, False, "could not run the helper with Hermes's Python"
        if not isinstance(got, dict):
            return None, False, "the helper said nothing"
        state = got.get("state") if isinstance(got.get("state"), dict) else None
        return state, got.get("applied") is True, str(got.get("reason") or "")[:200]

    def turn(self, session_id: str | None, text: str, doing=None, image: Path | None = None) -> tuple[str, str, str, str | None]:
        """(outcome, ref, reason, session id) for one message, with the photo Patrick attached, if any.
        `doing(state, tool)` hears what the turn is doing as Hermes's stream says it: thinking, writing, or a
        tool by its name."""
        if session_id is not None and not SESSION_ID.match(session_id):
            return "failed", "", "that is not a chat id Hermes gives", None
        with tempfile.TemporaryDirectory(prefix="tw-own-") as folder:
            if not WINDOWS:
                os.chmod(folder, 0o700)
            path = Path(folder) / "message.txt"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            argv = [*self.turn_cmd, "chat", "-Q", "--format", "stream-json", "--query-file", str(path)]
            if image is not None:
                argv += ["--image", str(image)]
            if session_id:
                argv += ["--resume", session_id]
            else:
                argv += ["--yolo", "--source", "cli", "--in", str(self.home_dir)]
            return self._run(argv, session_id, doing)

    def _run(self, argv: list[str], session_id: str | None, doing=None) -> tuple[str, str, str, str | None]:
        seen: dict = {}
        tell = doing or (lambda state, tool, admitted=False, taken=None: None)
        began = time.time()
        moved = threading.Event()  # the turn said more than its start: it has the chat's turn

        def heard(state, tool):
            if seen.get("events", 0) > 1:
                moved.set()
            tell(state, tool, bool(seen.get("admitted")), seen.get("taken"))

        def read(stream):
            follow_turn(stream, seen, heard)

        def held():
            # While the turn has said nothing past its start, it may be waiting for the chat's turn lease,
            # held by another Hermes process running a turn in the same chat: then it is in a queue too.
            waiting = False
            while not moved.wait(2) and proc.poll() is None:
                now = turn_held(self.state_db, session_id, began)
                if now != waiting and not moved.is_set():
                    waiting = now
                    tell("queued" if now else "thinking", None, bool(seen.get("admitted")), seen.get("taken"))

        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, **quiet(),
                                    stderr=subprocess.DEVNULL, cwd=str(self.home_dir), text=True,
                                    encoding="utf-8", errors="replace")
        except OSError:
            return "failed", "", "could not start the hermes command", None
        reader = threading.Thread(target=read, args=(proc.stdout,), daemon=True)
        reader.start()
        if session_id and self.state_db is not None:
            threading.Thread(target=held, daemon=True).start()
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
    TWHermesAgents on One fetches main, the commit has to be on main, and its two files are taken
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

    def _folder(self, release: str) -> str | None:
        """The folder of the release that holds this package, from that release's own
        deploy/layout.json (a release made before the file existed keeps it at its root);
        None when git cannot read it, or it does not name a folder inside the release."""
        listed = self._git("ls-tree", "--name-only", release, "deploy/layout.json")
        if listed.returncode != 0:
            return None
        if not listed.stdout.strip():
            return "thornswood_decisions"
        got = self._git("show", f"{release}:deploy/layout.json")
        if got.returncode != 0:
            return None
        try:
            packages = json.loads(got.stdout)["packages"]
        except (ValueError, KeyError, TypeError):
            return None
        rel = PurePosixPath(packages) if isinstance(packages, str) else None
        if rel is None or rel.is_absolute() or ".." in rel.parts:
            return None
        return str(rel / "thornswood_decisions")

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
            folder = self._folder(release)
            if folder is None:
                return False, f"could not read where {release[:12]} keeps its packages (deploy/layout.json)"
            files = {}
            for name in UPDATE_FILES:
                got = self._git("show", f"{release}:{folder}/{name}")
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
    try:
        job = _job() if WINDOWS and contain else None
        n = 0
        while rounds is None or n < rounds:
            n += 1
            proc = run(child, stdin=subprocess.DEVNULL, **quiet())
            if job is not None:
                _in_job(job, proc)
            code = proc.wait()
            if code == UPDATE_EXIT:
                say("NOTE the connector stopped to load an update; starting the new copy")
            else:
                say(f"NOTE the connector stopped with exit code {code}; starting it again in {RESTART_SECONDS}s")
                sleep(RESTART_SECONDS)
    except BaseException as exc:
        say(f"STOP the supervisor: {_why(exc)}")
        raise
    return 0


def _why(exc: BaseException) -> str:
    """What stopped it, on one line: a SystemExit's own words, else the exception and where it was raised."""
    if isinstance(exc, SystemExit):
        return str(exc.code)
    return " | ".join(" ".join(part.split()) for part in traceback.format_exception(exc)[-3:])


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

    def fetch(self, op: str, body: dict) -> tuple[int, bytes]:
        """An answer that is bytes (a file Patrick attached), not JSON."""
        req = urllib.request.Request(f"{self.base}/own/{self.gateway}/{op}", data=json.dumps(body).encode("utf-8"),
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


# Files and photos Patrick attached on the phone go where Hermes Desktop puts what is dropped or pasted into
# its box: images in Hermes's own images folder, every other file in its attachments folder (the staging
# folders Hermes's file and vision tools are given, tools/credential_files.py _CACHE_DIRS), each message's
# in a folder of its own. The first photo goes to Hermes as the turn's image (`--image`), and each other file
# as an @file: reference in the message, as Desktop attaches a file.
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"}
REF_NEEDS_QUOTES = re.compile(r"""[\s()\[\]{}<>"'`]""")
MESSAGE_ID = re.compile(r"^own_[A-Za-z0-9_-]{4,64}$")
BAD_NAME = re.compile(r'[\\/\x00-\x1f\x7f<>:"|?*]')


def file_ref(path: Path) -> str:
    """A path as Hermes reads an @file: reference back whole (agent/context_references.py quote_value)."""
    value = str(path)
    return "@file:" + (f'"{value}"' if REF_NEEDS_QUOTES.search(value) else value)


def save_attachment(root: Path, message_id: str, f: dict, body: bytes) -> Path:
    """Writes one attached file under root/<images or attachments>/phone/<message id>/<its name>, readable
    by this user only. The name is the core's (own.file_name): one part, no path, nothing Windows refuses."""
    kind = "images" if f.get("mime") in IMAGE_TYPES else "attachments"
    name = str(f.get("name") or "")
    if not MESSAGE_ID.match(message_id) or not name or name in (".", "..") or BAD_NAME.search(name):
        raise ValueError("an attachment's name or message is not one the core gives")
    folder = root / kind / "phone" / message_id
    folder.mkdir(parents=True, exist_ok=True)
    if not WINDOWS:
        os.chmod(folder, 0o700)
    path = folder / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(body)
    return path


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
        self.doing: dict[str, dict] = {}    # message id -> what its turn is doing now (state, tool)
        self.wanted: list[str] = []
        self._lock = threading.Lock()
        self._ready = ("check_failed", "not checked yet")
        self._ready_at = None
        self._fed_at = None
        self.feed_now = True
        self._tasks_at = None
        self._tasks_run: threading.Thread | None = None
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
        def doing(state, tool, admitted=False, taken=None):
            # `admitted`: Hermes has the message (room_send.py's turn said how it took it). The core hands this
            # connector the chat's next message only once each before it is in Hermes, so they keep order.
            # `taken`: steered or redirected into the turn already running, kept while that turn goes on.
            with self._lock:
                self.doing[item["id"]] = {"id": item["id"], "state": state, "tool": tool, "admitted": admitted,
                                          **({"taken": taken} if taken else {})}
        text, image = item["text"], None
        files = item.get("attachments") or []
        if files:
            try:
                paths = self._files(item["id"], files)
            except ValueError as exc:
                # Nothing was handed to Hermes, so the message did not go: Patrick sees why and can send again.
                with self._lock:
                    self.unsaid.append({"id": item["id"], "outcome": "failed", "ref": "", "reason": str(exc)[:200]})
                    self.feed_now = True
                return
            image = next((pth for f, pth in paths if f.get("mime") in IMAGE_TYPES), None)
            refs = [file_ref(pth) for _, pth in paths if pth != image]
            text = "\n\n".join(x for x in (text, " ".join(refs)) if x) or f"[Patrick attached {image.name}]"
        try:
            outcome, ref, reason, sid = self.hermes.turn(item.get("session_id"), text, doing, image)
        except Exception as exc:  # whatever it was, the message may have gone: say so, never resend
            outcome, ref, reason, sid = ("uncertain", "", f"the connector hit {type(exc).__name__} during the turn",
                                         item.get("session_id"))
        finally:
            with self._lock:
                self.doing.pop(item["id"], None)
        r = {"id": item["id"], "outcome": outcome, "ref": ref, "reason": reason}
        if not item.get("session_id") and sid:
            r["session_id"] = sid
        with self._lock:
            self.unsaid.append(r)
            self.feed_now = True

    def _files(self, message_id: str, files: list) -> list[tuple[dict, Path]]:
        """Each attached file, fetched from the core, checked against its size and SHA-256, and saved."""
        root = self.state_db.parent
        out = []
        for f in files[:10]:
            if not isinstance(f, dict) or not isinstance(f.get("id"), str):
                raise ValueError("an attachment the core named is not well formed")
            status, body = self.core.fetch("attachment", {"id": f["id"]})
            if status != 200:
                raise ValueError(f"could not fetch {f.get('name')} from the core (it answered {status or 'nothing'})")
            if len(body) != f.get("size") or hashlib.sha256(body).hexdigest() != f.get("sha256"):
                raise ValueError(f"{f.get('name')} came from the core changed or cut short")
            try:
                out.append((f, save_attachment(root, message_id, f, body)))
            except OSError as exc:
                raise ValueError(f"could not save {f.get('name')} into Hermes's folder ({type(exc).__name__})") from None
        return out

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

    def controls(self, wanted: list) -> int:
        """The chats whose model and thinking the core asked about, read (and changed, when asked) one after
        another, and posted. Returns how many were posted."""
        sent = 0
        for r in wanted[:3]:
            if not (isinstance(r, dict) and isinstance(r.get("id"), str) and isinstance(r.get("session_id"), str)
                    and SESSION_ID.match(r["session_id"]) and (r.get("set") is None or isinstance(r["set"], dict))):
                continue
            state, applied, reason = self.hermes.control(r["session_id"], r.get("set"))
            if r.get("set"):
                self.say(f"{'PASS' if applied else 'NOTE'} chat settings: {'changed' if applied else reason}")
            status, _ = self.core.post("control", {"id": r["id"], "state": state, "applied": applied,
                                                   "reason": reason})
            if status == 200:
                sent += 1
            elif status not in (409, 423):
                self.say(f"FAIL control: the core answered {status}")
        return sent

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

    def tasks(self) -> bool:
        """Every TASKS_SECONDS, in a thread of its own so a slow read holds up no message: what this Hermes
        has going besides its chats, read by the helper and posted (`POST /own/<gateway>/tasks`). Returns
        whether a round started."""
        now = self.clock()
        if (self._tasks_run is not None and self._tasks_run.is_alive()) or (
                self._tasks_at is not None and now - self._tasks_at < TASKS_SECONDS):
            return False
        self._tasks_at = now

        def report():
            got = self.hermes.tasks()
            status, _ = self.core.post("tasks", {"why": got["why"]} if set(got) == {"why"} else {"tasks": got})
            if set(got) == {"why"}:
                self.say(f"FAIL tasks: {got['why']}")
            elif status not in (200, 423):
                self.say(f"FAIL tasks: the core answered {status}")

        self._tasks_run = threading.Thread(target=report, daemon=True)
        self._tasks_run.start()
        return True

    def busy(self) -> dict:
        """What it is doing, for the phone's words about a message that waits: how many turns are running,
        of how many at once, since when the oldest runs, its commit, and an update it is on or could not do."""
        now = self.clock()
        started = [getattr(t, "started_at", now) for t in self.running]
        with self._lock:
            turns = list(self.doing.values())[:PARALLEL]
        return {"commit": self.commit, "busy": len(self.running), "parallel": PARALLEL,
                "busy_for": int(now - min(started)) if started else 0,
                "updating": self.leaving, "update_why": self.update_why, "turns": turns}

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
        controls = body.get("controls", [])
        if isinstance(controls, list) and controls:
            self.controls(controls)
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
        self.tasks()
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
    ap.add_argument("--hermes-root", help="with --hermes-python: Hermes's source folder, when it is not installed "
                                          "into that Python")
    ap.add_argument("--hermes-home", help="Hermes's state folder for this user (default: as Hermes finds it)")
    ap.add_argument("--token", help="this connector's token file")
    ap.add_argument("--core", default=LOCAL_CORE, help="the core's API: loopback on the VM, the VM's ts.net on One")
    ap.add_argument("--once", action="store_true", help="one round, then stop")
    ap.add_argument("--clone", help="One: Patrick's clone of TWHermesAgents, which this copy updates itself from")
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
    ready = helper_command(args.hermes, python=args.hermes_python, root=args.hermes_root)
    hermes = Hermes([args.hermes], ready, Path.home(),
                    GATEWAYS[args.gateway], state_db=home / "state.db", turn_cmd=ready)
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
    try:
        conn.forever()
    except SystemExit as exc:
        if exc.code != UPDATE_EXIT:
            say(f"STOP the connector: {_why(exc)}")
        raise
    except BaseException as exc:
        say(f"STOP the connector: {_why(exc)}")
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
