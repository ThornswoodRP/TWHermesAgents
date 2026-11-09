"""Step 4: the phone answers like Desktop, and chats with the six Bots and the room.

Checks the chat outbox (flags, idempotency, who may send), the connector's
side of the API (its own token, one message in flight per Bot, a lapsed lease
is unclear and never resent), the room settling an unclear send, the Bot Chat
feed, the "Bot told" receipt, the restore quarantine, migration 4 keeping the
step 3 rows, and the connector process itself against a stand-in `hermes`
command and room helper. Everything runs in a scratch test workspace; nothing
reaches a real Hermes.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from decisions_helpers import World
from test_decisions_api import ORIGIN, Served, answer_body

from thornswood_decisions import backup, connector, feed_reader, store as store_mod
from thornswood_decisions.chat import Chat, room_event_id
from thornswood_decisions.feeds import Feeds
from thornswood_decisions.schema import MIGRATIONS, REPLAY_16, _replay
from thornswood_decisions.store import CoreError, Store, migrate

ROOM = "54b3ad46-3419-4adb-b60d-c79cb347fcee"
BOTS = ("bramble", "rift", "sprocket", "echo", "magpie", "wisp")


def seat(w: World) -> None:
    w.core.bind_room(w.ws, ROOM, "Thornswood Workbench", by="tests")
    for bot in BOTS:
        w.core.bind_bot(w.ws, bot, bot, by="tests")


def flags(w: World, *names: str) -> None:
    for n in names:
        w.store.set_flag(w.ws, n, True, by="tests")


class ChatSend(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        seat(self.w)
        self.chat = Chat(self.w.store)

    def test_off_until_mobile_chat_write(self):
        with self.assertRaises(CoreError) as e:
            self.chat.send(self.w.patrick(), "bramble", "hi", "key-00000001")
        self.assertEqual(e.exception.reason, "chat_write_off")
        flags(self.w, "mobile_chat_write")
        self.assertEqual(self.chat.send(self.w.patrick(), "bramble", "hi", "key-00000001")["status"], "queued")

    def test_same_key_same_receipt_and_one_message(self):
        flags(self.w, "mobile_chat_write")
        first = self.chat.send(self.w.patrick(), "room", " hello room ", "key-00000002")
        again = self.chat.send(self.w.patrick(), "room", "hello room", "key-00000002")
        self.assertEqual(again["message_id"], first["message_id"])
        self.assertTrue(again["replayed"])
        self.assertEqual(self.w.store.db.execute("SELECT count(*) FROM chat_messages").fetchone()[0], 1)
        with self.assertRaises(CoreError) as e:
            self.chat.send(self.w.patrick(), "room", "something else", "key-00000002")
        self.assertEqual(e.exception.reason, "key_reused")

    def test_only_patrick_only_real_chats_and_bounded_text(self):
        flags(self.w, "mobile_chat_write")
        with self.assertRaises(CoreError) as e:
            self.chat.send(self.w.rift, "bramble", "hi", "key-00000003")
        self.assertEqual(e.exception.reason, "not_patrick")
        for target in ("gateway", "default", "../bramble", "Bramble"):
            with self.assertRaises(CoreError) as e:
                self.chat.send(self.w.patrick(), target, "hi", "key-00000004")
            self.assertEqual(e.exception.reason, "no_chat", target)
        for text in ("", "   ", None, 5, "x" * 8001):
            with self.assertRaises(CoreError) as e:
                self.chat.send(self.w.patrick(), "bramble", text, "key-00000005")
            self.assertEqual(e.exception.reason, "bad_text")

    def test_no_text_in_the_event_chain(self):
        flags(self.w, "mobile_chat_write")
        self.chat.send(self.w.patrick(), "bramble", "a private sentence", "key-00000006")
        bodies = " ".join(r[0] for r in self.w.store.db.execute("SELECT body_json FROM events"))
        self.assertNotIn("private sentence", bodies)

    def test_the_view_shows_history_and_my_messages(self):
        flags(self.w, "mobile_chat_write")
        f = Feeds(self.w.store)
        tok = f.new_token(self.w.ws, "bots", by="tests")
        f.put(self.w.ws, "bots", tok, {"source_time": "2026-11-01T18:00:00+00:00", "bots": [
            {"bot": "bramble", "session_id": "s1", "messages": [
                {"role": "user", "text": "status?", "at": "2026-11-01T17:00:00+00:00"},
                {"role": "assistant", "text": "All green.", "at": "2026-11-01T17:01:00+00:00"}]}]})
        self.chat.send(self.w.patrick(), "bramble", "thanks", "key-00000007")
        v = self.chat.view(self.w.patrick(), "bramble")
        self.assertEqual([h["text"] for h in v["history"]], ["status?", "All green."])
        self.assertEqual([(m["text"], m["status"]) for m in v["mine"]], [("thanks", "queued")])
        s = self.chat.summary(self.w.patrick())
        self.assertEqual([b["bot"] for b in s["bots"]], list(BOTS))
        self.assertEqual(s["bots"][0]["unsettled"], 1)
        self.assertEqual(s["bots"][0]["last"]["text"], "All green.")


class ConnectorSide(unittest.TestCase):
    def at_schema(self, n):
        """The store as an older release left it at schema n: the stored version, and no column a later
        migration adds (13: own_messages.reply_to; 14: own_attachments; 15: passkeys.aaguid and phone_key),
        so this release's migrations run on it as they would."""
        if n < 15:
            self.w.store.db.execute("ALTER TABLE passkeys DROP COLUMN phone_key")
            self.w.store.db.execute("ALTER TABLE passkeys DROP COLUMN aaguid")
        if n < 14:
            self.w.store.db.execute("DROP TABLE own_attachments")
        if n < 13:
            self.w.store.db.execute("ALTER TABLE own_messages DROP COLUMN reply_to")
        self.w.store.db.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(n),))

    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        seat(self.w)
        flags(self.w, "mobile_chat_write")
        self.chat = Chat(self.w.store)
        self.tok = Feeds(self.w.store).new_token(self.w.ws, "connector", by="tests")

    def send(self, target, text, key):
        return self.chat.send(self.w.patrick(), target, text, key)["message_id"]

    def test_nothing_while_connector_effects_is_off(self):
        self.send("bramble", "hi", "key-00000010")
        with self.assertRaises(CoreError) as e:
            self.chat.claim(self.w.ws, self.tok)
        self.assertEqual(e.exception.reason, "connector_effects_off")

    def test_only_its_own_token(self):
        flags(self.w, "connector_effects")
        feed = Feeds(self.w.store).new_token(self.w.ws, "work", by="tests")
        for tok in (None, "", "nope", feed):
            with self.assertRaises(CoreError) as e:
                self.chat.claim(self.w.ws, tok)
            self.assertEqual(e.exception.reason, "bad_feed_token")
        with self.assertRaises(CoreError):
            Feeds(self.w.store).put(self.w.ws, "work", self.tok, {"source_time": "2026-11-01T18:00:00Z", "doing": []})

    def test_one_in_flight_for_each_chat_oldest_first(self):
        flags(self.w, "connector_effects")
        a1 = self.send("bramble", "one", "key-00000011")
        self.send("bramble", "two", "key-00000012")
        r1 = self.send("room", "to all", "key-00000013")
        items = self.chat.claim(self.w.ws, self.tok)["items"]
        self.assertEqual(sorted(i["id"] for i in items), sorted([a1, r1]))
        room = next(i for i in items if i["target"] == "room")
        self.assertEqual(room["room_id"], ROOM)
        self.assertEqual(self.chat.claim(self.w.ws, self.tok)["items"], [])
        self.chat.result(self.w.ws, self.tok, a1, "confirmed", "turn finished")
        self.send("rift", "three", "key-00000018")
        self.send("echo", "four", "key-00000019")
        self.assertEqual([i["text"] for i in self.chat.claim(self.w.ws, self.tok, limit=1)["items"]], ["two"])
        for bad in (0, 8, True, "3"):
            with self.assertRaises(CoreError):
                self.chat.claim(self.w.ws, self.tok, limit=bad)
        self.assertEqual(sorted(i["text"] for i in self.chat.claim(self.w.ws, self.tok)["items"]), ["four", "three"])


    def test_a_lapsed_lease_is_unclear_and_never_sent_again(self):
        flags(self.w, "connector_effects")
        m = self.send("rift", "hello", "key-00000014")
        self.chat.claim(self.w.ws, self.tok)
        self.w.clock.advance(minutes=21)
        self.assertEqual(self.chat.claim(self.w.ws, self.tok)["items"], [])
        row = self.w.store.db.execute("SELECT status, attempts FROM chat_messages WHERE id = ?", (m,)).fetchone()
        self.assertEqual((row["status"], row["attempts"]), ("uncertain", 1))
        with self.assertRaises(CoreError) as e:
            self.chat.result(self.w.ws, self.tok, m, "confirmed")
        self.assertEqual(e.exception.reason, "not_leased")

    def test_outcomes_and_replays(self):
        flags(self.w, "connector_effects")
        m = self.send("echo", "hello", "key-00000015")
        self.chat.claim(self.w.ws, self.tok)
        with self.assertRaises(CoreError):
            self.chat.result(self.w.ws, self.tok, m, "approved")
        self.assertEqual(self.chat.result(self.w.ws, self.tok, m, "failed", reason="the Bots' gateway is absent")
                         ["status"], "dead")
        self.assertTrue(self.chat.result(self.w.ws, self.tok, m, "failed")["replayed"])
        v = self.chat.view(self.w.patrick(), "echo")
        self.assertEqual((v["mine"][0]["status"], v["mine"][0]["reason"]), ("dead", "the Bots' gateway is absent"))

    def test_the_room_settles_an_unclear_send(self):
        flags(self.w, "connector_effects")
        m = self.send("room", "hello room", "key-00000016")
        item = self.chat.claim(self.w.ws, self.tok)["items"][0]
        self.chat.result(self.w.ws, self.tok, m, "uncertain", reason="the gateway stopped answering")
        f = Feeds(self.w.store)
        tok = f.new_token(self.w.ws, "room", by="tests")
        f.put(self.w.ws, "room", tok, {"source_time": "2026-11-01T18:05:00+00:00", "room_id": ROOM, "events": [
            {"seq": 9, "event_id": room_event_id(item["send_key"]), "kind": "message.user", "actor": "Patrick",
             "text": "hello room", "at": "2026-11-01T18:04:00+00:00"}]})
        self.assertEqual(self.w.store.db.execute("SELECT status FROM chat_messages WHERE id = ?", (m,)).fetchone()[0],
                         "sent")

    def test_a_task_with_a_brief_reaches_the_specialist_once(self):
        # 9 Nov 2026: an assignment reached nobody, so no worker ever took up a picked issue.
        flags(self.w, "connector_effects")
        out = self.w.core.assign_task(self.w.bramble, "5547", "magpie",
                                      "Claimed for you: Thornswood #5547. Name the failing check from the log.")
        self.assertEqual(out, {"task": "5547", "bot": "magpie", "queued": True})
        again = self.w.core.assign_task(self.w.bramble, "5547", "magpie",
                                        "Claimed for you: Thornswood #5547. Name the failing check from the log.")
        self.assertFalse(again["queued"])
        # Without a brief nothing is told, as before.
        self.assertFalse(self.w.core.assign_task(self.w.bramble, "5548", "echo")["queued"])
        items = self.chat.claim(self.w.ws, self.tok)["items"]
        self.assertEqual([(i["kind"], i["target"]) for i in items], [("task.assigned", "magpie")])
        self.assertTrue(items[0]["send_key"].startswith("tw-task."))
        self.assertIn("assigned you task 5547", items[0]["text"])
        self.assertIn("Name the failing check from the log.", items[0]["text"])
        self.assertIn("approves nothing by itself", items[0]["text"])
        self.chat.result(self.w.ws, self.tok, items[0]["id"], "confirmed", "turn finished")
        self.assertEqual(self.w.store.db.execute("SELECT status FROM connector_outbox WHERE id = ?",
                                                 (items[0]["id"],)).fetchone()[0], "sent")
        self.assertEqual(self.chat.claim(self.w.ws, self.tok)["items"], [])
        for bad in ("", " ", "x" * 8001):
            with self.subTest(brief=len(bad)), self.assertRaises(CoreError):
                self.w.core.assign_task(self.w.bramble, "5549", "wisp", bad)

    def test_no_new_work_is_handed_out_from_11_pm_to_6_am_mountain(self):
        # Patrick, 9 Nov 2026: the Bots auto work 6 AM to 11 PM; at night the Grok lanes audit.
        flags(self.w, "connector_effects")
        cases = [(datetime(2026, 11, 9, 12, 59, tzinfo=timezone.utc), False),  # 5:59 AM MST
                 (datetime(2026, 11, 9, 13, 0, tzinfo=timezone.utc), True),    # 6:00 AM
                 (datetime(2026, 11, 10, 5, 59, tzinfo=timezone.utc), True),   # 10:59 PM
                 (datetime(2026, 11, 10, 6, 0, tzinfo=timezone.utc), False)]   # 11:00 PM
        for n, (t, ok) in enumerate(cases):
            self.w.clock.t = t
            with self.subTest(t=t.isoformat()):
                if ok:
                    self.assertTrue(self.w.core.assign_task(self.w.bramble, f"6{n}", "echo", "Do it.")["queued"])
                else:
                    with self.assertRaises(CoreError) as e:
                        self.w.core.assign_task(self.w.bramble, f"6{n}", "echo", "Do it.")
                    self.assertEqual(e.exception.reason, "after_hours")
        # An assignment with no brief tells nobody and is not held.
        self.assertFalse(self.w.core.assign_task(self.w.bramble, "70", "echo")["queued"])

    def test_the_bot_that_asked_is_told_and_that_is_bot_told(self):
        flags(self.w, "connector_effects")
        card = self.w.card()
        body = answer_body(card)
        self.w.core.answer(self.w.patrick(), decision_id=card["id"], option_key=body["option_key"],
                           revision=body["revision"], attention_revision=body["attention_revision"],
                           artifact_sha256=body["artifact_sha256"], idempotency_key=body["idempotency_key"])
        self.assertFalse(self.w.core.states(card["id"])["bot_told"])
        items = self.chat.claim(self.w.ws, self.tok)["items"]
        self.assertEqual([(i["kind"], i["target"]) for i in items], [("decision.answered", "bramble")])
        self.assertIn("Do it (option a)", items[0]["text"])
        self.assertIn("approves nothing by itself", items[0]["text"])
        self.chat.result(self.w.ws, self.tok, items[0]["id"], "confirmed", "turn finished")
        self.assertTrue(self.w.core.states(card["id"])["bot_told"])

    def answered(self, key="key-00000001", **kw):
        card = self.w.card(**kw)
        self.w.core.answer(self.w.patrick(), **self.w.answer_args(card, key=key))
        return card

    def outbox(self, cx):
        return self.w.store.db.execute("SELECT status, attempts, lease_until, reason FROM connector_outbox "
                                       "WHERE id = ?", (cx,)).fetchone()

    def receipts(self, cx):
        return [r[0] for r in self.w.store.db.execute(
            "SELECT outcome FROM effect_receipts WHERE outbox = 'connector' AND outbox_id = ?", (cx,))]

    def test_a_helper_failure_leaves_the_answer_queued_and_names_the_helper_not_the_gateway(self):
        # 2 Nov: the connector's helper ran under /bin/sh and every check read as the gateway.
        flags(self.w, "connector_effects")
        card = self.answered()
        cx = self.chat.claim(self.w.ws, self.tok)["items"][0]["id"]
        why = "the connector's Hermes helper failed (exit 2): room_send.py: 61: Syntax error: \"(\" unexpected"
        self.assertEqual(self.chat.result(self.w.ws, self.tok, cx, "not_ready", reason=why)["status"], "queued")
        row = self.outbox(cx)
        self.assertEqual((row["status"], row["attempts"], row["reason"]), ("queued", 0, why))
        self.assertEqual(self.receipts(cx), [])
        self.assertFalse(self.w.core.states(card["id"])["bot_told"])

    def test_a_gateway_that_is_not_ready_leaves_the_answer_queued(self):
        flags(self.w, "connector_effects")
        card = self.answered()
        cx = self.chat.claim(self.w.ws, self.tok)["items"][0]["id"]
        out = self.chat.result(self.w.ws, self.tok, cx, "not_ready", reason="could not check the Bots' gateway")
        self.assertEqual(out["status"], "queued")
        row = self.outbox(cx)
        self.assertEqual((row["status"], row["attempts"], row["lease_until"]), ("queued", 0, None))
        self.assertEqual(row["reason"], "waiting for the Bots' gateway: could not check the Bots' gateway")
        self.assertEqual(self.receipts(cx), [], "a gateway that was not ready is no delivery failure")
        self.assertTrue(self.chat.result(self.w.ws, self.tok, cx, "not_ready")["replayed"])
        self.assertFalse(self.w.core.states(card["id"])["bot_told"])
        # Once the gateway is ready, the same message goes, and that is "Bot told".
        items = self.chat.claim(self.w.ws, self.tok)["items"]
        self.assertEqual([(i["id"], i["target"]) for i in items], [(cx, "bramble")])
        self.chat.result(self.w.ws, self.tok, cx, "confirmed", "turn finished")
        self.assertEqual((self.outbox(cx)["status"], self.outbox(cx)["attempts"]), ("sent", 1))
        self.assertTrue(self.w.core.states(card["id"])["bot_told"])

    def test_a_chat_message_waits_for_a_gateway_that_is_not_ready(self):
        flags(self.w, "connector_effects")
        m = self.send("rift", "hello", "key-00000020")
        self.chat.claim(self.w.ws, self.tok)
        self.chat.result(self.w.ws, self.tok, m, "not_ready", reason="the Bots' gateway is starting")
        row = self.w.store.db.execute("SELECT status, attempts FROM chat_messages WHERE id = ?", (m,)).fetchone()
        self.assertEqual(tuple(row), ("queued", 0))
        mine = self.chat.view(self.w.patrick(), "rift")["mine"][0]
        self.assertEqual(mine["waits"], "waiting for the Bots' gateway: the Bots' gateway is starting")
        self.assertEqual([i["id"] for i in self.chat.claim(self.w.ws, self.tok)["items"]], [m])
        with self.assertRaises(CoreError) as e:  # only a leased message can be put back
            self.chat.result(self.w.ws, self.tok, "msg_nope", "not_ready")
        self.assertEqual(e.exception.reason, "no_item")

    def test_an_answer_whose_turn_the_fleet_queue_turned_away_goes_back_and_says_why(self):
        # 2 Nov: Patrick's answer on the #5327 ad18c2c card reached Bramble while the queue turned turns
        # away; the connector called it sent and nothing ran it. Now it comes back not_ready, unspent.
        flags(self.w, "connector_effects")
        card = self.answered()
        cx = self.chat.claim(self.w.ws, self.tok)["items"][0]["id"]
        why = "the fleet queue turned the turn away: 2 turn already running; the fleet runs at most 2 at once"
        self.assertEqual(self.chat.result(self.w.ws, self.tok, cx, "not_ready", reason=why)["status"], "queued")
        row = self.outbox(cx)
        self.assertEqual((row["status"], row["attempts"], row["reason"]), ("queued", 0, why))
        self.assertEqual(self.receipts(cx), [])
        self.assertFalse(self.w.core.states(card["id"])["bot_told"])
        m = self.send("rift", "hello", "key-00000022")
        self.chat.claim(self.w.ws, self.tok)
        self.chat.result(self.w.ws, self.tok, m, "not_ready", reason="the fleet queue has no room for a turn of rift's: x")
        self.assertEqual(self.chat.view(self.w.patrick(), "rift")["mine"][0]["waits"],
                         "the fleet queue has no room for a turn of rift's: x")

    def test_migration_16_sends_each_named_answer_once_more_and_nothing_else(self):
        self.assertEqual(MIGRATIONS[15], _replay(REPLAY_16))
        self.assertEqual(len(REPLAY_16), 6)
        self.assertIn("dec_2XL-fP9HNMMtGBXp_5KW1A", REPLAY_16)
        flags(self.w, "connector_effects")
        told = self.answered(key="key-00000150", title="Told")
        cx = self.chat.claim(self.w.ws, self.tok)["items"][0]["id"]
        self.chat.result(self.w.ws, self.tok, cx, "confirmed", "turn finished")
        waiting = self.answered(key="key-00000151", title="Waiting")
        other = self.answered(key="key-00000152", title="Not named")
        ox = self.chat.claim(self.w.ws, self.tok)["items"]
        for i in ox:
            if i["id"] != self.w.store.db.execute("SELECT id FROM connector_outbox WHERE decision_id = ?",
                                                  (waiting["id"],)).fetchone()[0]:
                self.chat.result(self.w.ws, self.tok, i["id"], "confirmed", "turn finished")
        before = {r[0]: tuple(r[1:]) for r in self.w.store.db.execute(
            "SELECT id, status, attempts, body_json FROM connector_outbox")}
        for stmt in [_replay((told["id"], waiting["id"], "dec_gone"))]:
            self.w.store.db.executescript(stmt)
        rows = self.w.store.db.execute("SELECT id, decision_id, status, target, body_json, idempotency_key "
                                       "FROM connector_outbox ORDER BY rowid").fetchall()
        new = [r for r in rows if r["id"] not in before]
        self.assertEqual([(r["decision_id"], r["status"], r["target"], r["idempotency_key"]) for r in new],
                         [(told["id"], "queued", "bramble", f"again:{cx}")])
        self.assertEqual(new[0]["body_json"], before[cx][2], "the same answer, word for word")
        self.assertEqual({r[0]: tuple(r[1:]) for r in self.w.store.db.execute(
            "SELECT id, status, attempts, body_json FROM connector_outbox") if r[0] in before}, before,
            "every message already there stays as it was")
        # It goes to the Bot that asked, as a new message, once the one on its way there has its result.
        leased = self.w.store.db.execute("SELECT id FROM connector_outbox WHERE status = 'leased'").fetchone()[0]
        self.chat.result(self.w.ws, self.tok, leased, "confirmed", "turn finished")
        went = []
        for _ in range(5):
            for i in self.chat.claim(self.w.ws, self.tok)["items"]:
                went.append(i["id"])
                self.chat.result(self.w.ws, self.tok, i["id"], "confirmed", "turn finished")
        self.assertEqual(went.count(new[0]["id"]), 1)
        self.assertTrue(self.w.core.states(told["id"])["bot_told"])
        self.assertTrue(self.w.store.verify_events()[0])

    def test_a_real_failure_of_the_answer_still_dies(self):
        flags(self.w, "connector_effects")
        self.answered()
        cx = self.chat.claim(self.w.ws, self.tok)["items"][0]["id"]
        self.chat.result(self.w.ws, self.tok, cx, "failed", reason="could not start the hermes command")
        self.assertEqual(self.outbox(cx)["status"], "dead")
        self.assertEqual(self.receipts(cx), ["failed"])
        self.assertEqual(self.chat.claim(self.w.ws, self.tok)["items"], [])
        with self.assertRaises(CoreError) as e:
            self.chat.result(self.w.ws, self.tok, cx, "not_ready")
        self.assertEqual(e.exception.reason, "not_leased")

    def test_migration_11_queues_again_only_answers_that_died_of_a_gateway_not_ready(self):
        flags(self.w, "connector_effects")
        died = {}
        for n, reason in enumerate(("could not check the Bots' gateway", "the Bots' gateway is stopped",
                                    "could not start the hermes command")):
            card = self.answered(key=f"key-0000010{n}", title=f"Pick {n}")
            cx = self.chat.claim(self.w.ws, self.tok)["items"][0]["id"]
            self.chat.result(self.w.ws, self.tok, cx, "failed", reason=reason)
            died[reason] = (card["id"], cx)
        m = self.send("rift", "hello", "key-00000021")
        self.chat.claim(self.w.ws, self.tok)
        self.chat.result(self.w.ws, self.tok, m, "failed", reason="could not check the Bots' gateway")
        # The store as the release before this one left it: schema 10, then this release opens it.
        self.at_schema(10)
        self.assertEqual(migrate(self.w.store.db), len(MIGRATIONS))
        not_ready = [died["could not check the Bots' gateway"][1], died["the Bots' gateway is stopped"][1]]
        for cx in not_ready:
            row = self.outbox(cx)
            self.assertEqual((row["status"], row["attempts"], row["lease_until"]), ("queued", 0, None))
            self.assertTrue(row["reason"].startswith("waiting for the Bots' gateway: "))
            self.assertEqual(self.receipts(cx), ["failed"], "the failed receipt stays as history")
        self.assertEqual(self.outbox(died["could not start the hermes command"][1])["status"], "dead")
        self.assertEqual(self.w.store.db.execute("SELECT status FROM chat_messages WHERE id = ?", (m,)).fetchone()[0],
                         "dead", "a chat message Patrick saw as not sent is left for him")
        # Through the fixed path: each goes to the Bot that asked, one at a time, and is "Bot told".
        for _ in not_ready:
            items = self.chat.claim(self.w.ws, self.tok)["items"]
            self.assertEqual(len(items), 1)
            self.assertIn(items[0]["id"], not_ready)
            self.assertEqual(items[0]["target"], "bramble")
            self.chat.result(self.w.ws, self.tok, items[0]["id"], "confirmed", "turn finished")
        for reason in ("could not check the Bots' gateway", "the Bots' gateway is stopped"):
            self.assertTrue(self.w.core.states(died[reason][0])["bot_told"])
        self.assertFalse(self.w.core.states(died["could not start the hermes command"][0])["bot_told"])
        self.assertTrue(self.w.store.verify_events()[0])

    def test_migration_12_queues_again_the_answer_an_old_connector_failed_after_migration_11(self):
        # 1 Nov, 10:53 PM: migration 11 put the answer back, then the release before's connector,
        # still running, leased it and failed it the old way: dead again, a second failed receipt.
        flags(self.w, "connector_effects")
        card = self.answered(key="key-00000110", title="Pick A")
        cx = self.chat.claim(self.w.ws, self.tok)["items"][0]["id"]
        self.chat.result(self.w.ws, self.tok, cx, "failed", reason="could not check the Bots' gateway")
        real = self.answered(key="key-00000111", title="Pick B")
        rx = self.chat.claim(self.w.ws, self.tok)["items"][0]["id"]
        self.chat.result(self.w.ws, self.tok, rx, "failed", reason="could not start the hermes command")
        self.at_schema(10)
        with mock.patch.object(store_mod, "MIGRATIONS", MIGRATIONS[:11]):
            self.assertEqual(migrate(self.w.store.db), 11)
        self.assertEqual(self.outbox(cx)["status"], "queued")
        items = self.chat.claim(self.w.ws, self.tok)["items"]
        self.assertEqual([i["id"] for i in items], [cx])
        self.chat.result(self.w.ws, self.tok, cx, "failed", reason="could not check the Bots' gateway")
        row = self.outbox(cx)
        self.assertEqual((row["status"], row["attempts"]), ("dead", 1))
        self.assertEqual(self.receipts(cx), ["failed", "failed"])
        # This release opens the store at schema 11.
        self.assertEqual(migrate(self.w.store.db), len(MIGRATIONS))
        row = self.outbox(cx)
        self.assertEqual((row["status"], row["attempts"], row["lease_until"]), ("queued", 0, None))
        self.assertEqual(row["reason"], "waiting for the Bots' gateway: could not check the Bots' gateway")
        self.assertEqual(self.receipts(cx), ["failed", "failed"], "both failed receipts stay as history")
        self.assertEqual(self.outbox(rx)["status"], "dead", "a real failure stays dead")
        items = self.chat.claim(self.w.ws, self.tok)["items"]
        self.assertEqual([(i["id"], i["target"]) for i in items], [(cx, "bramble")])
        self.chat.result(self.w.ws, self.tok, cx, "confirmed", "turn finished")
        self.assertTrue(self.w.core.states(card["id"])["bot_told"])
        self.assertFalse(self.w.core.states(real["id"])["bot_told"])
        self.assertTrue(self.w.store.verify_events()[0])

    def test_migration_12_leaves_a_delivered_answer_alone(self):
        flags(self.w, "connector_effects")
        self.answered(key="key-00000112", title="Pick C")
        cx = self.chat.claim(self.w.ws, self.tok)["items"][0]["id"]
        self.chat.result(self.w.ws, self.tok, cx, "confirmed", "turn finished")
        before = dict(self.outbox(cx))
        self.at_schema(11)
        self.assertEqual(migrate(self.w.store.db), len(MIGRATIONS))
        self.assertEqual(dict(self.outbox(cx)), before)

    def test_a_restore_quarantines_waiting_chat(self):
        self.send("wisp", "hello", "key-00000017")
        copy = self.w.dir / "copy.db"
        backup.backup(self.w.store, copy)
        target = self.w.dir / "restored.db"
        out = backup.restore(copy, target, by="tests")
        self.assertEqual(out["quarantined"]["chat_messages"], 1)
        s = Store(target)
        self.addCleanup(s.close)
        self.assertEqual(s.db.execute("SELECT status FROM chat_messages").fetchone()[0], "quarantined")


