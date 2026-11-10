"""Photos and files on a message to a Bot or a room (#81: "A Bot chat and a room take text alone, so a
screenshot for a Bot has to go through his own Hermes"), and photos to his own Hermes again.

Checks:
- the core takes a file for one Bot or room on its own (named, typed and bounded as own.py checks one),
  names it on one message to that chat when it is sent (only his, only for that chat, only once), lets a
  message be files alone, refuses more than a room's store takes, hands each file's name, size and SHA-256
  with the claim, gives its bytes only to the Bots' connector and only while the message is leased, keeps
  them while the message waits for a gateway, and drops them once the message has a result;
- the Bots' connector, over HTTP against the real core, saves a Bot's files in that Bot's own home, hands
  each photo to the turn (room_send.py --tw-image) and every other file as an @file: reference, and sends
  a room's files to the room helper; a file that does not come whole fails the message before Hermes is
  asked;
- room_send.py's turn puts each photo in Hermes's image staging folder and names it on prompt.submit
  (Hermes's gateway chat refuses its own --image), never steers a message with files into a running turn,
  and a room message uploads each file into the room's store and names them on groups.send;
- the app's + goes to the chat's own route in a Bot chat and a room.
Everything runs in a scratch test workspace with stand-ins for Hermes; nothing reaches a real Hermes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from decisions_helpers import World
from test_decisions_api import Served
from test_decisions_busy import MAIN, VIEW
from test_decisions_chat import FAKE_HELPER, ROOM, STUB_CLIENT, STUB_RUNTIME, flags, seat

from thornswood_decisions import connector, own_connector
from thornswood_decisions.chat import Chat
from thornswood_decisions.feeds import Feeds
from thornswood_decisions.store import CoreError, downgrade

HERE = Path(connector.__file__).resolve().parent
ROOM_SEND = HERE / "room_send.py"
APP = (HERE / "app" / "app.js").read_text(encoding="utf-8")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x01" * 64
PDF = b"%PDF-1.7\n" + b"x" * 200
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x02" * 64


class TheCore(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        seat(self.w)
        flags(self.w, "mobile_chat_write", "connector_effects")
        self.chat = Chat(self.w.store)
        self.p = self.w.patrick()
        self.tok = Feeds(self.w.store).new_token(self.w.ws, "connector", by="tests")

    def attach(self, target="rift", name="shot.png", mime="image/png", body=PNG):
        return self.chat.attach(self.p, target, name, mime, body)

    def send(self, target, files, text="look at this", key="chat-attach-0001"):
        return self.chat.send(self.p, target, text, key, attachments=files)

    def test_a_file_goes_with_one_message_and_its_bytes_only_to_the_connector_while_it_is_sent(self):
        a, b = self.attach(), self.attach(name="notes.pdf", mime="application/pdf", body=PDF)
        self.assertEqual((a["name"], a["mime"], a["size"], a["sha256"]),
                         ("shot.png", "image/png", len(PNG), hashlib.sha256(PNG).hexdigest()))
        with self.assertRaises(CoreError) as e:
            self.chat.attachment(self.w.ws, self.tok, a["id"])
        self.assertEqual(e.exception.reason, "no_attachment", "not before a message names it and is leased")
        mid = self.send("rift", [a["id"], b["id"]])["message_id"]
        (m,) = self.chat.view(self.p, "rift")["mine"]
        self.assertEqual([f["name"] for f in m["files"]], ["shot.png", "notes.pdf"])
        (item,) = self.chat.claim(self.w.ws, self.tok)["items"]
        self.assertEqual([(f["id"], f["name"], f["mime"], f["size"], f["sha256"]) for f in item["attachments"]],
                         [(a["id"], "shot.png", "image/png", a["size"], a["sha256"]),
                          (b["id"], "notes.pdf", "application/pdf", b["size"], b["sha256"])])
        feed = Feeds(self.w.store).new_token(self.w.ws, "work", by="tests")
        for tok in (None, "nope", feed):
            with self.assertRaises(CoreError):
                self.chat.attachment(self.w.ws, tok, a["id"])
        self.assertEqual(self.chat.attachment(self.w.ws, self.tok, a["id"]), PNG)
        self.chat.result(self.w.ws, self.tok, mid, "confirmed", "turn finished")
        with self.assertRaises(CoreError):
            self.chat.attachment(self.w.ws, self.tok, b["id"])
        self.assertEqual(self.w.store.db.execute("SELECT count(*) FROM chat_attachments WHERE body IS NOT NULL")
                         .fetchone()[0], 0, "the bytes go once the message has a result")
        self.assertEqual([f["name"] for f in self.chat.view(self.p, "rift")["mine"][0]["files"]],
                         ["shot.png", "notes.pdf"], "the names stay")

    def test_a_message_waiting_for_the_gateway_keeps_its_files_and_a_lapsed_one_drops_them(self):
        a = self.attach()
        mid = self.send("rift", [a["id"]])["message_id"]
        self.chat.claim(self.w.ws, self.tok)
        self.chat.result(self.w.ws, self.tok, mid, "not_ready", reason="it is starting")
        (item,) = self.chat.claim(self.w.ws, self.tok)["items"]
        self.assertEqual(item["attachments"][0]["id"], a["id"])
        self.assertEqual(self.chat.attachment(self.w.ws, self.tok, a["id"]), PNG, "sent again whole")
        self.w.clock.advance(minutes=21)
        self.chat.claim(self.w.ws, self.tok)
        self.assertEqual(self.w.store.db.execute("SELECT count(*) FROM chat_attachments WHERE body IS NOT NULL")
                         .fetchone()[0], 0, "a message that may have gone keeps only the names")

    def test_a_file_goes_once_only_to_the_chat_it_was_added_for(self):
        a = self.attach()
        with self.assertRaises(CoreError) as e:
            self.send("echo", [a["id"]])
        self.assertEqual(e.exception.reason, "no_attachment")
        self.send("rift", [a["id"]])
        with self.assertRaises(CoreError) as e:
            self.send("rift", [a["id"]], key="chat-attach-0002")
        self.assertEqual(e.exception.reason, "no_attachment")
        for bad in ("att_x", ["nope"], [a["id"], a["id"]], ["att_" + "a" * 20] * 11):
            with self.assertRaises(CoreError) as e:
                self.send("rift", bad, key="chat-attach-0003")
            self.assertEqual(e.exception.reason, "bad_attachments")
        with self.assertRaises(CoreError) as e:
            self.chat.attach(self.p, "nobody", "a.png", "image/png", PNG)
        self.assertEqual(e.exception.reason, "no_chat")

    def test_files_alone_the_same_key_twice_and_the_way_back(self):
        a = self.attach("bramble")
        r = self.send("bramble", [a["id"]], text="")
        self.assertEqual(self.send("bramble", [a["id"]], text="")["message_id"], r["message_id"], "the same request again")
        with self.assertRaises(CoreError) as e:
            self.chat.send(self.p, "bramble", "  ", "chat-attach-0004")
        self.assertEqual(e.exception.reason, "bad_text")
        with self.assertRaises(CoreError) as e:
            downgrade(self.w.store.db, 20)
        self.assertIn("schema 21: 1 files and photos on their way to a Bot or a room", str(e.exception))
        self.w.store.db.execute("UPDATE chat_attachments SET body = NULL")
        with self.assertRaises(CoreError) as e:
            downgrade(self.w.store.db, 20)
        self.assertIn("messages to a Bot or a room that are files alone", str(e.exception))
        self.w.store.db.execute("UPDATE chat_messages SET text = 'x'")
        self.assertEqual(downgrade(self.w.store.db, 20)["to"], 20)
        with self.assertRaises(Exception):
            self.w.store.db.execute("INSERT INTO chat_messages(id, workspace_id, target, text, send_key, principal_id, "
                                    "status, created_at, updated_at) VALUES('m', ?, 'rift', '', 'k', ?, 'queued', "
                                    "'t', 't')", (self.w.ws, self.p.id))

    def test_a_room_takes_what_its_store_takes(self):
        nine = [self.attach(ROOM)["id"] for _ in range(9)]
        with self.assertRaises(CoreError) as e:
            self.send(ROOM, nine)
        self.assertEqual(e.exception.reason, "room_files")
        self.assertEqual(self.w.store.db.execute("SELECT count(*) FROM chat_attachments WHERE message_id IS NULL")
                         .fetchone()[0], 9, "refused whole: every file is still his to send")
        big = self.attach(ROOM, "big.bin", "application/octet-stream", b"x" * 15_000_001)["id"]
        with self.assertRaises(CoreError) as e:
            self.send(ROOM, [big], key="chat-attach-0005")
        self.assertEqual(e.exception.reason, "room_files")
        self.assertEqual(self.send(ROOM, nine[:8], key="chat-attach-0006")["target"], ROOM)
        (item,) = self.chat.claim(self.w.ws, self.tok)["items"]
        self.assertEqual((item["target"], item["room_id"], len(item["attachments"])), ("room", ROOM, 8))

    def test_taken_off_before_sending_and_off_with_chat_write(self):
        a = self.attach()
        self.assertTrue(self.chat.unattach(self.p, "rift", a["id"])["removed"])
        with self.assertRaises(CoreError):
            self.chat.unattach(self.p, "rift", a["id"])
        self.w.store.set_flag(self.w.ws, "mobile_chat_write", False, by="tests")
        with self.assertRaises(CoreError) as e:
            self.attach()
        self.assertEqual(e.exception.reason, "chat_write_off")


class TheConnector(unittest.TestCase):
    """The Bots' connector over HTTP against the real core, with a stand-in helper for Hermes."""

    def setUp(self):
        self.s = Served(self)
        self.w = self.s.w
        seat(self.w)
        flags(self.w, "mobile_chat_write", "connector_effects")
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (self.dir / "hermes.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        (self.dir / "helper.py").write_text(FAKE_HELPER, encoding="utf-8")
        self.log = self.dir / "log.jsonl"
        old = {k: os.environ.get(k) for k in ("TW_FAKE_LOG", "TW_FAKE_RC", "TW_FAKE_READY", "TW_FAKE_ROOM")}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in old.items()])
        os.environ["TW_FAKE_LOG"] = str(self.log)
        tok = Feeds(self.w.store).new_token(self.w.ws, "connector", by="tests")
        hermes = connector.Hermes([sys.executable, str(self.dir / "hermes.py")], sys.executable, self.dir,
                                  helper=self.dir / "helper.py")
        self.core = connector.Core(f"http://127.0.0.1:{self.s.port}/api/v1", tok)
        self.c = connector.Connector(self.core, hermes, say=lambda _: None)
        self.chat = Chat(self.w.store)
        self.p = self.w.patrick()

    def lines(self):
        return [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()]

    def status(self, mid):
        return tuple(self.w.store.db.execute("SELECT status, reason FROM chat_messages WHERE id = ?", (mid,)).fetchone())

    def test_a_bots_photos_go_to_its_turn_and_other_files_as_references_in_its_own_home(self):
        a = self.chat.attach(self.p, "wisp", "screen shot.png", "image/png", PNG)
        b = self.chat.attach(self.p, "wisp", "log.txt", "text/plain", b"line one\n")
        c = self.chat.attach(self.p, "wisp", "phone.heic", "image/heic", HEIC)
        d = self.chat.attach(self.p, "wisp", "two.jpg", "image/jpeg", JPEG)
        mid = self.chat.send(self.p, "wisp", "what is wrong here?", "chat-attach-0101",
                             attachments=[a["id"], b["id"], c["id"], d["id"]])["message_id"]
        self.c.once()
        turn = next(x for x in self.lines() if "argv" in x)
        home = self.dir / ".hermes" / "profiles" / "wisp"
        shot = home / "images" / "phone" / mid / "screen shot.png"
        log = home / "attachments" / "phone" / mid / "log.txt"
        heic = home / "attachments" / "phone" / mid / "phone.heic"
        two = home / "images" / "phone" / mid / "two.jpg"
        self.assertEqual(turn["home"], str(home), "the Bot's own home, where the files are")
        args = turn["argv"]
        self.assertEqual(args[args.index("--query-file") + 2:],
                         ["--tw-files", "--tw-image", str(shot), "--tw-image", str(two)])
        self.assertEqual(turn["text"], f"what is wrong here?\n\n@file:{log} @file:{heic}",
                         "a photo Hermes would not take as an image goes as a file")
        self.assertEqual((shot.read_bytes(), log.read_bytes(), two.read_bytes()), (PNG, b"line one\n", JPEG))
        if os.name != "nt":
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.status(mid)[0], "sent")
        self.assertEqual(self.w.store.db.execute("SELECT count(*) FROM chat_attachments WHERE body IS NOT NULL")
                         .fetchone()[0], 0)

    def test_photos_alone_say_so_and_go_into_a_session_too(self):
        a = self.chat.attach(self.p, "rift", "a.png", "image/png", PNG)
        self.chat.send(self.p, "rift", "", "chat-attach-0102", attachments=[a["id"]])
        self.c.once()
        turn = next(x for x in self.lines() if "argv" in x)
        self.assertEqual(turn["text"], "[Patrick attached a.png]")

    def test_a_rooms_files_go_to_the_room_helper_whole(self):
        a = self.chat.attach(self.p, ROOM, "plan.pdf", "application/pdf", PDF)
        b = self.chat.attach(self.p, ROOM, "a.png", "image/png", PNG)
        mid = self.chat.send(self.p, ROOM, "for everyone", "chat-attach-0103", attachments=[a["id"], b["id"]])["message_id"]
        self.c.once()
        (req,) = [x["helper"] for x in self.lines() if x.get("helper", {}).get("op") == "send"]
        self.assertEqual((req["room_id"], req["event_id"], req["text"]), (ROOM, f"tw-chat.{mid}", "for everyone"))
        self.assertEqual([(f["id"], f["name"], f["mime"], base64.b64decode(f["data_base64"])) for f in req["files"]],
                         [(a["id"], "plan.pdf", "application/pdf", PDF), (b["id"], "a.png", "image/png", PNG)])
        self.assertEqual(self.status(mid)[0], "sent")
        self.assertFalse((self.dir / ".hermes").exists(), "a room keeps its files in its own store")

    def test_a_file_that_does_not_come_whole_fails_the_message_before_hermes_is_asked(self):
        a = self.chat.attach(self.p, "echo", "a.png", "image/png", PNG)
        mid = self.chat.send(self.p, "echo", "look", "chat-attach-0104", attachments=[a["id"]])["message_id"]
        real = self.core.fetch
        self.core.fetch = lambda op, body: (lambda r: (r[0], r[1][:-1]))(real(op, body))
        self.c.once()
        self.assertFalse(any("argv" in x for x in self.lines()) if self.log.exists() else False, "Hermes was never asked")
        status, reason = self.status(mid)
        self.assertEqual(status, "dead")
        self.assertIn("changed or cut short", reason)


