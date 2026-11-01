"""Off-site copies of every backup, from the VM, with no other machine involved.

Two kinds of backup leave the VM for Google Drive (Patrick, 31 Oct, #2862):

  the VM's own   hermes-YYYYMMDD-HHMMSS.tar.gz.age, written to /srv/tw/backups
                 each night at 3:30 AM Mountain by tw-backup (backup.py),
                 encrypted to Patrick's age public key;
  the box's      thornswood-YYYYMMDD-HHMMSS.zip.enc, the game server's
                 nightly, encrypted on the box with its passphrase and checked
                 there before it is sent. The box drops each one here as the
                 tw-boxdrop user, over SFTP, into a jail that can only add a
                 file: it cannot list, read, rename, overwrite or delete one
                 (deploy/sshd/51-tw-boxdrop.conf), so a stolen box cannot reach
                 the copies it already sent. After the file it drops
                 <name>.done, holding "<sha256> <size>", which says the file is
                 whole.

Neither passphrase nor age key is ever on the VM, so every copy here and on
Drive stays encrypted.

  intake   (root, ExecStartPre=+ of tw-offsite.service) moves each dropped
           file whose .done marker matches it out of the jail into
           /srv/tw/boxbackups, where only tw-offsite works on it. A file whose
           marker does not match goes to /srv/tw/boxdrop/rejected, kept for a
           look, and the run fails.
  copy     (tw-offsite) copies both folders to Drive with rclone, deletes
           Drive copies older than the policy's days, prunes the box's copies
           here, and fails when the newest of either kind is older than the
           policy's hours: a missed night is said, never passed over.

Before this, One pulled both (Fetch-Backups.ps1 and Pull-HermesBackup.ps1).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from thornswood_guard.policy import Policy

HERMES_NAME = re.compile(r"^hermes-(\d{8})-(\d{6})\.tar\.gz\.age$")
BOX_NAME = re.compile(r"^thornswood-(\d{8})-(\d{6})\.zip\.enc$")
MARK = re.compile(r"^([0-9a-f]{64}) ([0-9]{1,12})\s*$")


# rclone's exit code for "directory not found" (rclone docs, Exit code).
RCLONE_DIR_NOT_FOUND = 3


def rules(policy: Policy) -> dict:
    return policy.data["backups"]["offsite"]


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def intake(drop: Path, store: Path, rejected: Path, *, owner: tuple[int, int] | None = None) -> tuple[list[str], list[str]]:
    """Moves each complete drop into the store. Returns (taken, problems).

    Only a name the box gives a backup is ever looked at, and only once its
    marker is there: a file still arriving has no marker yet, so it waits for
    the next run, which its marker's own arrival starts (tw-offsite.path)."""
    taken, problems = [], []
    for mark in sorted(drop.glob("*.done")):
        name = mark.name[: -len(".done")]
        f = drop / name
        if not BOX_NAME.match(name):
            problems.append(f"{mark.name} names no backup the box makes; set aside")
            _set_aside(mark, rejected)
            continue
        m = MARK.match(mark.read_text(encoding="ascii", errors="replace"))
        if not m or not f.is_file():
            problems.append(f"{name}: its marker is not '<sha256> <size>' or the file is not there; set aside")
            _set_aside(mark, rejected)
            if f.exists():
                _set_aside(f, rejected)
            continue
        size = f.stat().st_size
        if size != int(m.group(2)) or _sha256(f) != m.group(1):
            problems.append(f"{name} is not the file its marker names ({size} bytes); set aside")
            _set_aside(f, rejected)
            _set_aside(mark, rejected)
            continue
        dest = store / name
        if dest.exists():
            # The same backup sent twice: the one already here is kept.
            if _sha256(dest) != m.group(1):
                problems.append(f"{name} was sent again with other bytes; the first is kept, the second set aside")
                _set_aside(f, rejected)
            else:
                f.unlink()
            mark.unlink()
            continue
        tmp = store / (name + ".in")
        shutil.copyfile(f, tmp)
        if owner:
            os.chown(tmp, *owner)
        os.chmod(tmp, 0o440)
        os.replace(tmp, dest)
        f.unlink()
        mark.unlink()
        taken.append(name)
    return taken, problems


def _set_aside(p: Path, rejected: Path) -> None:
    rejected.mkdir(mode=0o700, exist_ok=True)
    os.replace(p, rejected / f"{p.name}.{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}")


