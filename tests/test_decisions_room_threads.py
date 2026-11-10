"""Threads in a room (TWHermesAgents #108, from the mock-up's Workbench room: its threads along the top with
their counts, and "Reply in this thread" on a message's menu).

Checks:
- the feed reader reads each thread from Hermes's own room store, read only: its count, who started it and
  with what words, and when its last message came; newest first, at most ROOM_THREADS; each message carries
  its thread;
- a page of one thread holds only that thread's messages and pages back by the room's own sequence;
- the core takes a room's threads from the feed and checks them, and the room's view has them;
- a page of a thread is asked of the Bots' connector and read by it; one that holds another thread's message
  is refused; only a room has threads, and only by Hermes's own thread ids;
- a message sent into a thread keeps it: the connector hands it to the room helper, which sends it in that
  thread as Desktop's Reply does; without one, a message starts its own thread as before;
- migration 26's way back refuses while a message is on its way into a thread.
Everything runs in a scratch test workspace; nothing reaches a real Hermes.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from decisions_helpers import World
from test_decisions_chat import ROOM, flags, seat
import test_decisions_chat_attach as attach

from thornswood_decisions import feed_reader
from thornswood_decisions.chat import Chat
from thornswood_decisions.feeds import Feeds
from thornswood_decisions.own import PAGES
from thornswood_decisions.store import CoreError, downgrade

OTHER_ROOM = "00000000-0000-4000-8000-000000000002"
NOW = datetime(2026, 11, 10, 3, 0, tzinfo=timezone.utc)


def shared_state(folder: Path) -> Path:
    """A room store as Hermes keeps it (gateway/hosted_rooms.py hosted_room_events), with three threads."""
    db = folder / "shared-state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE hosted_room_events (room_id TEXT, seq INTEGER, event_id TEXT, kind TEXT, "
                "actor_json TEXT, authority_epoch INTEGER, payload_json TEXT, created_at REAL)")
    rows = [
        (1, "message.user", "Patrick", {"text": "Which night for the rehearsal?", "thread_id": "tw-chat.msg_a"}),
        (2, "message.member", "Bramble", {"text": "Thursday works.", "thread_id": "tw-chat.msg_a"}),
        (3, "message.user", "Patrick", {"text": "Load order is broken again", "thread_id": "tw-chat.msg_b"}),
        (4, "room.joined", "Rift", {"thread_id": "tw-chat.msg_b"}),
        (5, "message.member", "Rift", {"text": "Looking now.", "thread_id": "tw-chat.msg_b"}),
        (6, "message.member", "Echo", {"text": "Also Thursday for me.", "thread_id": "tw-chat.msg_a"}),
        (7, "message.member", "Wisp", {"text": "no thread on this one"}),
        (8, "message.user", "Patrick", {"text": "Ship it", "thread_id": "desk-7f3a"}),
    ]
    for seq, kind, who, payload in rows:
        con.execute("INSERT INTO hosted_room_events VALUES(?,?,?,?,?,?,?,?)",
                    (ROOM, seq, f"e{seq}", kind, json.dumps({"display_name": who}), 1, json.dumps(payload),
                     1_794_000_000 + seq))
    con.execute("INSERT INTO hosted_room_events VALUES(?,?,?,?,?,?,?,?)",
                (ROOM, 9, "e9", "message.member", "{}", 1, "not json", 1_794_000_009))
    con.execute("INSERT INTO hosted_room_events VALUES(?,?,?,?,?,?,?,?)",
                (OTHER_ROOM, 1, "o1", "message.user", json.dumps({"display_name": "Patrick"}), 1,
                 json.dumps({"text": "elsewhere", "thread_id": "tw-chat.msg_a"}), 1_794_000_001))
    con.commit()
    con.close()
    return db


class TheReader(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.db = shared_state(self.dir)

    def test_each_thread_with_its_count_start_and_last_message_newest_first(self):
        snap = feed_reader.room_snapshot(self.db, ROOM, NOW)
        self.assertEqual([(t["thread"], t["count"], t["by"], t["title"], t["last_seq"]) for t in snap["threads"]], [
            ("desk-7f3a", 1, "Patrick", "Ship it", 8),
            ("tw-chat.msg_a", 3, "Patrick", "Which night for the rehearsal?", 6),
            ("tw-chat.msg_b", 2, "Patrick", "Load order is broken again", 5)])
        self.assertEqual(snap["threads"][1]["at"], datetime.fromtimestamp(1_794_000_006, timezone.utc).isoformat())
        self.assertEqual([(e["seq"], e["thread"]) for e in snap["events"]],
                         [(1, "tw-chat.msg_a"), (2, "tw-chat.msg_a"), (3, "tw-chat.msg_b"), (5, "tw-chat.msg_b"),
                          (6, "tw-chat.msg_a"), (7, ""), (8, "desk-7f3a")])
        con = feed_reader._ro(self.db)
        self.addCleanup(con.close)
        self.assertEqual([t["thread"] for t in feed_reader.room_threads(con, ROOM, 2)], ["desk-7f3a", "tw-chat.msg_a"])
        both = feed_reader.rooms_snapshot(self.db, [ROOM, OTHER_ROOM], NOW)
        self.assertEqual([[t["thread"] for t in r["threads"]] for r in both["rooms"]],
                         [["desk-7f3a", "tw-chat.msg_a", "tw-chat.msg_b"], ["tw-chat.msg_a"]])

    def test_a_page_of_one_thread_holds_only_its_messages_and_pages_back(self):
        page = feed_reader.room_page(self.db, ROOM, None, 2, thread="tw-chat.msg_a")
        self.assertEqual(([e["seq"] for e in page["events"]], page["more"]), ([2, 6], True))
        self.assertEqual({e["thread"] for e in page["events"]}, {"tw-chat.msg_a"})
        page = feed_reader.room_page(self.db, ROOM, 2, 2, thread="tw-chat.msg_a")
        self.assertEqual(([e["text"] for e in page["events"]], page["more"]), (["Which night for the rehearsal?"], False))
        self.assertEqual([e["seq"] for e in feed_reader.room_page(self.db, ROOM, None, 50)["events"]],
                         [1, 2, 3, 5, 6, 7, 8], "the whole room as before")
        self.assertNotIn("event_id", feed_reader.room_page(self.db, ROOM, None, 50)["events"][0])


class TheCore(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        seat(self.w)
        flags(self.w, "mobile_chat_write", "mobile_read")
        self.chat = Chat(self.w.store)
        self.f = Feeds(self.w.store)
        self.room_tok = self.f.new_token(self.w.ws, "room", by="tests")
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.db = shared_state(self.dir)

    def put(self, body):
        return self.f.put(self.w.ws, "room", self.room_tok, body)

    def snap(self):
        s = feed_reader.rooms_snapshot(self.db, [ROOM], NOW)
        return json.loads(json.dumps(s))

    def test_the_rooms_view_has_its_threads_and_each_message_its_thread(self):
        self.put(self.snap())
        v = self.chat.view(self.w.patrick(), ROOM)
        self.assertEqual([(t["thread"], t["count"], t["title"]) for t in v["threads"]],
                         [("desk-7f3a", 1, "Ship it"), ("tw-chat.msg_a", 3, "Which night for the rehearsal?"),
                          ("tw-chat.msg_b", 2, "Load order is broken again")])
        self.assertEqual([m["thread"] for m in v["history"]][:2], ["tw-chat.msg_a", "tw-chat.msg_a"])
        self.assertNotIn("threads", self.chat.view(self.w.patrick(), "rift"), "a Bot's chat has no threads")

    def test_threads_the_core_refuses(self):
        good = self.snap()
        t0 = good["rooms"][0]["threads"][0]
        for bad in ([dict(t0, thread="../x")], [dict(t0, thread="")], [dict(t0, count=0)], [dict(t0, count=True)],
                    [dict(t0, last_seq="8")], "x", [t0] * 21):
            body = json.loads(json.dumps(good))
            body["rooms"][0]["threads"] = bad
            with self.assertRaises(CoreError) as e:
                self.put(body)
            self.assertEqual(e.exception.reason, "bad_feed", bad)
        body = json.loads(json.dumps(good))
        body["rooms"][0]["threads"] = [dict(t0, title="x" * 900)]
        self.put(body)
        self.assertEqual(len(self.chat.view(self.w.patrick(), ROOM)["threads"][0]["title"]), 200)

    def connector(self):
        from thornswood_decisions import connector
        chat, ws, tok = self.chat, self.w.ws, self.f.new_token(self.w.ws, "connector", by="tests")

        class Direct:
            def post(me, path, body):
                try:
                    if path == "/connector/pages":
                        return 200, chat.page_requests(ws, tok)
                    return 200, chat.page_post(ws, tok, json.loads(json.dumps(body)))
                except CoreError as exc:
                    return exc.status, {"error": exc.reason}
        return connector.Connector(Direct(), None, say=lambda _: None, hermes_home=self.dir), tok

    def tearDown(self):
        with PAGES.cond:
            PAGES.held.clear()
            PAGES.asked.clear()

    def test_a_page_of_one_thread_is_read_by_the_connector(self):
        p = self.w.patrick()
        conn, tok = self.connector()
        self.assertEqual(self.chat.page(p, ROOM, thread="tw-chat.msg_b"), {"pending": True})
        self.assertEqual(conn.pages(), 1)
        page = self.chat.page(p, ROOM, thread="tw-chat.msg_b")
        self.assertEqual([(e["seq"], e["text"], e["thread"]) for e in page["events"]],
                         [(3, "Load order is broken again", "tw-chat.msg_b"), (5, "Looking now.", "tw-chat.msg_b")])
        self.chat.page(p, ROOM)
        conn.pages()
        self.assertEqual(len(self.chat.page(p, ROOM)["events"]), 7, "the whole room is its own page")
        for target, thread in (("rift", "tw-chat.msg_b"), (ROOM, "../x"), (ROOM, ""), (ROOM, 7)):
            with self.assertRaises(CoreError) as e:
                self.chat.page(p, target, thread=thread)
            self.assertEqual(e.exception.reason, "bad_thread", (target, thread))

    def test_a_page_of_a_thread_with_another_threads_message_is_refused(self):
        self.chat.page(self.w.patrick(), ROOM, thread="tw-chat.msg_a")
        _, tok = self.connector()
        [req] = self.chat.page_requests(self.w.ws, tok)["pages"]
        self.assertEqual((req["kind"], req["q"]), ("room_thread", "tw-chat.msg_a"))
        stray = {"seq": 3, "kind": "message.user", "actor": "Patrick", "text": "x", "at": "", "thread": "tw-chat.msg_b"}
        with self.assertRaises(CoreError) as e:
            self.chat.page_post(self.w.ws, tok, {"id": req["id"], "target": ROOM,
                                                 "page": {"events": [stray], "more": False}})
        self.assertEqual(e.exception.reason, "bad_page")

    def test_a_message_into_a_thread_keeps_it(self):
        p = self.w.patrick()
        r = self.chat.send(p, ROOM, "Thursday it is", "room-thread-0001", thread="tw-chat.msg_a")
        row = self.w.store.db.execute("SELECT thread_id FROM chat_messages WHERE id = ?", (r["message_id"],)).fetchone()
        self.assertEqual(row[0], "tw-chat.msg_a")
        again = self.chat.send(p, ROOM, "Thursday it is", "room-thread-0001", thread="tw-chat.msg_a")
        self.assertEqual(again["message_id"], r["message_id"])
        with self.assertRaises(CoreError) as e:
            self.chat.send(p, ROOM, "Thursday it is", "room-thread-0001", thread="tw-chat.msg_b")
        self.assertEqual(e.exception.reason, "key_reused")
        plain = self.chat.send(p, ROOM, "a new thread", "room-thread-0002")
        mine = {m["id"]: m.get("thread") for m in self.chat.view(p, ROOM)["mine"]}
        self.assertEqual(mine, {r["message_id"]: "tw-chat.msg_a", plain["message_id"]: None})
        for target, thread in (("rift", "tw-chat.msg_a"), (ROOM, "a b"), (ROOM, "x" * 129), (ROOM, 5)):
            with self.assertRaises(CoreError) as e:
                self.chat.send(p, target, "no", f"room-thread-01{len(str(thread)):02}", thread=thread)
            self.assertEqual(e.exception.reason, "bad_thread", (target, thread))
        self.assertNotIn("thread", self.chat.view(p, "rift")["mine"][0] if self.chat.view(p, "rift")["mine"] else {})
        tok = self.f.new_token(self.w.ws, "connector", by="tests")
        flags(self.w, "connector_effects")
        # One room message at a time, in order: the one into the thread first, then the one of its own.
        (item,) = self.chat.claim(self.w.ws, tok)["items"]
        self.assertEqual((item["id"], item["thread"]), (r["message_id"], "tw-chat.msg_a"))
        self.chat.result(self.w.ws, tok, item["id"], "confirmed")
        (item,) = self.chat.claim(self.w.ws, tok)["items"]
        self.assertEqual(item["id"], plain["message_id"])
        self.assertNotIn("thread", item)

    def test_the_way_back_refuses_while_a_message_is_on_its_way_into_a_thread(self):
        r = self.chat.send(self.w.patrick(), ROOM, "Thursday", "room-thread-0003", thread="tw-chat.msg_a")
        with self.assertRaises(CoreError) as e:
            downgrade(self.w.store.db, 27)
        self.assertIn("schema 28: 1 messages on their way into a room's thread", str(e.exception))
        self.w.store.db.execute("UPDATE chat_messages SET status = 'sent' WHERE id = ?", (r["message_id"],))
        self.assertEqual(downgrade(self.w.store.db, 27)["to"], 27)


class ToTheRoom(attach.TheConnector):
    """The Bots' connector hands the thread to the room helper with the message."""

    def test_a_message_goes_to_the_helper_with_its_thread(self):
        a = self.chat.send(self.p, ROOM, "in the thread", "room-thread-0101", thread="tw-chat.msg_a")["message_id"]
        b = self.chat.send(self.p, ROOM, "a thread of its own", "room-thread-0102")["message_id"]
        self.c.once()
        self.c.once()
        reqs = {x["helper"]["event_id"]: x["helper"] for x in self.lines() if x.get("helper", {}).get("op") == "send"}
        self.assertEqual(reqs[f"tw-chat.{a}"]["thread_id"], "tw-chat.msg_a")
        self.assertNotIn("thread_id", reqs[f"tw-chat.{b}"])


