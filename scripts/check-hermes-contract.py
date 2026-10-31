#!/usr/bin/env python3
"""Check the VM's Hermes against the pinned contract. Reads only; changes nothing.

Run on the VM as the hermes user, with the system python3, from this repo's
release folder:

    sudo -u hermes python3 -I scripts/check-hermes-contract.py

It checks three things against thornswood_decisions/contracts.json:

1. the installed Hermes is the pinned commit (from `hermes --version` and the
   install's own git checkout or install stamp);
2. the six Bot profiles are there under ~/.hermes/profiles;
3. exactly one live room is named "Thornswood Workbench", its members are the
   six Bots, and it prints the room's id, which the core binds to.

The room store is opened read-only. Each finding prints as PASS or STOP; any
STOP exits 1. `--json` prints the same as one JSON object.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from thornswood_decisions import contracts  # noqa: E402


def installed_hermes(hermes_cmd: list[str]) -> dict:
    """Version, install folder and commit of the Hermes that `hermes_cmd` runs."""
    out = subprocess.run(hermes_cmd + ["--version"], capture_output=True, text=True, timeout=120)
    text = out.stdout
    found = {"version": None, "install_dir": None, "commit": None, "raw": text.strip()}
    if m := re.search(r"Hermes Agent v(\S+)", text):
        found["version"] = m.group(1)
    if m := re.search(r"^Install directory:\s*(.+?)\s*$", text, re.M):
        found["install_dir"] = m.group(1)
    if found["install_dir"]:
        root = Path(found["install_dir"])
        if (root / ".git").exists():
            git = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                 capture_output=True, text=True, timeout=60)
            if git.returncode == 0:
                found["commit"] = git.stdout.strip()
        stamp = root / "install-stamp.json"
        if not found["commit"] and stamp.is_file():
            data = json.loads(stamp.read_text(encoding="utf-8-sig"))
            found["commit"] = data.get("commit") if isinstance(data, dict) else None
    return found


def workbench_rooms(db_path: Path, name: str) -> list[dict]:
    """Live rooms with this name, read from the gateway's room store without writing."""
    uri = db_path.resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        rows = con.execute(
            "SELECT room_id, name, members_json, authority_gateway_id, authority_epoch, revision "
            "FROM hosted_rooms WHERE name = ? AND disbanded_at IS NULL", (name,)).fetchall()
    finally:
        con.close()
    rooms = []
    for room_id, rname, members_json, gateway_id, epoch, revision in rows:
        members = json.loads(members_json)
        rooms.append({"room_id": room_id, "name": rname, "authority_gateway_id": gateway_id,
                      "authority_epoch": epoch, "revision": revision,
                      "profiles": sorted(str(m.get("profile")) for m in members if isinstance(m, dict))})
    return rooms


def run(home: Path, hermes_cmd: list[str], contract: dict | None = None) -> dict:
    contract = contract or contracts.load()
    report: dict = {"pass": [], "stop": [], "facts": {}}

    def ok(msg):
        report["pass"].append(msg)

    def stop(msg):
        report["stop"].append(msg)

    pinned = contract["hermes"]
    try:
        h = installed_hermes(hermes_cmd)
    except (OSError, subprocess.SubprocessError) as exc:
        h = {"version": None, "install_dir": None, "commit": None, "raw": ""}
        stop(f"could not run {' '.join(hermes_cmd)} --version: {exc}")
    report["facts"]["hermes"] = {k: h[k] for k in ("version", "install_dir", "commit")}
    if h["commit"] == pinned["commit"]:
        ok(f"Hermes is the pinned commit {pinned['commit'][:12]} (v{h['version']})")
    elif h["commit"]:
        stop(f"Hermes is commit {h['commit'][:12]} (v{h['version']}), not the pinned "
             f"{pinned['commit'][:12]} (v{pinned['version']}); re-pin from that source before building on it")
    elif h["version"]:
        stop(f"Hermes printed v{h['version']} but its commit could not be read from {h['install_dir']}")
    elif not report["stop"]:
        stop(f"{' '.join(hermes_cmd)} --version printed no Hermes version")

    profiles_dir = home / "profiles"
    have = sorted(p.name for p in profiles_dir.iterdir() if p.is_dir()) if profiles_dir.is_dir() else []
    report["facts"]["profiles"] = have
    want = contract["profiles"]["ids"]
    missing = [p for p in want if p not in have]
    if missing:
        stop(f"profiles missing under {profiles_dir}: {', '.join(missing)}")
    else:
        ok(f"the six profiles are there: {', '.join(want)}")
    extra = [p for p in have if p not in want]
    if extra:
        report["facts"]["other_profiles"] = extra

    room = contract["room"]
    db = home / "shared-state.db"
    if not db.is_file():
        stop(f"no room store at {db}")
    else:
        try:
            rooms = workbench_rooms(db, room["name"])
        except sqlite3.Error as exc:
            rooms = None
            stop(f"could not read the room store {db} read-only: {exc}")
        if rooms is not None:
            report["facts"]["rooms"] = rooms
            if len(rooms) != 1:
                stop(f"{len(rooms)} live rooms are named {room['name']!r}; the core binds to exactly one")
            elif rooms[0]["profiles"] != sorted(room["members"]):
                stop(f"{room['name']!r} seats {rooms[0]['profiles']}, not {sorted(room['members'])}")
            else:
                ok(f"{room['name']!r} is room {rooms[0]['room_id']} with the six Bots")
    return report


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home", default=str(Path.home() / ".hermes"), help="Hermes home (default ~/.hermes)")
    ap.add_argument("--hermes", default="hermes", help="the hermes command (default: hermes on PATH)")
    ap.add_argument("--json", action="store_true", help="print one JSON object")
    args = ap.parse_args(argv)
    report = run(Path(args.home), [args.hermes])
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for line in report["pass"]:
            print(f"PASS {line}")
        for line in report["stop"]:
            print(f"STOP {line}")
        for key, value in report["facts"].items():
            print(f"FACT {key}: {json.dumps(value)}")
    return 1 if report["stop"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
