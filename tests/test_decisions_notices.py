"""A row Hermes put in a chat by itself is a notice, never Patrick's message.

Patrick's screenshot, 11 Nov 8:25 PM: the Bots list showed Magpie's line as "You: [IMPORTANT: Background
process proc_...", a notice the gateway put in Magpie's Bot Chat when a background command matched its
watch. Hermes (v0.23.0) keeps such a turn as a user row marked with display_kind (internal_notification,
process_complete or async_delegation_complete), and a row it never shows as display_kind "hidden". These
check that every reader of Hermes's chats (the Bots' feed, the own connectors' lists and pages) says
"notice" for the marked rows, in Hermes's own short words where it kept them, never uses one as a chat's
line or name, and leaves hidden rows out; and that the core keeps a notice out of a Bot's line.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from decisions_helpers import World
from test_decisions_chat import BOTS, seat

from thornswood_decisions import feed_reader, own_connector
from thornswood_decisions.chat import Chat
from thornswood_decisions.feeds import Feeds
from thornswood_decisions.own import clean_page

# Hermes v0.23.0's columns for these (hermes_state_common.py messages: display_kind TEXT, display_metadata TEXT).
STATE_SQL = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL DEFAULT 'cli', title TEXT, started_at REAL NOT NULL,
  last_activity_at REAL, end_reason TEXT, parent_session_id TEXT, model_config TEXT, model TEXT,
  archived INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
  content TEXT, timestamp REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1, display_kind TEXT, display_metadata TEXT);
"""
WATCH = '[IMPORTANT: Background process proc_c2c7919eddb2 matched watch pattern "done".\nCommand: make\nMatched output:\ndone]'
DONE = "[IMPORTANT: Background process proc_a exited (exit code 0).\nOutput:\nok]"
DONE_CARD = json.dumps({"display_text": "Background process proc_a finished (exit 0)"})


def state_db(folder: Path, chat: str, title: str, rows: list[tuple]) -> Path:
    path = folder / "state.db"
    con = sqlite3.connect(path)
    con.executescript(STATE_SQL)
    con.execute("INSERT INTO sessions(id, title, started_at, last_activity_at, hidden) VALUES(?,?,?,?,?)",
                (chat, title, 1762900000.0, 1762900100.0, 1 if title == "Bot Chat" else 0))
    con.executemany("INSERT INTO messages(session_id, role, content, timestamp, display_kind, display_metadata) "
                    "VALUES(?,?,?,?,?,?)", [(chat, *r) for r in rows])
    con.commit()
    con.close()
    return path


class Notices(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_a_bots_feed_says_notice_for_what_hermes_put_in_and_leaves_out_hidden_rows(self):
        prof = self.dir / ".hermes" / "profiles" / "magpie"
        prof.mkdir(parents=True)
        state_db(prof, "bc", "Bot Chat", [
            ("user", "Is CI green?", 1762900001.0, None, None),
            ("assistant", "It is.", 1762900002.0, None, None),
            ("user", WATCH, 1762900003.0, "internal_notification", None),
            ("user", DONE, 1762900004.0, "process_complete", DONE_CARD),
            ("user", "scaffolding the model reads", 1762900005.0, "hidden", None)])
        now = datetime(2026, 11, 12, 3, 0, tzinfo=timezone.utc)
        snap = feed_reader.bots_snapshot(self.dir / ".hermes", list(BOTS), now)
        magpie = next(b for b in snap["bots"] if b["bot"] == "magpie")
        self.assertEqual([(m["role"], m["text"]) for m in magpie["messages"]], [
            ("user", "Is CI green?"), ("assistant", "It is."), ("notice", WATCH),
            ("notice", "Background process proc_a finished (exit 0)")])
        # The core takes the feed, and the Bot's line in the Bots list is the newest thing said, never a notice.
        w = World()
        self.addCleanup(w.close)
        seat(w)
        f = Feeds(w.store)
        f.put(w.ws, "bots", f.new_token(w.ws, "bots", by="tests"), snap)
        line = next(b for b in Chat(w.store).summary(w.patrick())["bots"] if b["bot"] == "magpie")["last"]
        self.assertEqual((line["role"], line["text"]), ("assistant", "It is."))
        view = Chat(w.store).view(w.patrick(), "magpie")
        self.assertEqual([m["role"] for m in view["history"]], ["user", "assistant", "notice", "notice"])

    def test_a_bot_chat_holding_only_a_notice_has_no_line(self):
        prof = self.dir / ".hermes" / "profiles" / "magpie"
        prof.mkdir(parents=True)
        state_db(prof, "bc", "Bot Chat", [("user", WATCH, 1762900003.0, "internal_notification", None)])
        snap = feed_reader.bots_snapshot(self.dir / ".hermes", list(BOTS), datetime.now(timezone.utc))
        w = World()
        self.addCleanup(w.close)
        seat(w)
        f = Feeds(w.store)
        f.put(w.ws, "bots", f.new_token(w.ws, "bots", by="tests"), snap)
        line = next(b for b in Chat(w.store).summary(w.patrick())["bots"] if b["bot"] == "magpie")["last"]
        self.assertIsNone(line)

    def test_a_page_says_notice_and_the_core_takes_it(self):
        db = state_db(self.dir, "s1", "Fix the build", [
            ("user", "Run the build", 1762900001.0, None, None),
            ("user", DONE, 1762900002.0, "process_complete", DONE_CARD),
            ("user", "hidden", 1762900003.0, "hidden", None),
            ("assistant", "It passed.", 1762900004.0, None, None)])
        page = own_connector.messages_page(db, "s1", None)
        self.assertEqual([(m["mid"], m["role"], m["text"]) for m in page["messages"]], [
            (1, "user", "Run the build"), (2, "notice", "Background process proc_a finished (exit 0)"),
            (4, "assistant", "It passed.")])
        self.assertEqual([m["role"] for m in clean_page("messages", page)["messages"]], ["user", "notice", "assistant"])

    def test_an_own_chats_line_and_name_are_never_a_notice(self):
        db = state_db(self.dir, "s1", None, [
            ("user", WATCH, 1762900001.0, "internal_notification", None),
            ("user", "Plan the week", 1762900002.0, None, None),
            ("assistant", "Three things.", 1762900003.0, None, None),
            ("user", DONE, 1762900004.0, "process_complete", DONE_CARD)])
        snap = own_connector.snapshot(db, ["s1"], datetime.now(timezone.utc))
        chat = next(c for c in snap["chats"] if c["id"] == "s1")
        self.assertEqual((chat["preview"], chat["first"]), ("Three things.", "Plan the week"))
        opened = next(o for o in snap["open"] if o["id"] == "s1")
        self.assertEqual([m["role"] for m in opened["messages"]], ["notice", "user", "assistant", "notice"])

    def test_a_store_from_before_hermes_marked_rows_reads_as_before(self):
        path = self.dir / "state.db"
        con = sqlite3.connect(path)
        con.executescript(STATE_SQL.replace(", display_kind TEXT, display_metadata TEXT", ""))
        con.execute("INSERT INTO sessions(id, title, started_at) VALUES('s1', 'Old', 1.0)")
        con.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES('s1', 'user', 'hello', 2.0)")
        con.commit()
        con.close()
        page = own_connector.messages_page(path, "s1", None)
        self.assertEqual([(m["role"], m["text"]) for m in page["messages"]], [("user", "hello")])


if __name__ == "__main__":
    unittest.main()
