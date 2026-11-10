"""A subagent's own transcript, opened from the Tasks tab (#111).

Checks:
- room_send.py op `tail`, against stand-in Hermes modules: Hermes's own subagent.tail for that chat and
  subagent, as Desktop's status stack calls it, cut to its newest 16384 characters; a subagent that has
  ended said so; a bad request, a Bot's subagent and a stopped gateway refused before anything is asked;
- the core: a read names a subagent that Hermes listed lately, of one of his own two gateways; it is asked
  of that gateway's connector, answered, kept 3 seconds in memory, asked again after that, and never put
  in the event chain; an ask nobody answered is handed out again after 10 seconds; the routes over HTTP;
- the connector: each read run through the helper and posted.
Everything runs on stand-ins and a scratch workspace; nothing reaches a real Hermes.
"""

from __future__ import annotations

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
from test_decisions_api import ORIGIN, Served
from test_decisions_own import Gateways, on, passkey
from test_decisions_tasks import STUB_CONSTANTS, STUB_RUNTIME, report
from thornswood_decisions import connector, own_connector, own_tails, tasks as tasks_mod
from thornswood_decisions.store import CoreError

HELPER = Path(connector.__file__).with_name("room_send.py")
SID = "20261101_201500_ab12cd"

# gateway/session_ancillary.py subagent.tail: {available, text, truncated} for a live chat's subagent.
STUB_CLIENT = textwrap.dedent('''
    import json, os
    from contextlib import asynccontextmanager
    class GatewayClientError(ValueError):
        pass
    class GatewayRPCError(GatewayClientError):
        pass
    class Client:
        def __init__(self, endpoint):
            self.endpoint = endpoint
        async def rpc(self, method, **params):
            with open(os.environ["TW_STUB_LOG"], "a", encoding="utf-8") as f:
                f.write(json.dumps({"method": method, "params": params}) + "\\n")
            if method != "subagent.tail":
                raise GatewayRPCError("method_not_found")
            if params["subagent_id"] == "sa_gone":
                raise GatewayRPCError("not_found")
            text = "x" * 20000 + "the end" if params["subagent_id"] == "sa_long" else "read the log\\nfound it"
            return {"available": True, "text": text, "truncated": False}
    @asynccontextmanager
    async def connect_gateway(endpoint=None, subject=None):
        yield Client(endpoint)
''')

SUB = {"id": "sa_1", "chat": SID, "chat_title": "Fix the launcher", "goal": "Read the log", "model": "m",
       "status": "running", "depth": 1, "tools": 3, "last_tool": "terminal", "started": 1000.5, "background": False}