# Stand-ins for the parts of Hermes room_send.py stages a photo with.
STAGING = textwrap.dedent('''
    import os
    from pathlib import Path
    def get_image_cache_dir():
        d = Path(os.environ["HERMES_HOME"]) / "cache" / "images"
        d.mkdir(parents=True, exist_ok=True)
        return d
''')
SNIFF = textwrap.dedent('''
    def sniff_image_mime(data):
        if data.startswith(b'\\x89PNG\\r\\n\\x1a\\n'):
            return 'image/png'
        if data.startswith(b'\\xff\\xd8\\xff'):
            return 'image/jpeg'
        return None
''')
ROOM_STORE = textwrap.dedent('''
    def _sniff_known_mime(data):
        if data.startswith(b'\\x89PNG\\r\\n\\x1a\\n'):
            return 'image/png'
        if data.startswith(b'%PDF-'):
            return 'application/pdf'
        try:
            data.decode('utf-8')
            return 'text/plain' if b'\\x00' not in data else 'application/octet-stream'
        except UnicodeDecodeError:
            return 'application/octet-stream'
''')
UPLOAD = '''        if method == "groups.attachment.upload":
            if os.environ.get("TW_STUB_UPLOAD") == "refused":
                raise GatewayRPCError("image bytes do not match the declared MIME type")
            import base64
            return {"attachment_id": "att_" + "0" * 32, "kind": params["kind"], "name": params["name"],
                    "size": len(base64.b64decode(params["data_base64"])), "mime": params["mime"],
                    "sha256": "x", "state": "uploaded", "created_at": 1.0, "idempotent": False}
'''
SENT = '        return {"event": {"seq": 12}}'
assert SENT in STUB_CLIENT, "the room stub's answer moved"
UPLOADING_CLIENT = STUB_CLIENT.replace(SENT, UPLOAD + SENT)