class DecidedLine(unittest.TestCase):
    """An answered question or change card tells its Bot to give DECIDED.md the line (q-p18-decided-auto)."""

    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        seat(self.w)
        flags(self.w, "connector_effects")
        self.chat = Chat(self.w.store)
        self.tok = Feeds(self.w.store).new_token(self.w.ws, "connector", by="tests")

    def told(self, key: str = "key-00000001", **kw) -> tuple[str, str]:
        card = self.w.card(**kw)
        self.w.core.answer(self.w.patrick(), **self.w.answer_args(card, key=key))
        items = self.chat.claim(self.w.ws, self.tok)["items"]
        self.assertEqual([i["kind"] for i in items], ["decision.answered"])
        self.chat.result(self.w.ws, self.tok, items[0]["id"], "confirmed", "turn finished")  # one lease per Bot
        return card["id"], items[0]["text"]

    def test_a_question_names_the_issue_the_format_and_the_checks(self):
        cid, text = self.told(links=[("issue", "#4321")])
        self.assertIn("Do it (option a)", text)
        self.assertIn("docs/DECIDED.md", text)
        self.assertIn("Comment on issue #4321", text)
        self.assertIn(f"- **The decision as one plain sentence.** ({cid}) 1 Nov 2026, [#N]", text)
        self.assertIn("that line goes and this one takes its place", text)
        self.assertIn("never reorder or rewrite any other line", text)
        for check in ("scripts/dev/Check-Words.ps1", "tests/test-docs-index.js", "tests/test-3283-docs-word-number.js"):
            self.assertIn(check, text)
        self.assertNotIn("\u2014", text)

    def test_a_change_card_gets_it_too(self):
        self.assertIn("docs/DECIDED.md", self.told(kind="change", links=[("issue", "#7")])[1])

    def test_no_issue_means_one_is_filed_first(self):
        text = self.told()[1]
        self.assertIn("Comment on a new issue for it (file it on the board first, unclaimed, in Todo)", text)

    def test_the_date_is_patricks_mountain_day(self):
        # 3:30 AM UTC on 2 Nov is 8:30 PM on 1 Nov in Mountain time.
        self.w.clock.t = datetime(2026, 11, 2, 3, 30, tzinfo=timezone.utc)
        self.assertIn(") 1 Nov 2026, [#N]", self.told()[1])

    def test_a_step_approval_settles_nothing_and_writes_no_line(self):
        for kind in ("review", "todo"):
            text = self.told(key=f"key-{kind}-0001", kind=kind)[1]
            self.assertIn("approves nothing by itself", text)
            self.assertNotIn("DECIDED", text)


