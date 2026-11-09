"""Full history of Patrick's own chats (step 2, plan Part 4).

Checks the connector's pages against a scratch state.db (500 messages paged
back to the first, in order, with no gaps and no repeats; the chat list paged
and searched by title; a compressed chat read across its lineage and followed
on under its new id; a chat Hermes no longer has said as gone), the core's side
(a page asked for, handed to that gateway's connector on its next claim, posted
back and held in memory; a chat id answers only on the gateway that listed it;
another gateway's token and the Bots' connector refused; nothing of a page in
the store's file), a resume of a deleted chat refused with no new chat, and the
routes. Everything runs in a scratch test workspace; nothing reaches a real
Hermes.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from decisions_helpers import World
from test_decisions_api import Served
from test_decisions_own import HERMES, READY, SID, STATE_SQL, Gateways, on, passkey, state_db

from thornswood_decisions import own_connector
from thornswood_decisions.feeds import Feeds
from thornswood_decisions.own import PAGES, OwnChat, clean_page
from thornswood_decisions.store import CoreError


def long_chat(folder: Path, n: int = 500) -> Path:
    """A state.db with one chat of `n` messages, a tool message and an inactive one among them."""
    path = folder / "long.db"
    con = sqlite3.connect(path)
    con.executescript(STATE_SQL)
    con.execute("INSERT INTO sessions VALUES('s_long', 'cli', 'A long chat', 1, 9000, NULL, NULL, NULL, 0, 0)")
    for i in range(n):
        con.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES(?,?,?,?)",
                    ("s_long", "user" if i % 2 == 0 else "assistant", f"message {i}", 10 + i))
        if i == 100:
            con.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES('s_long', 'tool', 'x', 1)")
        if i == 200:
            con.execute("INSERT INTO messages(session_id, role, content, timestamp, active) "
                        "VALUES('s_long', 'user', 'rewound', 1, 0)")
    for i in range(120):
        con.execute("INSERT INTO sessions VALUES(?, 'cli', ?, 1, ?, NULL, NULL, NULL, 0, 0)",
                    (f"s_{i:03d}", f"Chat {i}" + (" backup" if i % 10 == 0 else ""), 100 + i))
    con.commit()
    con.close()
    return path


class Pages(unittest.TestCase):
    """The connector's own reads, straight from a scratch state.db."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_five_hundred_messages_page_back_to_the_first_in_order_with_no_gaps_or_repeats(self):
        db = long_chat(self.dir)
        seen, before = [], None
        while True:
            page = own_connector.messages_page(db, "s_long", before)
            self.assertLessEqual(len(page["messages"]), 50)
            seen = page["messages"] + seen
            if not page["more"]:
                break
            before = page["messages"][0]["mid"]
        self.assertEqual([m["text"] for m in seen], [f"message {i}" for i in range(500)])
        mids = [m["mid"] for m in seen]
        self.assertEqual(mids, sorted(set(mids)))
        self.assertEqual(clean_page("messages", page)["messages"][0]["text"], "message 0")

    def test_the_chat_list_pages_back_and_shows_what_hermes_search_found(self):
        db = long_chat(self.dir)
        first = own_connector.chats_page(db, None, "")
        self.assertEqual((len(first["chats"]), first["more"]), (50, True))
        seen, before = list(first["chats"]), first["chats"][-1]["last_at"]
        while before:
            page = own_connector.chats_page(db, before, "")
            seen += page["chats"]
            before = page["chats"][-1]["last_at"] if page["more"] else None
        self.assertEqual(len(seen), 121)
        self.assertEqual(len({c["id"] for c in seen}), 121)
        # What Hermes's own search found, in its order: shown newest first, with the words it matched.
        hits = [{"id": f"s_{i:03d}", "snippet": "" if i else "the >backup< plan"} for i in range(0, 120, 10)]
        found = own_connector.chats_page(db, None, "backup", found=hits)
        self.assertEqual([c["title"] for c in found["chats"]][:2], ["Chat 110 backup", "Chat 100 backup"])
        self.assertEqual((len(found["chats"]), found["more"]), (12, False))
        self.assertEqual(found["chats"][-1]["preview"], "the >backup< plan")
        self.assertEqual(own_connector.chats_page(db, None, "backup", found=[])["chats"], [])
        self.assertEqual(own_connector.chats_page(db, None, "backup")["chats"], [], "no search, nothing found")

    def test_a_compressed_chat_reads_across_its_lineage_and_says_what_it_goes_on_as(self):
        db = state_db(self.dir)
        con = sqlite3.connect(db)
        con.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES('s_old', 'user', 'before', 55)")
        con.commit()
        con.close()
        old = own_connector.messages_page(db, "s_old", None)
        # By Hermes's own message ids, never by time: "before" was written last.
        self.assertEqual([m["text"] for m in old["messages"]], ["carry on", "before"])
        self.assertEqual(old["continued_as"], "s_old2")
        self.assertIsNone(own_connector.messages_page(db, "s_old2", None)["continued_as"])

    def test_a_chat_hermes_no_longer_has_is_gone(self):
        db = state_db(self.dir)
        page = own_connector.messages_page(db, "s_deleted", None)
        self.assertEqual((page["gone"], page["messages"]), (True, []))

    def test_a_page_fits_under_the_cores_size(self):
        db = long_chat(self.dir, 60)
        con = sqlite3.connect(db)
        con.execute("UPDATE messages SET content = ?", ("x" * 7000,))
        con.commit()
        con.close()
        body = own_connector.fitted_page(db, {"id": "pg_aaaaaaaaaaaa", "kind": "messages", "chat": "s_long",
                                              "before": None})
        self.assertLessEqual(len(body), own_connector.FEED_BYTES)
        page = json.loads(body)["page"]
        self.assertTrue(page["more"])


