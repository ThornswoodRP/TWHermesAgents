"""Patrick answers a card in his own words instead of one of its options, and a card says which Bot opened
it (TWHermesAgents #107, from the mock-up's decision card: "From Bramble · kind · answer by", options A, B, C,
and "Answer in your words" under them).

Checks:
- an answer in his words is the card's answer: no option, his words its note, the card resolved, the receipt
  carrying the words, the same checks as an option (still waiting, the revision shown, a fresh sign-in for a
  high-risk card), and the same key again is the same answer;
- words and an option together, no words, and more than 2000 characters are refused, and change nothing;
- the Bot that asked is told his words as his answer, choosing no option; no words go into the event chain;
- words choose no option, so they authorize nothing: a Main card's answer authorizes no release, and tw-git
  reads no outside pull request answer from them;
- the table keeps every answer as history, and refuses an answer with neither an option nor words;
- migration 25's way back refuses while an answer in his words is kept, and keeps every other answer;
- a card names the Bot that opened it;
- over HTTP: the phone's answer carries `words`.
Everything runs in a scratch test workspace; nothing reaches a real Hermes.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest

from decisions_helpers import World
from test_decisions_api import Served, answer_body, bearer
from test_decisions_core import main_card

from thornswood_decisions.chat import Chat
from thornswood_decisions.store import CoreError, downgrade

WORDS = "Neither. Wait until Djinn is back on Thursday, then ask me again with both builds."


def words_args(card: dict, words: str = WORDS, key: str = "key-words-0001") -> dict:
    return dict(decision_id=card["id"], option_key=None, revision=card["revision"],
                attention_revision=card["attention_revision"], artifact_sha256=card["artifact_sha256"],
                idempotency_key=key, words=words)


class InHisWords(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.w.core.assign_task(self.w.bramble, "t1", "rift")
        self.card = self.w.card(by=self.w.rift, task="t1", title="Turn on the nightly check?")

    def count(self, table):
        return self.w.store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def test_his_words_are_the_answer(self):
        r = self.w.core.answer(self.w.patrick(), **words_args(self.card, words="  " + WORDS + "\n"))
        self.assertEqual((r["option_key"], r["words"]), (None, WORDS))
        row = self.w.store.db.execute("SELECT option_key, note FROM responses").fetchone()
        self.assertEqual(tuple(row), (None, WORDS))
        card = self.w.core.get_card(self.w.patrick(), self.card["id"])
        self.assertEqual((card["status"], card["receipt"]["words"]), ("resolved", WORDS))
        again = self.w.core.answer(self.w.patrick(), **words_args(self.card, words="  " + WORDS + "\n"))
        self.assertEqual((again["response_id"], again["replayed"]), (r["response_id"], True))
        with self.assertRaises(CoreError) as e:
            self.w.core.answer(self.w.patrick(), **words_args(self.card, words="Something else"))
        self.assertEqual(e.exception.reason, "key_reused")
        with self.assertRaises(CoreError) as e:
            self.w.core.answer(self.w.patrick(), **words_args(self.card, key="key-words-0002"))
        self.assertEqual(e.exception.reason, "already_answered")
        self.assertEqual(self.count("responses"), 1)

    def test_an_option_answer_carries_no_words(self):
        r = self.w.core.answer(self.w.patrick(), **self.w.answer_args(self.card))
        self.assertEqual(r["option_key"], "a")
        self.assertNotIn("words", r)

    def test_what_is_refused_changes_nothing(self):
        cases = [(dict(words_args(self.card), option_key="a"), "bad_answer"),
                 (words_args(self.card, words="   "), "bad_words"),
                 (words_args(self.card, words=""), "bad_words"),
                 (words_args(self.card, words="x" * 2001), "bad_words"),
                 (dict(words_args(self.card), revision=self.card["revision"] + 1), "stale_revision")]
        for n, (args, reason) in enumerate(cases):
            with self.assertRaises(CoreError) as e:
                self.w.core.answer(self.w.patrick(), **dict(args, idempotency_key=f"key-words-01{n:02}"))
            self.assertEqual(e.exception.reason, reason, args)
        self.assertEqual(self.count("responses"), 0)
        self.assertEqual(self.w.core.get_card(self.w.patrick(), self.card["id"])["status"], "pending")

    def test_a_high_risk_card_needs_a_fresh_sign_in(self):
        card = self.w.card(by=self.w.rift, task="t1", title="Restart the live server?", risk="high")
        with self.assertRaises(CoreError) as e:
            self.w.core.answer(self.w.patrick(fresh=False), **words_args(card))
        self.assertEqual(e.exception.reason, "sign_in_again")
        self.assertIsNone(self.w.core.answer(self.w.patrick(), **words_args(card))["option_key"])

    def test_the_bot_is_told_his_words_and_the_event_chain_keeps_none(self):
        r = self.w.core.answer(self.w.patrick(), **words_args(self.card))
        cx = self.w.store.db.execute("SELECT id, target, body_json FROM connector_outbox "
                                     "WHERE kind = 'decision.answered'").fetchone()
        self.assertEqual(cx["target"], "rift")
        self.assertIsNone(json.loads(cx["body_json"])["option"])
        told = Chat._told_text(self.w.store.db, cx["id"])
        self.assertIn("in his own words, choosing none of its options", told)
        self.assertIn(f"Response {r['response_id']}", told)
        self.assertTrue(told.split("His words:\n", 1)[1].startswith(WORDS), told)
        self.assertIn("approves nothing by itself", told)
        bodies = " ".join(x[0] for x in self.w.store.db.execute("SELECT body_json FROM events"))
        self.assertNotIn("Thursday", bodies)
        ev = self.w.store.db.execute("SELECT body_json FROM events WHERE kind = 'decision.answered'").fetchone()
        self.assertEqual(json.loads(ev[0])["in_words"], True)

    def test_words_authorize_no_release(self):
        card = main_card(self.w)
        self.w.core.answer(self.w.patrick(), **words_args(card))
        a = self.w.core.main_answer(self.w.ws, card["id"])
        self.assertIsNone(a["option"])
        self.assertFalse(self.w.core.states(card["id"])["release_authorized"])

    def test_the_table_keeps_answers_as_history_and_refuses_an_empty_one(self):
        self.w.core.answer(self.w.patrick(), **words_args(self.card))
        db = self.w.store.db
        for sql in ("UPDATE responses SET note = 'other words'", "DELETE FROM responses"):
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute(sql)
        other = self.w.card(by=self.w.rift, task="t1", title="Another")
        for note in ("", "   "):
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO responses(id, workspace_id, decision_id, revision, attention_revision, "
                           "artifact_sha256, option_key, note, principal_id, answered_at) "
                           "VALUES('rsp_x', ?, ?, ?, ?, ?, NULL, ?, ?, 't')",
                           (self.w.ws, other["id"], other["revision"], other["attention_revision"],
                            other["artifact_sha256"], note, self.w.patrick_id))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO responses(id, workspace_id, decision_id, revision, attention_revision, "
                       "artifact_sha256, option_key, note, principal_id, answered_at) "
                       "VALUES('rsp_y', ?, ?, ?, ?, ?, 'zz', '', ?, 't')",
                       (self.w.ws, other["id"], other["revision"], other["attention_revision"],
                        other["artifact_sha256"], self.w.patrick_id))

    def test_a_card_names_the_bot_that_opened_it(self):
        self.assertEqual(self.w.core.get_card(self.w.patrick(), self.card["id"])["from"], "rift")
        mine = self.w.card(title="Bramble's own")
        self.assertEqual(self.w.core.get_card(self.w.patrick(), mine["id"])["from"], "bramble")
        self.assertEqual({c["id"]: c["from"] for c in self.w.core.needs_you(self.w.patrick())},
                         {self.card["id"]: "rift", mine["id"]: "bramble"})


class Migration25(unittest.TestCase):
    def test_the_way_back_keeps_every_option_answer_and_refuses_his_words(self):
        w = World()
        self.addCleanup(w.close)
        one, two = w.card(title="One"), w.card(title="Two")
        w.core.answer(w.patrick(), **w.answer_args(one, option="b"))
        w.core.answer(w.patrick(), **words_args(two))
        with self.assertRaises(CoreError) as e:
            downgrade(w.store.db, 24)
        self.assertIn("schema 25: 1 answers in Patrick's own words", str(e.exception))
        self.assertEqual(w.store.db.execute("SELECT count(*) FROM responses").fetchone()[0], 2)

    def test_the_way_back_and_forward_again(self):
        w = World()
        self.addCleanup(w.close)
        card = w.card(title="One")
        r = w.core.answer(w.patrick(), **w.answer_args(card, option="b"))
        self.assertEqual(downgrade(w.store.db, 24)["to"], 24)
        db = w.store.db
        self.assertEqual(tuple(db.execute("SELECT id, option_key FROM responses").fetchone()), (r["response_id"], "b"))
        with self.assertRaises(sqlite3.DatabaseError):
            db.execute("DELETE FROM responses")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO responses(id, workspace_id, decision_id, revision, attention_revision, "
                       "artifact_sha256, option_key, note, principal_id, answered_at) "
                       "VALUES('rsp_x', ?, 'dec_x', 1, 1, 'x', NULL, 'words', ?, 't')", (w.ws, w.patrick_id))
        from thornswood_decisions.schema import MIGRATIONS
        from thornswood_decisions.store import migrate
        self.assertEqual(migrate(db), len(MIGRATIONS))
        self.assertEqual(tuple(db.execute("SELECT id, option_key FROM responses").fetchone()), (r["response_id"], "b"))
        with self.assertRaises(sqlite3.DatabaseError):
            db.execute("UPDATE responses SET note = 'x'")


@unittest.skipUnless(sys.platform.startswith("linux"),
                     "the route's ops are served by the brokers' wire, which is Linux code (Unix sockets, pwd)")
class NoOutsideAnswer(unittest.TestCase):
    def test_words_on_an_outside_card_are_nothing_tw_git_acts_on(self):
        from test_decisions_bots import OutsideAnswers
        t = OutsideAnswers("test_only_patricks_answer_on_the_current_revision_is_read")
        t.setUp()
        self.addCleanup(t.doCleanups)
        card = t.card()
        t.w.core.answer(t.w.patrick(device=t.w.device()), **words_args(card))
        self.assertEqual(t.w.core.get_card(t.w.patrick(), card["id"])["status"], "resolved")
        self.assertEqual(t.answers(), [])


class OverHttp(unittest.TestCase):
    def test_the_phone_answers_in_his_words(self):
        s = Served(self)
        d = s.desktop()
        card = s.w.card()
        path = f"/cards/{card['id']}/answer"
        body = dict(answer_body(card), option_key="", words=WORDS)
        status, _, r = s.call("POST", path, headers=bearer(d, csrf=True), body=body)
        self.assertEqual((status, r["option_key"], r["words"]), (200, None, WORDS), r)
        status, _, c = s.call("GET", f"/cards/{card['id']}", headers=bearer(d))
        self.assertEqual((c["status"], c["receipt"]["words"], c["from"]), ("resolved", WORDS, "bramble"))
        other = s.w.card(title="Two")
        for n, (words, error) in enumerate([("", "bad_words"), (7, "bad_words")]):
            status, _, r = s.call("POST", f"/cards/{other['id']}/answer", headers=bearer(d, csrf=True),
                                  body=dict(answer_body(other, key=f"desk-words-00{n}"), option_key="", words=words))
            self.assertEqual((status, r["error"]), (400, error))
        status, _, r = s.call("POST", f"/cards/{other['id']}/answer", headers=bearer(d, csrf=True),
                              body=dict(answer_body(other, key="desk-words-009"), words="Do both"))
        self.assertEqual((status, r["error"]), (400, "bad_answer"), "an option and words together")


if __name__ == "__main__":
    unittest.main()
