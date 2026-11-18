"""The Bot's displayed route follows actual main-loop calls, including resumed chats."""

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import REPO  # sets up this checkout's src
from thornswood_decisions.feed_reader import bot_route


class ActualBotRoute(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.db = self.folder / "state.db"
        with sqlite3.connect(self.db) as con:
            con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT, model_config TEXT, "
                        "started_at REAL, last_activity_at REAL)")
            con.execute("CREATE TABLE session_model_usage (session_id TEXT, model TEXT, billing_provider TEXT, "
                        "task TEXT, api_call_count INTEGER, last_seen REAL)")
        self.session("chat", "gpt-6.1-sol", "openai-codex", True, 200)

    def session(self, sid, model, provider, fallback, at):
        config = json.dumps({"gateway_runtime": {"provider": provider, "fallback_active": fallback}})
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?)", (sid, model, config, 1, at))

    def usage(self, model, at, *, sid="chat", provider="openai-codex", task="", calls=1):
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO session_model_usage VALUES (?,?,?,?,?,?)",
                        (sid, model, provider, task, calls, at))

    def test_resumed_chat_uses_actual_62_call_not_its_frozen_61_route(self):
        self.usage("gpt-6.1-sol", 100, calls=900)
        self.usage("gpt-6.2-sol", 200)
        self.assertEqual(bot_route(self.db), {"model": "gpt-6.2-sol", "provider": "openai-codex",
                                             "fallback": False, "at": "1970-01-01T00:03:20+00:00"})

    def test_auxiliary_calls_do_not_replace_the_current_main_loop(self):
        self.usage("gpt-6.2-sol", 200)
        self.usage("small-title-model", 300, task="title_generation")
        self.assertEqual(bot_route(self.db)["model"], "gpt-6.2-sol")

    def test_requested_but_unused_new_session_does_not_replace_actual_usage(self):
        self.usage("gpt-6.2-sol", 200)
        self.session("new", "claude-opus-5-5", "anthropic", False, 400)
        self.assertEqual(bot_route(self.db)["model"], "gpt-6.2-sol")

    def test_same_time_uses_the_official_rowid_tiebreak(self):
        self.usage("gpt-6.1-sol", 200)
        self.usage("gpt-6.2-sol", 200)
        self.assertEqual(bot_route(self.db)["model"], "gpt-6.2-sol")

    def test_confirmed_matching_snapshot_can_name_the_fallback(self):
        self.session("chat", "gpt-6.2-sol", "openai-codex", True, 300)
        self.usage("gpt-6.2-sol", 200)
        self.assertIs(bot_route(self.db)["fallback"], True)

    def test_stale_provider_does_not_supply_a_fallback_claim(self):
        self.session("chat", "gpt-6.2-sol", "anthropic", True, 300)
        self.usage("gpt-6.2-sol", 200)
        route = bot_route(self.db)
        self.assertEqual(route["provider"], "openai-codex")
        self.assertIs(route["fallback"], False)

    def test_non_boolean_snapshot_is_not_a_confirmed_fallback(self):
        self.session("chat", "gpt-6.2-sol", "openai-codex", "true", 300)
        self.usage("gpt-6.2-sol", 200)
        self.assertIs(bot_route(self.db)["fallback"], False)

    def test_without_main_loop_usage_the_legacy_route_still_works(self):
        self.usage("title-model", 300, task="title_generation")
        self.assertEqual(bot_route(self.db)["model"], "gpt-6.1-sol")

    def test_absent_usage_schema_preserves_the_legacy_reader(self):
        with sqlite3.connect(self.db) as con:
            con.execute("DROP TABLE session_model_usage")
        self.assertEqual(bot_route(self.db)["model"], "gpt-6.1-sol")

    def test_unknown_usage_schema_preserves_the_legacy_reader(self):
        with sqlite3.connect(self.db) as con:
            con.execute("ALTER TABLE session_model_usage RENAME COLUMN task TO future_task")
        self.assertEqual(bot_route(self.db)["model"], "gpt-6.1-sol")

    def test_unreadable_parent_gives_no_route_on_python_312(self):
        # Python 3.12 Path.exists propagates EACCES; only later Python suppresses all OSError.
        with mock.patch.object(Path, "exists", side_effect=PermissionError("metadata unavailable")):
            self.assertIsNone(bot_route(self.db))

    def test_corrupt_store_gives_no_route(self):
        self.db.write_bytes(b"not a sqlite database")
        self.assertIsNone(bot_route(self.db))

    def test_invalid_call_time_gives_no_current_route(self):
        self.usage("gpt-6.2-sol", float("inf"))
        self.assertIsNone(bot_route(self.db))

    def test_metadata_read_does_not_rewrite_history_or_frozen_settings(self):
        self.usage("gpt-6.2-sol", 200)
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        names = sorted(p.name for p in self.folder.iterdir())
        bot_route(self.db)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), names)


if __name__ == "__main__":
    unittest.main()
