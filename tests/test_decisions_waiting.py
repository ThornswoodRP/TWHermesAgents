"""A message to his own Hermes that waits for the turn already running in that chat says so (Patrick, 2 Nov:
his Reply to One took 3 min 40 s while his Desktop ran a long turn in the same chat).

Hermes runs one turn at a time in a conversation: a new turn waits for the chat's turn lease
(agent/turn_facade_lease.py:297, up to 30 minutes), keyed by the chat's first compression ancestor
(hermes_state_compression.py:683) in session_turn_leases. Checks:
- turn_held reads that lease, read only: held before this turn started, by the chat or its compression
  ancestor, and not run out; a fork's own lease is its own; anything unreadable is "not known to wait";
- a turn waiting on it says "waiting", and "thinking" once the lease is free, then what it does;
- the core keeps "waiting" as a state of the live line.
Everything runs in a scratch folder with a stand-in for `hermes`; nothing reaches a real Hermes.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from test_decisions_own import state_db

from thornswood_decisions import own_connector
from thornswood_decisions.own import DOING_STATES

LEASES = "CREATE TABLE session_turn_leases (conversation_id TEXT PRIMARY KEY, holder TEXT NOT NULL, " \
         "acquired_at REAL NOT NULL, expires_at REAL NOT NULL)"

HERMES = textwrap.dedent("""\
    import json, sqlite3, sys, time
    db = sys.argv[1]
    print(json.dumps({"type": "system", "subtype": "init", "session_id": "s_plan"}), flush=True)
    time.sleep(3)
    con = sqlite3.connect(db)
    con.execute("DELETE FROM session_turn_leases")
    con.commit()
    time.sleep(2.5)
    print(json.dumps({"type": "text", "text": "done"}), flush=True)
    print(json.dumps({"type": "result", "session_id": "s_plan", "exit_code": 0}), flush=True)
""")


class Held(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.db = state_db(self.dir)
        con = sqlite3.connect(self.db)
        con.execute(LEASES)
        con.commit()
        con.close()

    def lease(self, key, acquired, expires):
        con = sqlite3.connect(self.db)
        con.execute("INSERT OR REPLACE INTO session_turn_leases VALUES(?, 'pid=1:turn=x:platform=desktop', ?, ?)",
                    (key, acquired, expires))
        con.commit()
        con.close()

    def test_the_lease_of_the_chat_or_its_compression_ancestor(self):
        now = time.time()
        self.assertFalse(own_connector.turn_held(self.db, "s_plan", now))
        self.lease("s_plan", now - 60, now + 240)
        self.assertTrue(own_connector.turn_held(self.db, "s_plan", now))
        self.assertFalse(own_connector.turn_held(self.db, "s_plan", now - 120), "taken after this turn began: its own")
        self.lease("s_plan", now - 400, now - 100)
        self.assertFalse(own_connector.turn_held(self.db, "s_plan", now), "run out")
        self.lease("s_old", now - 60, now + 240)
        self.assertTrue(own_connector.turn_held(self.db, "s_old2", now), "a compression continuation shares it")
        self.lease("s_plan", now - 60, now + 240)
        self.assertFalse(own_connector.turn_held(self.db, "s_sub", now), "a delegate child has its own")
        self.assertFalse(own_connector.turn_held(self.dir / "missing.db", "s_plan", now))

    def test_a_turn_says_it_waits_and_then_what_it_does(self):
        now = time.time()
        self.lease("s_plan", now - 60, now + 240)
        (self.dir / "hermes.py").write_text(HERMES, encoding="utf-8")
        h = own_connector.Hermes([sys.executable, str(self.dir / "hermes.py"), str(self.db)], [], self.dir, "One",
                                 state_db=self.db)
        said = []
        outcome = h._run([*h.cmd], "s_plan", lambda state, tool: said.append(state))
        self.assertEqual(outcome[0], "confirmed")
        self.assertEqual(said[:2], ["thinking", "waiting"])
        self.assertIn("thinking", said[2:])
        self.assertEqual(said[-1], "writing")
        self.assertLess(said.index("waiting"), len(said) - 2)

    def test_the_core_keeps_waiting_as_a_state(self):
        self.assertIn("waiting", DOING_STATES)


if __name__ == "__main__":
    unittest.main()
