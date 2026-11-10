"""What Hermes, One and the six Bots have going besides their chats, for the app's Tasks tab.

Patrick, 3 Nov: "I wanted to see the subagents, jobs, and background tasks on the Mobile and Desktop app."
Each Hermes keeps these per profile, so the tab shows all of them at once (Hermes, One and each Bot is
its own profile), the way Hermes Desktop's views show one profile's:

- subagents: the children a chat's turn delegated to, running now, in the foreground or the background;
- background commands: the commands a turn started with `background: true`, running or just ended;
- scheduled jobs: Hermes's cron jobs, paused ones too, with the next and last run.

The connectors read them with room_send.py op `tasks` (contracts.json "tasks") and post them here: the
Bots' connector for the Bots every 30 seconds (`POST /connector/tasks`), and each own-Hermes connector
for its Hermes every 20 seconds (`POST /own/<gateway>/tasks`). The core keeps only the latest report of
each, in memory, as it keeps the online dots (chat.py _LIVE): nothing of it is written down. A Hermes
not heard from in TASKS_STALE shows as not heard, never with an old list as though it were current.
"""

from __future__ import annotations

import threading
from datetime import timedelta

from .store import CoreError

TASKS_STALE = timedelta(seconds=150)  # the slower connector reports every 30 s, and its read takes up to 60 s
ROWS = 30                             # room_send.py TASK_ROWS
_LOCK = threading.Lock()
_TASKS: dict[tuple[str, str], dict] = {}

_TEXT = {"state": 20, "live_why": 200, "jobs_why": 200}
_KINDS = {
    "subagents": {"id": 64, "chat": 128, "chat_title": 120, "goal": 300, "model": 80, "status": 20,
                  "last_tool": 60, "depth": int, "tools": int, "started": float, "background": bool},
    "processes": {"id": 64, "chat": 128, "chat_title": 120, "command": 200, "status": 20, "why": 40,
                  "exit_code": int, "started": float, "ended": float},
    "jobs": {"id": 64, "name": 120, "schedule": 80, "state": 20, "next": 40, "last": 40, "last_status": 30,
             "last_error": 200, "running": bool},
}


def _bad(why: str) -> CoreError:
    return CoreError(400, "bad_tasks", why)


def _value(v, kind):
    """One field as its kind, or None. Text is cut to its length on one line."""
    if v is None:
        return None
    if kind is bool:
        if not isinstance(v, bool):
            raise _bad("a yes-or-no field is true or false")
        return v
    if kind is int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise _bad("a count is a whole number")
        return v
    if kind is float:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or abs(v) > 1e11:
            raise _bad("a time is seconds since 1970")
        return float(v)
    if not isinstance(v, str):
        raise _bad("a text field is words")
    return " ".join(v.split())[:kind] or None


def clean(report) -> dict:
    """One Hermes's report as the helper wrote it, checked field by field: only the known fields, each its
    kind, text cut short, at most ROWS rows of each kind. A part the helper could not read is null with its
    reason beside it, and stays null here, so the app never shows an empty list for one it does not know."""
    if not isinstance(report, dict):
        raise _bad("a report is a JSON object")
    out: dict = {k: _value(report.get(k), n) for k, n in _TEXT.items()}
    for kind, fields in _KINDS.items():
        rows = report.get(kind)
        if rows is None:
            out[kind] = None
            continue
        if not isinstance(rows, list) or len(rows) > ROWS or not all(isinstance(r, dict) for r in rows):
            raise _bad(f"{kind} is a list of at most {ROWS} objects")
        out[kind] = [{f: _value(r.get(f), n) for f, n in fields.items()} for r in rows]
    return out


class Tasks:
    def __init__(self, s):
        self.s = s

    def put(self, workspace_id: str, agent: str, body) -> None:
        """A connector's report for one Hermes: {"tasks": <report>}, or {"why": ...} when its helper could
        not read anything."""
        if not isinstance(body, dict) or (("tasks" in body) == ("why" in body)):
            raise _bad("a report has its tasks, or why there are none")
        entry = {"at": self.s.clock()}
        if "why" in body:
            if not isinstance(body["why"], str):
                raise _bad("why is words")
            entry["why"] = " ".join(body["why"].split())[:200] or "it gave no reason"
        else:
            entry["report"] = clean(body["tasks"])
        with _LOCK:
            _TASKS[(workspace_id, agent)] = entry

    def view(self, workspace_id: str, agents: list[str]) -> dict:
        """Each Hermes in `agents` order: "heard" with what it has going, or not heard since when."""
        now, out = self.s.clock(), []
        for agent in agents:
            with _LOCK:
                got = _TASKS.get((workspace_id, agent))
            if got is None or now - got["at"] > TASKS_STALE:
                out.append({"id": agent, "heard": False, "since": got["at"].isoformat() if got else None})
            elif "why" in got:
                out.append({"id": agent, "heard": True, "at": got["at"].isoformat(), "why": got["why"]})
            else:
                out.append({"id": agent, "heard": True, "at": got["at"].isoformat(), **got["report"]})
        return {"agents": out}

    def job(self, workspace_id: str, agent: str, job_id: str) -> dict | None:
        """That Hermes's job with this id, as its last report listed it; None when the report is stale, could
        not read its jobs, or did not list it (own_jobs.py: a change names a job Hermes listed)."""
        with _LOCK:
            got = _TASKS.get((workspace_id, agent))
        if got is None or self.s.clock() - got["at"] > TASKS_STALE or "report" not in got:
            return None
        return next((j for j in got["report"].get("jobs") or [] if j["id"] == job_id), None)