class Rooms(unittest.TestCase):
    """The Rooms list: every bound room, each its own chat (Patrick, 1 Nov)."""
    SECOND = "00000000-0000-4000-8000-000000000002"

    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        seat(self.w)
        self.w.core.bind_room(self.w.ws, self.SECOND, "Second test room", by="tests")
        flags(self.w, "mobile_chat_write")
        self.chat = Chat(self.w.store)
        self.f = Feeds(self.w.store)
        self.room_tok = self.f.new_token(self.w.ws, "room", by="tests")

    def put(self, body):
        return self.f.put(self.w.ws, "room", self.room_tok, body)

    def test_the_list_has_each_room_in_the_order_bound(self):
        self.put({"source_time": "2026-11-01T18:00:00+00:00", "rooms": [
            {"room_id": ROOM, "events": [{"seq": 1, "event_id": "e1", "kind": "message.member", "actor": "Bramble",
                                          "text": "morning", "at": "2026-11-01T17:00:00+00:00"}]},
            {"room_id": self.SECOND, "events": []}]})
        self.chat.send(self.w.patrick(), self.SECOND, "hi second", "key-00000100")
        s = self.chat.summary(self.w.patrick())
        self.assertEqual([(r["room_id"], r["name"]) for r in s["rooms"]],
                         [(ROOM, "Thornswood Workbench"), (self.SECOND, "Second test room")])
        self.assertEqual(s["rooms"][0]["last"], {"who": "Bramble", "text": "morning", "at": "2026-11-01T17:00:00+00:00"})
        self.assertEqual((s["rooms"][1]["last"], s["rooms"][1]["unsettled"]), (None, 1))
        self.assertEqual(s["rooms_as_of"], "2026-11-01T18:00:00+00:00")

    def test_each_room_is_its_own_chat(self):
        self.put({"source_time": "2026-11-01T18:00:00+00:00", "rooms": [
            {"room_id": ROOM, "events": [{"seq": 1, "event_id": "e1", "kind": "message.member", "actor": "Rift",
                                          "text": "in the bench", "at": "2026-11-01T17:00:00+00:00"}]},
            {"room_id": self.SECOND, "events": [{"seq": 4, "event_id": "e4", "kind": "message.user", "actor": "Patrick",
                                                 "text": "in the second", "at": "2026-11-01T17:02:00+00:00"}]}]})
        a, b = self.chat.view(self.w.patrick(), ROOM), self.chat.view(self.w.patrick(), self.SECOND)
        self.assertEqual(([h["text"] for h in a["history"]], a["kind"], a["name"]),
                         (["in the bench"], "room", "Thornswood Workbench"))
        self.assertEqual(([h["text"] for h in b["history"]], b["name"]), (["in the second"], "Second test room"))
        self.assertEqual(self.chat.view(self.w.patrick(), "room")["target"], ROOM, "room is the first room's first name")
        self.chat.send(self.w.patrick(), "room", "old name", "key-00000101")
        self.chat.send(self.w.patrick(), self.SECOND, "new room", "key-00000102")
        self.assertEqual([m["text"] for m in self.chat.view(self.w.patrick(), ROOM)["mine"]], ["old name"])
        self.assertEqual([m["text"] for m in self.chat.view(self.w.patrick(), self.SECOND)["mine"]], ["new room"])

    def test_the_connector_sends_each_to_its_own_room(self):
        flags(self.w, "connector_effects")
        tok = self.f.new_token(self.w.ws, "connector", by="tests")
        self.chat.send(self.w.patrick(), ROOM, "to the bench", "key-00000103")
        self.chat.send(self.w.patrick(), self.SECOND, "to the second", "key-00000104")
        items = {i["text"]: i for i in self.chat.claim(self.w.ws, tok)["items"]}
        self.assertEqual((items["to the bench"]["target"], items["to the bench"]["room_id"]), ("room", ROOM))
        self.assertEqual((items["to the second"]["target"], items["to the second"]["room_id"]), ("room", self.SECOND))

    def test_a_room_message_settles_from_its_own_room_only(self):
        flags(self.w, "connector_effects")
        tok = self.f.new_token(self.w.ws, "connector", by="tests")
        mid = self.chat.send(self.w.patrick(), self.SECOND, "is it there", "key-00000105")["message_id"]
        item = self.chat.claim(self.w.ws, tok)["items"][0]
        self.chat.result(self.w.ws, tok, item["id"], "uncertain", reason="no answer")
        seen = {"seq": 9, "event_id": room_event_id(item["send_key"]), "kind": "message.user", "actor": "Patrick",
                "text": "is it there", "at": "2026-11-01T18:00:00+00:00"}
        self.put({"source_time": "2026-11-01T18:01:00+00:00", "rooms": [{"room_id": ROOM, "events": [seen]}]})
        status = lambda: self.w.store.db.execute("SELECT status FROM chat_messages WHERE id = ?", (mid,)).fetchone()[0]
        self.assertEqual(status(), "uncertain", "seen in another room settles nothing")
        self.put({"source_time": "2026-11-01T18:02:00+00:00", "rooms": [{"room_id": self.SECOND, "events": [seen]}]})
        self.assertEqual(status(), "sent")

    def test_the_feed_takes_both_shapes_and_only_bound_rooms(self):
        self.put({"source_time": "2026-11-01T18:00:00+00:00", "room_id": ROOM, "events": []})
        self.assertEqual(self.f.get(self.w.ws, "room")["room"]["rooms"], [{"room_id": ROOM, "events": []}])
        for bad in ({"source_time": "2026-11-01T18:00:00+00:00", "rooms": [{"room_id": "elsewhere", "events": []}]},
                    {"source_time": "2026-11-01T18:00:00+00:00", "room_id": "elsewhere", "events": []}):
            with self.assertRaises(CoreError) as e:
                self.put(bad)
            self.assertEqual(e.exception.reason, "not_the_room")
        for bad in ({"source_time": "2026-11-01T18:00:00+00:00", "rooms": []},
                    {"source_time": "2026-11-01T18:00:00+00:00", "rooms": [{"room_id": ROOM, "events": []}] * 2},
                    {"source_time": "2026-11-01T18:00:00+00:00", "rooms": [{"room_id": ROOM, "events": "x"}]}):
            with self.assertRaises(CoreError) as e:
                self.put(bad)
            self.assertEqual(e.exception.reason, "bad_feed")

    def test_a_room_id_is_what_a_path_can_carry(self):
        for bad in ("", "../x", "a b", "x" * 65, None):
            with self.assertRaises(CoreError) as e:
                self.w.core.bind_room(self.w.ws, bad, "Bad", by="tests")
            self.assertEqual(e.exception.reason, "bad_room")
        with self.assertRaises(CoreError):
            self.w.core.bind_room(self.w.ws, "fine-id", "   ", by="tests")


