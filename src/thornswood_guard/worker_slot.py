"""Profile state shared by the worker check and Bramble's turn gate."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from .hours import work_hours

OWED_WAKE_JOB = "tw-owed-wake"
IDLE_FILE = ("local", "thornswood-hermes", "idle-wake.json")
JOBS_FILE = ("cron", "jobs.json")
MOUNTAIN = "America/Denver"
# Allow the next five-minute tick and its board read. This generation is
# never extended, even if no scheduled check ran before the expiry.
WORKER_SLOT_SECONDS = 540.0
WORKER_SLOT_FILE = ("local", "thornswood-hermes", "worker-check-slot.json")


def load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def mountain_time(now: float) -> datetime | None:
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(now, ZoneInfo(MOUNTAIN))
    except Exception:
        return None


def working(now: float, policy: dict | None = None) -> bool:
    local = mountain_time(now)
    start, end = work_hours(policy)
    return local is not None and start <= local.hour < end


def cron_enabled(home: Path) -> bool:
    jobs = load(home.joinpath(*JOBS_FILE)).get("jobs")
    return isinstance(jobs, list) and any(isinstance(job, dict) and job.get("name") == OWED_WAKE_JOB
                                          and job.get("enabled") is True
                                          and job.get("state") in ("scheduled", "running") for job in jobs)


def _checked_at(home: Path) -> float:
    at = load(home.joinpath(*IDLE_FILE)).get("idle_checked")
    return at if type(at) in (int, float) and 0 <= at < float("inf") else 0.0


def reserve(home: Path, now: float) -> None:
    """Reserve at most once between two actual worker-board attempts."""
    path = home.joinpath(*WORKER_SLOT_FILE)
    checked_at = _checked_at(home)
    previous = load(path)
    if previous.get("checked_at") == checked_at:
        return
    try:
        save(path, {"checked_at": checked_at, "created_at": now, "consumed": False})
    except OSError:
        pass  # a local marker failure does not change the ordinary cron check


def active(home: Path, policy: dict, now: float) -> bool:
    slot = load(home.joinpath(*WORKER_SLOT_FILE))
    at = slot.get("created_at")
    return (slot.get("consumed") is False and type(at) in (int, float)
            and now - WORKER_SLOT_SECONDS < at <= now and working(now, policy) and cron_enabled(home))


def consume(home: Path) -> None:
    """A fresh board attempt needs the slot until its scheduled turn admits."""
    path = home.joinpath(*WORKER_SLOT_FILE)
    slot = load(path)
    checked_at = slot.get("checked_at")
    if slot.get("consumed") is False and type(checked_at) in (int, float) and _checked_at(home) > checked_at:
        slot["consumed"] = True
        try:
            save(path, slot)
        except OSError:
            pass  # a stale marker expires without interrupting an admitted turn