def newest_age_hours(folder: Path, pattern: re.Pattern, now: float) -> float | None:
    """Hours since the newest backup of that name was written here. The
    file's own time, not its name: the box names its backups in its own
    clock, and what matters is when the copy arrived."""
    named = [p for p in folder.iterdir() if pattern.match(p.name)] if folder.is_dir() else []
    if not named:
        return None
    newest = max(named, key=lambda p: p.name)
    return (now - newest.stat().st_mtime) / 3600


def prune(folder: Path, pattern: re.Pattern, keep_days: int, keep_newest: int, now: float) -> list[str]:
    """Removes copies older than keep_days, never the newest keep_newest."""
    if not folder.is_dir():
        return []
    named = sorted((p for p in folder.iterdir() if pattern.match(p.name)), key=lambda p: p.name, reverse=True)
    gone = []
    for p in named[keep_newest:]:
        if now - p.stat().st_mtime > keep_days * 86400:
            p.unlink()
            gone.append(p.name)
    return gone


def copy(policy: Policy, *, run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
         now: float | None = None) -> tuple[list[str], list[str]]:
    """rclone copy and Drive prune for both kinds, then the freshness check.
    Returns (lines, problems)."""
    r = rules(policy)
    now = time.time() if now is None else now
    lines, problems = [], []
    conf = ["--config", r["rclone_config"]]
    kinds = (
        ("the VM's", Path(policy.data["brokers"]["state"]["backups"]), r["hermes_folder"], "hermes-*.tar.gz.age",
         HERMES_NAME, r["drive_keep_days"]),
        ("the box's", Path(r["box_store"]), r["box_folder"], "thornswood-*.zip.enc", BOX_NAME, r["box_drive_keep_days"]),
    )
    signed_in = Path(r["rclone_config"]).is_file()
    if not signed_in:
        problems.append(f"no Drive sign-in at {r['rclone_config']} yet, so nothing went to Drive (README, Off-site copies)")
    for label, folder, remote_folder, glob, pattern, keep in kinds:
        dest = f"{r['remote']}:{remote_folder}"
        if signed_in:
            c = run(["rclone", "copy", str(folder), dest, "--include", glob, "--no-traverse", "--quiet", *conf],
                    capture_output=True, text=True)
            if c.returncode != 0:
                problems.append(f"rclone could not copy {label} backups to {dest} (exit {c.returncode}): {(c.stderr or '').strip()[-300:]}")
            else:
                d = run(["rclone", "delete", dest, "--include", glob, "--min-age", f"{keep}d", "--quiet", *conf],
                        capture_output=True, text=True)
                if d.returncode == RCLONE_DIR_NOT_FOUND:
                    # The folder is made by the first copy that has a file in it. Until
                    # then there is nothing on Drive to keep or to delete (1 Nov: the
                    # box's folder, before the box's first drop).
                    lines.append(f"PASS nothing of {label} is on Drive in {dest} yet, so nothing there is older than {keep} days")
                elif d.returncode != 0:
                    problems.append(f"rclone could not delete {label} Drive copies older than {keep} days (exit {d.returncode})")
                else:
                    lines.append(f"PASS {label} backups are on Drive in {dest}, kept {keep} days")
        age = newest_age_hours(folder, pattern, now)
        if age is None:
            problems.append(f"{label} backups: there is none in {folder}")
        elif age > r["fresh_hours"]:
            problems.append(f"{label} newest backup arrived {age:.0f} hours ago, over {r['fresh_hours']}: last night's did not arrive")
        else:
            lines.append(f"PASS {label} newest backup arrived {age:.0f} hours ago")
    gone = prune(Path(r["box_store"]), BOX_NAME, r["box_keep_days"], r["box_keep_newest"], now)
    if gone:
        lines.append(f"PASS removed {len(gone)} of the box's copies here older than {r['box_keep_days']} days")
    return lines, problems


def main(argv: list[str]) -> int:
    policy = Policy.load()
    r = rules(policy)
    if argv[:1] == ["intake"]:
        if os.geteuid() != 0:
            print("intake runs as root, before tw-offsite.service copies")
            return 2
        import grp
        import pwd
        owner = (pwd.getpwnam("tw-offsite").pw_uid, grp.getgrnam("tw-offsite").gr_gid)
        taken, problems = intake(Path(r["box_drop"]) / "in", Path(r["box_store"]), Path(r["box_drop"]) / "rejected", owner=owner)
        for n in taken:
            print(f"PASS took {n} from the box's drop")
        for p in problems:
            print(f"FAIL {p}")
        return 1 if problems else 0
    if argv[:1] == ["copy"]:
        lines, problems = copy(policy)
        for line in lines:
            print(line)
        for p in problems:
            print(f"FAIL {p}")
        return 1 if problems else 0
    print("usage: tw-offsite intake|copy")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