class Api(unittest.TestCase):
    def test_phone_chats_with_csrf_and_origin_and_the_connector_never_from_a_browser(self):
        s = Served(self)
        seat(s.w)
        flags(s.w, "mobile_read", "mobile_chat_write", "connector_effects")
        cookie, paired = s.phone()
        h = {"Cookie": cookie, "Origin": ORIGIN, "X-CSRF-Token": paired["csrf"]}
        status, _, body = s.call("POST", "/chat/bramble", headers={"Cookie": cookie, "Origin": ORIGIN},
                                 body={"text": "hi", "idempotency_key": "key-00000020"})
        self.assertEqual(status, 403)
        status, _, body = s.call("POST", "/chat/bramble", headers=h, body={"text": "hi", "idempotency_key": "key-00000020"})
        self.assertEqual((status, body["status"]), (200, "queued"))
        status, _, body = s.call("GET", "/chat/bramble", headers={"Cookie": cookie})
        self.assertEqual((status, body["mine"][0]["text"]), (200, "hi"))
        status, _, body = s.call("GET", "/chat", headers={"Cookie": cookie})
        self.assertEqual((status, len(body["bots"])), (200, 6))
        tok = Feeds(s.w.store).new_token(s.w.ws, "connector", by="tests")
        status, _, body = s.call("POST", "/connector/claim", headers={"Authorization": f"Bearer {tok}", "Origin": ORIGIN})
        self.assertEqual((status, body["error"]), (403, "not_a_feed"))
        status, _, body = s.call("POST", "/connector/claim", headers={"Authorization": f"Bearer {tok}"})
        self.assertEqual((status, [i["text"] for i in body["items"]]), (200, ["hi"]))
        status, _, body = s.call("POST", "/connector/claim", headers=h)
        self.assertEqual(status, 403)

    def test_phone_chat_reads_wait_for_mobile_read(self):
        s = Served(self)
        seat(s.w)
        cookie, _ = s.phone()
        status, _, body = s.call("GET", "/chat/bramble", headers={"Cookie": cookie})
        self.assertEqual((status, body["error"]), (423, "mobile_read_off"))

    def test_a_high_risk_answer_from_the_phone_needs_a_fresh_sign_in(self):
        s = Served(self)
        flags(s.w, "mobile_read")
        cookie, paired = s.phone()
        card = s.w.card(risk="high")
        s.w.clock.advance(minutes=6)
        h = {"Cookie": cookie, "Origin": ORIGIN, "X-CSRF-Token": paired["csrf"]}
        status, _, body = s.call("POST", f"/cards/{card['id']}/answer", headers=h, body=answer_body(card))
        self.assertEqual((status, body["error"]), (403, "sign_in_again"))

    def test_the_app_has_a_deep_link_for_each_bot(self):
        s = Served(self)
        status, headers, _ = s.api.handle("GET", "/m/bots/rift", {}, b"")
        self.assertEqual((status, headers["Content-Type"]), (200, "text/html; charset=utf-8"))
        status, _, _ = s.api.handle("GET", "/m/bots/Rift%2F..", {}, b"")
        self.assertEqual(status, 404)


