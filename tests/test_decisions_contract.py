"""Step 0 of the decision core: the pinned Hermes contract (07 section 11.2).

The contract must agree with the policy (the same six Bots, the same room),
and the VM check must stop on anything that differs from the pin, while
writing nothing.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from helpers import REPO, policy

from thornswood_decisions import contracts

_spec = importlib.util.spec_from_file_location("check_hermes_contract", REPO / "scripts" / "check-hermes-contract.py")
check_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_script)

SIX = ["bramble", "rift", "sprocket", "echo", "magpie", "wisp"]


class Pinned(unittest.TestCase):
    def setUp(self):
        self.c = contracts.load()

    def test_contract_agrees_with_the_policy(self):
        self.assertEqual(contracts.check(self.c, policy().data), [])

    def test_pinned_version_and_commit(self):
        h = self.c["hermes"]
        self.assertEqual(h["version"], "0.22.5")
        self.assertEqual(h["tag"], "v0.22.5")
        self.assertRegex(h["commit"], r"^[0-9a-f]{40}$")

    def test_profile_ids_are_ones_hermes_accepts(self):
        for pid in SIX:
            self.assertTrue(contracts.profile_id_ok(self.c, pid), pid)
        for bad in ("Bramble", "-rift", "a" * 65, "wisp/../x", ""):
            self.assertFalse(contracts.profile_id_ok(self.c, bad), bad)

    def test_a_seventh_bot_or_a_wrong_room_is_a_stop(self):
        c = copy.deepcopy(self.c)
        c["profiles"]["ids"].append("atlas")
        self.assertTrue(any("not the policy's Bots" in p for p in contracts.check(c, policy().data)))
        c = copy.deepcopy(self.c)
        c["room"]["members"] = SIX[:5]
        self.assertTrue(any("seats in" in p for p in contracts.check(c, policy().data)))
        c = copy.deepcopy(self.c)
        c["hermes"]["commit"] = "bf0203a7"
        self.assertTrue(any("40-character" in p for p in contracts.check(c, policy().data)))


class VmCheck(unittest.TestCase):
    """scripts/check-hermes-contract.py against a made-up Hermes home."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / ".hermes"
        self.install = self.tmp / "install"
        self.install.mkdir()
        self.c = contracts.load()
        self.commit = self.c["hermes"]["commit"]
        for p in SIX:
            (self.home / "profiles" / p).mkdir(parents=True)
        self.db = self.home / "shared-state.db"
        con = sqlite3.connect(self.db)
        con.execute("""CREATE TABLE hosted_rooms (room_id TEXT PRIMARY KEY, name TEXT NOT NULL,
            members_json TEXT NOT NULL, authority_gateway_id TEXT NOT NULL,
            authority_epoch INTEGER NOT NULL DEFAULT 1, next_seq INTEGER NOT NULL DEFAULT 1,
            event_bytes INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL, updated_at REAL NOT NULL, disbanded_at REAL)""")
        con.commit()
        con.close()
        self.add_room("room-wb", "Thornswood Workbench", SIX)
        self.stamp(self.commit)
        self.fake = self.tmp / "fake_hermes.py"
        self.fake.write_text(textwrap.dedent(f"""\
            import sys
            if sys.argv[1:] == ["--version"]:
                print("Hermes Agent v0.22.5 (2026.10.22) · stable")
                print("Install directory: {self.install.as_posix()}")
                print("Python: 3.12.3")
            """), encoding="utf-8")

    def add_room(self, room_id, name, profiles, disbanded=None):
        con = sqlite3.connect(self.db)
        members = [{"member_id": f"m-{p}", "profile": p, "display_name": p.title()} for p in profiles]
        con.execute("INSERT INTO hosted_rooms(room_id, name, members_json, authority_gateway_id, created_at, "
                    "updated_at, disbanded_at) VALUES(?,?,?,?,?,?,?)",
                    (room_id, name, json.dumps(members), "gw-1", 1.0, 1.0, disbanded))
        con.commit()
        con.close()

    def stamp(self, commit):
        (self.install / "install-stamp.json").write_text(json.dumps({"commit": commit}), encoding="utf-8")

    def run_check(self):
        return check_script.run(self.home, [sys.executable, str(self.fake)], self.c)

    def test_all_pinned_passes_and_names_the_room(self):
        r = self.run_check()
        self.assertEqual(r["stop"], [])
        self.assertEqual(len(r["pass"]), 3)
        self.assertEqual(r["facts"]["rooms"][0]["room_id"], "room-wb")
        self.assertEqual(r["facts"]["hermes"]["commit"], self.commit)

    def test_another_commit_is_a_stop(self):
        self.stamp("c" * 40)
        r = self.run_check()
        self.assertTrue(any("not the pinned" in s for s in r["stop"]), r["stop"])

    def test_a_missing_profile_is_a_stop(self):
        (self.home / "profiles" / "wisp").rmdir()
        r = self.run_check()
        self.assertTrue(any("profiles missing" in s and "wisp" in s for s in r["stop"]), r["stop"])

    def test_two_workbench_rooms_is_a_stop(self):
        self.add_room("room-wb2", "Thornswood Workbench", SIX)
        r = self.run_check()
        self.assertTrue(any("2 live rooms" in s for s in r["stop"]), r["stop"])

    def test_a_disbanded_twin_is_ignored(self):
        self.add_room("room-old", "Thornswood Workbench", SIX, disbanded=2.0)
        self.assertEqual(self.run_check()["stop"], [])

    def test_wrong_members_is_a_stop(self):
        con = sqlite3.connect(self.db)
        con.execute("DELETE FROM hosted_rooms")
        con.commit()
        con.close()
        self.add_room("room-wb", "Thornswood Workbench", SIX[:5])
        r = self.run_check()
        self.assertTrue(any("seats" in s for s in r["stop"]), r["stop"])

    def test_the_check_writes_nothing(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        files_before = sorted(p.name for p in self.home.iterdir())
        self.run_check()
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), files_before)


if __name__ == "__main__":
    unittest.main()