class Core(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.g = Gateways(self.w)
        self.g.hello("one")
        self.g.hello("me")
        self.g.feed("one")
        self.own = OwnChat(self.w.store)
        self.p = passkey(self.w)

    def hand(self, gw="one"):
        return self.own.claim(self.w.ws, gw, self.g.tok[gw], 0, True)["pages"]

    def test_a_page_is_asked_for_handed_to_its_connector_posted_back_and_held(self):
        self.assertEqual(self.own.page(self.p, "one", "messages", chat=SID), {"pending": True})
        [req] = self.hand()
        self.assertEqual((req["kind"], req["chat"], req["before"]), ("messages", SID, None))
        self.assertEqual(self.hand(), [], "handed out again only after 30 seconds")
        self.assertEqual(self.hand("me"), [], "never to the other gateway")
        page = {"messages": [{"mid": 7, "role": "user", "text": "hello", "at": ""}], "more": False}
        self.assertEqual(self.own.page_post(self.w.ws, "one", self.g.tok["one"], {"id": req["id"], "page": page}),
                         {"ok": True})
        got = self.own.page(self.p, "one", "messages", chat=SID)
        self.assertEqual((got["pending"], got["messages"][0]["text"]), (False, "hello"))
        with self.assertRaises(CoreError) as e:
            self.own.page_post(self.w.ws, "one", self.g.tok["one"], {"id": req["id"], "page": page})
        self.assertEqual(e.exception.reason, "not_asked")

    def test_the_newest_page_is_read_again_after_20_seconds_and_an_older_one_is_kept_10_minutes(self):
        for before in (None, "50"):
            self.own.page(self.p, "one", "messages", chat=SID, before=before)
        for req in self.hand():
            self.own.page_post(self.w.ws, "one", self.g.tok["one"], {"id": req["id"], "page": {
                "messages": [], "more": False}})
        self.w.clock.advance(seconds=21)
        self.assertEqual(self.own.page(self.p, "one", "messages", chat=SID), {"pending": True})
        self.assertFalse(self.own.page(self.p, "one", "messages", chat=SID, before="50")["pending"])
        self.w.clock.advance(minutes=10)
        self.assertEqual(self.own.page(self.p, "one", "messages", chat=SID, before="50"), {"pending": True})

    def test_the_answer_comes_within_the_wait(self):
        # The connector's requests come on a connection of their own, as each
        # request does in api.py. One SQLite connection shared by two threads
        # misreads rows (sqlite3 shares its cached statement between them),
        # which once read own_chat_write as off here.
        theirs = OwnChat(self.w.other().s)

        def connector():
            for _ in range(50):
                reqs = theirs.claim(self.w.ws, "one", self.g.tok["one"], 0, True)["pages"]
                if reqs:
                    theirs.page_post(self.w.ws, "one", self.g.tok["one"], {"id": reqs[0]["id"], "page": {
                        "messages": [], "more": False}})
                    return
                threading.Event().wait(0.05)
        t = threading.Thread(target=connector)
        t.start()
        got = self.own.page(self.p, "one", "messages", chat=SID, before="40", wait=5)
        t.join()
        self.assertEqual(got["pending"], False)

    def test_a_chat_id_answers_only_on_the_gateway_that_listed_it(self):
        with self.assertRaises(CoreError) as e:
            self.own.page(self.p, "me", "messages", chat=SID)
        self.assertEqual((e.exception.status, e.exception.reason), (404, "not_listed"))
        with self.assertRaises(CoreError) as e:
            self.own.page(self.p, "one", "messages", chat="s_never_listed")
        self.assertEqual(e.exception.reason, "not_listed")

    def test_a_chat_on_a_chats_page_or_one_it_goes_on_as_is_listed(self):
        self.own.page(self.p, "one", "chats", q="old")
        [req] = self.hand()
        self.own.page_post(self.w.ws, "one", self.g.tok["one"], {"id": req["id"], "page": {
            "chats": [{"id": "s_found", "title": "Old chat", "last_at": "", "preview": ""}], "more": False}})
        self.assertEqual(self.own.page(self.p, "one", "chats", q="old")["chats"][0]["id"], "s_found")
        self.assertEqual(self.own.page(self.p, "one", "messages", chat="s_found"), {"pending": True})
        [req] = self.hand()
        self.own.page_post(self.w.ws, "one", self.g.tok["one"], {"id": req["id"], "page": {
            "messages": [], "more": False, "continued_as": "s_found2"}})
        self.assertEqual(self.own.page(self.p, "one", "messages", chat="s_found2"), {"pending": True})

    def test_the_first_chats_page_is_the_list_already_sent(self):
        got = self.own.page(self.p, "one", "chats")
        self.assertEqual((got["pending"], got["chats"][0]["id"], got["more"]), (False, SID, False))
        self.assertEqual(self.hand(), [])

    def test_another_gateways_token_and_the_bots_connector_are_refused(self):
        self.own.page(self.p, "one", "messages", chat=SID)
        [req] = self.hand()
        body = {"id": req["id"], "page": {"messages": [], "more": False}}
        with self.assertRaises(CoreError) as e:
            self.own.page_post(self.w.ws, "one", self.g.tok["me"], body)
        self.assertEqual(e.exception.status, 401)
        connector = Feeds(self.w.store).new_token(self.w.ws, "connector", by="tests")
        with self.assertRaises(CoreError) as e:
            self.own.page_post(self.w.ws, "one", connector, body)
        self.assertEqual(e.exception.status, 401)
        with self.assertRaises(CoreError) as e:
            self.own.page_post(self.w.ws, "me", self.g.tok["me"], body)
        self.assertEqual(e.exception.reason, "not_asked", "a request asked of One is never answered by the VM")

    def test_only_patrick_and_only_while_own_chat_write_is_on(self):
        with self.assertRaises(CoreError) as e:
            self.own.page(self.w.bramble, "one", "chats")
        self.assertEqual(e.exception.status, 403)
        self.w.store.set_flag(self.w.ws, "own_chat_write", False, by="tests")
        with self.assertRaises(CoreError) as e:
            self.own.page(self.p, "one", "messages", chat=SID)
        self.assertEqual(e.exception.status, 423)

    def test_a_page_is_checked_before_it_is_held(self):
        for bad in ({"messages": [{"mid": 2, "role": "user", "text": "", "at": ""},
                                  {"mid": 1, "role": "user", "text": "", "at": ""}], "more": False},
                    {"messages": [{"mid": 1, "role": "tool", "text": "", "at": ""}], "more": False},
                    {"messages": [{"role": "user", "text": "", "at": ""}], "more": False},
                    {"messages": [], "more": "yes"},
                    {"messages": [], "more": False, "continued_as": "-rf"}):
            with self.assertRaises(CoreError):
                clean_page("messages", bad)

    def test_nothing_of_a_page_is_in_the_stores_file(self):
        self.own.page(self.p, "one", "messages", chat=SID)
        [req] = self.hand()
        self.own.page_post(self.w.ws, "one", self.g.tok["one"], {"id": req["id"], "page": {
            "messages": [{"mid": 9, "role": "assistant", "text": "a page-only marker 4471", "at": ""}],
            "more": False}})
        self.w.store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        folder = Path(self.w.store.path).parent
        for f in folder.iterdir():
            if f.is_file():
                self.assertNotIn(b"page-only marker 4471", f.read_bytes(), f.name)

    def tearDown(self):
        with PAGES.cond:
            PAGES.held.clear()
            PAGES.asked.clear()


class Resume(unittest.TestCase):
    def test_a_message_to_a_deleted_chat_is_refused_and_no_new_chat_starts(self):
        from test_decisions_own import FakeCore
        w = World()
        self.addCleanup(w.close)
        g = Gateways(w)
        g.hello("one")
        folder = Path(tempfile.mkdtemp())
        db = state_db(folder)
        (folder / "hermes.py").write_text(HERMES, encoding="utf-8")
        (folder / "ready.py").write_text(READY, encoding="utf-8")
        log = folder / "turns.jsonl"
        import os
        os.environ["TW_FAKE_LOG"] = str(log)
        self.addCleanup(lambda: os.environ.pop("TW_FAKE_LOG", None))
        hermes = own_connector.Hermes([sys.executable, str(folder / "hermes.py")],
                                      [sys.executable, "-I", str(folder / "ready.py")], folder, "One's Hermes")
        conn = own_connector.OwnConnector(FakeCore(g.own, w.ws, "one", g.tok["one"]), hermes, db, say=lambda _: None)
        mid = g.own.send(passkey(w), "one", "s_deleted", None, "are you there", "own-key-gone-01")["message_id"]
        conn.once()
        for t in conn.running:
            t.join(30)
        conn._report()
        self.assertFalse(log.exists(), "Hermes was never run")
        row = w.store.db.execute("SELECT status, reason, made_session_id FROM own_messages WHERE id = ?",
                                 (mid,)).fetchone()
        self.assertEqual((row["status"], row["reason"], row["made_session_id"]), ("dead", "that chat is gone", None))


class ConnectorRound(unittest.TestCase):
    def tearDown(self):
        with PAGES.cond:
            PAGES.held.clear()
            PAGES.asked.clear()

    def test_one_round_answers_the_pages_the_core_handed_it(self):
        from test_decisions_own import FakeCore
        w = World()
        self.addCleanup(w.close)
        g = Gateways(w)
        g.hello("one")
        folder = Path(tempfile.mkdtemp())
        db = long_chat(folder)
        hermes = own_connector.Hermes([sys.executable, "-c", "pass"], [sys.executable, "-I", "-c", "pass"], folder,
                                      "One's Hermes")
        hermes.ready = lambda: ("ready", "")
        asked = []
        hermes.search = lambda q: (asked.append(q), ([{"id": f"s_{i:03d}", "snippet": ""} for i in range(0, 120, 10)], ""))[1]
        conn = own_connector.OwnConnector(FakeCore(g.own, w.ws, "one", g.tok["one"]), hermes, db, say=lambda _: None)
        conn.once()  # its first round sends the chat list, which lists s_long
        p = passkey(w)
        self.assertEqual(g.own.page(p, "one", "messages", chat="s_long"), {"pending": True})
        self.assertEqual(g.own.page(p, "one", "chats", q="backup"), {"pending": True})
        conn.once()
        got = g.own.page(p, "one", "messages", chat="s_long")
        self.assertEqual((got["messages"][-1]["text"], len(got["messages"]), got["more"]), ("message 499", 50, True))
        self.assertEqual(len(g.own.page(p, "one", "chats", q="backup")["chats"]), 12)
        self.assertEqual(asked, ["backup"], "a search goes to Hermes's own search")


class Routes(unittest.TestCase):
    def setUp(self):
        self.s = Served(self)
        self.w = self.s.w
        self.g = Gateways(self.w)
        on(self.w, "mobile_read")
        self.g.hello("one")
        self.g.feed("one")

    def tearDown(self):
        with PAGES.cond:
            PAGES.held.clear()
            PAGES.asked.clear()

    def test_the_phone_asks_and_the_connector_answers(self):
        dev = self.w.device()
        sess = self.w.core.auth.issue_session(self.w.ws, method="passkey", device_id=dev)
        h = {"Cookie": f"tw_session={sess.token}"}
        status, _, body = self.s.call("GET", f"/own/one/chats/{SID}/messages?before=99", headers=h)
        self.assertEqual((status, body), (200, {"pending": True}))
        tok = {"Authorization": f"Bearer {self.g.tok['one']}"}
        status, _, body = self.s.call("POST", "/own/one/claim", headers=tok, body={"limit": 0, "ready": True})
        [req] = body["pages"]
        self.assertEqual(req["before"], 99)
        status, _, body = self.s.call("POST", "/own/one/page", headers=tok, body={"id": req["id"], "page": {
            "messages": [{"mid": 3, "role": "user", "text": "hi", "at": ""}], "more": True}})
        self.assertEqual(status, 200, body)
        status, _, body = self.s.call("GET", f"/own/one/chats/{SID}/messages?before=99&wait=1", headers=h)
        self.assertEqual((body["pending"], body["more"], body["messages"][0]["mid"]), (False, True, 3))
        status, _, body = self.s.call("GET", "/own/one/chats?q=plan", headers=h)
        self.assertEqual(body, {"pending": True})
        status, _, body = self.s.call("GET", "/own/me/chats/" + SID + "/messages", headers=h)
        self.assertEqual((status, body["error"]), (404, "not_listed"))
        status, _, body = self.s.call("POST", "/own/one/page", headers={"Authorization": "Bearer nope"}, body={})
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()


ROOM_ID = "54b3ad46-3419-4adb-b60d-c79cb347fcee"


def bots_home(folder: Path) -> Path:
    """A stand-in ~/.hermes: Rift's profile with a 130-message Bot Chat, and a room of 140 messages."""
    home = folder / ".hermes"
    (home / "profiles" / "rift").mkdir(parents=True)
    con = sqlite3.connect(home / "profiles" / "rift" / "state.db")
    con.executescript(STATE_SQL)
    con.execute("INSERT INTO sessions VALUES('s_bot', 'cli', 'Bot Chat', 1, 900, NULL, NULL, NULL, 0, 0)")
    con.execute("INSERT INTO sessions VALUES('s_other', 'cli', 'Something else', 1, 900, NULL, NULL, NULL, 0, 0)")
    for i in range(130):
        con.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES('s_bot', ?, ?, ?)",
                    ("user" if i % 2 == 0 else "assistant", f"rift {i}", 10 + i))
    con.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES('s_other', 'user', 'not here', 1)")
    con.commit()
    con.close()
    con = sqlite3.connect(home / "shared-state.db")
    con.execute("CREATE TABLE hosted_room_events (room_id TEXT, seq INTEGER, event_id TEXT, kind TEXT, "
                "actor_json TEXT, authority_epoch INTEGER, payload_json TEXT, created_at REAL)")
    for i in range(1, 141):
        con.execute("INSERT INTO hosted_room_events VALUES(?,?,?,?,?,?,?,?)",
                    (ROOM_ID, i, f"e{i}", "message.member" if i % 3 else "room.joined",
                     json.dumps({"display_name": "Bramble"}), 1, json.dumps({"text": f"room {i}"}), 100 + i))
    con.commit()
    con.close()
    return home