class Migration4(unittest.TestCase):
    def test_step_3_rows_survive_the_rebuild(self):
        d = Path(tempfile.mkdtemp())
        con = sqlite3.connect(d / "core.db", isolation_level=None)
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        from thornswood_decisions.store import _statements
        for n in (1, 2, 3):
            for stmt in _statements(MIGRATIONS[n - 1]):
                con.execute(stmt)
        con.execute("INSERT INTO meta(key, value) VALUES('schema_version', '3')")
        con.execute("INSERT INTO workspaces(id, name, is_test, created_at) VALUES('ws1', 't', 1, 'now')")
        con.execute("INSERT INTO feed_sources VALUES('ws1', 'work', ?, 'now')", ("a" * 64,))
        con.execute("INSERT INTO feed_snapshots VALUES('ws1', 'room', '{}', ?, 'then', 'now')", ("b" * 64,))
        con.close()
        s = Store(d / "core.db")
        self.addCleanup(s.close)
        self.assertEqual(s.db.execute("SELECT token_hash FROM feed_sources").fetchone()[0], "a" * 64)
        self.assertEqual(s.db.execute("SELECT name FROM feed_snapshots").fetchone()[0], "room")
        s.db.execute("INSERT INTO feed_sources VALUES('ws1', 'connector', ?, 'now')", ("c" * 64,))
        with self.assertRaises(sqlite3.IntegrityError):
            s.db.execute("INSERT INTO feed_sources VALUES('ws1', 'admin', ?, 'now')", ("d" * 64,))


