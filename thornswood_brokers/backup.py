"""Backups (02 section 7; attack test 21).

On the VM, as root from tw-backup.timer:
  snapshot   tw-policy's store, copied with SQLite's backup API (a consistent
             copy while it runs), hourly, kept by the policy's counts.
  archive    the nightly tar of what the policy includes, minus every
             sign-in, .env, key and cache it names, with a fresh snapshot of
             the store in place of the live file, encrypted to Patrick's age
             public key. Only the encrypted file is written to disk. Owned by
             tw-backup, which One pulls over SFTP and nothing else.

On One (scripts/one/Pull-HermesBackup.ps1): pull, keep, upload to Google
Drive, and once a month decrypt one to prove it opens.

Restore, on the VM as root after One decrypted the archive and copied the tar
over: restore puts the files back and the store's mark_restored runs before
tw-policy serves again, so every Main, publish and cutover record is dead and
every write that was in flight is unknown.
"""

from __future__ import annotations

import fnmatch
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from thornswood_guard.policy import Policy

STORE_IN_ARCHIVE = "srv/tw/policy/policy.db"


def excluded(rel: str, is_dir: bool, rules: dict) -> bool:
    name = rel.rsplit("/", 1)[-1]
    if is_dir:
        return name in rules["exclude_dirs"]
    if any(fnmatch.fnmatchcase(name, p) for p in rules["exclude_names"]):
        return True
    # The live store is never copied file by file; the snapshot stands in.
    return rel == STORE_IN_ARCHIVE or rel.startswith(STORE_IN_ARCHIVE + "-")


def _is_sqlite(p: Path) -> bool:
    with open(p, "rb") as f:
        return f.read(16) == b"SQLite format 3\x00"


