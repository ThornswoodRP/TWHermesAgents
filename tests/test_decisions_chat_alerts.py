"""Chat alerts (alerts.py; TWHermesAgents #83 and #42, 9 Nov): one line on his phone when a turn he started
ends, or when his own Hermes asks him something, as Hermes Desktop notifies for both.

Checks that a Bot Chat or Bot session turn that finished, and a turn of his own Hermes, each queue one
"answered" alert opening that chat, and a steer into a Bot's running turn or a room message queues none; that
a turn that failed or ended unconfirmed queues one "stopped" alert, in a Bot's chat or his own Hermes (#109); that
a question or approval his own Hermes opens queues one "asks" alert, once; that the push worker is handed them
with a payload of who and the chat's path and nothing of the chat; that results, Retry-After and a lapsed
lease work for them as for a card's; and that the service worker and the app know the chat paths. Everything
runs in a scratch test workspace; nothing reaches a push service.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from decisions_helpers import World
from test_decisions_chat import flags, seat
from test_decisions_own import SID, Gateways, passkey
from test_decisions_own_turns import QUESTION
from test_decisions_push import NEEDS_CRYPTO, Browser, worker_keys

from thornswood_decisions import alerts, push_worker, webpush
from thornswood_decisions.chat import Chat
from thornswood_decisions.feeds import Feeds
from thornswood_decisions.push import Push

APP = Path(alerts.__file__).resolve().parent / "app"


def rows(w: World) -> list[tuple]:
    return [tuple(r) for r in w.store.db.execute("SELECT kind, ref, who, path, status FROM chat_alerts ORDER BY rowid")]


class Queued(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        seat(self.w)
        flags(self.w, "mobile_chat_write", "connector_effects")
        self.chat = Chat(self.w.store)
        self.tok = Feeds(self.w.store).new_token(self.w.ws, "connector", by="tests")

    def ended(self, target, ref="turn finished", outcome="confirmed", session=None, key="key-alert-0001"):
        mid = self.chat.send(self.w.patrick(), target, "hi", key, session=session)["message_id"]
        [item] = self.chat.claim(self.w.ws, self.tok, 3)["items"]
        self.chat.result(self.w.ws, self.tok, item["id"], outcome, ref)
        return mid

    def test_a_bot_chat_turn_that_finished_is_said(self):
        mid = self.ended("rift")
        self.assertEqual(rows(self.w), [("answered", mid, "Rift", "/m/bots/rift", "queued")])

    def test_a_session_turn_opens_that_session(self):
        mid = self.ended("rift", session="s_rift_lo")
        self.assertEqual(rows(self.w), [("answered", mid, "Rift", "/m/bots/rift/s/s_rift_lo", "queued")])

    def test_a_steer_into_a_bots_turn_and_a_room_message_are_not(self):
        self.ended("rift", ref="steered into the running turn", session="s_rift_lo", key="key-alert-0002")
        self.ended("room", ref="", key="key-alert-0004")
        self.ended("room", outcome="failed", ref="", key="key-alert-0005")
        self.assertEqual(rows(self.w), [])

    def test_a_bot_turn_that_failed_or_ended_unconfirmed_is_said_as_stopped(self):
        failed = self.ended("echo", outcome="failed", ref="", key="key-alert-0003")
        unsure = self.ended("rift", outcome="uncertain", ref="", session="s_rift_lo", key="key-alert-0006")
        self.assertEqual(rows(self.w), [("stopped", failed, "Echo", "/m/bots/echo", "queued"),
                                        ("stopped", unsure, "Rift", "/m/bots/rift/s/s_rift_lo", "queued")])

    def test_a_message_the_bot_was_not_ready_for_is_not(self):
        self.ended("rift", outcome="not_ready", ref="", key="key-alert-0007")
        self.assertEqual(rows(self.w), [])

    def test_the_same_result_twice_is_one_alert(self):
        mid = self.ended("rift")
        self.chat.result(self.w.ws, self.tok, mid, "confirmed", "turn finished")
        self.assertEqual(len(rows(self.w)), 1)


class Own(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.g = Gateways(self.w)
        self.own = self.g.own
        self.g.hello("one")

    def claim(self, turns=()):
        return self.own.claim(self.w.ws, "one", self.g.tok["one"], 3, True, doing={"turns": list(turns)})

    def test_his_own_turn_that_ended_is_said(self):
        mid = self.own.send(passkey(self.w), "one", SID, None, "tidy up", "own-key-0001")["message_id"]
        self.claim()
        self.own.result(self.w.ws, "one", self.g.tok["one"], mid, "confirmed", "turn finished")
        self.assertEqual(rows(self.w), [("answered", mid, "One", f"/m/hermes/one/{SID}", "queued")])

    def test_his_own_turn_that_failed_or_ended_unconfirmed_is_said_as_stopped(self):
        for n, outcome in enumerate(("failed", "uncertain")):
            mid = self.own.send(passkey(self.w), "one", SID, None, "tidy up", f"own-key-001{n}")["message_id"]
            self.claim()
            self.own.result(self.w.ws, "one", self.g.tok["one"], mid, outcome, reason="the turn stopped")
            self.assertEqual(rows(self.w)[-1], ("stopped", mid, "One", f"/m/hermes/one/{SID}", "queued"))
        self.assertEqual(len(rows(self.w)), 2)

    def test_a_new_chat_opens_the_chat_hermes_made(self):
        mid = self.own.send(passkey(self.w), "one", None, True, "start", "own-key-0002")["message_id"]
        self.claim()
        self.own.result(self.w.ws, "one", self.g.tok["one"], mid, "confirmed", session_id="made_1")
        self.assertEqual(rows(self.w)[0][3], "/m/hermes/one/made_1")

    def test_a_question_is_said_once(self):
        mid = self.own.send(passkey(self.w), "one", SID, None, "tidy up", "own-key-0003")["message_id"]
        self.claim()
        turn = {"id": mid, "state": "tool", "tool": "clarify", "admitted": True, "asks": [QUESTION]}
        self.claim([turn])
        self.claim([turn])
        self.assertEqual(rows(self.w), [("asks", f"{mid}:p9", "One", f"/m/hermes/one/{SID}", "queued")])


@NEEDS_CRYPTO
class Sent(unittest.TestCase):
    def setUp(self):
        from test_decisions_phone import phone
        self.w = World()
        self.addCleanup(self.w.close)
        self.push = Push(self.w.store)
        self.token = Feeds(self.w.store).new_token(self.w.ws, "push", by="tests")
        self.vapid, self.seal = worker_keys()
        _, _, self.p = phone(self.w)
        self.push.hello(self.w.ws, self.token, webpush.vapid_public(self.vapid), push_worker.seal_public(self.seal))
        self.w.store.set_flag(self.w.ws, "push_send", True, by="tests")

    def queue(self, path="/m/bots/rift", who="Rift", ref="msg_1", kind="answered"):
        with self.w.store.tx() as db:
            return alerts.queue(self.w.store, db, self.w.ws, kind, ref, who, path)

    def test_the_payload_is_who_and_the_chat_only(self):
        self.push.subscribe(self.p, Browser().subscription())
        self.queue()
        [it] = self.push.claim(self.w.ws, self.token)["items"]
        self.assertEqual(set(it["payload"]), {"v", "event_id", "kind", "who", "path"})
        self.assertEqual((it["payload"]["kind"], it["payload"]["who"], it["payload"]["path"]),
                         ("answered", "Rift", "/m/bots/rift"))
        self.assertLessEqual(len(it["topic"]), 32)
        sid = it["subscriptions"][0]["id"]
        self.assertEqual(self.push.result(self.w.ws, self.token, it["id"], [{"subscription_id": sid, "status": 201}]),
                         {"id": it["id"], "status": "sent"})
        self.assertEqual(rows(self.w)[0][4], "sent")

    def test_a_busy_push_service_is_tried_again(self):
        self.push.subscribe(self.p, Browser().subscription())
        self.queue()
        [it] = self.push.claim(self.w.ws, self.token)["items"]
        sid = it["subscriptions"][0]["id"]
        got = self.push.result(self.w.ws, self.token, it["id"], [{"subscription_id": sid, "status": 503}])
        self.assertEqual(got["status"], "queued")

    def test_no_phone_with_alerts_on_means_none(self):
        self.queue()
        self.assertEqual(self.push.claim(self.w.ws, self.token)["items"], [])
        self.assertEqual(rows(self.w)[0][4], "cancelled")

    def test_only_a_chat_path_and_a_plain_name(self):
        for bad in ({"path": "/m/decisions"}, {"path": "https://example.com/m/bots/rift"}, {"path": "/m/bots/../x"},
                    {"who": "<b>Rift</b>"}, {"kind": "told"}, {"ref": ""}):
            self.assertFalse(self.queue(**bad), bad)
        self.assertEqual(rows(self.w), [])


class TheApp(unittest.TestCase):
    def test_the_worker_and_the_app_take_the_same_chat_paths(self):
        sw = (APP / "sw.js").read_text(encoding="utf-8")
        app = (APP / "app.js").read_text(encoding="utf-8")
        line = next(ln for ln in sw.splitlines() if ln.startswith("const CHAT_PATH"))
        self.assertIn(line, app)
        self.assertIn(alerts.PATH.pattern.replace("^/m/", "").rstrip("$").replace("/", "\\/"), line)
        self.assertIn("toWorker({ opened: location.pathname })", app)
        self.assertIn("' answered'", sw)
        self.assertIn("' asks you something'", sw)
        self.assertIn("' did not finish'", sw)
        self.assertEqual(set(alerts.KINDS), set(__import__("re").findall(r"(\w+): who =>", sw)))


if __name__ == "__main__":
    unittest.main()