class Helper(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        src = self.dir / "src"
        (src / "hermes_cli").mkdir(parents=True)
        (src / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
        (src / "hermes_cli" / "gateway_runtime.py").write_text(STUB_RUNTIME, encoding="utf-8")
        (src / "hermes_cli" / "gateway_client.py").write_text(STUB_CLIENT, encoding="utf-8")
        (src / "hermes_constants.py").write_text(STUB_CONSTANTS, encoding="utf-8")
        self.src, self.root, self.log = src, self.dir / "hermes", self.dir / "rpc.jsonl"
        self.root.mkdir()

    def run_helper(self, req: dict, **env) -> dict:
        out = subprocess.run([sys.executable, "-I", str(HELPER), "--hermes-root", str(self.src)],
                             input=json.dumps({"op": "tail", "profile": "", "name": "One", **req}).encode(),
                             capture_output=True, timeout=60,
                             env={**os.environ, "TW_STUB_LOG": str(self.log), "TW_STUB_ROOT": str(self.root), **env})
        self.assertEqual(out.returncode, 0, out.stderr.decode())
        return json.loads(out.stdout.decode().strip().splitlines()[-1])

    def rpcs(self) -> list:
        return [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()] if self.log.exists() else []

    def test_the_transcript_is_hermess_own_subagent_tail(self):
        out = self.run_helper({"session_id": SID, "subagent_id": "sa_1"})
        self.assertEqual(out, {"available": True, "text": "read the log\nfound it", "truncated": False})
        self.assertEqual(self.rpcs(), [{"method": "subagent.tail", "params": {"session_id": SID, "subagent_id": "sa_1"}}])

    def test_a_long_one_is_cut_to_its_newest_part_and_an_ended_one_says_so(self):
        out = self.run_helper({"session_id": SID, "subagent_id": "sa_long"})
        self.assertEqual((len(out["text"]), out["text"][-7:], out["truncated"]), (16384, "the end", True))
        out = self.run_helper({"session_id": SID, "subagent_id": "sa_gone"})
        self.assertEqual(out, {"available": False, "reason": "that subagent has ended"})

    def test_a_bad_request_a_bots_subagent_and_a_stopped_gateway(self):
        for req in ({"session_id": "-x", "subagent_id": "sa_1"}, {"session_id": SID, "subagent_id": "../sa"},
                    {"session_id": SID}):
            with self.subTest(req=req):
                self.assertEqual(self.run_helper(req), {"available": False, "reason": "bad request"})
        self.assertEqual(self.run_helper({"session_id": SID, "subagent_id": "sa_1", "profile": "rift"}),
                         {"available": False, "reason": "a Bot's subagents are not read from here"})
        self.assertEqual(self.run_helper({"session_id": SID, "subagent_id": "sa_1"}, TW_STUB_DOWN="hermes"),
                         {"available": False, "reason": "One's gateway is absent"})
        self.assertEqual(self.rpcs(), [])


class Core(unittest.TestCase):
    def setUp(self):
        tasks_mod._TASKS.clear()
        own_tails.TAILS.read.clear()
        own_tails.TAILS.asked.clear()
        self.w = World()
        self.addCleanup(self.w.close)
        self.g = Gateways(self.w)
        on(self.w, "mobile_read")
        self.own, self.p = self.g.own, passkey(self.w)
        self.g.hello("one")
        self.own.tasks(self.w.ws, "one", self.g.tok["one"], {"tasks": report(subagents=[SUB])})

    def read(self, sub="sa_1", gw="one", chat=SID):
        return self.own.subagent_tail(self.p, gw, chat, sub)

    def hand(self, gw="one") -> list:
        return self.g.hello(gw)["tails"]

    def answer(self, a, **body):
        return self.own.tail_post(self.w.ws, "one", self.g.tok["one"],
                                  {"id": a["id"], "available": True, "text": "read the log", "truncated": False} | body)

    def test_a_read_is_asked_of_that_gateway_answered_and_kept_3_seconds(self):
        got = self.read()
        self.assertEqual((got["pending"], got["tail"], got["subagent"]["goal"]), (True, None, "Read the log"))
        self.read()
        asks = self.hand()
        self.assertEqual([(a["session_id"], a["subagent_id"]) for a in asks], [(SID, "sa_1")], "asked once")
        self.assertEqual(self.hand("me"), [])
        self.answer(asks[0])
        got = self.read()
        self.assertFalse(got["pending"])
        self.assertEqual({k: got["tail"][k] for k in ("available", "text", "truncated")},
                         {"available": True, "text": "read the log", "truncated": False})
        self.assertEqual(self.hand(), [], "a fresh read answers from memory")
        self.w.clock.advance(seconds=4)
        self.read()
        self.assertEqual(len(self.hand()), 1, "and is asked again once it is 3 seconds old")
        # The child's words never reach the event chain.
        rows = self.w.store.db.execute("SELECT body_json FROM events").fetchall()
        self.assertFalse(any("read the log" in r[0] for r in rows))

    def test_an_unanswered_ask_goes_out_again_and_a_refusal_says_why(self):
        self.read()
        a = self.hand()[0]
        self.assertEqual(self.hand(), [])
        self.w.clock.advance(seconds=11)
        self.assertEqual([x["id"] for x in self.hand()], [a["id"]], "a read is safe to ask again")
        self.answer(a, available=False, text="", reason="that subagent has ended")
        self.assertEqual(self.read()["tail"]["why"], "that subagent has ended")
        with self.assertRaises(CoreError) as e:
            self.answer(a)
        self.assertEqual(e.exception.reason, "not_asked")

    def test_only_a_listed_subagent_of_his_own_two_gateways(self):
        for kw, reason in (({"sub": "sa_9"}, "not_listed"), ({"chat": "other_chat"}, "not_listed"),
                           ({"gw": "me"}, "not_listed"), ({"gw": "rift"}, "no_gateway"),
                           ({"sub": "../sa"}, "bad_subagent")):
            with self.subTest(kw=kw), self.assertRaises(CoreError) as e:
                self.read(**kw)
            self.assertEqual(e.exception.reason, reason)
        # A report older than TASKS_STALE lists nothing.
        self.w.clock.advance(seconds=151)
        self.g.hello("one")
        with self.assertRaises(CoreError) as e:
            self.read()
        self.assertEqual(e.exception.reason, "not_listed")

    def test_tokens_the_switch_and_a_long_answer(self):
        self.read()
        a = self.hand()[0]
        with self.assertRaises(CoreError) as e:
            self.own.tail_post(self.w.ws, "one", self.g.tok["me"], {"id": a["id"]})
        self.assertEqual(e.exception.status, 401)
        with self.assertRaises(CoreError) as e:
            self.own.tail_post(self.w.ws, "one", self.g.tok["one"], {"id": "nope"})
        self.assertEqual(e.exception.reason, "bad_tail")
        self.answer(a, text="y" * 20000)
        tail = self.read()["tail"]
        self.assertEqual((len(tail["text"]), tail["truncated"]), (16384, True))
        self.w.store.set_flag(self.w.ws, "own_chat_write", False, by="tests")
        with self.assertRaises(CoreError) as e:
            self.read()
        self.assertEqual(e.exception.reason, "own_chat_off")


class Routes(unittest.TestCase):
    def setUp(self):
        tasks_mod._TASKS.clear()
        own_tails.TAILS.read.clear()
        own_tails.TAILS.asked.clear()

    def test_the_routes_over_http(self):
        s = Served(self)
        on(s.w, "own_chat_write", "mobile_read")
        g = Gateways(s.w)
        g.hello("one")
        g.own.tasks(s.w.ws, "one", g.tok["one"], {"tasks": report(subagents=[SUB])})
        sess = s.w.core.auth.issue_session(s.w.ws, method="passkey", device_id=s.w.device())
        h = {"Cookie": f"tw_session={sess.token}", "Origin": ORIGIN, "X-CSRF-Token": sess.csrf}
        path = f"/own/one/chats/{SID}/subagents/sa_1"
        status, _, body = s.call("GET", path, headers=h)
        self.assertEqual((status, body.get("pending")), (200, True), body)
        bearer = {"Authorization": f"Bearer {g.tok['one']}"}
        status, _, body = s.call("POST", "/own/one/claim", headers=bearer, body={"limit": 0, "ready": True})
        self.assertEqual([(t["session_id"], t["subagent_id"]) for t in body["tails"]], [(SID, "sa_1")])
        status, _, _ = s.call("POST", "/own/one/tail", headers=bearer,
                              body={"id": body["tails"][0]["id"], "available": True, "text": "done", "truncated": False})
        self.assertEqual(status, 200)
        status, _, body = s.call("GET", path, headers=h)
        self.assertEqual((status, body["tail"]["text"]), (200, "done"))
        status, _, body = s.call("POST", "/own/one/tail", headers=h, body={"id": "tail_xxxxxxxxxx"})
        self.assertEqual((status, body.get("error")), (403, "not_a_feed"))


class FakeCore:
    def __init__(self, status=200):
        self.posts, self.status = [], status

    def post(self, op, body):
        self.posts.append((op, json.loads(json.dumps(body))))
        return self.status, {}


class Connector(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_each_read_is_run_through_the_helper_and_posted(self):
        script = self.dir / "helper.py"
        script.write_text("import json, sys, pathlib\nreq = json.loads(sys.stdin.read())\n"
                          f"p = pathlib.Path({str(self.dir / 'reqs.jsonl')!r})\n"
                          "p.write_text((p.read_text() if p.exists() else '') + json.dumps(req) + '\\n')\n"
                          "print(json.dumps({'available': True, 'text': 'went well', 'truncated': False} "
                          "if req['subagent_id'] == 'sa_1' else {'available': False, 'reason': 'that subagent has ended'}))\n",
                          encoding="utf-8")
        hermes = own_connector.Hermes(["hermes"], [sys.executable, "-I", str(script)], self.dir, "One")
        said, core = [], FakeCore()
        conn = own_connector.OwnConnector.__new__(own_connector.OwnConnector)
        conn.hermes, conn.core, conn.say = hermes, core, said.append
        sent = conn.tails([{"id": "tail_aaaaaaaaaa", "session_id": SID, "subagent_id": "sa_1"},
                           {"id": "tail_bbbbbbbbbb", "session_id": SID, "subagent_id": "sa_2"},
                           {"id": "tail_cccccccccc", "session_id": "-bad", "subagent_id": "sa_1"}, "junk"])
        self.assertEqual(sent, 2)
        reqs = [json.loads(x) for x in (self.dir / "reqs.jsonl").read_text().splitlines()]
        self.assertEqual(reqs, [{"op": "tail", "profile": "", "session_id": SID, "subagent_id": "sa_1"},
                                {"op": "tail", "profile": "", "session_id": SID, "subagent_id": "sa_2"}])
        self.assertEqual(core.posts, [
            ("tail", {"id": "tail_aaaaaaaaaa", "available": True, "text": "went well", "truncated": False, "reason": ""}),
            ("tail", {"id": "tail_bbbbbbbbbb", "available": False, "text": "", "truncated": False,
                      "reason": "that subagent has ended"})])
        self.assertEqual(said, [])


if __name__ == "__main__":
    unittest.main()
