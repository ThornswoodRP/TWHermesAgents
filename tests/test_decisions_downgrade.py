"""The way back from a store migration, for the VM's follower (1 Nov).

When a release failed after its migrations ran, the release before it could
not open the store ("the store is at schema 6; this code knows only 5"), so
the follower could not put it back. `tw-decide downgrade --to N`, run from the
failed release, undoes the newer migrations only when that loses nothing.

Each test uses a scratch store. No test reaches a real workspace, a Bot, a
deploy, a merge or a Ship.
"""

from __future__ import annotations

import io
import re
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from decisions_helpers import World

from thornswood_decisions import cli, store as store_mod
from thornswood_decisions.schema import DOWNS, FLAGS, FLAGS_1, MIGRATIONS
from thornswood_decisions.store import CoreError, Store, _statements, connect, downgrade, schema_version

FIRST_WITH_A_WAY_BACK = 6


def at_schema(n: int) -> Path:
    """A scratch store migrated to exactly schema n."""
    path = Path(tempfile.mkdtemp()) / "core.db"
    con = sqlite3.connect(path, isolation_level=None)
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    for m in range(1, n + 1):
        for stmt in _statements(MIGRATIONS[m - 1]):
            con.execute(stmt)
    con.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (str(n),))
    con.close()
    return path


def shape(path: Path) -> list[tuple]:
    """Every table, index and trigger, as SQLite keeps them."""
    con = sqlite3.connect(path)
    try:
        rows = con.execute("SELECT type, name, tbl_name, sql FROM sqlite_master "
                           "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name").fetchall()
    finally:
        con.close()
    # A table rebuilt and renamed is kept as CREATE TABLE "name"; the same table either way.
    return [(t, n, tb, re.sub(r'^CREATE TABLE "([a-z_0-9]+)"', r"CREATE TABLE \1", q or "")) for t, n, tb, q in rows]


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class EveryMigrationHasAWayBack(unittest.TestCase):
    def test_each_new_migration_ships_its_way_back(self):
        for n in range(FIRST_WITH_A_WAY_BACK, len(MIGRATIONS) + 1):
            self.assertIn(n, DOWNS, f"migration {n} has no way back in schema.DOWNS")
        self.assertEqual(set(DOWNS) - set(range(FIRST_WITH_A_WAY_BACK, len(MIGRATIONS) + 1)), set())

    def test_each_way_back_gives_the_older_schema_exactly(self):
        for n in range(FIRST_WITH_A_WAY_BACK, len(MIGRATIONS) + 1):
            path = at_schema(n)
            con = connect(path)
            self.assertEqual(downgrade(con, n - 1)["to"], n - 1)
            self.assertEqual(schema_version(con), n - 1)
            con.close()
            self.assertEqual(shape(path), shape(at_schema(n - 1)), f"the way back from migration {n}")


