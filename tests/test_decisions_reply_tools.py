"""A reply keeps the tools it ran (TWHermesAgents #106).

Checks the connector's page against a scratch state.db with Hermes's tool
columns: each reply carries the tools its turn ran, in order, with what each was
given and what it returned, a failed one marked; a tool called just above a
page's first reply still lands on it; a turn that ran tools and wrote no words
puts them on nothing; interim words keep their tools for the reply after them;
a reply with more tools than a page carries keeps the newest and counts the
rest; an older Hermes without the columns pages as before. Then the core's
check of a page's tools and the app's drawing of them. Nothing reaches a real
Hermes.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from helpers import REPO

from thornswood_decisions import own_connector
from thornswood_decisions.own import clean_page
from thornswood_decisions.store import CoreError

STATE = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT, started_at REAL NOT NULL,
  last_activity_at REAL, end_reason TEXT, parent_session_id TEXT, model_config TEXT,
  archived INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
  content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL NOT NULL,
  active INTEGER NOT NULL DEFAULT 1, _compressed_summary INTEGER NOT NULL DEFAULT 0);
"""


def call(cid: str, name: str, **args) -> dict:
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class Chat:
    def __init__(self, folder: Path):
        self.path = folder / "state.db"
        self.con = sqlite3.connect(self.path)
        self.con.executescript(STATE)
        self.con.execute("INSERT INTO sessions VALUES('s_t', 'cli', 'Tools', 1, 900, NULL, NULL, NULL, 0, 0)")
        self.t = 10

    def add(self, role: str, content=None, calls=None, answers=None, name=None) -> int:
        self.t += 1
        cur = self.con.execute(
            "INSERT INTO messages(session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp) "
            "VALUES('s_t',?,?,?,?,?,?)", (role, content, answers, json.dumps(calls) if calls else None, name, self.t))
        self.con.commit()
        return cur.lastrowid

    def result(self, cid: str, name: str, content: str) -> int:
        return self.add("tool", content, answers=cid, name=name)