class TheRoomHelper(attach.TheHelper):
    """room_send.py sends it in that thread, as Desktop's Reply in thread does."""

    def test_groups_send_carries_the_thread(self):
        out, rpcs = self.run_room({"op": "send", "room_id": ROOM, "event_id": "tw-chat.msg_9", "text": "yes",
                                   "thread_id": "tw-chat.msg_a"})
        self.assertEqual(out["sent"], True)
        (send,) = [r["params"] for r in rpcs if r["method"] == "groups.send"]
        self.assertEqual((send["event_id"], send["payload"]["thread_id"]), ("tw-chat.msg_9", "tw-chat.msg_a"))
        out, rpcs = self.run_room({"op": "send", "room_id": ROOM, "event_id": "tw-chat.msg_10", "text": "new"})
        (send,) = [r["params"] for r in rpcs if r["method"] == "groups.send" and r["params"]["event_id"] == "tw-chat.msg_10"]
        self.assertEqual(send["payload"]["thread_id"], "tw-chat.msg_10", "its own thread, as before")


# The inherited checks run in their own files; here only this file's own.
for _name in [n for n in dir(attach.TheConnector) if n.startswith("test_")]:
    setattr(ToTheRoom, _name, None)
for _name in [n for n in dir(attach.TheHelper) if n.startswith("test_")]:
    setattr(TheRoomHelper, _name, None)


if __name__ == "__main__":
    unittest.main()
