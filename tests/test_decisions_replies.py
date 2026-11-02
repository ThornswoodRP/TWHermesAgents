"""Replying to one message in Patrick's own Hermes and One (redesign step 4, reply style A, plan Part 11).

Hermes keeps no reply link. So the core keeps the link (own_messages.reply_to, migration 13, Hermes's own
id of the message answered), and the message Hermes gets opens with Hermes's own reply pointer, the line
its gateway writes for a reply on a messaging app, in plain sight, exactly as Patrick sees it. Checks:
what Hermes is handed, the quote rules, a reply only in an existing chat, the idempotency key covering
the link, no words in the event chain, the view giving the link back, the route, and the way back from
migration 13. Everything runs in a scratch test workspace;
nothing reaches a real Hermes.
"""

from __future__ import annotations

import json
import unittest

from decisions_helpers import World
from test_decisions_api import Served
from test_decisions_own import SID, Gateways, passkey

from thornswood_decisions.store import CoreError


class Replies(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.g = Gateways(self.w)
        self.own = self.g.own
        self.g.hello("one")
        self.g.feed("one")
        self.n = 0

    def send(self, text="Yes, do that first.", reply_to=7, quote="Shall I fix the launcher log?", sid=SID, key=None,
             by="hermes"):
        self.n += 1
        return self.own.send(passkey(self.w), "one", sid, None if sid else True, text, key or f"reply-key-{self.n:04d}",
                             reply_to, quote, by)

    def refused(self, reason, **kw):
        with self.assertRaises(CoreError) as e:
            self.send(**kw)
        self.assertEqual(e.exception.reason, reason)

    def test_hermes_gets_its_own_reply_pointer_at_the_top_and_the_core_keeps_the_link(self):
        # Hermes's gateway: f'[Replying to{_who}: "{reply_text}"]\n\n{message_text}' (contracts.json "replies").
        r = self.send(quote="Shall I fix:\r\n- the launcher log\n- the backup  ")
        sent = '[Replying to your previous message: "Shall I fix:\n- the launcher log\n- the backup"]\n\nYes, do that first.'
        (item,) = self.own.claim(self.w.ws, "one", self.g.tok["one"], 3, True)["items"]
        self.assertEqual(item["text"], sent)
        self.assertEqual(item["id"], r["message_id"])
        mine = self.own.view(self.w.patrick(), "one", SID)["mine"]
        self.assertEqual([(m["text"], m["reply_to"]) for m in mine], [(sent, 7)])

    def test_a_reply_to_his_own_message_says_so_as_hermes_does(self):
        self.send(quote="Plan the week", by="patrick", reply_to=3)
        (m,) = self.own.view(self.w.patrick(), "one", SID)["mine"]
        self.assertEqual(m["text"], '[Replying to: "Plan the week"]\n\nYes, do that first.')

    def test_the_pointer_is_the_one_hermes_strips_when_it_compresses(self):
        import re
        # agent/context_compressor.py _GATEWAY_REPLY_POINTER_RE at the pinned commit.
        pointer = re.compile(r'\A(?:\[Triggering message id: [^\n]*\]\n\n)?\[Replying to(?: your previous message)?: ".*?"\]\n\n',
                             re.DOTALL)
        for by in ("hermes", "patrick"):
            self.send(by=by, quote="two\nlines", key=f"reply-ptr-{by}")
        for m in self.own.view(self.w.patrick(), "one", SID)["mine"]:
            self.assertEqual(pointer.sub("", m["text"]), "Yes, do that first.")

    def test_a_quote_never_asks_hermes_for_a_file(self):
        # Patrick's Hermes on One, 2 Nov: a quote that held a paste's reference attached the paste a second
        # time, and one cut inside a reference asked for a file named 'AppData/Local/hermes/..."]'.
        import re
        # agent/context_references.py REFERENCE_PATTERN and _PLUGIN_REFERENCE_PATTERN at the pinned commit.
        value = r"""(?:`[^`\n]+`|"[^"\n]+"|'[^'\n]+')"""
        hermes = [re.compile(rf"(?<![\w/])@(?:(?P<simple>diff|staged)\b|(?P<kind>file|folder|git|url):(?P<value>{value}(?::\d+(?:-\d+)?)?|\S+))"),
                  re.compile(rf"(?<![\w/])@(?P<kind>[a-zA-Z][a-zA-Z0-9_-]*):(?P<value>{value}(?::\d+(?:-\d+)?)?|\S+)")]
        quote = ("Look at @file:AppData/Local/hermes/composer-pastes/pasted_content_2026-11-02_18-42-16-109_2255e5.txt "
                 "and @file:\"C:/a b/notes.md\", @folder:src/, @url:https://example.invalid/x, @git:3, @diff, "
                 "@staged and @thing:x; mail a@b.example, ask @bramble.\n\n--- Attached Context ---\n\n"
                 "📄 @file:AppData/Local/hermes/composer-pastes/pasted_content_x.txt (40 tokens)\nthe paste")
        self.send(quote=quote, by="patrick")
        (item,) = self.own.claim(self.w.ws, "one", self.g.tok["one"], 3, True)["items"]
        for pattern in hermes:
            self.assertIsNone(pattern.search(item["text"]), item["text"])
        self.assertEqual(item["text"], '[Replying to: "Look at [pasted text] and [file: notes.md], [folder: src], '
                         'https://example.invalid/x, [git: 3], @\u2060diff, @\u2060staged and [thing: x]; mail a@b.example, '
                         'ask @bramble."]\n\nYes, do that first.')
        self.refused("bad_quote", quote="--- Attached Context ---\n\n📄 @file:x.txt (1 tokens)\nx", key="reply-ctx")

    def test_a_plain_message_carries_no_link(self):
        self.own.send(passkey(self.w), "one", SID, None, "hello", "plain-key-0001")
        (m,) = self.own.view(self.w.patrick(), "one", SID)["mine"]
        self.assertEqual((m["text"], m["reply_to"]), ("hello", None))

    def test_the_quote_rules(self):
        self.refused("bad_quote", quote="")
        self.refused("bad_quote", quote="   \n ")
        self.refused("bad_quote", quote=None)
        self.refused("bad_quote", quote=["words"])
        self.refused("bad_quote", quote="a\x00b")
        self.refused("bad_quote", quote="x" * 2001)
        self.refused("bad_reply", by=None)
        self.refused("bad_reply", by="bramble")
        self.refused("bad_message", reply_to=0)
        self.refused("bad_message", reply_to="7")
        self.refused("bad_message", reply_to=True)
        self.refused("bad_message", reply_to=None)
        self.refused("bad_reply", sid=None)
        self.refused("bad_text", text="x" * 7970)
        self.assertEqual(self.w.store.db.execute("SELECT count(*) FROM own_messages").fetchone()[0], 0)

    def test_the_same_key_is_one_message_and_never_for_another_reply(self):
        first = self.send(key="reply-same-01")
        again = self.send(key="reply-same-01")
        self.assertTrue(again["replayed"])
        self.assertEqual(again["message_id"], first["message_id"])
        with self.assertRaises(CoreError) as e:
            self.send(key="reply-same-01", reply_to=8)
        self.assertEqual(e.exception.reason, "key_reused")
        with self.assertRaises(CoreError) as e:
            self.own.send(passkey(self.w), "one", SID, None, "Yes, do that first.", "reply-same-01")
        self.assertEqual(e.exception.reason, "key_reused")

    def test_a_reply_needs_the_passkey_window_like_any_message(self):
        with self.assertRaises(CoreError) as e:
            self.own.send(passkey(self.w, minutes_ago=31), "one", SID, None, "ok", "reply-late-01", 7, "words", "hermes")
        self.assertEqual(e.exception.reason, "passkey_needed")

    def test_no_words_go_into_the_event_chain(self):
        self.send(text="the secret answer", quote="a secret question")
        rows = [json.loads(r[0]) for r in self.w.store.db.execute(
            "SELECT body_json FROM events WHERE kind = 'own.queued'")]
        self.assertEqual(rows[-1]["reply"], True)
        self.assertFalse(any("secret" in json.dumps(r) for r in rows))


class Route(unittest.TestCase):
    def test_the_phone_sends_a_reply(self):
        from test_decisions_own import ORIGIN
        s = Served(self)
        s.w.store.set_flag(s.w.ws, "mobile_read", True, by="tests")
        g = Gateways(s.w)
        g.hello("one")
        g.feed("one")
        dev = s.w.device()
        sess = s.w.core.auth.issue_session(s.w.ws, method="passkey", device_id=dev)
        h = {"Cookie": f"tw_session={sess.token}", "Origin": ORIGIN, "X-CSRF-Token": sess.csrf}
        status, _, body = s.call("POST", "/own/one/send", headers=h,
                                 body={"session_id": SID, "text": "Go on.", "idempotency_key": "route-reply-01",
                                       "reply_to": 3, "quote": "Step two?", "quote_by": "hermes"})
        self.assertEqual(status, 200, body)
        row = s.w.store.db.execute("SELECT text, reply_to FROM own_messages").fetchone()
        self.assertEqual(tuple(row), ('[Replying to your previous message: "Step two?"]\n\nGo on.', 3))


class Migration13(unittest.TestCase):
    def test_the_way_back_refuses_while_a_message_keeps_a_link(self):
        from thornswood_decisions.schema import DOWNS
        w = World()
        self.addCleanup(w.close)
        checks = DOWNS[13][0]
        count = lambda: [w.store.db.execute(q).fetchone()[0] for q, _ in checks]
        self.assertEqual(count(), [0])
        g = Gateways(w)
        g.hello("one")
        g.feed("one")
        g.own.send(passkey(w), "one", SID, None, "ok", "mig13-key-01", 5, "words", "patrick")
        self.assertEqual(count(), [1])


if __name__ == "__main__":
    unittest.main()
