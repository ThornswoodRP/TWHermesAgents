"""Step 1 of the decision core: backup and restore (07 section 11.1; T30).

A backup is a consistent copy with an intact event chain. A restore runs
nothing: every undelivered push and connector message is quarantined, every
session and open pairing code dies, and an answer from before the backup is
never sent again.
"""

from __future__ import annotations

import shutil
import sqlite3
import unittest

from decisions_helpers import World

from thornswood_decisions import backup
from thornswood_decisions.core import DecisionCore
from thornswood_decisions.store import CoreError, Store


class Backup(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.c = self.w.card()
        self.answered = self.w.card(title="Answered before the backup")
        self.w.core.answer(self.w.patrick(), **World.answer_args(self.answered))
        self.session = self.w.core.auth.issue_session(self.w.ws, method="test", device_id=None)
        self.w.core.auth.new_pairing_code(self.w.patrick(fresh=True), "phone", "p")
        self.dest = self.w.dir / "backups" / "core-1.db"

    def test_backup_is_a_copy_with_an_intact_chain(self):
        out = backup.backup(self.w.store, self.dest)
        self.assertTrue(self.dest.is_file())
        self.assertEqual(len(out["sha256"]), 64)
        self.assertTrue(backup.verify_file(self.dest)[0])
        con = sqlite3.connect(self.dest)
        n = con.execute("SELECT count(*) FROM decisions").fetchone()[0]
        con.close()
        self.assertEqual(n, 2)
        ok = self.w.store.db.execute("SELECT ok FROM backup_runs WHERE kind = 'backup'").fetchone()[0]
        self.assertEqual(ok, 1)

    def test_a_backup_never_overwrites(self):
        backup.backup(self.w.store, self.dest)
        with self.assertRaises(CoreError) as e:
            backup.backup(self.w.store, self.dest)
        self.assertEqual(e.exception.reason, "backup_exists")

    def test_t30_restore_runs_nothing(self):
        backup.backup(self.w.store, self.dest)
        target = self.w.dir / "restored" / "core.db"
        target.parent.mkdir()
        out = backup.restore(self.dest, target, clock=self.w.clock)
        q = out["quarantined"]
        self.assertEqual(q["connector_outbox"], 1)   # the answer's message to Bramble
        self.assertEqual(q["push_outbox"], 1)        # the open card's push
        self.assertGreaterEqual(q["sessions"], 1)
        self.assertEqual(q["pairing_codes"], 1)
        s = Store(target, clock=self.w.clock)
        self.addCleanup(s.close)
        core = DecisionCore(s)
        states = {r[0] for r in s.db.execute(
            "SELECT status FROM connector_outbox UNION SELECT status FROM push_outbox")}
        self.assertEqual(states, {"quarantined", "cancelled"})  # the answered card's push was cancelled before
        with self.assertRaises(CoreError):
            core.auth.authenticate(self.session.token)
        with self.assertRaises(CoreError) as e:
            core.confirm_effect(s.db.execute("SELECT id FROM connector_outbox").fetchone()[0],
                                outbox="connector", outcome="confirmed", detail={})
        self.assertEqual(e.exception.reason, "not_deliverable")
        ok, why = s.verify_events()
        self.assertTrue(ok, why)
        last = s.db.execute("SELECT kind FROM events ORDER BY seq DESC LIMIT 1").fetchone()[0]
        self.assertEqual(last, "store.restored")
        # the open card can still be answered, and that new answer is a new message
        p = self.w.patrick()
        r = core.answer(p, **World.answer_args(self.c, key="after-restore-1"))
        queued = s.db.execute("SELECT count(*) FROM connector_outbox WHERE status = 'queued'").fetchone()[0]
        self.assertEqual(queued, 1)
        self.assertIsNotNone(r)

    def test_restore_never_overwrites_a_store(self):
        backup.backup(self.w.store, self.dest)
        with self.assertRaises(CoreError) as e:
            backup.restore(self.dest, self.w.path)
        self.assertEqual(e.exception.reason, "store_exists")

    def test_a_tampered_backup_is_refused(self):
        backup.backup(self.w.store, self.dest)
        bad = self.w.dir / "bad.db"
        shutil.copy(self.dest, bad)
        con = sqlite3.connect(bad)
        con.execute("DROP TRIGGER events_no_update")
        con.execute("UPDATE events SET actor = 'patrick' WHERE seq = 2")
        con.commit()
        con.close()
        with self.assertRaises(CoreError) as e:
            backup.restore(bad, self.w.dir / "never.db")
        self.assertEqual(e.exception.reason, "chain_broken")
        self.assertFalse((self.w.dir / "never.db").exists())


if __name__ == "__main__":
    unittest.main()
