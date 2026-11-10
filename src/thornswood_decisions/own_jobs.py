"""Pause, resume, run now and remove his own Hermes's and One's scheduled jobs from the app's Tasks tab (#43).

The Tasks tab listed every scheduled job read only, so Patrick still needed Hermes Desktop's Scheduled jobs
page (or a chat with Hermes) to pause, resume, run or remove one. Now a job of his own Hermes or One has
those four in the app; a Bot's jobs stay read only, since each Bot keeps its own.

A change is asked of that Hermes's own connector (own_connector.py), which makes it with Hermes's own
cron.jobs calls under its home, the ones Hermes's dashboard makes for Desktop (room_send.py "job",
contracts.json "jobs"), then reads the jobs again at once so the tab shows what Hermes now holds. Run now
is Hermes's own trigger_job: the job is due at once and the running gateway's scheduler fires it at its
next tick. Each change needs a passkey sign-in from the last 30 minutes, as a message to his own Hermes
does, and names a job that Hermes listed in its last report.

Nothing here is kept on disk: an ask lives at most 2 minutes, its outcome 10, in memory. An ask is handed to
the connector once, never again by itself, since a removal or a run asked twice is not the same as once; one
the connector never answered says so, and the tab shows what Hermes really holds.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta

from .store import CoreError, new_id
from .wakes import WAKES

ASK_TTL = timedelta(minutes=2)     # an ask the connector never answered is given up after this
SHOWN = timedelta(minutes=10)      # how long an outcome stays beside its job
DO = ("pause", "resume", "run", "remove")
JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")   # room_send.py JOB_ID
ASK_ID = re.compile(r"^job_[A-Za-z0-9_-]{8,40}$")


def _text(v, n: int) -> str | None:
    return " ".join(v.split())[:n] or None if isinstance(v, str) else None


class _Jobs:
    def __init__(self):
        self.lock = threading.Lock()
        self.asked: dict[tuple, dict] = {}   # (ws, gw, job) -> {"id", "do", "at", "handed"}
        self.shown: dict[tuple, dict] = {}   # (ws, gw, job) -> {"id", "do", "status", "reason", "at", "key"}

    def _drop_old(self, now: datetime) -> None:
        for k in [k for k, a in self.asked.items() if now - a["at"] > ASK_TTL]:
            a = self.asked.pop(k)
            sh = self.shown.get(k)
            if sh and sh["id"] == a["id"] and sh["status"] == "queued":
                sh.update(status="lapsed", at=now,
                          reason="its Hermes did not answer in 2 minutes; the list shows what Hermes holds")
        for k in [k for k, sh in self.shown.items() if now - sh["at"] > SHOWN and k not in self.asked]:
            del self.shown[k]

    def add(self, ws: str, gw: str, job: str, do: str, idem: str, now: datetime) -> dict:
        key = (ws, gw, job)
        with self.lock:
            self._drop_old(now)
            sh = self.shown.get(key)
            if sh and sh["key"] == idem:
                if sh["do"] != do:
                    raise CoreError(409, "key_reused", "that idempotency key was used for a different change")
                return {"id": sh["id"], "status": sh["status"], "replayed": True}
            a = self.asked.get(key)
            if a and a["handed"]:
                raise CoreError(409, "job_busy", "the last change to this job is being made now; wait for it")
            # The newest ask wins: an older one not handed out yet is replaced, never made after it.
            aid = new_id("job")
            self.asked[key] = {"id": aid, "do": do, "at": now, "handed": None}
            self.shown[key] = {"id": aid, "do": do, "status": "queued", "reason": None, "at": now, "key": idem}
        WAKES.bump(ws, gw)
        return {"id": aid, "status": "queued"}

    def hand(self, ws: str, gw: str, now: datetime) -> list[dict]:
        """The asks not handed out yet, oldest first. Each goes once."""
        with self.lock:
            self._drop_old(now)
            out = []
            for (w, g, job), a in sorted(self.asked.items(), key=lambda kv: kv[1]["at"]):
                if w == ws and g == gw and not a["handed"]:
                    a["handed"] = now
                    out.append({"id": a["id"], "job": job, "do": a["do"]})
            return out

    def answer(self, ws: str, gw: str, body, now: datetime) -> dict:
        if not isinstance(body, dict) or not isinstance(body.get("id"), str) or not ASK_ID.match(body["id"]):
            raise CoreError(400, "bad_job", "a job answer names the ask it answers")
        if not isinstance(body.get("done"), bool):
            raise CoreError(400, "bad_job", "a job answer says whether it was done")
        with self.lock:
            key = next((k for k, a in self.asked.items() if a["id"] == body["id"] and k[:2] == (ws, gw)), None)
            if key is None:
                raise CoreError(409, "not_asked", "no job change with that id was asked of this gateway")
            a = self.asked.pop(key)
            sh = self.shown.get(key)
            if sh and sh["id"] == a["id"]:
                sh.update(status="done" if body["done"] else "refused", at=now,
                          reason=None if body["done"] else _text(body.get("reason"), 200) or "Hermes did not do it")
            return {"ok": True}

    def state(self, ws: str, gw: str, now: datetime) -> dict:
        """Each job's newest change and how it went, by job id."""
        with self.lock:
            self._drop_old(now)
            return {k[2]: {f: sh[f] for f in ("id", "do", "status", "reason")}
                    for k, sh in self.shown.items() if k[:2] == (ws, gw)}


JOBS = _Jobs()