class TheHelper(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        root = self.dir / "hermes"
        for pkg in ("hermes_cli", "gateway", "gateway/platforms"):
            (root / pkg).mkdir(parents=True)
            (root / pkg / "__init__.py").write_text("", encoding="utf-8")
        (root / "hermes_cli" / "gateway_chat_view.py").write_text(VIEW, encoding="utf-8")
        (root / "hermes_cli" / "main.py").write_text(MAIN, encoding="utf-8")
        (root / "hermes_cli" / "gateway_runtime.py").write_text(STUB_RUNTIME, encoding="utf-8")
        (root / "hermes_cli" / "gateway_client.py").write_text(UPLOADING_CLIENT, encoding="utf-8")
        (root / "gateway" / "platforms" / "base.py").write_text(STAGING, encoding="utf-8")
        (root / "gateway" / "session_ingress_media.py").write_text(SNIFF, encoding="utf-8")
        (root / "gateway" / "hosted_room_attachments.py").write_text(ROOM_STORE, encoding="utf-8")
        (root / "hermes_constants.py").write_text(
            "from pathlib import Path\ndef get_default_hermes_root():\n    return Path('/home/hermes/.hermes')\n",
            encoding="utf-8")
        self.root = root
        self.home = self.dir / "home"
        self.calls = self.dir / "calls.jsonl"

    def run_turn(self, *extra, text="look", running=False):
        query = self.dir / "message.txt"
        query.write_text(text, encoding="utf-8")
        self.calls.write_text("", encoding="utf-8")
        env = {**os.environ, "TW_STUB": json.dumps({"busy": "steer", "running": running}), "TW_CALLS": str(self.calls),
               "HERMES_HOME": str(self.home)}
        out = subprocess.run([sys.executable, "-I", str(ROOM_SEND), "--hermes-root", str(self.root), "chat", "-Q",
                              "--format", "stream-json", "--query-file", str(query), *extra, "--resume", "s1"],
                             capture_output=True, text=True, env=env, timeout=60)
        calls = [json.loads(ln) for ln in self.calls.read_text(encoding="utf-8").splitlines()]
        return out, calls

    def test_photos_are_staged_and_named_on_prompt_submit(self):
        a, b = self.dir / "a.png", self.dir / "b.jpg"
        a.write_bytes(PNG)
        b.write_bytes(JPEG)
        out, calls = self.run_turn("--tw-files", "--tw-image", str(a), "--tw-image", str(b))
        self.assertEqual(out.returncode, 0, out.stderr)
        (submit,) = [c[1] for c in calls if c[0] == "prompt.submit"]
        staged = self.home / "cache" / "images"
        self.assertEqual(submit["attachments"], [
            {"path": str((staged / (hashlib.sha256(PNG).hexdigest() + ".png")).resolve()), "mime": "image/png"},
            {"path": str((staged / (hashlib.sha256(JPEG).hexdigest() + ".jpg")).resolve()), "mime": "image/jpeg"}])
        self.assertEqual([p.read_bytes() for p in sorted(staged.iterdir())],
                         [x for _, x in sorted([(hashlib.sha256(PNG).hexdigest(), PNG),
                                                (hashlib.sha256(JPEG).hexdigest(), JPEG)])])
        self.assertNotIn("--tw-image", out.stdout + out.stderr)

    def test_a_message_with_files_is_never_steered_and_one_without_photos_names_none(self):
        out, calls = self.run_turn("--tw-files", running=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual([c[0] for c in calls], ["prompt.submit"], "queued behind the running turn")
        self.assertNotIn("attachments", calls[0][1])

    def test_a_file_hermes_would_not_take_as_a_photo_stops_the_turn_before_anything_is_sent(self):
        bad = self.dir / "x.png"
        bad.write_bytes(HEIC)
        out, calls = self.run_turn("--tw-image", str(bad))
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(calls, [])

    def run_room(self, req, **env):
        code = f"import runpy, sys; sys.path.insert(0, {str(self.root)!r}); sys.argv = ['room_send']; " \
               f"runpy.run_path({str(ROOM_SEND)!r}, run_name='__main__')"
        log = self.dir / "rpc.jsonl"
        out = subprocess.run([sys.executable, "-I", "-c", code], input=json.dumps(req).encode(), capture_output=True,
                             timeout=60, env={**os.environ, "TW_STUB_LOG": str(log), **env})
        rpcs = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
        self.assertTrue(out.stdout.strip(), out.stderr.decode())
        return json.loads(out.stdout.decode().strip().splitlines()[-1]), rpcs

    def files(self, *items):
        return [{"id": f"att_{i}abcdefgh", "name": n, "mime": m, "data_base64": base64.b64encode(b).decode()}
                for i, (n, m, b) in enumerate(items)]

    def test_a_room_message_uploads_each_file_and_names_them(self):
        req = {"op": "send", "room_id": ROOM, "event_id": "tw-chat.msg_1", "text": "",
               "files": self.files(("plan.pdf", "application/pdf", PDF), ("a.png", "image/png", PNG),
                                   ("odd.png", "image/png", b"not a png"), ("n.txt", "text/plain", b"hi"))}
        out, rpcs = self.run_room(req)
        self.assertEqual(out, {"sent": True, "seq": 12})
        ups = [r["params"] for r in rpcs if r["method"] == "groups.attachment.upload"]
        self.assertEqual([(u["kind"], u["name"], u["mime"]) for u in ups],
                         [("pdf", "plan.pdf", "application/pdf"), ("image", "a.png", "image/png"),
                          ("file", "odd.png", "text/plain"), ("file", "n.txt", "text/plain")])
        self.assertEqual(ups[0]["upload_id"], "tw-" + hashlib.sha256(b"att_0abcdefgh").hexdigest())
        (send,) = [r["params"] for r in rpcs if r["method"] == "groups.send"]
        self.assertEqual(send["payload"]["thread_id"], "tw-chat.msg_1")
        self.assertEqual([(a["kind"], a["name"]) for a in send["payload"]["attachments"]],
                         [("pdf", "plan.pdf"), ("image", "a.png"), ("file", "odd.png"), ("file", "n.txt")])
        self.assertEqual(set(send["payload"]["attachments"][0]), {"attachment_id", "kind", "name", "size", "mime"})

    def test_a_file_the_rooms_store_refuses_sends_nothing(self):
        req = {"op": "send", "room_id": ROOM, "event_id": "tw-chat.msg_2", "text": "x",
               "files": self.files(("a.png", "image/png", PNG))}
        out, rpcs = self.run_room(req, TW_STUB_UPLOAD="refused")
        self.assertIs(out["sent"], False)
        self.assertIn("did not take a file", out["reason"])
        self.assertNotIn("ready", out, "a refusal no retry changes: the message is dead, not waiting")
        self.assertNotIn("groups.send", [r["method"] for r in rpcs])


class TheApp(unittest.TestCase):
    def test_the_plus_goes_to_each_chats_own_route(self):
        self.assertIn("status, null, '/chat/' + encodeURIComponent(target), v.cards)", APP)
        self.assertIn("...(files.length ? { attachments: files } : {}) }),\n          status, null, '/chat/'", APP)
        self.assertIn("status, gw, '/own/' + gw)", APP)
        self.assertIn("`${API}${base}/attach?name=", APP)
        self.assertNotIn("/own/${gw}/attach", APP)


class OwnHermesPhotos(unittest.TestCase):
    def test_hand_over_names_every_photo_and_never_hermess_own_image_switch(self):
        d = Path("/tmp/x")
        words, extra = own_connector.hand_over("hi", [({}, d / "a.png", "image/png"), ({}, d / "n.pdf", None),
                                                       ({}, d / "b.png", "image/png")])
        self.assertEqual(words, f"hi\n\n@file:{d / 'n.pdf'}")
        self.assertEqual(extra, ["--tw-files", "--tw-image", str(d / "a.png"), "--tw-image", str(d / "b.png")])
        self.assertEqual(own_connector.hand_over("", []), ("", []))
        source = (HERE / "own_connector.py").read_text(encoding="utf-8")
        self.assertNotIn('"--image"', source)
        self.assertEqual([own_connector.photo_type(x) for x in (PNG, JPEG, PDF, HEIC, b"GIF89a..", b"RIFF1234WEBPVP8 ")],
                         ["image/png", "image/jpeg", None, None, "image/gif", "image/webp"])


if __name__ == "__main__":
    unittest.main()
