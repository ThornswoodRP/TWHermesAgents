"""Photos and files on a message to Patrick's own Hermes (the + in the box; Patrick, 2 Nov: "no way to add
files or photos or anything add a + button like Claude and Hermes have").

Checks:
- the core takes a file on its own (its name made safe, its size and count bounded), names it on one
  message when that is sent (only his, only for that Hermes, only once), hands its name, size and SHA-256
  with the claim, gives its bytes only to that gateway's connector and only while the message is leased,
  and drops the bytes once the message has a result;
- a file is taken off before sending, and an upload never sent goes after a day;
- the connector fetches each file, checks it, saves it in Hermes's own images or attachments folder and
  hands each photo to the turn (room_send.py --tw-image) and every other file as an @file: reference; a file
  that does not come whole fails the message before anything reaches Hermes;
- over HTTP: the upload is its bytes, a browser never gets a file's bytes, and the way back from schema 14.
Everything runs in a scratch test workspace with a stand-in for `hermes`; nothing reaches a real Hermes.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from decisions_helpers import World
from test_decisions_api import ORIGIN, Served
from test_decisions_own import HERMES, READY, SID, FakeCore, Gateways, passkey, state_db

from thornswood_decisions import own_connector
from thornswood_decisions.own import file_name
from thornswood_decisions.store import CoreError, downgrade

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"x" * 200


class TheCore(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.g = Gateways(self.w)
        self.g.hello("one")
        self.g.feed("one")
        self.own = self.g.own
        self.p = passkey(self.w)

    def attach(self, name="photo.png", mime="image/png", body=PNG, gw="one"):
        return self.own.attach(self.p, gw, name, mime, body)

    def send(self, files, text="look at these", key="attach-key-0001", gw="one"):
        return self.own.send(self.p, gw, SID, None, text, key, attachments=files)

    def test_a_file_goes_with_one_message_and_its_bytes_only_to_that_connector_while_it_is_sent(self):
        a, b = self.attach(), self.attach("notes.pdf", "application/pdf", PDF)
        self.assertEqual((a["name"], a["mime"], a["size"], a["sha256"]), ("photo.png", "image/png", len(PNG),
                                                                          hashlib.sha256(PNG).hexdigest()))
        with self.assertRaises(CoreError) as e:
            self.own.attachment(self.w.ws, "one", self.g.tok["one"], a["id"])
        self.assertEqual(e.exception.reason, "no_attachment", "not before a message names it and is leased")
        mid = self.send([a["id"], b["id"]])["message_id"]
        (m,) = [x for x in self.own.view(self.p, "one", SID)["mine"]]
        self.assertEqual([f["name"] for f in m["files"]], ["photo.png", "notes.pdf"])
        (item,) = self.own.claim(self.w.ws, "one", self.g.tok["one"], 3, True)["items"]
        self.assertEqual([(f["id"], f["size"], f["sha256"]) for f in item["attachments"]],
                         [(a["id"], a["size"], a["sha256"]), (b["id"], b["size"], b["sha256"])])
        with self.assertRaises(CoreError):
            self.own.attachment(self.w.ws, "me", self.g.tok["me"], a["id"])
        with self.assertRaises(CoreError):
            self.own.attachment(self.w.ws, "one", self.g.tok["me"], a["id"])
        self.assertEqual(self.own.attachment(self.w.ws, "one", self.g.tok["one"], a["id"]), ("image/png", PNG))
        self.own.result(self.w.ws, "one", self.g.tok["one"], mid, "confirmed")
        with self.assertRaises(CoreError):
            self.own.attachment(self.w.ws, "one", self.g.tok["one"], b["id"])
        self.assertEqual(self.w.store.db.execute("SELECT count(*) FROM own_attachments WHERE body IS NOT NULL")
                         .fetchone()[0], 0, "the bytes go once the message has a result")
        self.assertEqual([f["name"] for f in self.own.view(self.p, "one", SID)["mine"][0]["files"]],
                         ["photo.png", "notes.pdf"], "the names stay")

    def test_a_file_goes_once_only_to_the_hermes_it_was_added_for(self):
        self.g.hello("me")
        a = self.attach()
        with self.assertRaises(CoreError) as e:
            self.send([a["id"]], gw="me", key="attach-key-0002")
        self.assertEqual(e.exception.reason, "no_attachment")
        self.send([a["id"]])
        with self.assertRaises(CoreError) as e:
            self.send([a["id"]], key="attach-key-0003")
        self.assertEqual(e.exception.reason, "no_attachment")
        for bad in ("att_x", ["nope"], [a["id"], a["id"]], ["att_" + "a" * 20] * 11):
            with self.assertRaises(CoreError) as e:
                self.send(bad, key="attach-key-0004")
            self.assertEqual(e.exception.reason, "bad_attachments")

    def test_a_message_of_files_only_and_the_same_key_twice(self):
        a = self.attach()
        r = self.send([a["id"]], text="")
        self.assertEqual(self.send([a["id"]], text="")["message_id"], r["message_id"], "the same request again")
        # The older schema cannot hold a message with no words, so the way back stops on it.
        self.w.store.db.execute("UPDATE own_attachments SET body = NULL")
        with self.assertRaises(CoreError) as e:
            downgrade(self.w.store.db, 13)
        self.assertIn("schema 14: 1 messages to his own Hermes that are files alone", str(e.exception))
        with self.assertRaises(CoreError) as e:
            self.own.send(self.p, "one", SID, None, "  ", "attach-key-0005")
        self.assertEqual(e.exception.reason, "bad_text")

    def test_names_sizes_and_counts(self):
        self.assertEqual(file_name("C:\\Users\\p\\My <best> photo?.JPG"), "My _best_ photo_.JPG")
        self.assertEqual(file_name("../../etc/passwd"), "passwd")
        self.assertEqual(file_name("a" * 200 + ".pdf"), "a" * 110 + ".pdf")
        for bad in ("", " . ", None, "///"):
            with self.assertRaises(CoreError):
                file_name(bad)
        self.assertEqual(self.attach(mime="text/html; charset=x")["mime"], "text/html")
        self.assertEqual(self.attach(mime="<script>")["mime"], "application/octet-stream")
        with self.assertRaises(CoreError) as e:
            self.attach(body=b"")
        self.assertEqual(e.exception.reason, "bad_file")
        with self.assertRaises(CoreError) as e:
            self.attach(body=b"x" * (20 * 1024 * 1024 + 1))
        self.assertEqual(e.exception.reason, "too_big")
        for _ in range(18):
            self.attach()
        with self.assertRaises(CoreError) as e:
            self.attach()
        self.assertEqual(e.exception.reason, "too_many_files")

    def test_taken_off_before_sending_and_gone_after_a_day(self):
        a, b = self.attach(), self.attach()
        self.assertTrue(self.own.unattach(self.p, "one", a["id"])["removed"])
        with self.assertRaises(CoreError):
            self.own.unattach(self.p, "one", a["id"])
        self.w.clock.advance(seconds=86401)
        self.attach()
        ids = [r[0] for r in self.w.store.db.execute("SELECT id FROM own_attachments")]
        self.assertNotIn(b["id"], ids)

    def test_off_with_own_chat_write(self):
        self.w.store.set_flag(self.w.ws, "own_chat_write", False, by="tests")
        with self.assertRaises(CoreError) as e:
            self.attach()
        self.assertEqual(e.exception.reason, "own_chat_off")


class AttachCore(FakeCore):
    def fetch(self, op, body):
        try:
            return 200, self.own.attachment(self.ws, self.gw, self.token, body["id"])[1]
        except CoreError as exc:
            return exc.status, b""


class TheConnector(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.g = Gateways(self.w)
        self.g.hello("one")
        self.g.feed("one")
        self.p = passkey(self.w)
        self.dir = Path(tempfile.mkdtemp())
        self.db = state_db(self.dir)
        (self.dir / "hermes.py").write_text(HERMES, encoding="utf-8")
        (self.dir / "ready.py").write_text(READY, encoding="utf-8")
        self.log = self.dir / "turns.jsonl"
        os.environ["TW_FAKE_LOG"] = str(self.log)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("TW_FAKE_LOG", "TW_FAKE_MODE", "TW_FAKE_READY")])
        hermes = own_connector.Hermes([sys.executable, str(self.dir / "hermes.py")],
                                      [sys.executable, "-I", str(self.dir / "ready.py")], self.dir, "One's Hermes")
        self.core = AttachCore(self.g.own, self.w.ws, "one", self.g.tok["one"])
        self.conn = own_connector.OwnConnector(self.core, hermes, self.db, say=lambda _: None)

    def round(self):
        self.conn.once()
        for t in self.conn.running:
            t.join(30)
        self.conn._report()

    def mine(self):
        return self.g.own.view(self.p, "one", "s_plan")["mine"]

    def test_photos_go_as_the_image_and_files_as_references_in_hermess_own_folders(self):
        own = self.g.own
        a = own.attach(self.p, "one", "beach.png", "image/png", PNG)
        b = own.attach(self.p, "one", "my notes.pdf", "application/pdf", PDF)
        c = own.attach(self.p, "one", "second.png", "image/png", PNG)
        mid = own.send(self.p, "one", "s_plan", None, "what do you see?", "attach-key-0101",
                       attachments=[a["id"], b["id"], c["id"]])["message_id"]
        self.round()
        (turn,) = [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()]
        args, text = turn["args"], turn["text"]
        beach = self.dir / "images" / "phone" / mid / "beach.png"
        notes = self.dir / "attachments" / "phone" / mid / "my notes.pdf"
        second = self.dir / "images" / "phone" / mid / "second.png"
        # Each photo goes to the turn the way Desktop sends one (room_send.py --tw-image); Hermes's own --image
        # is refused by its gateway chat, so it is never used.
        at = args.index("--query-file") + 2
        self.assertEqual(args[at:at + 5],
                         ["--tw-files", "--tw-image", str(beach), "--tw-image", str(second)])
        self.assertNotIn("--image", args)
        self.assertEqual(text, f'what do you see?\n\n@file:"{notes}"')
        self.assertEqual((beach.read_bytes(), notes.read_bytes()), (PNG, PDF))
        if os.name != "nt":
            self.assertEqual(notes.stat().st_mode & 0o777, 0o600)
            self.assertEqual(notes.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.mine()[0]["status"], "sent")

    def test_a_file_that_does_not_come_whole_fails_the_message_before_hermes_is_asked(self):
        own = self.g.own
        a = own.attach(self.p, "one", "beach.png", "image/png", PNG)
        own.send(self.p, "one", "s_plan", None, "look", "attach-key-0102", attachments=[a["id"]])
        real = self.core.fetch
        self.core.fetch = lambda op, body: (200, real(op, body)[1][:-1])
        self.round()
        self.assertFalse(self.log.exists(), "Hermes was never asked")
        m = self.mine()[0]
        self.assertEqual(m["status"], "dead")
        self.assertIn("changed or cut short", m["reason"])

    def test_a_name_the_core_never_gives_is_refused(self):
        for name in ("../x", "a/b", "c:d", "", ".."):
            with self.assertRaises(ValueError):
                own_connector.save_attachment(self.dir, "own_abcdefgh", {"name": name, "mime": "image/png"}, PNG)
        with self.assertRaises(ValueError):
            own_connector.save_attachment(self.dir, "../own_x", {"name": "a.png", "mime": "image/png"}, PNG)


class OverHttp(unittest.TestCase):
    def setUp(self):
        self.s = Served(self)
        self.w = self.s.w
        self.g = Gateways(self.w)
        self.g.hello("one")
        self.g.feed("one")
        self.w.store.set_flag(self.w.ws, "mobile_read", True, by="tests")
        sess = self.w.core.auth.issue_session(self.w.ws, method="passkey", device_id=self.w.device())
        self.h = {"Cookie": f"tw_session={sess.token}", "Origin": ORIGIN, "X-CSRF-Token": sess.csrf}

    def upload(self, path, body, headers):
        con = http.client.HTTPConnection("127.0.0.1", self.s.port, timeout=10)
        try:
            con.request("POST", "/api/v1" + path, body=body, headers=headers)
            resp = con.getresponse()
            return resp.status, json.loads(resp.read() or b"{}")
        finally:
            con.close()

    def test_the_upload_is_its_bytes_and_a_browser_never_gets_a_files_bytes(self):
        status, body = self.upload("/own/one/attach?name=beach.png", PNG, {**self.h, "Content-Type": "image/png"})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["name"], body["size"], body["sha256"]),
                         ("beach.png", len(PNG), hashlib.sha256(PNG).hexdigest()))
        status, _, again = self.s.call("POST", "/own/one/attachment", headers=self.h, body={"id": body["id"]})
        self.assertEqual((status, again.get("error")), (403, "not_a_feed"))
        status, _, sent = self.s.call("POST", "/own/one/send", headers=self.h,
                                      body={"session_id": SID, "text": "", "idempotency_key": "phone-key-77",
                                            "attachments": [body["id"]]})
        self.assertEqual((status, sent["status"]), (200, "queued"), sent)

    def test_an_upload_without_the_csrf_token_is_refused(self):
        h = {k: v for k, v in self.h.items() if k != "X-CSRF-Token"}
        status, body = self.upload("/own/one/attach?name=beach.png", PNG, {**h, "Content-Type": "image/png"})
        self.assertEqual((status, body.get("error")), (403, "csrf"))


if __name__ == "__main__":
    unittest.main()
