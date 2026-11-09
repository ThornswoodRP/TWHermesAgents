"""Renaming, hiding, bringing back and deleting one of Patrick's own chats (redesign step 3, plan Part 11).

Checks the core's side (session_manage_write and own_chat_write, a listed chat or, to bring one back,
a hidden one a page listed, the gateway answering, a passkey sign-in from the last 30 minutes for a
delete only, the idempotency key, one request at a time in each chat, a lease that ran out handed out
again, an answer to a request already answered changing nothing, no title in the event chain), the
routes, the way back from migration 10, the connector handing each request to the helper and saying
what came of it, the hidden chats' page, and the helper against a stand-in for Hermes's store that
keeps Hermes's own rules (a title another chat holds is refused; a delete is refused while a turn
runs). Everything runs in a scratch test workspace; nothing reaches a real Hermes.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import timedelta
from pathlib import Path

from decisions_helpers import World
from test_decisions_api import Served
from test_decisions_own import READY, SID, FakeCore, Gateways, on, passkey, state_db

from thornswood_decisions import own_connector
from thornswood_decisions.store import CoreError

HELPER = Path(__file__).resolve().parent.parent / "src" / "thornswood_decisions" / "room_send.py"


class Requests(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.g = Gateways(self.w)
        self.own = self.g.own
        self.g.hello("one")
        self.g.feed("one")
        on(self.w, "session_manage_write")
        self.n = 0

    def ask(self, op="rename", title="Weekend plans", p=None, sid=SID, key=None):
        self.n += 1
        return self.own.session(p or self.w.patrick(), "one", sid, op, title if op == "rename" else None,
                                key or f"sess-key-{self.n:04d}")

    def claim(self, ready=True):
        return self.own.claim(self.w.ws, "one", self.g.tok["one"], 0, ready)["sessions"]

    def answer(self, r, outcome="done", reason=""):
        return self.own.session_done(self.w.ws, "one", self.g.tok["one"], {"id": r["id"], "outcome": outcome,
                                                                           "reason": reason})

    def refused(self, reason, **kw):
        with self.assertRaises(CoreError) as e:
            self.ask(**kw)
        self.assertEqual(e.exception.reason, reason)

    def test_off_until_session_manage_write_and_own_chat_write(self):
        self.w.store.set_flag(self.w.ws, "session_manage_write", False, by="tests")
        self.refused("session_manage_off")
        self.assertFalse(self.own.chats(self.w.patrick(), "one")["can_manage"])
        on(self.w, "session_manage_write")
        self.assertTrue(self.own.chats(self.w.patrick(), "one")["can_manage"])
        self.w.store.set_flag(self.w.ws, "own_chat_write", False, by="tests")
        self.refused("own_chat_off")

    def test_a_rename_is_done_once_and_the_same_key_gets_the_same_receipt(self):
        first = self.ask(key="sess-key-same")
        self.assertEqual((first["op"], first["status"]), ("rename", "queued"))
        self.assertTrue(self.own.session(self.w.patrick(), "one", SID, "rename", "Weekend plans",
                                         "sess-key-same")["replayed"])
        with self.assertRaises(CoreError) as e:
            self.own.session(self.w.patrick(), "one", SID, "rename", "Other", "sess-key-same")
        self.assertEqual(e.exception.reason, "key_reused")
        (r,) = self.claim()
        self.assertEqual((r["op"], r["session_id"], r["title"]), ("rename", SID, "Weekend plans"))
        self.assertEqual(self.answer(r), {"status": "done"})
        self.assertTrue(self.answer(r)["stale"])
        shown = self.own.view(self.w.patrick(), "one", SID)["ops"]
        self.assertEqual([(o["op"], o["status"]) for o in shown], [("rename", "done")])

    def test_his_bot_chat_is_refused_as_listed(self):
        bot = {"id": "s_bot", "title": "Bot Chat", "last_at": self.w.clock().isoformat(), "preview": "", "bot_chat": True}
        self.g.feed("one", chats=[bot, {"id": SID, "title": "Plan the week", "last_at": self.w.clock().isoformat(),
                                        "preview": "ok"}])
        for op in ("rename", "archive", "delete"):
            self.refused("bot_chat_kept", op=op, sid="s_bot")
        self.assertEqual(self.ask()["status"], "queued")
        self.assertEqual(self.w.store.db.execute("SELECT count(*) FROM own_session_ops WHERE session_id = 's_bot'")
                         .fetchone()[0], 0)
        # A page of the chat list that names it so is the same.
        self.g.feed("one")
        self.assertEqual(self.own.page(passkey(self.w), "one", "chats", q="bot"), {"pending": True})
        (req,) = self.own.claim(self.w.ws, "one", self.g.tok["one"], 0, True)["pages"]
        self.own.page_post(self.w.ws, "one", self.g.tok["one"], {"id": req["id"], "page": {"chats": [bot], "more": False}})
        self.refused("bot_chat_kept", op="archive", sid="s_bot")

    def test_the_bots_list_shows_the_chat_last_written_in_not_the_bot_chat_listed_first(self):
        now = self.w.clock()
        self.g.feed("one", chats=[
            {"id": "s_bot", "title": "Bot Chat", "last_at": (now - timedelta(hours=8)).isoformat(), "preview": "",
             "bot_chat": True},
            {"id": SID, "title": "Plan the week", "last_at": now.isoformat(), "preview": "ok"}])
        (one,) = [g for g in self.own.summary(self.w.patrick())["gateways"] if g["gateway"] == "one"]
        self.assertEqual(one["last"]["id"], SID)

    def test_titles(self):
        for title in ("", "   ", "two\nlines", "x" * 101, None, 5):
            self.refused("bad_title", title=title)
        self.assertEqual(self.claim(), [])
        self.ask(title="  lots   of  space ")
        self.assertEqual(self.claim()[0]["title"], "lots of space")

    def test_a_delete_needs_a_fresh_passkey_and_nothing_else_does(self):
        self.refused("passkey_needed", op="delete")
        self.refused("passkey_needed", op="delete", p=passkey(self.w, minutes_ago=31))
        self.assertEqual(self.ask(op="delete", p=passkey(self.w))["status"], "queued")
        for op in ("archive", "rename"):
            self.assertEqual(self.ask(op=op)["status"], "queued")  # a pairing session, no passkey

    def test_only_a_chat_this_gateway_listed_and_only_while_it_answers(self):
        self.refused("not_listed", sid="s_other")
        self.refused("not_listed", op="unarchive")  # not a hidden chat any page listed
        with self.assertRaises(CoreError) as e:
            self.own.session(self.w.patrick(), "me", SID, "rename", "x", "sess-key-me01")
        self.assertEqual(e.exception.reason, "not_listed")
        self.w.clock.advance(minutes=3)
        self.refused("gateway_off")
        self.refused("bad_op", op="pin")

    def test_one_at_a_time_in_each_chat_and_a_lapsed_lease_goes_out_again(self):
        self.ask(title="First")
        self.ask(title="Second")
        (r,) = self.claim()
        self.assertEqual(r["title"], "First")
        self.assertEqual(self.claim(), [])
        self.w.clock.advance(minutes=3)
        (again,) = self.claim()
        self.assertEqual(again["id"], r["id"])
        self.answer(again, "failed", "Title 'First' is already in use by session s_x")
        (nxt,) = self.claim()
        self.assertEqual(nxt["title"], "Second")
        ops = self.own.view(self.w.patrick(), "one", SID)["ops"]
        self.assertEqual([(o["title"], o["status"], o["reason"]) for o in ops],
                         [("First", "failed", "Title 'First' is already in use by session s_x"),
                          ("Second", "leased", None)])

    def test_nothing_is_handed_out_while_hermes_is_not_ready(self):
        self.ask()
        self.assertEqual(self.claim(ready=False), [])

    def test_bringing_back_a_chat_a_page_of_hidden_chats_listed(self):
        self.assertEqual(self.own.page(self.w.patrick(), "one", "archived"), {"pending": True})
        (req,) = self.own.claim(self.w.ws, "one", self.g.tok["one"], 0, True)["pages"]
        self.assertEqual(req["kind"], "archived")
        self.own.page_post(self.w.ws, "one", self.g.tok["one"], {"id": req["id"], "page": {
            "chats": [{"id": "s_hidden", "title": "Old", "last_at": "", "preview": ""}], "more": False}})
        got = self.own.page(self.w.patrick(), "one", "archived")
        self.assertEqual([c["id"] for c in got["chats"]], ["s_hidden"])
        self.assertEqual(self.ask(op="unarchive", sid="s_hidden")["status"], "queued")
        self.refused("not_listed", op="archive", sid="s_hidden")  # hidden already: not in the chat list

    def test_the_event_chain_has_no_title(self):
        self.ask(title="A secret plan")
        (r,) = self.claim()
        self.answer(r)
        rows = self.w.store.db.execute("SELECT kind, body_json FROM events WHERE kind LIKE 'own.session%'").fetchall()
        self.assertEqual([x[0] for x in rows], ["own.session_rename", "own.session_done"])
        self.assertNotIn("secret", " ".join(x[1] for x in rows))

    def test_a_token_answers_only_for_its_own_gateway(self):
        self.ask()
        (r,) = self.claim()
        with self.assertRaises(CoreError):
            self.own.session_done(self.w.ws, "one", self.g.tok["me"], {"id": r["id"], "outcome": "done"})
        with self.assertRaises(CoreError) as e:
            self.own.session_done(self.w.ws, "me", self.g.tok["me"], {"id": r["id"], "outcome": "done"})
        self.assertEqual(e.exception.reason, "no_item")


class Routes(unittest.TestCase):
    def test_the_phone_asks_and_only_the_connector_answers(self):
        from test_decisions_api import ORIGIN
        s = Served(self)
        g = Gateways(s.w)
        on(s.w, "mobile_read", "session_manage_write")
        g.hello("one")
        g.feed("one")
        dev = s.w.device()
        sess = s.w.core.auth.issue_session(s.w.ws, method="pairing", device_id=dev)
        h = {"Cookie": f"tw_session={sess.token}", "Origin": ORIGIN, "X-CSRF-Token": sess.csrf}
        status, _, body = s.call("POST", "/own/one/session", headers=h,
                                 body={"session_id": SID, "op": "archive", "idempotency_key": "route-key-0001"})
        self.assertEqual((status, body["status"]), (200, "queued"), body)
        status, _, body = s.call("POST", "/own/one/session", headers=h,
                                 body={"session_id": SID, "op": "delete", "idempotency_key": "route-key-0002"})
        self.assertEqual((status, body["error"]), (403, "passkey_needed"))
        status, _, body = s.call("GET", "/own/one/chats/archived", headers=h)
        self.assertEqual((status, body), (200, {"pending": True}))
        tok = g.tok["one"]
        status, _, body = s.call("POST", "/own/one/claim", headers={"Authorization": f"Bearer {tok}"},
                                 body={"limit": 0, "ready": True})
        (r,) = body["sessions"]
        status, _, body = s.call("POST", "/own/one/session_done", headers=h, body={"id": r["id"], "outcome": "done"})
        self.assertEqual((status, body["error"]), (403, "not_a_feed"))
        status, _, body = s.call("POST", "/own/one/session_done", headers={"Authorization": f"Bearer {tok}"},
                                 body={"id": r["id"], "outcome": "done"})
        self.assertEqual((status, body["status"]), (200, "done"))


# A stand-in for Hermes's store with Hermes's own rules: a title another chat holds is refused
# (hermes_state_titles.py _resolve_title_conflict), archive hides the chat and keeps it, and a delete
# is refused while a turn runs (SessionActiveWriteGuardError) or one is queued (session_busy).
FAKE_HERMES = {
    "hermes_constants.py": """
        import os
        from pathlib import Path
        def get_hermes_home():
            return Path(os.environ["TW_FAKE_HOME"])
        def get_default_hermes_root():
            return os.environ["TW_FAKE_HOME"]
    """,
    "hermes_state_errors.py": """
        class SessionActiveWriteGuardError(RuntimeError):
            pass
    """,
    "hermes_state_runtime.py": """
        class RuntimeStoreError(RuntimeError):
            def __init__(self, reason):
                super().__init__(reason)
                self.reason = reason
    """,
    "hermes_state_registry.py": """
        import json, os, re
        from pathlib import Path
        from hermes_state_errors import SessionActiveWriteGuardError
        from hermes_state_runtime import RuntimeStoreError
        class DB:
            CANONICAL_BOT_CHAT_TITLE = "Bot Chat"
            def __init__(self):
                self.path = Path(os.environ["TW_FAKE_HOME"]) / "sessions.json"
                self.data = json.loads(self.path.read_text())
            def _save(self):
                self.data["writes"] = self.data.get("writes", 0) + 1
                self.path.write_text(json.dumps(self.data))
            def get_session(self, sid):
                return self.data["sessions"].get(sid)
            def get_session_by_title(self, title):
                return next(({"id": k, **v} for k, v in self.data["sessions"].items() if v.get("title") == title), None)
            def get_compression_lineage(self, sid):
                out = [sid]
                while self.data["sessions"].get(out[-1], {}).get("next"):
                    out.append(self.data["sessions"][out[-1]]["next"])
                return out
            def get_session_title(self, sid):
                return (self.data["sessions"].get(sid) or {}).get("title")
            @staticmethod
            def sanitize_title(title):
                t = re.sub(r"\\s+", " ", title or "").strip()
                if len(t) > 100:
                    raise ValueError("Title too long")
                return t or None
            def set_session_title(self, sid, title):
                t = self.sanitize_title(title)
                if any(k != sid and v.get("title") == t for k, v in self.data["sessions"].items()):
                    raise ValueError(f"Title '{t}' is already in use by session " +
                                     next(k for k, v in self.data["sessions"].items() if v.get("title") == t))
                self.data["sessions"][sid]["title"] = t
                self._save()
                return True
            def set_session_archived(self, sid, archived):
                self.data["sessions"][sid]["archived"] = archived
                self._save()
                return True
            def delete_session(self, sid, sessions_dir=None, exclude_active_write_guards=False):
                if sid == "s_busy":
                    raise SessionActiveWriteGuardError("an active turn lease")
                if sid == "s_queued":
                    raise RuntimeStoreError("session_busy")
                self.data["sessions"].pop(sid)
                self.data["deleted_in"] = str(sessions_dir)
                self._save()
                return True
        def acquire(path=None):
            return DB()
        def release_or_close(db):
            pass
    """,
}


class TheHelper(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        for name, text in FAKE_HERMES.items():
            (self.dir / name).write_text(textwrap.dedent(text), encoding="utf-8")
        (self.dir / "sessions.json").write_text(json.dumps({"sessions": {
            "s_plan": {"title": "Plan"}, "s_taken": {"title": "Taken"}, "s_busy": {"title": "Busy"},
            "s_queued": {"title": "Queued"}, "s_bot": {"title": "Bot Chat", "hidden": True, "next": "s_bot2"},
            "s_bot2": {"title": None, "hidden": True}}}))
        self.env = {**os.environ, "PYTHONPATH": str(self.dir), "TW_FAKE_HOME": str(self.dir)}

    def ask(self, action, sid="s_plan", title=None, profile=""):
        out = subprocess.run([sys.executable, str(HELPER)], input=json.dumps(
            {"op": "session", "profile": profile, "action": action, "session_id": sid, "title": title}),
            capture_output=True, text=True, env=self.env, timeout=60)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def store(self):
        return json.loads((self.dir / "sessions.json").read_text())

    def test_rename_twice_writes_once_and_a_taken_title_says_why(self):
        self.assertEqual(self.ask("rename", title="Weekend"), {"done": True})
        self.assertEqual(self.ask("rename", title=" Weekend "), {"done": True})
        s = self.store()
        self.assertEqual((s["sessions"]["s_plan"]["title"], s["writes"]), ("Weekend", 1))
        self.assertEqual(self.ask("rename", title="Taken"),
                         {"done": False, "reason": "Title 'Taken' is already in use by session s_taken"})
        self.assertEqual(self.ask("rename", title="two\nlines")["done"], False)

    def test_his_bot_chat_and_every_part_it_was_compressed_into_are_kept_as_they_are(self):
        kept = {"done": False, "reason": "his Bot Chat is kept as it is: it is not renamed, hidden or deleted"}
        for sid in ("s_bot", "s_bot2"):
            self.assertEqual(self.ask("rename", sid=sid, title="Mine now"), kept)
            self.assertEqual(self.ask("archive", sid=sid), kept)
            self.assertEqual(self.ask("delete", sid=sid), kept)
        s = self.store()
        self.assertNotIn("writes", s)
        self.assertEqual(s["sessions"]["s_bot"]["title"], "Bot Chat")
        self.assertIn("s_bot2", s["sessions"])
        # A chat only named so, that Hermes shows (not hidden), is an ordinary chat.
        s["sessions"]["s_bot"]["hidden"] = False
        (self.dir / "sessions.json").write_text(json.dumps(s))
        self.assertEqual(self.ask("archive", sid="s_bot2"), {"done": True})

    def test_archive_and_bring_back_keep_the_chat(self):
        self.assertEqual(self.ask("archive"), {"done": True})
        self.assertTrue(self.store()["sessions"]["s_plan"]["archived"])
        self.assertEqual(self.ask("unarchive"), {"done": True})
        self.assertFalse(self.store()["sessions"]["s_plan"]["archived"])

    def test_delete_is_done_once_and_refused_while_a_turn_runs(self):
        self.assertEqual(self.ask("delete"), {"done": True})
        self.assertNotIn("s_plan", self.store()["sessions"])
        self.assertEqual(self.store()["deleted_in"], str(self.dir / "sessions"))
        self.assertEqual(self.ask("delete"), {"done": True}, "already gone is done")
        self.assertEqual(self.ask("rename", title="x"), {"done": False, "reason": "that chat is gone"})
        busy = self.ask("delete", sid="s_busy")
        self.assertEqual(busy["done"], False)
        self.assertIn("a turn is running", busy["reason"])
        self.assertIn("queued or running turn", self.ask("delete", sid="s_queued")["reason"])
        self.assertIn("s_busy", self.store()["sessions"])

    def test_never_for_a_bot_and_a_bad_request_changes_nothing(self):
        self.assertEqual(self.ask("delete", profile="bramble")["done"], False)
        self.assertEqual(self.ask("pin")["done"], False)
        self.assertEqual(self.ask("rename")["done"], False)  # no title
        self.assertIn("s_plan", self.store()["sessions"])
        self.assertNotIn("writes", self.store())


READY_SESSION = READY.replace('req = json.loads(sys.stdin.read())', textwrap.dedent('''\
    req = json.loads(sys.stdin.read())
    if req.get("op") == "session":
        with open(os.environ["TW_FAKE_LOG"], "a", encoding="utf-8") as f:
            f.write(json.dumps(req) + "\\n")
        ok = req["session_id"] != "s_busy"
        print(json.dumps({"done": True} if ok else {"done": False, "reason": "a turn is running in that chat"}))
        sys.exit(0)'''))


class SessionCore(FakeCore):
    def post(self, op, body):
        if op == "session_done":
            try:
                return 200, self.own.session_done(self.ws, self.gw, self.token, body)
            except CoreError as exc:
                return exc.status, {"error": exc.reason}
        return super().post(op, body)


class TheConnector(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.g = Gateways(self.w)
        on(self.w, "session_manage_write")
        self.g.hello("one")
        self.dir = Path(tempfile.mkdtemp())
        self.db = state_db(self.dir)
        (self.dir / "ready.py").write_text(READY_SESSION, encoding="utf-8")
        self.log = self.dir / "helper.jsonl"
        os.environ["TW_FAKE_LOG"] = str(self.log)
        self.addCleanup(lambda: os.environ.pop("TW_FAKE_LOG", None))
        hermes = own_connector.Hermes([sys.executable, "-c", "pass"], [sys.executable, "-I", str(self.dir / "ready.py")],
                                      self.dir, "One's Hermes")
        self.lines = []
        self.conn = own_connector.OwnConnector(SessionCore(self.g.own, self.w.ws, "one", self.g.tok["one"]), hermes,
                                               self.db, say=self.lines.append)
        self.g.feed("one", chats=[{"id": "s_plan", "title": "Plan the week", "last_at": "", "preview": ""},
                                  {"id": "s_busy", "title": "Busy", "last_at": "", "preview": ""}], opened=[])

    def test_each_request_goes_to_the_helper_with_the_title_on_stdin_and_comes_back(self):
        self.g.own.session(self.w.patrick(), "one", "s_plan", "rename", "Weekend", "conn-key-0001")
        self.g.own.session(passkey(self.w), "one", "s_busy", "delete", None, "conn-key-0002")
        self.conn.once()
        reqs = [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(r["op"], r["profile"], r["action"], r["session_id"], r["title"]) for r in reqs],
                         [("session", "", "rename", "s_plan", "Weekend"), ("session", "", "delete", "s_busy", None)])
        rows = dict(self.w.store.db.execute("SELECT session_id, status FROM own_session_ops").fetchall())
        self.assertEqual(rows, {"s_plan": "done", "s_busy": "failed"})
        self.assertIn("PASS chat rename: done", self.lines)
        self.assertTrue(self.conn.feed_now or self.conn._fed_at is not None)

    def test_the_hidden_chats_page_lists_only_hidden_chats(self):
        page = own_connector.chats_page(self.db, None, "", archived=True)
        self.assertEqual([c["id"] for c in page["chats"]], ["s_arch"])
        self.assertNotIn("s_arch", [c["id"] for c in own_connector.chats_page(self.db, None, "")["chats"]])
        body = json.loads(own_connector.fitted_page(self.db, {"id": "pg_aaaaaaaaaaaa", "kind": "archived"}))
        self.assertEqual([c["id"] for c in body["page"]["chats"]], ["s_arch"])


class Migration10(unittest.TestCase):
    def test_the_flag_starts_off_and_the_way_back_refuses_while_one_is_on_its_way(self):
        from thornswood_decisions.schema import DOWNS
        w = World()
        self.addCleanup(w.close)
        self.assertFalse(w.store.flag(w.ws, "session_manage_write"))
        checks = DOWNS[10][0]
        count = lambda: [w.store.db.execute(q).fetchone()[0] for q, _ in checks]
        self.assertEqual(count(), [0, 0])
        g = Gateways(w)
        on(w, "session_manage_write")
        g.hello("one")
        g.feed("one")
        g.own.session(w.patrick(), "one", SID, "archive", None, "mig-key-0001")
        self.assertEqual(count(), [1, 1])


if __name__ == "__main__":
    unittest.main()