class BotChatFeed(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        prof = self.home / "profiles" / "bramble"
        prof.mkdir(parents=True)
        con = sqlite3.connect(prof / "state.db")
        con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, archived INTEGER NOT NULL DEFAULT 0, "
                    "started_at REAL NOT NULL, hidden INTEGER NOT NULL DEFAULT 0)")
        con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, "
                    "role TEXT NOT NULL, content TEXT, timestamp REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1)")
        con.executemany("INSERT INTO sessions VALUES(?,?,?,?,?)", [
            ("bc", "Bot Chat", 0, 1761990000.0, 1), ("other", "Something else", 0, 1761999999.0, 0)])
        con.executemany("INSERT INTO messages(session_id, role, content, timestamp, active) VALUES(?,?,?,?,?)", [
            ("bc", "user", "status?", 1761998400.0, 1),
            ("bc", "tool", "secret tool output", 1761998401.0, 1),
            ("bc", "assistant", json.dumps([{"type": "text", "text": "All green."}, {"type": "image"}]), 1761998402.0, 1),
            ("bc", "assistant", "an old draft", 1761998403.0, 0),
            ("other", "user", "not the Bot Chat", 1761998404.0, 1)])
        con.commit()
        con.close()

    def test_only_the_bot_chat_and_only_what_was_said(self):
        now = datetime(2026, 11, 1, 18, 0, tzinfo=timezone.utc)
        snap = feed_reader.bots_snapshot(self.home, list(BOTS), now)
        b = snap["bots"][0]
        self.assertEqual((b["bot"], b["session_id"]), ("bramble", "bc"))
        self.assertEqual([(m["role"], m["text"]) for m in b["messages"]], [("user", "status?"), ("assistant", "All green.")])
        self.assertEqual([x["messages"] for x in snap["bots"][1:]], [[]] * 5)
        w = World()
        self.addCleanup(w.close)
        seat(w)
        f = Feeds(w.store)
        tok = f.new_token(w.ws, "bots", by="tests")
        f.put(w.ws, "bots", tok, snap)
        with self.assertRaises(CoreError) as e:
            f.put(w.ws, "bots", tok, {"source_time": now.isoformat(), "bots": [{"bot": "gateway", "messages": []}]})
        self.assertEqual(e.exception.reason, "not_the_bots")
        with self.assertRaises(CoreError):
            f.put(w.ws, "bots", tok, {"source_time": now.isoformat(),
                                      "bots": [{"bot": "rift", "messages": [{"role": "system", "text": "x"}]}]})