class BotsAndRooms(unittest.TestCase):
    def setUp(self):
        from thornswood_decisions.chat import Chat
        self.w = World()
        self.addCleanup(self.w.close)
        on(self.w, "mobile_read")
        for bot in ("bramble", "rift"):
            self.w.core.bind_bot(self.w.ws, bot, bot, by="tests")
        self.w.core.bind_room(self.w.ws, ROOM_ID, "Thornswood Workbench", by="tests")
        self.chat = Chat(self.w.store)
        self.tok = Feeds(self.w.store).new_token(self.w.ws, "connector", by="tests")
        self.home = bots_home(Path(tempfile.mkdtemp()))

    def tearDown(self):
        with PAGES.cond:
            PAGES.held.clear()
            PAGES.asked.clear()

    def connector(self):
        from thornswood_decisions import connector

        class Direct:
            def __init__(me):
                me.chat, me.ws, me.tok = self.chat, self.w.ws, self.tok

            def post(me, path, body):
                try:
                    if path == "/connector/pages":
                        return 200, me.chat.page_requests(me.ws, me.tok)
                    return 200, me.chat.page_post(me.ws, me.tok, json.loads(json.dumps(body)))
                except CoreError as exc:
                    return exc.status, {"error": exc.reason}
        return connector.Connector(Direct(), None, say=lambda _: None, hermes_home=self.home)

    def test_a_bots_chat_pages_back_to_its_first_message(self):
        p, conn, seen, before = self.w.patrick(), self.connector(), [], None
        while True:
            self.assertEqual(self.chat.page(p, "rift", before=before), {"pending": True})
            self.assertEqual(conn.pages(), 1)
            page = self.chat.page(p, "rift", before=before)
            seen = page["messages"] + seen
            if not page["more"]:
                break
            before = page["messages"][0]["mid"]
        self.assertEqual([m["text"] for m in seen], [f"rift {i}" for i in range(130)])

    def test_a_room_pages_back_by_its_own_sequence_with_only_its_messages(self):
        p, conn, seen, before = self.w.patrick(), self.connector(), [], None
        while True:
            self.chat.page(p, ROOM_ID, before=before)
            conn.pages()
            page = self.chat.page(p, ROOM_ID, before=before)
            seen = page["events"] + seen
            if not page["more"]:
                break
            before = page["events"][0]["seq"]
        self.assertEqual([e["text"] for e in seen], [f"room {i}" for i in range(1, 141) if i % 3])

    def test_a_bot_with_no_bot_chat_yet_is_said_as_that(self):
        self.chat.page(self.w.patrick(), "bramble")
        self.connector().pages()
        self.assertEqual(self.chat.page(self.w.patrick(), "bramble")["gone"], True)

    def test_one_session_of_a_bot_reads_only_that_session(self):
        # The Board's row opens the session the Bot is working in, not its Bot Chat (Patrick, 9 Nov).
        p, conn = self.w.patrick(), self.connector()
        self.assertEqual(self.chat.page(p, "rift", session="s_other"), {"pending": True})
        self.assertEqual(conn.pages(), 1)
        page = self.chat.page(p, "rift", session="s_other")
        self.assertEqual([m["text"] for m in page["messages"]], ["not here"])
        self.assertFalse(page.get("gone"))
        # A session id that is not one of that Bot's own reads as gone, never another Bot's session.
        self.chat.page(p, "rift", session="s_nobody")
        conn.pages()
        self.assertTrue(self.chat.page(p, "rift", session="s_nobody")["gone"])
        for bad in ("../x", "", "a b"):
            with self.assertRaises(CoreError) as e:
                self.chat.page(p, "rift", session=bad)
            self.assertEqual(e.exception.reason, "bad_session")
        with self.assertRaises(CoreError) as e:
            self.chat.page(p, ROOM_ID, session="s_other")
        self.assertEqual(e.exception.reason, "bad_session")

    def test_a_page_of_another_bot_than_the_one_asked_for_is_refused(self):
        self.chat.page(self.w.patrick(), "rift")
        [req] = self.chat.page_requests(self.w.ws, self.tok)["pages"]
        with self.assertRaises(CoreError) as e:
            self.chat.page_post(self.w.ws, self.tok, {"id": req["id"], "target": "bramble",
                                                      "page": {"messages": [], "more": False}})
        self.assertEqual(e.exception.reason, "not_that_chat")

    def test_only_the_bots_connector_token_and_only_while_mobile_read_is_on(self):
        g = Gateways(self.w)
        self.chat.page(self.w.patrick(), "rift")
        for tok in (g.tok["one"], g.tok["me"], "nope"):
            with self.assertRaises(CoreError) as e:
                self.chat.page_requests(self.w.ws, tok)
            self.assertEqual(e.exception.status, 401)
        with self.assertRaises(CoreError) as e:
            g.own.page_post(self.w.ws, "one", g.tok["one"], {"id": "pg_" + "a" * 20, "page": {}})
        self.assertEqual(e.exception.reason, "not_asked")
        [req] = self.chat.page_requests(self.w.ws, self.tok)["pages"]
        with self.assertRaises(CoreError) as e:
            g.own.page_post(self.w.ws, "one", g.tok["one"], {"id": req["id"], "page": {"messages": [], "more": False}})
        self.assertEqual(e.exception.reason, "not_asked", "One's connector cannot answer a page of a Bot")
        self.w.store.set_flag(self.w.ws, "mobile_read", False, by="tests")
        with self.assertRaises(CoreError) as e:
            self.chat.page_requests(self.w.ws, self.tok)
        self.assertEqual(e.exception.status, 423)

    def test_the_phone_route(self):
        s = Served(self)
        on(s.w, "mobile_read")
        s.w.core.bind_bot(s.w.ws, "rift", "rift", by="tests")
        dev = s.w.device()
        sess = s.w.core.auth.issue_session(s.w.ws, method="passkey", device_id=dev)
        status, _, body = s.call("GET", "/chat/rift/messages?before=12", headers={"Cookie": f"tw_session={sess.token}"})
        self.assertEqual((status, body), (200, {"pending": True}))
        status, _, body = s.call("GET", "/chat/rift/messages?session=s_x", headers={"Cookie": f"tw_session={sess.token}"})
        self.assertEqual((status, body), (200, {"pending": True}))
        status, _, _ = s.api.handle("GET", "/m/bots/rift/s/s_x", {}, b"")
        self.assertEqual(status, 200, "the session's own link opens the app")
        self.assertEqual(s.api.handle("GET", "/m/bots/rift/s/..%2Fx", {}, b"")[0], 404)
        status, _, body = s.call("GET", "/chat/nobody/messages", headers={"Cookie": f"tw_session={sess.token}"})
        self.assertEqual(status, 404)
