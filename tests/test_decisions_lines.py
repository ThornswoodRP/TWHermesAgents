"""A chat's name and its list line read as Patrick wrote them (2 Nov).

On the phone, One's Bot Chat line showed "@file:AppData/Local/hermes/composer-pastes/pasted_content_...
--- At", and his Hermes's Bot Chat line was empty. Hermes keeps a pasted file as an @file: reference in
the message and the file's text under "--- Attached Context ---" (agent/context_references.py); a list
line came from the newest row whatever it held. Checks:

- a list line is the words written, else "Pasted text" or the file's name, never the reference, the
  attached text or Hermes's context warnings;
- it comes from the newest message that has words, across a Bot Chat's whole compression lineage;
- a chat Hermes has not titled is named by its first message (Hermes Desktop's own rule), sent as
  `first` only with an untitled chat, and the core keeps it only there.

Everything reads a scratch state.db in a temporary folder.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import decisions_helpers  # noqa: F401  (puts the repository on the path)
from test_decisions_own import STATE_SQL

from thornswood_decisions import own, own_connector

REF = "@file:AppData/Local/hermes/composer-pastes/pasted_content_2026-11-02_03-17-07-762_54a0c5.txt"
PASTE = f"{REF}\n\n--- Attached Context ---\n\n\U0001F4C4 {REF} (2639 tokens)\n```\nStep 1. gh pr merge 55\n```"


class Lines(unittest.TestCase):
    def test_a_line_is_the_words_else_what_was_attached(self):
        line = own_connector.line_of
        self.assertEqual(line(PASTE), "Pasted text")
        self.assertEqual(line(f"Here is the plan {REF}\n\n--- Attached Context ---\n\nsecret text"), "Here is the plan")
        self.assertEqual(line("Read @file:docs/plan.md please"), "Read please")
        self.assertEqual(line("@file:\"C:/My Files/notes.txt\""), "notes.txt")
        self.assertEqual(line("hi\n\n--- Context Warnings ---\n- @file:x: not expanded"), "hi")
        self.assertEqual(line("two\n\nlines   here"), "two lines here")
        self.assertEqual(line(""), "")


class FromStateDb(unittest.TestCase):
    def setUp(self):
        self.db = Path(tempfile.mkdtemp()) / "state.db"
        con = sqlite3.connect(self.db)
        con.executescript(STATE_SQL)
        con.executemany("INSERT INTO sessions VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
            ("s_bc", "cli", "Bot Chat", 10, 20, "compression", None, None, 0, 1),
            ("s_bc2", "cli", None, 21, 30, None, "s_bc", None, 0, 0),
            ("s_new", "desktop", None, 40, 50, None, None, None, 0, 0),
            ("s_named", "desktop", "Named", 60, 70, None, None, None, 0, 0)])
        con.executemany("INSERT INTO messages(session_id, role, content, timestamp) VALUES(?, ?, ?, ?)", [
            ("s_bc", "assistant", "Rift: load order is clean", 15),
            # The chat it goes on as holds only a row with no words yet: a list of parts with no text.
            ("s_bc2", "assistant", '[{"type": "image_url", "image_url": {"url": "x"}}]', 25),
            ("s_new", "user", PASTE, 41),
            ("s_new", "user", "Then check the backups", 42),
            ("s_new", "assistant", "Done.", 43),
            ("s_named", "user", "first words", 61)])
        con.commit()
        con.close()

    def snap(self):
        return {c["id"]: c for c in own_connector.snapshot(self.db, [], datetime(2026, 11, 2, tzinfo=timezone.utc))["chats"]}

    def test_the_bot_chat_line_comes_from_its_lineage_newest_with_words(self):
        bot = self.snap()["s_bc2"]
        self.assertTrue(bot["bot_chat"])
        self.assertEqual(bot["preview"], "Rift: load order is clean")

    def test_an_untitled_chat_is_named_by_its_first_message_with_words(self):
        chats = self.snap()
        self.assertEqual(chats["s_new"]["first"], "Pasted text")
        self.assertEqual(chats["s_new"]["title"], "")
        self.assertEqual(chats["s_new"]["preview"], "Done.")
        self.assertNotIn("first", chats["s_named"])
        page = {c["id"]: c for c in own_connector.chats_page(self.db, None, "")["chats"]}
        self.assertEqual(page["s_new"]["first"], "Pasted text")
        self.assertNotIn("first", page["s_named"])


class TheCoreKeepsFirst(unittest.TestCase):
    def test_first_is_kept_only_for_an_untitled_chat_and_cut_to_length(self):
        rows = [own._chat_row({"id": "s_a", "title": "", "last_at": "", "preview": "", "first": "x" * 500}),
                own._chat_row({"id": "s_b", "title": "Named", "last_at": "", "preview": "", "first": "ignored"}),
                own._chat_row({"id": "s_c", "title": "", "last_at": "", "preview": ""})]
        self.assertEqual(rows[0]["first"], "x" * own.FIRST)
        self.assertNotIn("first", rows[1])
        self.assertNotIn("first", rows[2])
        with self.assertRaises(own.CoreError):
            own._chat_row({"id": "s_d", "title": "", "last_at": "", "preview": "", "first": {"x": 1}})


if __name__ == "__main__":
    unittest.main()
