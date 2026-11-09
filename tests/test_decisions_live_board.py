"""The live Board (Patrick, 2 Nov: "only what each is currently doing ... it's supposed to be live").

The thornswood_live plugin, in each Bot's profile, writes what that process's running turns are doing; the
reader on the VM (src/thornswood_decisions/live_reader.py) keeps only the files whose process still runs and
sends the core one row per running turn. Here: the hooks, the files, the reader and the feed end to end.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from test_decisions_api import Served

import thornswood_live as live
from thornswood_decisions import live_reader
from thornswood_decisions.feeds import Feeds

BOTS = ["bramble", "rift", "sprocket", "echo", "magpie", "wisp"]
NOW = datetime(2026, 11, 2, 20, 0, tzinfo=timezone.utc)


class Home:
    """A Hermes home with the six Bots' profiles, and the plugin's get_hermes_home pointed at one of them."""

    def __init__(self, test):
        self.root = Path(tempfile.mkdtemp())
        test.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.hh = self.root / ".hermes"
        for bot in BOTS:
            (self.hh / "profiles" / bot).mkdir(parents=True)
        self.current = self.hh / "profiles" / "rift"
        fake = types.ModuleType("hermes_constants")
        fake.get_hermes_home = lambda: self.current
        patcher = mock.patch.dict(sys.modules, {"hermes_constants": fake})
        patcher.start()
        test.addCleanup(patcher.stop)
        live._turns.clear()
        test.addCleanup(live._turns.clear)

    def file(self, bot):
        return self.hh / "profiles" / bot / live.FOLDER / f"{os.getpid()}.json"

    def written(self, bot):
        return json.loads(self.file(bot).read_text(encoding="utf-8"))


class TheHooks(unittest.TestCase):
    def setUp(self):
        self.h = Home(self)

    def test_a_turn_thinks_runs_a_tool_thinks_and_is_gone(self):
        live.on_turn_start(session_id="s1", platform="cron", user_message="secret words")
        body = self.h.written("rift")
        self.assertEqual((body["pid"], body["start"]), (os.getpid(), live.process_start()))
        self.assertEqual([(t["session_id"], t["state"], t["tool"], t["platform"]) for t in body["turns"]],
                         [("s1", "thinking", None, "cron")])
        live.on_tool_start("terminal", {"command": "rm -rf secret"}, session_id="s1", tool_call_id="c1")
        self.assertEqual(self.h.written("rift")["turns"][0]["state"], "tool")
        self.assertEqual(self.h.written("rift")["turns"][0]["tool"], "terminal")
        live.on_tool_end(tool_name="terminal", session_id="s1", tool_call_id="c1", result="secret output")
        self.assertEqual(self.h.written("rift")["turns"][0]["state"], "thinking")
        # Never a tool's arguments, its output or any words of the turn.
        self.assertNotIn("secret", self.h.file("rift").read_text(encoding="utf-8"))
        live.on_turn_end(session_id="s1", completed=True)
        self.assertFalse(self.h.file("rift").exists())

    def test_tools_at_once_say_the_newest_until_each_ends(self):
        live.on_turn_start(session_id="s1")
        live.on_tool_start(tool_name="read_file", session_id="s1", tool_call_id="a")
        live.on_tool_start(tool_name="web_search", session_id="s1", tool_call_id="b")
        self.assertEqual(self.h.written("rift")["turns"][0]["tool"], "web_search")
        live.on_tool_end(tool_name="web_search", session_id="s1", tool_call_id="b")
        self.assertEqual(self.h.written("rift")["turns"][0]["tool"], "read_file")
        live.on_tool_end(tool_name="read_file", session_id="s1")  # no call id: ended by its name
        self.assertEqual(self.h.written("rift")["turns"][0]["state"], "thinking")

    def test_a_new_turn_starts_fresh(self):
        live.on_turn_start(session_id="s1")
        live.on_tool_start(tool_name="terminal", session_id="s1", tool_call_id="a")
        first = self.h.written("rift")["turns"][0]["since"]
        with mock.patch.object(live, "_now", lambda: "2099-01-01T00:00:00+00:00"):
            live.on_turn_start(session_id="s1")
        turn = self.h.written("rift")["turns"][0]
        self.assertEqual((turn["state"], turn["tool"]), ("thinking", None))
        self.assertNotEqual(turn["since"], first)

    def test_each_profile_has_its_own_file(self):
        live.on_turn_start(session_id="s1")
        self.h.current = self.h.hh / "profiles" / "echo"
        live.on_turn_start(session_id="s2")
        self.assertEqual([t["session_id"] for t in self.h.written("rift")["turns"]], ["s1"])
        self.assertEqual([t["session_id"] for t in self.h.written("echo")["turns"]], ["s2"])
        if os.name != "nt":  # the Bots run on the VM; Windows has no such mode bits to check
            self.assertEqual(oct(self.h.file("echo").stat().st_mode & 0o777), "0o600")

    def test_a_hook_never_breaks_a_turn(self):
        self.h.current = self.h.root / "not-a-folder-file"
        self.h.current.write_text("x", encoding="utf-8")
        with self.assertLogs("thornswood_live", "WARNING"):
            live.on_turn_start(session_id="s1")
        self.assertIsNone(live.on_tool_start(tool_name="terminal", session_id="s1"))
        # Odd values are left out, never written.
        self.h.current = self.h.hh / "profiles" / "rift"
        live.on_turn_start(session_id="../../x")
        live.on_turn_start(session_id="s1", platform="<b>")
        live.on_tool_start(tool_name="rm -rf /", session_id="s1")
        turn = self.h.written("rift")["turns"]
        self.assertEqual([(t["session_id"], t["platform"], t["state"]) for t in turn], [("s1", None, "thinking")])

    def test_it_registers_four_hooks_and_no_tool(self):
        got = []
        ctx = types.SimpleNamespace(register_hook=lambda name, fn: got.append(name),
                                    register_tool=mock.Mock(side_effect=AssertionError("no tool")))
        live.register(ctx)
        self.assertEqual(got, ["pre_llm_call", "pre_tool_call", "post_tool_call", "on_session_end"])


class TheReader(unittest.TestCase):
    def setUp(self):
        self.h = Home(self)

    def write(self, bot, pid, turns, start="111"):
        folder = self.h.hh / "profiles" / bot / live.FOLDER
        folder.mkdir(exist_ok=True)
        (folder / f"{pid}.json").write_text(json.dumps({"pid": pid, "start": start, "turns": turns}), encoding="utf-8")

    def titles(self, bot, rows):
        con = sqlite3.connect(self.h.hh / "profiles" / bot / "state.db")
        con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT)")
        con.executemany("INSERT INTO sessions VALUES(?,?)", rows)
        con.commit()
        con.close()

    def test_only_turns_whose_process_still_runs(self):
        self.write("rift", 10, [{"session_id": "s1", "state": "tool", "tool": "terminal", "since": "2026-11-02T19:58:00+00:00",
                                 "platform": "cron"}])
        self.write("echo", 11, [{"session_id": "s2", "state": "thinking", "since": "2026-11-02T19:59:00+00:00"}])
        self.write("wisp", 12, [{"session_id": "s3", "state": "thinking", "since": "x"}], start="999")
        self.titles("rift", [("s1", "Rebuild the load order")])
        alive = lambda pid, start: (pid, start) in {(10, "111"), (11, "111"), (12, "111")}  # 12 is a reused pid
        snap = live_reader.snapshot(self.h.hh, BOTS, NOW, alive=alive)
        self.assertEqual(snap["source_time"], NOW.isoformat())
        self.assertEqual(snap["doing"], [
            {"bot": "rift", "title": "Rebuild the load order", "session_id": "s1", "state": "tool", "tool": "terminal",
             "since": "2026-11-02T19:58:00+00:00", "platform": "cron"},
            {"bot": "echo", "title": "", "session_id": "s2", "state": "thinking", "tool": None,
             "since": "2026-11-02T19:59:00+00:00", "platform": ""}])

    def test_nothing_running_is_no_rows(self):
        self.write("rift", 10, [])
        self.assertEqual(live_reader.snapshot(self.h.hh, BOTS, NOW, alive=lambda *_: True)["doing"], [])

    def test_odd_files_and_rows_are_skipped(self):
        folder = self.h.hh / "profiles" / "rift" / live.FOLDER
        folder.mkdir()
        (folder / "10.json").write_text("{not json", encoding="utf-8")
        (folder / ".10.1.tmp").write_text("{}", encoding="utf-8")
        (folder / "11.json").write_text(json.dumps({"pid": 99, "start": "1", "turns": []}), encoding="utf-8")
        self.write("rift", 12, [{"session_id": "s1", "state": "sleeping"}, {"session_id": "../x", "state": "thinking"},
                                {"session_id": "s2", "state": "tool", "tool": "<b>"}, "row"])
        rows = live_reader.snapshot(self.h.hh, BOTS, NOW, alive=lambda *_: True)["doing"]
        self.assertEqual([(r["session_id"], r["state"], r["tool"]) for r in rows], [("s2", "thinking", None)])

    @unittest.skipUnless(Path("/proc/self/stat").exists(), "the reader runs on the VM, whose /proc says when a process started")
    def test_the_real_process_check(self):
        start = live.process_start()
        self.assertTrue(live_reader._alive(os.getpid(), start))
        self.assertFalse(live_reader._alive(os.getpid(), "0"))
        self.assertFalse(live_reader._alive(os.getpid(), None))

    def test_it_never_writes_to_hermes(self):
        self.write("rift", 10, [{"session_id": "s1", "state": "thinking", "since": ""}])
        self.titles("rift", [("s1", "t")])
        before = {p: p.read_bytes() for p in self.h.hh.rglob("*") if p.is_file()}
        live_reader.snapshot(self.h.hh, BOTS, NOW, alive=lambda *_: True)
        self.assertEqual({p: p.read_bytes() for p in self.h.hh.rglob("*") if p.is_file()}, before)


@unittest.skipIf(os.name == "nt", "file modes are POSIX: the reader runs on the VM and checks its token is 0600")
class EndToEnd(unittest.TestCase):
    def test_the_plugin_the_reader_and_the_core(self):
        h = Home(self)
        s = Served(self)
        for bot in BOTS:
            s.w.core.bind_bot(s.w.ws, bot, bot, by="tests")
        tokens = h.root / "tokens"
        tokens.mkdir()
        token = tokens / "feed-work.token"
        token.write_text(Feeds(s.w.store).new_token(s.w.ws, "work", by="tests"), encoding="utf-8")
        os.chmod(token, 0o600)
        live.on_turn_start(session_id="s1")
        live.on_tool_start(tool_name="tw_git", session_id="s1", tool_call_id="a")
        args = ["--hermes-home", str(h.hh), "--tokens", str(tokens), "--core", f"http://127.0.0.1:{s.port}/api/v1", "--once"]
        out = []
        with mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))):
            self.assertEqual(live_reader.main(args), 0, out)
        got = Feeds(s.w.store).get(s.w.ws, "work")
        self.assertTrue(got["live"])
        self.assertEqual([(d["bot"], d["state"], d["tool"]) for d in got["work"]["doing"]], [("rift", "tool", "tw_git")])
        live.on_turn_end(session_id="s1")
        with mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))):
            self.assertEqual(live_reader.main(args), 0, out)
        self.assertEqual(Feeds(s.w.store).get(s.w.ws, "work")["work"]["doing"], [])
        self.assertEqual(out, ["PASS work: the core answered 200"] * 2)

    def test_it_sends_on_a_change_and_otherwise_once_in_each_half_minute(self):
        h = Home(self)
        sent = []
        clock = [0.0]
        snaps = iter([[], [], [{"x": 1}], [{"x": 1}], [{"x": 1}]])

        def snapshot(*a, **k):
            return {"source_time": "t", "doing": next(snaps)}

        def sleep(_):
            clock[0] += 2.0
            if len(sent) >= 3 or clock[0] > 100:
                raise StopIteration

        token = h.root / "feed-work.token"
        token.write_text("t", encoding="utf-8")
        os.chmod(token, 0o600)
        with mock.patch.object(live_reader, "snapshot", snapshot), \
                mock.patch.object(live_reader, "send", lambda core, name, tok, body: sent.append(body["doing"]) or 200), \
                mock.patch.object(live_reader.time, "monotonic", lambda: clock[0] + 1000), \
                mock.patch.object(live_reader.time, "sleep", sleep), mock.patch("builtins.print"):
            with self.assertRaises(StopIteration):
                live_reader.main(["--hermes-home", str(h.hh), "--tokens", str(h.root)])
        # The first, then the change; the unchanged ones wait for the half minute.
        self.assertEqual(sent, [[], [{"x": 1}]])


if __name__ == "__main__":
    unittest.main()
