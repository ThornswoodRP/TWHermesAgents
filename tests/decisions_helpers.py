"""Fixtures for the decision core tests: a test workspace in a scratch store.

Nothing here touches a real workspace, a real Bot or a real deploy. Every
store is a fresh file in a temp folder, and the workspace is marked as a test
workspace before any flag is turned on.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import helpers  # noqa: F401  (puts the repo on sys.path)

from thornswood_decisions.auth import Principal
from thornswood_decisions.core import DecisionCore
from thornswood_decisions.store import Store

T0 = datetime(2026, 11, 1, 18, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, t: datetime = T0):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t = self.t + timedelta(**kw)


OPTIONS = [
    {"key": "a", "label": "Do it", "consequence": "Bramble does it today."},
    {"key": "b", "label": "Wait", "consequence": "Nothing happens until next week."},
]


class World:
    """A scratch store with a test workspace, Patrick, Bramble and one specialist."""

    def __init__(self, *, writes: bool = True, path: Path | None = None, clock: Clock | None = None):
        self.dir = Path(tempfile.mkdtemp())
        self.path = path or (self.dir / "core.db")
        self.clock = clock or Clock()
        self.store = Store(self.path, clock=self.clock)
        self._stores = [self.store]
        self.core = DecisionCore(self.store)
        self.ws = self.store.ensure_workspace("test", is_test=True, by="tests")
        if writes:
            self.store.set_flag(self.ws, "decision_write", True, by="tests")
        self.patrick_id = self.core.auth.ensure_patrick(self.ws)
        self.bramble = self.core.auth.ensure_service(self.ws, "bramble", "bramble")
        self.rift = self.core.auth.ensure_service(self.ws, "specialist", "rift")

    def other(self, fault=None) -> DecisionCore:
        """A second connection to the same store, like a second request handler."""
        s = Store(self.path, clock=self.clock)
        self._stores.append(s)
        return DecisionCore(s, fault=fault)

    def close(self) -> None:
        for s in self._stores:
            s.close()

    def device(self, kind: str = "phone", label: str = "Patrick's phone") -> str:
        """A paired device, written straight in (pairing itself is tested in test_decisions_auth)."""
        from thornswood_decisions.store import new_id
        dev = new_id("dv")
        with self.store.tx() as db:
            db.execute("INSERT INTO devices(id, workspace_id, principal_id, kind, label, created_at) "
                       "VALUES(?,?,?,?,?,?)", (dev, self.ws, self.patrick_id, kind, label, self.store.now()))
        return dev

    def patrick(self, *, fresh: bool = True, device: str | None = None) -> Principal:
        return Principal(self.patrick_id, self.ws, "patrick", None, device, "0" * 64, fresh)

    def card(self, core: DecisionCore | None = None, by: Principal | None = None, **kw) -> dict:
        args = dict(kind="question", title="Pick one", body="What is wrong, what was measured, each option.",
                    options=OPTIONS, recommendation="a", scope_of_yes="Only this one change.")
        args.update(kw)
        return (core or self.core).open_card(by or self.bramble, **args)

    @staticmethod
    def answer_args(card: dict, option: str = "a", key: str = "key-00000001") -> dict:
        return dict(decision_id=card["id"], option_key=option, revision=card["revision"],
                    attention_revision=card["attention_revision"], artifact_sha256=card["artifact_sha256"],
                    idempotency_key=key)
