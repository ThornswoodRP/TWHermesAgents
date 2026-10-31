"""Opening the store, migrating it, writing in one transaction, and the event log.

One writer at a time: every write runs inside BEGIN IMMEDIATE, so two devices
answering at once are put in order by SQLite, never interleaved. The event log
is hash-chained: each event's hash covers the one before it, so a removed,
reordered or edited event breaks the chain and `verify_events` says where.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from .schema import FLAGS, MIGRATIONS

GENESIS = "0" * 64


class CoreError(Exception):
    """A refused request. `status` is the HTTP status the API layer answers with."""

    def __init__(self, status: int, reason: str, message: str, **detail):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.detail = detail


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime) -> str:
    if t.tzinfo is None:
        raise ValueError("times are UTC with a zone")
    return t.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(16)}"


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def connect(path: Path | str) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 30000")
    mode = con.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        con.close()
        raise CoreError(500, "store_not_wal", f"the store at {path} would not switch to WAL (got {mode})")
    con.execute("PRAGMA synchronous = FULL")
    return con


def schema_version(con: sqlite3.Connection) -> int:
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    row = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return int(row[0]) if row else 0


def migrate(con: sqlite3.Connection) -> int:
    have = schema_version(con)
    if have > len(MIGRATIONS):
        raise CoreError(500, "store_too_new",
                        f"the store is at schema {have}; this code knows only {len(MIGRATIONS)}")
    for n in range(have + 1, len(MIGRATIONS) + 1):
        con.execute("BEGIN IMMEDIATE")
        try:
            # executescript would commit on its own, so the statements run one by one
            for stmt in _statements(MIGRATIONS[n - 1]):
                con.execute(stmt)
            con.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(n),))
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    return len(MIGRATIONS)


def _statements(script: str) -> Iterator[str]:
    """Split a migration into statements, keeping trigger bodies whole."""
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            stmt = buf.strip()
            buf = ""
            if stmt and stmt != ";":
                yield stmt
    if buf.strip():
        raise CoreError(500, "bad_migration", f"an unfinished statement in a migration: {buf.strip()[:80]}")


class Store:
    """One connection to the core's database, used by one thread at a time."""

    def __init__(self, path: Path | str, *, clock: Callable[[], datetime] = now_utc):
        self.path = Path(path)
        self.clock = clock
        self.db = connect(self.path)
        try:
            migrate(self.db)
        except BaseException:
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    def now(self) -> str:
        return iso(self.clock())

    # ── workspaces and flags ──────────────────────────────────────────────

    def ensure_workspace(self, name: str, *, is_test: bool, by: str) -> str:
        """The workspace's id, made with every flag off if it is new."""
        with self.tx() as db:
            row = db.execute("SELECT id, is_test FROM workspaces WHERE name = ?", (name,)).fetchone()
            if row:
                if bool(row["is_test"]) != is_test:
                    raise CoreError(409, "workspace_kind", f"workspace {name} exists with is_test={bool(row['is_test'])}")
                return row["id"]
            wid = new_id("ws")
            at = self.now()
            db.execute("INSERT INTO workspaces(id, name, is_test, created_at) VALUES(?,?,?,?)",
                       (wid, name, int(is_test), at))
            for flag in FLAGS:
                db.execute("INSERT INTO flags(workspace_id, name, enabled, changed_at, changed_by) "
                           "VALUES(?,?,0,?,?)", (wid, flag, at, by))
            self.event(db, wid, "workspace.created", wid, by, {"name": name, "is_test": is_test})
            return wid

    def flag(self, workspace_id: str, name: str) -> bool:
        if name not in FLAGS:
            raise CoreError(500, "unknown_flag", f"no flag named {name}")
        row = self.db.execute("SELECT enabled FROM flags WHERE workspace_id = ? AND name = ?",
                              (workspace_id, name)).fetchone()
        return bool(row and row["enabled"])

    def set_flag(self, workspace_id: str, name: str, enabled: bool, *, by: str) -> None:
        """Run on the VM by the operator only; no API route reaches it."""
        if name not in FLAGS:
            raise CoreError(400, "unknown_flag", f"no flag named {name}")
        with self.tx() as db:
            db.execute("UPDATE flags SET enabled = ?, changed_at = ?, changed_by = ? "
                       "WHERE workspace_id = ? AND name = ?", (int(enabled), self.now(), by, workspace_id, name))
            self.event(db, workspace_id, "flag.changed", name, by, {"enabled": enabled})

    # ── the event log ─────────────────────────────────────────────────────

    def event(self, db: sqlite3.Connection, workspace_id: str, kind: str, subject: str, actor: str,
              body: dict) -> dict:
        """Append one event inside the caller's transaction."""
        last = db.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        seq = (last["seq"] + 1) if last else 1
        prev = last["hash"] if last else GENESIS
        at = self.now()
        body_json = canonical(body)
        h = event_hash(prev, seq, workspace_id, kind, subject, actor, body_json, at)
        db.execute("INSERT INTO events(seq, workspace_id, kind, subject, actor, body_json, at, prev_hash, hash) "
                   "VALUES(?,?,?,?,?,?,?,?,?)", (seq, workspace_id, kind, subject, actor, body_json, at, prev, h))
        return {"seq": seq, "kind": kind, "subject": subject, "hash": h}

    def verify_events(self) -> tuple[bool, str]:
        return verify_chain(self.db)


def verify_chain(con: sqlite3.Connection) -> tuple[bool, str]:
    """Walk the whole event log and say whether its hash chain holds."""
    prev = GENESIS
    expect = 1
    for r in con.execute("SELECT seq, workspace_id, kind, subject, actor, body_json, at, prev_hash, hash "
                         "FROM events ORDER BY seq"):
        seq, wid, kind, subject, actor, body_json, at, prev_hash, h = tuple(r)
        if seq != expect:
            return False, f"event {expect} is missing (next is {seq})"
        if prev_hash != prev:
            return False, f"event {seq} does not follow event {seq - 1}"
        if event_hash(prev_hash, seq, wid, kind, subject, actor, body_json, at) != h:
            return False, f"event {seq} was changed after it was written"
        prev = h
        expect += 1
    return True, f"{expect - 1} events, chain intact"


def event_hash(prev: str, seq: int, workspace_id: str, kind: str, subject: str, actor: str,
               body_json: str, at: str) -> str:
    return sha256_hex(canonical([prev, seq, workspace_id, kind, subject, actor, body_json, at]))