class ThePage(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.c = Chat(Path(self.dir.name))

    def page(self, before=None, n=50):
        return own_connector.messages_page(self.c.path, "s_t", before, n)

    def test_a_reply_carries_the_tools_its_turn_ran_in_order(self):
        self.c.add("user", "what is on the board?")
        self.c.add("assistant", "", calls=[call("c1", "terminal", command="gh issue list --limit 5")])
        self.c.result("c1", "terminal", json.dumps({"output": "#1 a\n#2 b", "exit_code": 0}))
        self.c.add("assistant", None, calls=[call("c2", "read_file", path="/workspace/x.md"),
                                            call("c3", "terminal", command="false")])
        self.c.result("c2", "read_file", "the file")
        self.c.result("c3", "terminal", json.dumps({"output": "", "exit_code": 1}))
        reply = self.c.add("assistant", "Two issues are open.")
        msgs = self.page()["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
        self.assertNotIn("tools", msgs[0])
        tools = msgs[1]["tools"]
        self.assertEqual(msgs[1]["mid"], reply)
        self.assertEqual([t["name"] for t in tools], ["terminal", "read_file", "terminal"])
        self.assertEqual(tools[0]["preview"], "gh issue list --limit 5")
        self.assertIn('"command": "gh issue list --limit 5"', tools[0]["args"])
        self.assertIn("#1 a", tools[0]["result"])
        self.assertEqual([t["error"] for t in tools], [False, False, True])
        self.assertEqual(tools[1]["preview"], "/workspace/x.md")
        self.assertNotIn("tools_more", msgs[1])

    def test_each_reply_has_only_its_own_turns_tools(self):
        self.c.add("user", "one")
        self.c.add("assistant", "", calls=[call("a", "web_search", query="first")])
        self.c.result("a", "web_search", "found")
        self.c.add("assistant", "first answer")
        self.c.add("user", "two")
        self.c.add("assistant", "second answer, no tools")
        self.c.add("user", "three")
        self.c.add("assistant", "", calls=[call("b", "memory", action="add")])
        self.c.result("b", "memory", '{"error": "full"}')
        self.c.add("assistant", "third answer")
        msgs = self.page()["messages"]
        by = {m["text"]: m for m in msgs}
        self.assertEqual([t["preview"] for t in by["first answer"]["tools"]], ["first"])
        self.assertNotIn("tools", by["second answer, no tools"])
        self.assertEqual(by["third answer"]["tools"][0]["error"], True)

    def test_a_turn_without_words_puts_its_tools_on_nothing(self):
        self.c.add("user", "do it")
        self.c.add("assistant", "", calls=[call("x", "terminal", command="ls")])
        self.c.result("x", "terminal", "a b")
        self.c.add("user", "are you there?")
        self.c.add("assistant", "yes")
        msgs = self.page()["messages"]
        self.assertFalse(any("tools" in m for m in msgs))

    def test_words_written_between_tools_keep_their_tools_for_the_reply_after_them(self):
        self.c.add("user", "go")
        self.c.add("assistant", "Looking now.", calls=[call("k", "search_files", pattern="TODO")])
        self.c.result("k", "search_files", "3 hits")
        self.c.add("assistant", "Found three.")
        msgs = self.page()["messages"]
        self.assertEqual([m["text"] for m in msgs], ["go", "Looking now.", "Found three."])
        self.assertNotIn("tools", msgs[1])
        self.assertEqual([t["name"] for t in msgs[2]["tools"]], ["search_files"])

    def test_the_first_reply_of_an_older_page_finds_the_tools_above_it(self):
        self.c.add("user", "first")
        self.c.add("assistant", "", calls=[call("p", "terminal", command="date")])
        self.c.result("p", "terminal", "today")
        first = self.c.add("assistant", "It is today.")
        for i in range(6):
            self.c.add("user" if i % 2 == 0 else "assistant", f"later {i}")
        newest = self.page(n=4)
        older = self.page(before=newest["messages"][0]["mid"], n=4)
        reply = next(m for m in older["messages"] if m["mid"] == first)
        self.assertEqual([t["preview"] for t in reply["tools"]], ["date"])
        # And the page that starts at that reply, with the user's message cut off above it.
        alone = self.page(before=first + 1, n=1)
        self.assertEqual([t["preview"] for t in alone["messages"][0]["tools"]], ["date"])

    def test_a_long_turn_keeps_its_newest_tools_and_counts_the_rest(self):
        self.c.add("user", "lots")
        for i in range(30):
            self.c.add("assistant", "", calls=[call(f"t{i}", "read_file", path=f"/w/{i}")])
            self.c.result(f"t{i}", "read_file", "x" * 5000)
        self.c.add("assistant", "done")
        reply = self.page()["messages"][-1]
        self.assertEqual(len(reply["tools"]), own_connector.TOOLS_SHOWN)
        self.assertEqual(reply["tools_more"], 30 - own_connector.TOOLS_SHOWN)
        self.assertEqual(reply["tools"][-1]["preview"], "/w/29")
        self.assertEqual(len(reply["tools"][0]["result"]), own_connector.TOOL_RESULT)
        self.assertEqual(clean_page("messages", self.page())["messages"][-1]["tools_more"], 6)

    def test_an_older_hermes_without_tool_columns_pages_as_before(self):
        con = sqlite3.connect(Path(self.dir.name) / "old.db")
        con.executescript("CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT, end_reason TEXT, "
                          "started_at REAL); CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                          "session_id TEXT, role TEXT, content TEXT, timestamp REAL, active INTEGER DEFAULT 1);"
                          "INSERT INTO sessions VALUES('s_t', NULL, NULL, 1);"
                          "INSERT INTO messages(session_id, role, content, timestamp) VALUES('s_t','user','hi',1);"
                          "INSERT INTO messages(session_id, role, content, timestamp) VALUES('s_t','assistant','yo',2);")
        con.commit()
        con.close()
        msgs = own_connector.messages_page(Path(self.dir.name) / "old.db", "s_t", None)["messages"]
        self.assertEqual([m["text"] for m in msgs], ["hi", "yo"])
        self.assertFalse(any("tools" in m for m in msgs))


class TheCore(unittest.TestCase):
    def page(self, **tool):
        t = {"name": "terminal", "preview": "ls", "args": "{}", "result": "a", "error": False} | tool
        return {"messages": [{"mid": 1, "role": "assistant", "text": "hi", "at": "", "tools": [t]}], "more": False}

    def test_a_pages_tools_are_kept_cut_to_size(self):
        got = clean_page("messages", self.page(args="g" * 5000, result="r" * 5000, preview="p" * 500))
        t = got["messages"][0]["tools"][0]
        self.assertEqual((len(t["args"]), len(t["result"]), len(t["preview"])), (600, 1500, 160))
        self.assertIsNone(clean_page("messages", self.page(result=None))["messages"][0]["tools"][0]["result"])

    def test_tools_that_are_not_hermess_are_refused(self):
        for bad in ({"name": "a b"}, {"name": None}, {"error": "yes"}):
            with self.assertRaises(CoreError, msg=bad):
                clean_page("messages", self.page(**bad))
        page = self.page()
        page["messages"][0]["tools"] = [page["messages"][0]["tools"][0]] * 25
        with self.assertRaises(CoreError):
            clean_page("messages", page)

    def test_his_own_message_carries_no_tools(self):
        page = self.page()
        page["messages"][0]["role"] = "user"
        self.assertNotIn("tools", clean_page("messages", page)["messages"][0])


class TheApp(unittest.TestCase):
    def test_each_chat_hands_a_replys_tools_to_its_bubble(self):
        js = (REPO / "src/thornswood_decisions/app/app.js").read_text(encoding="utf-8")
        self.assertIn("!m.mine && m.tools && m.tools.length ? toolLines(m.tools, m.toolsMore) : null", js)
        self.assertEqual(js.count("tools: m.tools, toolsMore: m.tools_more"), 3)
        for word in ("'Given'", "'Returned'", "terminal: 'Ran a command'"):
            self.assertIn(word, js)
        css = (REPO / "src/thornswood_decisions/app/app.css").read_text(encoding="utf-8")
        self.assertIn('.tool[open] .tool-show::after { content: "Hide"; }', css)


if __name__ == "__main__":
    unittest.main()
