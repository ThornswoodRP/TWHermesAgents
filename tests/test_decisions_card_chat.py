"""A message in a Bot's Bot Chat that answers a card that Bot asked (Pixel check, 2 Nov: "A chat is
never an answer to a card" was wrong, "it should be").

Checks:
- a Bot's Bot Chat lists only the cards that Bot asked and that wait on Patrick, and a room lists none;
- a message with an answer is that answer: the same checks as a tap (the revision shown, still waiting,
  an option of that revision, a fresh sign-in for a high-risk card), his words are its note, the Bot is
  told it as for a tap, and the words go to the Bot as a message;
- a refused answer sends nothing; the same send again is the same answer and the same message;
- only a card that Bot asked, only in a Bot Chat, never from a room;
- over HTTP: the phone's send carries it, and the card leaves the chat once answered;
- a card carries when its alert was queued and sent.
Everything runs in a scratch test workspace; nothing reaches a real Hermes.
"""

from __future__ import annotations

import unittest

from decisions_helpers import World
from test_decisions_api import ORIGIN, Served
from test_decisions_chat import ROOM, flags, seat

from thornswood_decisions.chat import Chat
from thornswood_decisions.store import CoreError


def shown(card: dict, option: str = "a") -> dict:
    return {"decision_id": card["id"], "option_key": option, "revision": card["revision"],
            "attention_revision": card["attention_revision"], "artifact_sha256": card["artifact_sha256"]}