class PuttingBack(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.newest = len(MIGRATIONS)

    def subscribe(self, endpoint_sha256):
        dev = self.w.device()
        self.w.store.db.execute(
            "INSERT INTO push_subscriptions(id, workspace_id, principal_id, device_id, sealed, endpoint_sha256, "
            "created_at) VALUES(?,?,?,?,?,?,'now')",
            (f"ps-{dev}", self.w.ws, self.w.patrick_id, dev, b"sealed", endpoint_sha256))

    def test_the_older_code_opens_the_store_again_with_every_row_kept(self):
        self.subscribe(None)
        tables = ("workspaces", "principals", "devices", "events", "push_subscriptions")
        before = {t: self.w.store.db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}
        self.w.close()
        code, out, err = run("--db", str(self.w.path), "downgrade", "--to", str(self.newest - 1))
        self.assertEqual(code, 0, err)
        self.assertIn(f'"to": {self.newest - 1}', out)
        with mock.patch.object(store_mod, "MIGRATIONS", MIGRATIONS[:-1]):
            older = Store(self.w.path)
        self.addCleanup(older.close)
        self.assertEqual({t: older.db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}, before)
        self.assertTrue(older.verify_events()[0])
        # The flags the older schema knows, each as it was (schema 8 adds none).
        self.assertEqual({r[0] for r in older.db.execute("SELECT name FROM flags")}, set(FLAGS))

    def test_back_to_6_own_chat_write_goes_with_schema_7(self):
        self.w.close()
        code, out, err = run("--db", str(self.w.path), "downgrade", "--to", "6")
        self.assertEqual(code, 0, err)
        with mock.patch.object(store_mod, "MIGRATIONS", MIGRATIONS[:6]):
            older = Store(self.w.path)
        self.addCleanup(older.close)
        self.assertEqual({r[0] for r in older.db.execute("SELECT name FROM flags")}, set(FLAGS_1))

    def test_the_newer_release_takes_it_forward_again(self):
        self.w.close()
        self.assertEqual(run("--db", str(self.w.path), "downgrade", "--to", str(self.newest - 1))[0], 0)
        again = Store(self.w.path)
        self.addCleanup(again.close)
        self.assertEqual(schema_version(again.db), self.newest)

    def test_a_store_holding_what_the_older_schema_cannot_is_left_alone(self):
        self.subscribe("e" * 64)
        self.w.close()
        before = shape(self.w.path)
        code, _, err = run("--db", str(self.w.path), "downgrade", "--to", "5")
        self.assertEqual(code, 1)
        self.assertIn("STOP store_written", err)
        self.assertIn("schema 6: 1 phone alerts that keep their address's SHA-256", err)
        self.assertIn("fix forward", err)
        self.assertEqual(shape(self.w.path), before)
        con = connect(self.w.path)
        self.addCleanup(con.close)
        self.assertEqual(schema_version(con), self.newest)


class ThePutBackFromOwnChat(unittest.TestCase):
    """Schema 7 (chat with Patrick's own Hermes) goes back only while nothing was sent to it."""

    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)

    def test_a_message_to_his_own_hermes_keeps_schema_7(self):
        self.w.store.db.execute(
            "INSERT INTO own_messages(id, workspace_id, gateway, text, send_key, principal_id, status, created_at, "
            "updated_at) VALUES('om1', ?, 'me', 'hello', 'k1', ?, 'queued', 'now', 'now')", (self.w.ws, self.w.patrick_id))
        self.w.close()
        code, _, err = run("--db", str(self.w.path), "downgrade", "--to", "6")
        self.assertEqual(code, 1)
        self.assertIn("schema 7: 1 messages to Patrick's own Hermes", err)

    def test_own_chat_write_on_keeps_schema_7(self):
        self.w.store.set_flag(self.w.ws, "own_chat_write", True, by="tests")
        self.w.close()
        code, _, err = run("--db", str(self.w.path), "downgrade", "--to", "6")
        self.assertEqual(code, 1)
        self.assertIn("schema 7: 1 workspaces with own_chat_write on", err)

    def test_the_tokens_go_and_it_says_how_they_come_back(self):
        from thornswood_decisions.feeds import Feeds
        tok = Feeds(self.w.store).new_token(self.w.ws, "own-me", by="tests")
        Feeds(self.w.store).new_token(self.w.ws, "work", by="tests")
        self.w.close()
        code, out, err = run("--db", str(self.w.path), "downgrade", "--to", "6")
        self.assertEqual(code, 0, err)
        self.assertIn("Install-OwnConnector.ps1 -NewToken", out)
        con = connect(self.w.path)
        self.addCleanup(con.close)
        self.assertEqual([r[0] for r in con.execute("SELECT name FROM feed_sources")], ["work"])
        con.close()
        with mock.patch("sys.stdin", io.StringIO(tok + "\n")):
            code, out, _ = run("--db", str(self.w.path), "token-known", "--workspace", "test", "--name", "own-me")
        self.assertEqual(code, 0)
        self.assertIn('"known": false', out)


class TheCommands(unittest.TestCase):
    def test_schema_says_both_numbers_without_migrating(self):
        path = at_schema(5)
        code, out, _ = run("--db", str(path), "schema")
        self.assertEqual(code, 0)
        self.assertIn('"store": 5', out)
        self.assertIn(f'"code": {len(MIGRATIONS)}', out)
        con = connect(path)
        self.addCleanup(con.close)
        self.assertEqual(schema_version(con), 5)

    def test_token_known_says_whether_the_core_keeps_that_token(self):
        from thornswood_decisions.feeds import Feeds
        w = World()
        self.addCleanup(w.close)
        tok = Feeds(w.store).new_token(w.ws, "own-me", by="tests")
        for given, want in ((tok, "true"), ("not-it", "false"), ("", "false")):
            with self.subTest(given=given), mock.patch("sys.stdin", io.StringIO(given + "\n")):
                code, out, _ = run("--db", str(w.path), "token-known", "--workspace", "test", "--name", "own-me")
                self.assertEqual(code, 0)
                self.assertIn(f'"known": {want}', out)

    def test_no_store_is_made_by_asking(self):
        path = Path(tempfile.mkdtemp()) / "core.db"
        for argv in (("schema",), ("downgrade", "--to", "5")):
            code, _, err = run("--db", str(path), *argv)
            self.assertEqual(code, 1)
            self.assertIn("STOP no_store", err)
        self.assertFalse(path.exists())

    def test_a_schema_with_no_way_back_is_refused(self):
        path = at_schema(len(MIGRATIONS))
        code, _, err = run("--db", str(path), "downgrade", "--to", str(FIRST_WITH_A_WAY_BACK - 2))
        self.assertEqual(code, 1)
        self.assertIn("STOP no_way_back", err)
        con = connect(path)
        self.addCleanup(con.close)
        self.assertEqual(schema_version(con), len(MIGRATIONS))

    def test_to_must_be_below_the_store(self):
        path = at_schema(len(MIGRATIONS))
        for to in (0, len(MIGRATIONS) + 1):
            with self.subTest(to=to):
                con = connect(path)
                with self.assertRaises(CoreError) as e:
                    downgrade(con, to)
                con.close()
                self.assertEqual(e.exception.reason, "bad_schema")


if __name__ == "__main__":
    unittest.main()
