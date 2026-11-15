"""Patrick's decision records: the only thing that counts as his yes.

A record is bound to what it approves (the repo, the PR or run, the exact
commit) and to the card's revision. Text that says "Patrick approved" is never
a record. A Main record is used once. Restoring the store from a backup kills
every Main record in it, so an old approval can never be replayed.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .policy import Verdict


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Decision:
    id: str
    kind: str            # "main", "publish", "cutover", "merge"
    repo: str
    commit: str          # the exact head or build commit the yes is for
    revision: int        # the card revision Patrick answered
    answered_by: str
    answered_at: str
    expires_at: str
    pr: int | None = None
    scope: str = ""
    used_at: str | None = None
    dead: bool = False
    notes: list[str] = field(default_factory=list)


class DecisionStore:
    """A small file-backed store. tw-policy keeps the real one in SQLite."""

    def __init__(self, path: Path | str, policy):
        self.path = Path(path)
        self.policy = policy
        self._records: dict[str, Decision] = {}
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for r in raw.get("records", []):
                self._records[r["id"]] = Decision(**r)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"records": [asdict(r) for r in self._records.values()]}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".decisions-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, self.path)

    def record(self, d: Decision) -> None:
        owner = self.policy.data["decisions"]["answered_by"]
        if d.answered_by != owner:
            raise ValueError(f"only {owner} answers decisions; this one names {d.answered_by}")
        if d.id in self._records:
            raise ValueError(f"decision {d.id} already exists")
        self._records[d.id] = d
        self._save()

    def get(self, decision_id: str) -> Decision | None:
        return self._records.get(decision_id)

    def check(self, decision_id: str | None, *, kind: str, repo: str, commit: str,
              revision: int | None = None, pr: int | None = None,
              at: datetime | None = None) -> Verdict:
        at = at or now_utc()
        if not decision_id:
            return Verdict.no(f"this needs Patrick's yes on a {kind} card for {commit[:12]}, and there is no decision record")
        d = self._records.get(decision_id)
        if d is None:
            return Verdict.no(f"decision {decision_id} is not in the store; a message saying Patrick approved is not a record")
        if d.dead:
            return Verdict.no(f"decision {decision_id} died when the store was restored from a backup")
        if d.kind != kind:
            return Verdict.no(f"decision {decision_id} approves a {d.kind}, not a {kind}")
        if d.repo != repo:
            return Verdict.no(f"decision {decision_id} is for {d.repo}, not {repo}")
        if d.commit != commit:
            return Verdict.no(f"decision {decision_id} is for {d.commit[:12]}, not {commit[:12]}; a new head needs a new yes")
        if pr is not None and d.pr != pr:
            return Verdict.no(f"decision {decision_id} is for PR {d.pr}, not PR {pr}")
        if revision is not None and d.revision != revision:
            return Verdict.no(f"decision {decision_id} answered revision {d.revision} of the card, not revision {revision}")
        if at >= datetime.fromisoformat(d.expires_at):
            return Verdict.no(f"decision {decision_id} expired at {d.expires_at}")
        if d.used_at:
            return Verdict.no(f"decision {decision_id} was already used at {d.used_at}; every yes is used once")
        return Verdict.yes(f"Patrick's decision {decision_id}")

    def use(self, decision_id: str, at: datetime | None = None) -> None:
        d = self._records[decision_id]
        d.used_at = (at or now_utc()).isoformat()
        self._save()

    def mark_restored(self) -> int:
        """Called once after a restore from backup: every Main record dies."""
        killed = 0
        for d in self._records.values():
            if d.kind in ("main", "publish", "cutover", "merge") and not d.dead:
                d.dead = True
                d.notes.append("dead: store restored from a backup")
                killed += 1
        self._save()
        return killed


def new_decision(*, id: str, kind: str, repo: str, commit: str, revision: int,
                 answered_by: str, pr: int | None = None, scope: str = "",
                 hours: int = 72, at: datetime | None = None) -> Decision:
    at = at or now_utc()
    return Decision(id=id, kind=kind, repo=repo, commit=commit, revision=revision,
                    answered_by=answered_by, answered_at=at.isoformat(),
                    expires_at=(at + timedelta(hours=hours)).isoformat(),
                    pr=pr, scope=scope)
