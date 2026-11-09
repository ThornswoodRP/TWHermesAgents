"""What each turn in this profile is doing right now, for the phone app's live Board (Patrick, 2 Nov: "Is
supposed to show only what each is currently doing ... it's supposed to be live what they are doing").

Installed into each Bot's profile and the gateway's own (scripts/apply.py) and enabled in each config. It
observes four of Hermes's own hooks and blocks nothing: a turn starts thinking (pre_llm_call), runs a tool by
its name (pre_tool_call), thinks again after it (post_tool_call), and is gone when it ends (on_session_end,
which Hermes calls at the end of every turn, finished, failed or interrupted). Each change rewrites one small
file, `thornswood-live/<pid>.json` in the home of the profile whose turn it is (the gateway serves several
profiles from one process, so the home is read per turn, never at load), atomically. It holds this process's
id and start time, so the reader (src/thornswood_decisions/live_reader.py, the same `hermes` user) can tell a file
whose process has ended, and each running turn's session id, where it came from, its state, its tool (the
newest, when it runs several at once) and since when. Never a tool's arguments, its output or any words of
the turn.

A hook here must never raise or slow a turn: a failed write is logged once and the turn goes on.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

FOLDER = "thornswood-live"
TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
PLATFORM = re.compile(r"^[a-z0-9_-]{1,32}$")
SESSION_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
MAX_TURNS = 20

_lock = threading.Lock()
_turns: dict[str, dict[str, dict]] = {}   # home -> turn key -> what it is doing
_warned: set[str] = set()


def process_start(pid: int | str = "self") -> str | None:
    """The process's start time in clock ticks since boot (/proc/<pid>/stat field 22): with the pid, it
    names one process for good, so a reused pid is never taken for the one that wrote a file."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
        return stat[stat.rindex(")") + 2:].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def _home() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write(home: Path, turns: dict) -> None:
    folder = home / FOLDER
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / f"{os.getpid()}.json"
    if not turns:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    tmp = folder / f".{os.getpid()}.{threading.get_ident()}.tmp"
    body = {"pid": os.getpid(), "start": process_start(), "turns": list(turns.values())}
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(body, f)
    os.replace(tmp, path)


def _set(session_id, *, start=False, tool=None, call=None, done=None, platform=None, end=False) -> None:
    """One session runs one turn at a time, so a turn is known by its session id. A turn may run several
    tools at once: each is held by its call id until it ends, and the turn says the newest one."""
    try:
        if not (isinstance(session_id, str) and SESSION_ID.match(session_id)):
            return
        home = _home()
        with _lock:
            turns = _turns.setdefault(str(home), {})
            if end:
                turns.pop(session_id, None)
            elif session_id in turns or len(turns) < MAX_TURNS:
                if start:  # a new turn: its own start time, no tool of an earlier turn
                    turns.pop(session_id, None)
                turn = turns.setdefault(session_id, {"session_id": session_id, "since": _now(), "platform": None,
                                                     "running": {}})
                if isinstance(platform, str) and PLATFORM.match(platform):
                    turn["platform"] = platform
                key = call if isinstance(call, str) and call else (tool or "")
                if tool is not None and isinstance(tool, str) and TOOL_NAME.match(tool):
                    turn["running"][key] = tool
                if done is not None:
                    turn["running"].pop(done if isinstance(done, str) and done else "", None)
                    if done and done not in turn["running"]:
                        # A call that ended without its id: the tool by name.
                        for k, v in list(turn["running"].items()):
                            if v == done:
                                turn["running"].pop(k)
                                break
            _write(home, {k: _public(t) for k, t in turns.items()})
    except Exception as e:  # an observer never breaks a turn
        where = type(e).__name__
        if where not in _warned:
            _warned.add(where)
            logger.warning("thornswood_live could not say what a turn is doing (%s: %s)", where, e)


def _public(turn: dict) -> dict:
    tools = list(turn["running"].values())
    return {"session_id": turn["session_id"], "since": turn["since"], "platform": turn["platform"],
            "state": "tool" if tools else "thinking", "tool": tools[-1] if tools else None}


def on_turn_start(session_id=None, platform=None, **_):
    _set(session_id, start=True, platform=platform)


def on_tool_start(tool_name=None, args=None, session_id=None, tool_call_id=None, **_):
    _set(session_id, tool=tool_name, call=tool_call_id)
    return None  # an observer: it never blocks or changes the call


def on_tool_end(tool_name=None, args=None, session_id=None, tool_call_id=None, **_):
    _set(session_id, done=tool_call_id or tool_name)


def on_turn_end(session_id=None, **_):
    _set(session_id, end=True)


def register(ctx) -> None:
    # register() must never raise: Hermes skips a plugin whose register() fails.
    try:
        ctx.register_hook("pre_llm_call", on_turn_start)
        ctx.register_hook("pre_tool_call", on_tool_start)
        ctx.register_hook("post_tool_call", on_tool_end)
        ctx.register_hook("on_session_end", on_turn_end)
    except Exception as e:
        logger.error("thornswood_live could not register its hooks (%s: %s)", type(e).__name__, e)
