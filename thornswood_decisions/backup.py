"""Backup and restore of the decision core (07 section 11.1; 02 section 7).

A backup is a consistent copy made with SQLite's online backup API while the
core runs. Its event chain is checked before the copy is kept.

A restore never replays the past: every queued, leased or uncertain push and
connector message is quarantined, every session and open pairing code is
revoked, and no answer given before the backup is sent anywhere again. The
restore itself is the first new event. Encryption and the trip to One and
Google Drive belong to the nightly archive (Phase 2's backup job), which
takes this copy in place of the live file.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

from .store import CoreError, Store, new_id, verify_chain

QUARANTINE_FROM = ("queued", "leased", "uncertain")


def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_file(path: Path) -> tuple[bool, str]:
    """Check a backup's event chain without writing to it."""
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return verify_chain(con)
    except sqlite3.Error as exc:
        return False, f"not a readable store: {exc}"
    finally:
        con.close()


def backup(store: Store, dest: Path | str) -> dict:
    dest = Path(dest)
    if dest.exists():
        raise CoreError(409, "backup_exists", f"{dest} already exists; a backup never overwrites")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    target = sqlite3.connect(str(part))
    try:
        store.db.backup(target)
    finally:
        target.close()
    ok, why = verify_file(part)
    if not ok:
        part.unlink()
        _record(store, "backup", dest, None, False, why)
        raise CoreError(500, "chain_broken", f"the copy's event chain is broken: {why}")
    os.replace(part, dest)
    sha = _file_sha(dest)
    _record(store, "backup", dest, sha, True, why)
    return {"path": str(dest), "sha256": sha, "events": why}


def restore(src: Path | str, db_path: Path | str, *, clock=None, by: str = "operator") -> dict:
    """Put a backup in place of a store that is not running. Refuses to overwrite a live store."""
    src, db_path = Path(src), Path(db_path)
    if db_path.exists():
        raise CoreError(409, "store_exists",
                        f"{db_path} exists; move it aside first, so a restore never destroys the only copy")
    ok, why = verify_file(src)
    if not ok:
        raise CoreError(409, "chain_broken", f"the backup's event chain is broken: {why}")
    target = sqlite3.connect(str(db_path))
    source = sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        source.backup(target)
    finally:
        source.close()
        target.close()
    store = Store(db_path, **({"clock": clock} if clock else {}))
    try:
        counts = {}
        with store.tx() as db:
            for table in ("push_outbox", "connector_outbox"):
                counts[table] = db.execute(
                    f"UPDATE {table} SET status = 'quarantined', reason = 'restored from a backup', updated_at = ? "
                    f"WHERE status IN {QUARANTINE_FROM}", (store.now(),)).rowcount
            counts["sessions"] = db.execute(
                "UPDATE sessions SET revoked_at = ?, revoked_reason = 'restored from a backup' "
                "WHERE revoked_at IS NULL", (store.now(),)).rowcount
            counts["pairing_codes"] = db.execute(
                "UPDATE pairing_codes SET cancelled_at = ? WHERE used_at IS NULL AND cancelled_at IS NULL",
                (store.now(),)).rowcount
            for (wid,) in db.execute("SELECT id FROM workspaces").fetchall():
                store.event(db, wid, "store.restored", str(src.name), by, counts)
            db.execute("INSERT INTO meta(key, value) VALUES('restored_from', ?) "
                       "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (_file_sha(src),))
        _record(store, "restore", src, _file_sha(src), True, why)
        return {"restored_to": str(db_path), "quarantined": counts}
    finally:
        store.close()


def _record(store: Store, kind: str, path: Path, sha: str | None, ok: bool, detail: str) -> None:
    with store.tx() as db:
        db.execute("INSERT INTO backup_runs(id, kind, path, sha256, ok, detail, at) VALUES(?,?,?,?,?,?,?)",
                   (new_id("bk"), kind, str(path), sha, int(ok), detail, store.now()))