def snapshot(db: Path, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    dst = sqlite3.connect(str(tmp))
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
    finally:
        dst.close()
        src.close()
    os.replace(tmp, out)
    return out


def prune_snapshots(folder: Path, keep_hourly: int, keep_daily: int, now: float | None = None) -> list[Path]:
    """Keep the newest keep_hourly, plus the newest of each of the last
    keep_daily days. Returns what was removed."""
    snaps = sorted(folder.glob("policy-*.db"), key=lambda p: p.name, reverse=True)
    keep = set(snaps[:keep_hourly])
    days = {}
    for p in snaps:
        day = p.name[len("policy-"):len("policy-") + 8]
        days.setdefault(day, p)
    for day in sorted(days, reverse=True)[:keep_daily]:
        keep.add(days[day])
    removed = [p for p in snaps if p not in keep]
    for p in removed:
        p.unlink()
    return removed


def build_tar(policy: Policy, *, root: Path = Path("/"), store_snapshot: Path | None = None) -> bytes:
    rules = policy.data["backups"]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for inc in rules["include"]:
            base = root / inc.lstrip("/")
            if not base.exists():
                continue
            for dirpath, dirnames, filenames in os.walk(base):
                d = Path(dirpath)
                drel = d.relative_to(root).as_posix()
                dirnames[:] = sorted(n for n in dirnames if not excluded(f"{drel}/{n}", True, rules)
                                     and not (d / n).is_symlink())
                for n in sorted(filenames):
                    p = d / n
                    rel = f"{drel}/{n}"
                    if excluded(rel, False, rules) or p.is_symlink() or not p.is_file():
                        continue
                    if _is_sqlite(p):
                        # A live SQLite file is copied through its backup API,
                        # so the archive holds a consistent store. One that
                        # cannot be read that way fails the backup.
                        with tempfile.TemporaryDirectory() as t:
                            _add_copy(tar, p, snapshot(p, Path(t) / "copy.db"), rel)
                        continue
                    tar.add(p, arcname=rel, recursive=False)
        if store_snapshot is not None:
            live = root / policy.data["brokers"]["state"]["policy_db"].lstrip("/")
            _add_copy(tar, live if live.exists() else store_snapshot, store_snapshot, STORE_IN_ARCHIVE)
        manifest = json.dumps({"made": datetime.now(timezone.utc).isoformat(), "include": rules["include"]}).encode()
        info = tarfile.TarInfo("MANIFEST.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
    return buf.getvalue()


def _add_copy(tar: tarfile.TarFile, live: Path, copy: Path, rel: str) -> None:
    """Add a consistent copy under the live file's name, owner and mode."""
    info = tar.gettarinfo(str(live), arcname=rel)
    info.size = copy.stat().st_size
    with open(copy, "rb") as f:
        tar.addfile(info, f)


def _age(argv: list[str], stdin: bytes) -> bytes:
    r = subprocess.run(argv, input=stdin, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"age failed: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout


def encrypt(data: bytes, recipient_file: Path, out: Path,
            age: Callable[[list[str], bytes], bytes] | None = None) -> Path:
    age = age or _age
    tmp = out.with_suffix(".tmp")
    tmp.write_bytes(age(["age", "--encrypt", "-R", str(recipient_file)], data))
    os.replace(tmp, out)
    return out


def archive(policy: Policy, *, root: Path = Path("/"), out_dir: Path | None = None, age=None,
            now: datetime | None = None) -> Path:
    rules = policy.data["backups"]
    state = policy.data["brokers"]["state"]
    out_dir = out_dir or Path(state["backups"])
    out_dir.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory() as tmp:
        snap = None
        db = root / state["policy_db"].lstrip("/")
        if db.exists():
            snap = snapshot(db, Path(tmp) / "policy.db")
        data = build_tar(policy, root=root, store_snapshot=snap)
    out = out_dir / f"hermes-{now.strftime('%Y%m%d-%H%M%S')}.tar.gz.age"
    encrypt(data, root / rules["age_recipient_file"].lstrip("/"), out, age)
    cutoff = time.time() - rules["archive_keep_days"] * 86400
    for old in out_dir.glob("hermes-*.tar.gz.age"):
        if old.stat().st_mtime < cutoff:
            old.unlink()
    return out


# Whose files a restore may put back. Anything else is refused.
RESTORE_OWNERS = ("hermes", "tw-policy")


def _owner(m: tarfile.TarInfo) -> tuple[int, int]:
    import grp
    import pwd
    if m.uname not in RESTORE_OWNERS:
        raise ValueError(f"{m.name} belongs to {m.uname or m.uid}; a restore puts back only {', '.join(RESTORE_OWNERS)}'s files")
    return pwd.getpwnam(m.uname).pw_uid, grp.getgrnam(m.gname or m.uname).gr_gid


def restore(policy: Policy, tar_path: Path, *, root: Path = Path("/"),
            mark_restored: Callable[[Path], dict] | None = None,
            owner: Callable[[tarfile.TarInfo], tuple[int, int]] = _owner,
            chown: Callable[[Path, int, int], None] = os.chown) -> dict:
    """Put a decrypted archive back, each file under its own owner. Refuses
    members that climb out, links, other owners, and anything the archive
    should never have held; a refusal comes before any file is written."""
    rules = policy.data["backups"]
    allowed = [inc.lstrip("/") for inc in rules["include"]] + ["srv/tw/policy"]
    restored = 0
    with tarfile.open(tar_path, "r:gz") as tar:
        members = []
        for m in tar.getmembers():
            if m.name == "MANIFEST.json":
                continue
            if m.name.startswith("/") or ".." in Path(m.name).parts:
                raise ValueError(f"{m.name} climbs out; this archive is not restored")
            if not m.isfile():
                raise ValueError(f"{m.name} is not a plain file; this archive is not restored")
            if not any(m.name == a or m.name.startswith(a + "/") for a in allowed):
                raise ValueError(f"{m.name} is outside what a backup holds; this archive is not restored")
            if excluded(m.name, False, rules) and m.name != STORE_IN_ARCHIVE:
                raise ValueError(f"{m.name} should never be in a backup; this archive is not restored")
            if any(excluded("/".join(Path(m.name).parts[:i]), True, rules) for i in range(1, len(Path(m.name).parts))):
                raise ValueError(f"{m.name} is in a folder a backup leaves out; this archive is not restored")
            members.append((m, owner(m)))
        for m, (uid, gid) in members:
            dest = root / m.name
            missing = []
            d = dest.parent
            while not d.exists():
                missing.append(d)
                d = d.parent
            for d in reversed(missing):
                d.mkdir(mode=0o700)
                chown(d, uid, gid)
            tmp = dest.with_name(dest.name + ".restoring")
            with tar.extractfile(m) as src, open(tmp, "wb") as f:
                f.write(src.read())
            chown(tmp, uid, gid)
            os.chmod(tmp, m.mode & 0o777)
            os.replace(tmp, dest)
            restored += 1
    db = root / policy.data["brokers"]["state"]["policy_db"].lstrip("/")
    for side in (db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        if side.exists():
            side.unlink()
    marked = (mark_restored or _mark_restored)(db) if db.exists() else {"killed": 0}
    return {"restored": restored, **marked}


def _mark_restored(db: Path) -> dict:
    from .policy_service import PolicyService
    svc = PolicyService(db, Policy.load())
    try:
        return svc.mark_restored("root", {})
    finally:
        svc.db.close()


def main(argv: list[str]) -> int:
    if os.geteuid() != 0:
        print("backups run as root from tw-backup.timer, and restores by Patrick with sudo")
        return 2
    policy = Policy.load()
    state = policy.data["brokers"]["state"]
    rules = policy.data["backups"]
    if argv[:1] == ["snapshot"]:
        name = datetime.now(timezone.utc).strftime("policy-%Y%m%d-%H%M.db")
        out = snapshot(Path(state["policy_db"]), Path(state["snapshots"]) / name)
        prune_snapshots(Path(state["snapshots"]), rules["snapshot_keep_hourly"], rules["snapshot_keep_daily"])
        print(f"PASS snapshot {out}")
    elif argv[:1] == ["archive"]:
        out = archive(policy)
        import grp
        import pwd
        os.chown(out, pwd.getpwnam("tw-backup").pw_uid, grp.getgrnam("tw-backup").gr_gid)
        os.chmod(out, 0o440)
        print(f"PASS archive {out} ({out.stat().st_size} bytes)")
    elif argv[:1] == ["restore"] and len(argv) == 2:
        running = [u for u in ("tw-policy", "tw-git", "tw-files", "tw-proxy", "tw-hermes-gateway")
                   if subprocess.run(["systemctl", "is-active", "--quiet", f"{u}.service"]).returncode == 0]
        user_unit = ["systemctl", "--machine=hermes@.host", "--user", "is-active", "--quiet", "hermes-gateway.service"]
        if subprocess.run(user_unit, capture_output=True).returncode == 0:
            running.append("the hermes user's own hermes-gateway unit")
        if running:
            print(f"STOP: {', '.join(running)} still running; stop them first so nothing writes while files go back")
            return 2
        try:
            out = restore(policy, Path(argv[1]))
        except ValueError as e:
            print(f"FAIL {e}")
            return 1
        print(f"PASS restored {out['restored']} files; {out['killed']} Main, publish or cutover records are dead")
    else:
        print("usage: python3 -I -m thornswood_brokers.backup snapshot|archive|restore <decrypted.tar.gz>")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