# ── the connector process, against stand-ins for `hermes` and the room helper ──

FAKE_HERMES = textwrap.dedent('''
    import json, os, sys
    from pathlib import Path
    log = Path(os.environ["TW_FAKE_LOG"])
    args = sys.argv[1:]
    text = Path(args[args.index("--query-file") + 1]).read_text(encoding="utf-8")
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": args, "text": text}) + "\\n")
    sys.exit(int(os.environ.get("TW_FAKE_RC", "0")))
''')

FAKE_HELPER = textwrap.dedent('''
    import json, os, sys
    from pathlib import Path
    req = json.loads(sys.stdin.read())
    with Path(os.environ["TW_FAKE_LOG"]).open("a", encoding="utf-8") as f:
        f.write(json.dumps({"helper": req}) + "\\n")
    if req["op"] == "ready" and os.environ.get("TW_FAKE_READY") == "crash":
        sys.exit(1)  # Hermes's Python mid-install: no answer at all
    if req["op"] == "ready":
        print(json.dumps({"ready": os.environ.get("TW_FAKE_READY", "1") == "1", "reason": "the Bots' gateway is absent"}))
    else:
        print(json.dumps(json.loads(os.environ.get("TW_FAKE_ROOM", '{"sent": true, "seq": 7}'))))
''')


class ConnectorProcess(unittest.TestCase):
    def setUp(self):
        self.s = Served(self)
        seat(self.s.w)
        flags(self.s.w, "mobile_chat_write", "connector_effects")
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "hermes.py").write_text(FAKE_HERMES, encoding="utf-8")
        (self.dir / "helper.py").write_text(FAKE_HELPER, encoding="utf-8")
        self.log = self.dir / "log.jsonl"
        self.env = {"TW_FAKE_LOG": str(self.log)}
        old = {k: os.environ.get(k) for k in ("TW_FAKE_LOG", "TW_FAKE_RC", "TW_FAKE_READY", "TW_FAKE_ROOM")}

        def restore():
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        os.environ.update(self.env)
        tok = Feeds(self.s.w.store).new_token(self.s.w.ws, "connector", by="tests")
        hermes = connector.Hermes([sys.executable, str(self.dir / "hermes.py")], sys.executable, self.dir,
                                  helper=self.dir / "helper.py")
        self.said = []
        self.c = connector.Connector(connector.Core(f"http://127.0.0.1:{self.s.port}/api/v1", tok), hermes,
                                     say=self.said.append)

    def send(self, target, text, key):
        return Chat(self.s.w.store).send(self.s.w.patrick(), target, text, key)["message_id"]

    def status(self, mid):
        return self.s.w.store.db.execute("SELECT status, reason FROM chat_messages WHERE id = ?", (mid,)).fetchone()

    def lines(self):
        return [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()]

    def test_a_bot_message_goes_through_hermes_chat_into_the_bot_chat(self):
        m = self.send("sprocket", "please look at the launcher log", "key-00000030")
        self.assertEqual(self.c.once(), 1)
        self.assertEqual(self.status(m)["status"], "sent")
        run = [x for x in self.lines() if "argv" in x][0]
        self.assertEqual(run["argv"][:8], ["-p", "sprocket", "chat", "--in", str(self.dir), "-c", "Bot Chat",
                                           "--create-if-missing"])
        self.assertIn("--query-file", run["argv"])
        self.assertEqual(run["text"], "please look at the launcher log")
        self.assertNotIn("please look", " ".join(run["argv"]))
        self.assertFalse(Path(run["argv"][-1]).exists(), "the message file is left behind")

    def test_an_unclear_turn_is_uncertain_and_is_not_sent_again(self):
        os.environ["TW_FAKE_RC"] = "1"
        m = self.send("magpie", "hi", "key-00000031")
        self.c.once()
        self.assertEqual(self.status(m)["status"], "uncertain")
        self.c.once()
        self.assertEqual(len([x for x in self.lines() if "argv" in x]), 1)

    def test_a_gateway_that_is_not_running_keeps_the_message_queued_until_it_is(self):
        os.environ["TW_FAKE_READY"] = "0"
        m = self.send("wisp", "hi", "key-00000032")
        self.c.once()
        self.assertEqual(tuple(self.status(m)), ("queued", "waiting for the Bots' gateway: the Bots' gateway is absent"))
        self.assertEqual([x for x in self.lines() if "argv" in x], [])
        self.assertEqual(self.c.waiting, {"wisp": "the Bots' gateway is absent"})
        # While it is not ready, a round only checks it again: nothing is leased and put back.
        leases = lambda: self.s.w.store.db.execute(  # noqa: E731
            "SELECT count(*) FROM events WHERE kind = 'connector.leased'").fetchone()[0]
        before = leases()
        self.assertEqual(self.c.once(), 0)
        self.assertEqual(leases(), before)
        self.assertEqual(self.status(m)["status"], "queued")
        os.environ["TW_FAKE_READY"] = "1"
        self.assertEqual(self.c.once(), 1)
        self.assertEqual(self.status(m)["status"], "sent")
        self.assertEqual(self.c.waiting, {})
        self.assertEqual(len([x for x in self.lines() if "argv" in x]), 1)

    def test_an_answer_reaches_the_bot_after_a_reload_that_answered_nothing(self):
        # 1 Nov 2026, 10:17 PM: the ready check got no answer while an install reloaded the gateway.
        card = self.s.w.card()
        self.s.w.core.answer(self.s.w.patrick(), **self.s.w.answer_args(card))
        os.environ["TW_FAKE_READY"] = "crash"
        self.c.once()
        cx = self.s.w.store.db.execute("SELECT id, status, attempts, reason FROM connector_outbox").fetchone()
        # It waits, and says it is the connector's helper that failed (2 Nov), not the gateway.
        self.assertEqual((cx["status"], cx["attempts"], cx["reason"]),
                         ("queued", 0, "the connector's Hermes helper failed (exit 1): nothing"))
        self.assertTrue(any(x.startswith("FAIL the connector's Hermes helper failed (exit 1)") for x in self.said))
        self.assertFalse(self.s.w.core.states(card["id"])["bot_told"])
        os.environ["TW_FAKE_READY"] = "1"
        self.c.once()
        self.assertTrue(self.s.w.core.states(card["id"])["bot_told"])
        run = [x for x in self.lines() if "argv" in x]
        self.assertEqual(len(run), 1)
        self.assertEqual(run[0]["argv"][:2], ["-p", "bramble"])
        self.assertIn("Patrick answered the card", run[0]["text"])

    def test_a_room_whose_gateway_cannot_be_reached_waits_too(self):
        os.environ["TW_FAKE_ROOM"] = json.dumps({"sent": False, "ready": False,
                                                 "reason": "could not reach the Bots' gateway"})
        m = self.send("room", "hello all", "key-00000037")
        self.c.once()
        self.assertEqual(self.status(m)["status"], "queued")
        self.assertIn("", self.c.waiting)
        os.environ["TW_FAKE_ROOM"] = json.dumps({"sent": False, "reason": "the room's driver is not running"})
        self.c.once()
        self.assertEqual(tuple(self.status(m)), ("dead", "the room's driver is not running"))

    def test_a_room_message_goes_with_the_core_key_as_its_event_id(self):
        m = self.send("room", "hello all", "key-00000033")
        self.c.once()
        self.assertEqual(self.status(m)["status"], "sent")
        req = [x["helper"] for x in self.lines() if "helper" in x][0]
        self.assertEqual((req["op"], req["room_id"], req["event_id"], req["text"]),
                         ("send", ROOM, f"tw-chat.{m}", "hello all"))

    def test_a_result_the_core_did_not_take_is_kept_not_resent(self):
        m = self.send("echo", "hi", "key-00000034")
        real = self.c.core.post
        down = {"on": True}

        def flaky(path, body):
            if path == "/connector/result" and down["on"]:
                return 0, {}
            return real(path, body)
        self.c.core.post = flaky
        self.c.once()
        self.assertEqual(self.status(m)["status"], "leased")
        self.assertEqual(len(self.c.unsaid), 1)
        down["on"] = False
        self.c.once()
        self.assertEqual(self.status(m)["status"], "sent")
        self.assertEqual(self.c.unsaid, [])
        self.assertEqual(len([x for x in self.lines() if "argv" in x]), 1)

    def test_it_waits_while_connector_effects_is_off(self):
        self.s.w.store.set_flag(self.s.w.ws, "connector_effects", False, by="tests")
        self.send("rift", "hi", "key-00000035")
        self.assertEqual(self.c.once(), -1)
        self.assertFalse(self.log.exists())

    def test_a_missing_hermes_command_means_not_sent(self):
        self.c.hermes.cmd = [str(self.dir / "no-such-hermes")]
        m = self.send("rift", "hi", "key-00000036")
        self.c.once()
        self.assertEqual(tuple(self.status(m)), ("dead", "could not start the hermes command"))

    def test_hermes_python_is_read_from_the_command(self):
        cmd = self.dir / "hermes"
        cmd.write_text("#!/home/hermes/.hermes/hermes-agent/venv/bin/python3\nimport x\n", encoding="utf-8")
        self.assertEqual(connector.hermes_python(str(cmd)), "/home/hermes/.hermes/hermes-agent/venv/bin/python3")
        cmd.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            connector.hermes_python(str(cmd))