class InTheChat(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        seat(self.w)
        flags(self.w, "mobile_chat_write")
        self.chat = Chat(self.w.store)
        self.w.core.assign_task(self.w.bramble, "t1", "rift")
        self.card = self.w.card(by=self.w.rift, task="t1", title="Turn on the nightly check?")

    def count(self, table):
        return self.w.store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def test_only_that_bots_waiting_cards_are_in_its_chat(self):
        other = self.w.card(title="Bramble's own")
        self.assertEqual([c["id"] for c in self.chat.view(self.w.patrick(), "rift")["cards"]], [self.card["id"]])
        self.assertEqual([c["id"] for c in self.chat.view(self.w.patrick(), "bramble")["cards"]], [other["id"]])
        self.assertEqual(self.chat.view(self.w.patrick(), "magpie")["cards"], [])
        self.assertEqual(self.chat.view(self.w.patrick(), ROOM)["cards"], [])
        self.w.core.answer(self.w.patrick(), **self.w.answer_args(other))
        self.assertEqual(self.chat.view(self.w.patrick(), "bramble")["cards"], [])
        self.w.clock.advance(days=30)
        self.assertEqual(self.chat.view(self.w.patrick(), "rift")["cards"], [], "an expired card is not offered")

    def test_a_message_that_answers_is_the_answer_and_the_bot_gets_both(self):
        r = self.chat.send(self.w.patrick(), "rift", "Do it, after 4 PM please", "chat-key-0001",
                           answer=shown(self.card))
        self.assertEqual((r["status"], r["answered"]["option_key"]), ("queued", "a"))
        resp = self.w.store.db.execute("SELECT option_key, note FROM responses").fetchone()
        self.assertEqual(tuple(resp), ("a", "Do it, after 4 PM please"))
        self.assertEqual(self.w.core.get_card(self.w.patrick(), self.card["id"])["status"], "resolved")
        told = self.w.store.db.execute("SELECT target, kind FROM connector_outbox").fetchall()
        self.assertEqual([tuple(t) for t in told], [("rift", "decision.answered")])
        self.assertEqual(self.w.store.db.execute("SELECT target, text FROM chat_messages").fetchall()[0][:],
                         ("rift", "Do it, after 4 PM please"))
        again = self.chat.send(self.w.patrick(), "rift", "Do it, after 4 PM please", "chat-key-0001",
                               answer=shown(self.card))
        self.assertEqual((again["message_id"], again["answered"]["response_id"]),
                         (r["message_id"], r["answered"]["response_id"]))
        self.assertEqual((self.count("responses"), self.count("chat_messages")), (1, 1))
        bodies = " ".join(x[0] for x in self.w.store.db.execute("SELECT body_json FROM events"))
        self.assertNotIn("4 PM", bodies)

    def test_a_refused_answer_sends_nothing(self):
        stale = shown(self.card) | {"revision": self.card["revision"] + 1}
        cases = [("rift", stale, "stale_revision"), ("rift", shown(self.card, "zz"), "no_option"),
                 ("bramble", shown(self.card), "not_this_bots_card"), (ROOM, shown(self.card), "no_cards_here"),
                 ("rift", {"decision_id": self.card["id"]}, "bad_answer"), ("rift", "a", "bad_answer")]
        for n, (target, answer, reason) in enumerate(cases):
            with self.assertRaises(CoreError) as e:
                self.chat.send(self.w.patrick(), target, "Do it", f"chat-key-01{n:02}", answer=answer)
            self.assertEqual(e.exception.reason, reason, (target, answer))
        self.w.core.answer(self.w.patrick(), **self.w.answer_args(self.card))
        with self.assertRaises(CoreError) as e:
            self.chat.send(self.w.patrick(), "rift", "Do it", "chat-key-0199", answer=shown(self.card))
        self.assertEqual(e.exception.reason, "already_answered")
        self.assertEqual((self.count("chat_messages"), self.count("responses")), (0, 1))

    def test_a_high_risk_card_needs_a_fresh_sign_in(self):
        card = self.w.card(by=self.w.rift, task="t1", title="Restart the live server?", risk="high")
        with self.assertRaises(CoreError) as e:
            self.chat.send(self.w.patrick(fresh=False), "rift", "Do it", "chat-key-0201", answer=shown(card))
        self.assertEqual(e.exception.reason, "sign_in_again")
        self.assertEqual(self.count("chat_messages"), 0)
        self.assertEqual(self.chat.send(self.w.patrick(), "rift", "Do it", "chat-key-0201", answer=shown(card))
                         ["answered"]["option_key"], "a")

    def test_a_message_without_an_answer_answers_nothing(self):
        self.chat.send(self.w.patrick(), "rift", "Do it", "chat-key-0301")
        self.assertEqual(self.count("responses"), 0)
        self.assertEqual(self.w.core.get_card(self.w.patrick(), self.card["id"])["status"], "pending")


class AlertTimes(unittest.TestCase):
    """A card says when its alert was queued and sent, so a late alert shows which hop was slow."""

    def test_the_card_carries_its_alert_times(self):
        w = World()
        self.addCleanup(w.close)
        card = w.card()
        a = w.core.get_card(w.patrick(), card["id"])["alert"]
        self.assertEqual((a["status"], a["sent_at"]), ("queued", None))
        self.assertTrue(a["queued_at"])
        w.clock.advance(seconds=4)
        w.store.db.execute("UPDATE push_outbox SET status = 'sent', updated_at = ?", (w.store.now(),))
        a = w.core.get_card(w.patrick(), card["id"])["alert"]
        self.assertEqual((a["status"], a["sent_at"]), ("sent", w.store.now()))


class OverHttp(unittest.TestCase):
    def test_the_phone_sends_the_answer_and_the_card_leaves_the_chat(self):
        s = Served(self)
        seat(s.w)
        flags(s.w, "mobile_chat_write", "mobile_read")
        s.w.core.assign_task(s.w.bramble, "t1", "rift")
        card = s.w.card(by=s.w.rift, task="t1")
        sess = s.w.core.auth.issue_session(s.w.ws, method="passkey", device_id=s.w.device())
        h = {"Cookie": f"tw_session={sess.token}", "Origin": ORIGIN, "X-CSRF-Token": sess.csrf}
        status, _, v = s.call("GET", "/chat/rift", headers=h)
        self.assertEqual((status, [c["id"] for c in v["cards"]]), (200, [card["id"]]))
        status, _, body = s.call("POST", "/chat/rift", headers=h,
                                 body={"text": "Do it", "idempotency_key": "phone-key-0001", "answer": shown(card)})
        self.assertEqual((status, body["answered"]["option_key"]), (200, "a"), body)
        status, _, v = s.call("GET", "/chat/rift", headers=h)
        self.assertEqual(v["cards"], [])


if __name__ == "__main__":
    unittest.main()