# ── room_send.py, against stand-ins for the Hermes modules it imports ──

STUB_RUNTIME = textwrap.dedent('''
    import os
    from types import SimpleNamespace
    def gateway_profile_home(home):
        return home
    def discover_gateway_endpoint(home, timeout=2.0):
        state = os.environ.get("TW_STUB_STATE", "ready")
        return SimpleNamespace(state=state, endpoint="ep" if state == "ready" else None, home=str(home))
    def attachable(found):
        return found.endpoint is not None
''')

STUB_CLIENT = textwrap.dedent('''
    import json, os
    from contextlib import asynccontextmanager
    class GatewayClientError(ValueError):
        pass
    class GatewayRPCError(GatewayClientError):
        pass
    class Client:
        async def rpc(self, method, **params):
            with open(os.environ["TW_STUB_LOG"], "a", encoding="utf-8") as f:
                f.write(json.dumps({"method": method, "params": params}) + "\\n")
            mode = os.environ.get("TW_STUB_MODE", "ok")
            if method == "groups.capabilities":
                return {"driver": mode != "no_driver"}
            if mode == "refused":
                raise GatewayRPCError("invalid_params")
            if mode == "cut":
                raise GatewayClientError("Gateway disconnected")
            return {"event": {"seq": 12}}
    @asynccontextmanager
    async def connect_gateway(endpoint=None, subject=None):
        assert endpoint == "ep"
        yield Client()
''')


class RoomHelper(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "hermes_cli").mkdir()
        (self.dir / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
        (self.dir / "hermes_cli" / "gateway_runtime.py").write_text(STUB_RUNTIME, encoding="utf-8")
        (self.dir / "hermes_cli" / "gateway_client.py").write_text(STUB_CLIENT, encoding="utf-8")
        (self.dir / "hermes_constants.py").write_text(
            "from pathlib import Path\ndef get_default_hermes_root():\n    return Path('/home/hermes/.hermes')\n",
            encoding="utf-8")
        self.log = self.dir / "rpc.jsonl"

    def run_helper(self, req: dict, **env) -> dict:
        import subprocess
        helper = Path(connector.__file__).with_name("room_send.py")
        code = f"import runpy, sys; sys.path.insert(0, {str(self.dir)!r}); sys.argv = ['room_send']; " \
               f"runpy.run_path({str(helper)!r}, run_name='__main__')"
        out = subprocess.run([sys.executable, "-I", "-c", code], input=json.dumps(req).encode(),
                             capture_output=True, timeout=60,
                             env={**os.environ, "TW_STUB_LOG": str(self.log), **env})
        return json.loads(out.stdout.decode().strip().splitlines()[-1])

    def rpcs(self):
        if not self.log.exists():
            return []
        return [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()]

    def test_send_uses_the_key_as_event_id_and_thread(self):
        out = self.run_helper({"op": "send", "room_id": ROOM, "event_id": "tw-chat.msg_1", "text": "hi"})
        self.assertEqual(out, {"sent": True, "seq": 12})
        send = [r for r in self.rpcs() if r["method"] == "groups.send"][0]["params"]
        self.assertEqual(send, {"room_id": ROOM, "event_id": "tw-chat.msg_1",
                                "payload": {"text": "hi", "thread_id": "tw-chat.msg_1"}})

    def test_not_sent_when_the_gateway_or_driver_is_not_there_or_it_refuses(self):
        req = {"op": "send", "room_id": ROOM, "event_id": "tw-chat.msg_2", "text": "hi"}
        out = self.run_helper(req, TW_STUB_STATE="absent")
        self.assertEqual((out["sent"], out["ready"]), (False, False))
        self.assertEqual(self.rpcs(), [])
        self.assertIs(self.run_helper(req, TW_STUB_MODE="no_driver")["sent"], False)
        self.assertNotIn("groups.send", [r["method"] for r in self.rpcs()])
        self.assertIs(self.run_helper(req, TW_STUB_MODE="refused")["sent"], False)

    def test_a_cut_after_sending_is_unclear(self):
        out = self.run_helper({"op": "send", "room_id": ROOM, "event_id": "tw-chat.msg_3", "text": "hi"},
                              TW_STUB_MODE="cut")
        self.assertIsNone(out["sent"])

    def test_ready_names_the_bots_profile_and_never_starts_a_gateway(self):
        self.assertEqual(self.run_helper({"op": "ready", "profile": "rift"}), {"ready": True})
        self.assertEqual(self.run_helper({"op": "ready", "profile": "rift"}, TW_STUB_STATE="absent")["reason"],
                         "the Bots' gateway is absent")
        out = self.run_helper({"op": "ready", "profile": "rift"}, TW_STUB_STATE="absent")
        self.assertEqual((out["ready"], out["reason"]), (False, "the Bots' gateway is absent"))
        self.assertEqual(self.run_helper({"op": "start"})["sent"], False)


if __name__ == "__main__":
    unittest.main()
