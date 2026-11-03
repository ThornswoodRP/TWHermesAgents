"""A chat's details in the app: its model and thinking, read and changed for that chat only (plan Part 6).

Checks:

- room_send.py op `control`, against stand-in Hermes modules: the read (session.resume to open the chat,
  then session.info, config.get reasoning, busy and yolo, and model.options); only models his sign-ins, a
  free model or a local one covers are offered, with Anthropic offered only when its route carries his
  Claude login; a model change and a thinking change each go through Hermes's own PreparedMutations
  (session.mutate fenced to the chat's revision and generation); a pick not offered is refused before
  anything is sent; a model Hermes's selection guard holds for a confirmation is not switched; a pick
  while a turn runs is said to land after it; a Bot's chat is never changed from here;
- the core: a read asked for, handed to that gateway's connector and answered; a change checked against
  the last read's offer, needing a passkey sign-in of the last 30 minutes, the newest change winning, the
  same idempotency key replayed and never a second change; an unanswered change given up after 2 minutes;
  the routes over HTTP;
- One's connector: the asks the core handed out, run through the helper and posted back.

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
from datetime import timedelta
from pathlib import Path

from decisions_helpers import World
from test_decisions_api import Served
from test_decisions_own import SID, Gateways, on, passkey

from thornswood_decisions import connector, own_connector, own_control
from thornswood_decisions.own import OwnChat
from thornswood_decisions.store import CoreError

HELPER = Path(connector.__file__).with_name("room_send.py")

STUB_RUNTIME = textwrap.dedent('''
    import os
    from types import SimpleNamespace
    def gateway_profile_home(home):
        return home
    def discover_gateway_endpoint(home, timeout=2.0):
        state = "absent" if os.environ.get("TW_STUB_DOWN") else "ready"
        return SimpleNamespace(state=state, endpoint=str(home) if state == "ready" else None)
    def attachable(found):
        return found.endpoint is not None
''')

STUB_CONSTANTS = textwrap.dedent('''
    import os
    from pathlib import Path
    def get_default_hermes_root():
        return Path(os.environ["TW_STUB_ROOT"])
''')

# The gateway's answers, in the shapes gateway/session_controls.py, session_config.py and
# session_busy_controls.py give them. TW_STUB_GUARD makes a model pick need a confirmation; TW_STUB_BUSY
# makes it land after the running turn (session_deferred_pick.py).
STUB_CLIENT = textwrap.dedent('''
    import json, os
    from contextlib import asynccontextmanager
    class GatewayClientError(ValueError):
        pass
    class GatewayRPCError(GatewayClientError):
        pass
    STATE = {"model": "gpt-5.5", "provider": "openai-codex", "effort": "medium", "revision": 7, "generation": 3}
    OPTIONS = {"model": "gpt-5.5", "provider": "openai-codex", "providers": [
        {"slug": "openai-codex", "name": "OpenAI Codex", "authenticated": True, "models": ["gpt-5.5", "gpt-5.5-mini"]},
        {"slug": "anthropic", "name": "Anthropic", "authenticated": True, "models": ["claude-opus-5-5"]},
        {"slug": "openrouter", "name": "OpenRouter", "authenticated": True, "models": ["paid/model", "some/model:free",
         "priced/free"], "pricing": {"paid/model": {"input": "$3", "output": "$15", "free": False},
                                     "priced/free": {"input": "free", "output": "free", "free": True}}},
        {"slug": "nous", "name": "Nous Portal", "authenticated": True, "models": ["hermes-5", "hermes-5-free"],
         "pricing": {"hermes-5-free": {"input": "free", "output": "free", "free": True}},
         "unavailable_models": ["hermes-5"]},
        {"slug": "llamacpp", "name": "Local", "authenticated": True, "models": ["qwen3-8b.gguf"],
         "auth_type": "local", "source": "local-runtime"},
        {"slug": "gemini", "name": "Google AI Studio", "authenticated": False, "models": ["gemini-3"]}]}
    def log(entry):
        with open(os.environ["TW_STUB_LOG"], "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\\n")
    class Client:
        def __init__(self, endpoint):
            self.endpoint = endpoint
        async def rpc(self, method, **params):
            log({"method": method, "params": params})
            if params.get("session_id") != "20261101_201500_ab12cd":
                raise GatewayRPCError("not_found")
            if method == "session.resume":
                return {"session_id": params["session_id"], "revision": STATE["revision"],
                        "execution_generation": STATE["generation"]}
            if method == "session.info":
                return {"model": STATE["model"], "provider": STATE["provider"], "source": "tui", "lazy": False}
            if method == "config.get":
                return {"reasoning": {"value": STATE["effort"], "display": "show"},
                        "busy": {"key": "busy", "value": "steer", "scope": "session"},
                        "yolo": {"key": "yolo", "value": "0", "scope": "session"}}[params["key"]]
            if method == "model.options":
                return OPTIONS
            if method == "session.mutate":
                if (params["expected_revision"], params.get("expected_generation")) != (STATE["revision"], STATE["generation"]):
                    raise GatewayRPCError("revision_conflict")
                if params["operation"] == "model":
                    if os.environ.get("TW_STUB_GUARD") and "confirm" not in params["payload"]:
                        return {"status": "confirmation_required", "confirm": "tok", "confirm_message": "This model costs more.",
                                "warnings": [{"kind": "cost", "title": "Expensive model"}]}
                    if os.environ.get("TW_STUB_BUSY"):
                        return {"status": "deferred", "session_id": params["session_id"]}
                    STATE.update(model=params["payload"]["model"], provider=params["payload"]["provider"])
                elif params["operation"] == "reasoning":
                    STATE["effort"] = params["payload"]["effort"]
                STATE["revision"] += 1
                return {"status": "applied", "session_id": params["session_id"], "operation": params["operation"]}
            raise GatewayRPCError("method_not_found")
    @asynccontextmanager
    async def connect_gateway(endpoint=None, subject=None):
        yield Client(endpoint)
''')

# hermes_cli/gateway_mutations.py PreparedMutations.apply, as far as this uses it: the fences read from
# session.resume, and a confirmation_required answer with no prompt raised as a refusal.
STUB_MUTATIONS = textwrap.dedent('''
    import uuid
    from hermes_cli.gateway_client import GatewayClientError
    class PreparedMutations:
        async def apply(self, client, session_id, operation, payload, *, confirm=None):
            snap = await client.rpc("session.resume", session_id=session_id)
            got = await client.rpc("session.mutate", session_id=session_id, operation=operation, payload=payload,
                                   request_id=uuid.uuid4().hex, expected_revision=snap["revision"],
                                   expected_generation=snap["execution_generation"])
            if got.get("status") == "confirmation_required":
                raise GatewayClientError(got["confirm_message"] + "\\n\\nModel not switched: this target needs a confirmation.")
            return got
''')

STUB_AUTH = textwrap.dedent('''
    from types import SimpleNamespace
    PROVIDER_REGISTRY = {"openai-codex": SimpleNamespace(auth_type="oauth_external"),
                         "nous": SimpleNamespace(auth_type="oauth_device_code"),
                         "openrouter": SimpleNamespace(auth_type="api_key"),
                         "anthropic": SimpleNamespace(auth_type="api_key")}
''')

STUB_RUNTIME_PROVIDER = textwrap.dedent('''
    import os
    def resolve_runtime_provider(requested=None, **kw):
        assert requested == "anthropic"
        return {"provider": "anthropic", "base_url": "https://api.anthropic.com",
                "api_key": os.environ.get("TW_STUB_ANTHROPIC_KEY", "sk-ant-api03-billed")}
''')

STUB_ANTHROPIC = textwrap.dedent('''
    def anthropic_route_is_oauth(base_url, credential, *, provider=None):
        return isinstance(credential, str) and credential.startswith("sk-ant-oat")
''')

STUB_EFFORT = textwrap.dedent('''
    def route_supported_efforts(provider, model, api_mode=None):
        if provider == "openai-codex":
            return ("low", "medium", "high", "xhigh")
        return ("none", "minimal", "low", "medium", "high", "xhigh", "max")
''')


class Helper(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        src = self.dir / "src"
        for pkg in ("hermes_cli", "agent"):
            (src / pkg).mkdir(parents=True)
            (src / pkg / "__init__.py").write_text("", encoding="utf-8")
        for name, text in (("hermes_cli/gateway_runtime.py", STUB_RUNTIME), ("hermes_cli/gateway_client.py", STUB_CLIENT),
                           ("hermes_cli/gateway_mutations.py", STUB_MUTATIONS), ("hermes_cli/auth.py", STUB_AUTH),
                           ("hermes_cli/runtime_provider.py", STUB_RUNTIME_PROVIDER),
                           ("agent/anthropic_credentials.py", STUB_ANTHROPIC), ("agent/reasoning_effort.py", STUB_EFFORT),
                           ("hermes_constants.py", STUB_CONSTANTS)):
            (src / name).write_text(text, encoding="utf-8")
        self.src, self.root, self.log = src, self.dir / "hermes", self.dir / "rpc.jsonl"
        self.root.mkdir()

    def run_helper(self, req: dict, **env) -> tuple[int, dict]:
        out = subprocess.run([sys.executable, "-I", str(HELPER), "--hermes-root", str(self.src)],
                             input=json.dumps({"op": "control", "profile": "", "name": "One", **req}).encode(),
                             capture_output=True, timeout=60,
                             env={**os.environ, "TW_STUB_LOG": str(self.log), "TW_STUB_ROOT": str(self.root), **env})
        self.assertEqual(out.returncode, 0, out.stderr.decode())
        return out.returncode, json.loads(out.stdout.decode().strip().splitlines()[-1])

    def rpcs(self) -> list:
        return [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()] if self.log.exists() else []

    def test_the_read_is_what_hermes_holds_for_the_chat(self):
        _, out = self.run_helper({"session_id": SID})
        st = out["state"]
        self.assertEqual((out["applied"], out["reason"]), (False, ""))
        self.assertEqual((st["model"], st["provider"], st["reasoning"], st["busy"], st["yolo"]),
                         ("gpt-5.5", "openai-codex", "medium", "steer", False))
        self.assertEqual(st["levels"], ["low", "medium", "high", "xhigh"])
        # Opened as Desktop opens a chat, then only reads.
        self.assertEqual([r["method"] for r in self.rpcs()],
                         ["session.resume", "session.info", "config.get", "config.get", "config.get", "model.options"])

    def test_only_models_that_cost_nothing_per_message_are_offered(self):
        _, out = self.run_helper({"session_id": SID})
        got = {(o["provider"], o["model"]): o["why"] for o in out["state"]["offered"]}
        self.assertEqual(got, {("openai-codex", "gpt-5.5"): "subscription", ("openai-codex", "gpt-5.5-mini"): "subscription",
                               ("openrouter", "some/model:free"): "free", ("openrouter", "priced/free"): "free",
                               ("nous", "hermes-5-free"): "free", ("llamacpp", "qwen3-8b.gguf"): "local"})
        # Anthropic is a sign-in only when its route carries his Claude login.
        _, out = self.run_helper({"session_id": SID}, TW_STUB_ANTHROPIC_KEY="sk-ant-oat01-login")
        self.assertIn(("anthropic", "claude-opus-5-5", "subscription"),
                      [(o["provider"], o["model"], o["why"]) for o in out["state"]["offered"]])

    def test_a_change_goes_through_hermess_own_fenced_mutations(self):
        _, out = self.run_helper({"session_id": SID, "set": {"model": {"provider": "openai-codex", "model": "gpt-5.5-mini"},
                                                             "reasoning": "high"}})
        self.assertEqual((out["applied"], out["reason"]), (True, ""))
        self.assertEqual((out["state"]["model"], out["state"]["reasoning"]), ("gpt-5.5-mini", "high"))
        mutates = [r["params"] for r in self.rpcs() if r["method"] == "session.mutate"]
        self.assertEqual([(m["operation"], m["payload"], m["expected_revision"], m["expected_generation"]) for m in mutates],
                         [("model", {"model": "gpt-5.5-mini", "provider": "openai-codex"}, 7, 3),
                          ("reasoning", {"effort": "high"}, 8, 3)])

    def test_a_pick_not_offered_is_refused_before_anything_is_sent(self):
        for pick in ({"provider": "openrouter", "model": "paid/model"}, {"provider": "anthropic", "model": "claude-opus-5-5"},
                     {"provider": "nous", "model": "hermes-5"}, {"provider": "gemini", "model": "gemini-3"}):
            with self.subTest(pick=pick):
                _, out = self.run_helper({"session_id": SID, "set": {"model": pick}})
                self.assertFalse(out["applied"])
                self.assertIn("not one his sign-ins", out["reason"])
                self.assertEqual(out["state"]["model"], "gpt-5.5")
        self.assertNotIn("session.mutate", [r["method"] for r in self.rpcs()])

    def test_a_guarded_model_is_not_switched(self):
        _, out = self.run_helper({"session_id": SID, "set": {"model": {"provider": "openrouter", "model": "priced/free"}}},
                                 TW_STUB_GUARD="1")
        self.assertFalse(out["applied"])
        self.assertIn("Model not switched", out["reason"])
        self.assertEqual(out["state"]["model"], "gpt-5.5")
        # No confirmation was ever sent.
        self.assertFalse(any("confirm" in r["params"].get("payload", {}) for r in self.rpcs()))

    def test_a_pick_while_a_turn_runs_lands_after_it(self):
        _, out = self.run_helper({"session_id": SID, "set": {"model": {"provider": "llamacpp", "model": "qwen3-8b.gguf"}}},
                                 TW_STUB_BUSY="1")
        self.assertEqual((out["applied"], out["reason"]), (True, "a turn is running; Hermes switches this chat when it ends"))

    def test_bad_requests_a_bot_and_a_stopped_gateway(self):
        for req in ({"session_id": ""}, {"session_id": SID, "set": {}}, {"session_id": SID, "set": {"model": "x"}},
                    {"session_id": SID, "set": {"reasoning": "high", "other": 1}}):
            with self.subTest(req=req):
                self.assertEqual(self.run_helper(req)[1], {"state": None, "applied": False, "reason": "bad request"})
        _, out = self.run_helper({"session_id": SID, "profile": "rift", "set": {"reasoning": "high"}})
        self.assertEqual(out["reason"], "a Bot's chat settings are not changed from here")
        _, out = self.run_helper({"session_id": SID}, TW_STUB_DOWN="1")
        self.assertEqual(out, {"state": None, "applied": False, "reason": "One's gateway is absent"})
        self.assertEqual(self.rpcs(), [])


STATE = {"model": "gpt-5.5", "provider": "openai-codex", "reasoning": "medium", "levels": ["low", "medium", "high"],
         "yolo": False, "busy": "steer",
         "offered": [{"provider": "openai-codex", "model": "gpt-5.5", "name": "OpenAI Codex", "why": "subscription"},
                     {"provider": "llamacpp", "model": "qwen3-8b.gguf", "name": "Local", "why": "local"}]}


class Core(unittest.TestCase):
    def setUp(self):
        for d in (own_control.CONTROLS.read, own_control.CONTROLS.asked, own_control.CONTROLS.changes):
            d.clear()
        self.w = World()
        self.addCleanup(self.w.close)
        self.g = Gateways(self.w)
        self.own = self.g.own
        self.g.hello("one")
        self.g.feed("one")
        self.p = passkey(self.w)

    def hand(self) -> list:
        return self.g.hello("one")["controls"]

    def answer(self, ask, state=STATE, applied=False, reason=""):
        return self.own.control_post(self.w.ws, "one", self.g.tok["one"],
                                     {"id": ask["id"], "state": state, "applied": applied, "reason": reason})

    def test_a_read_is_asked_handed_to_that_gateway_and_answered(self):
        got = self.own.control(self.p, "one", SID)
        self.assertEqual((got["pending"], got["state"], got["can_change"]), (True, None, True))
        asks = self.hand()
        self.assertEqual([(a["session_id"], "set" in a) for a in asks], [(SID, False)])
        self.assertEqual(self.g.hello("me")["controls"], [])
        self.answer(asks[0], {**STATE, "offered": STATE["offered"] + [{"provider": "x", "model": "bad model!"}]})
        got = self.own.control(self.p, "one", SID)
        self.assertFalse(got["pending"])
        self.assertEqual([o["model"] for o in got["state"]["offered"]], ["gpt-5.5", "qwen3-8b.gguf"])
        # A fresh read answers from memory; nothing more is asked of the connector for 30 seconds.
        self.assertEqual(self.hand(), [])
        self.w.clock.advance(seconds=31)
        self.own.control(self.p, "one", SID)
        self.assertEqual(len(self.hand()), 1)

    def read(self):
        self.own.control(self.p, "one", SID)
        self.answer(self.hand()[0])

    def test_a_change_is_checked_against_the_last_reads_offer(self):
        with self.assertRaises(CoreError) as e:
            self.own.control_set(self.p, "one", SID, {"reasoning": "high", "idempotency_key": "k-00000001"})
        self.assertEqual(e.exception.reason, "control_unread")
        self.read()
        for body, reason in (({"model": {"provider": "openrouter", "model": "paid/model"}}, "model_not_offered"),
                             ({"reasoning": "max"}, "bad_reasoning"), ({}, "bad_control"),
                             ({"model": "gpt-5.5"}, "bad_model")):
            with self.subTest(body=body), self.assertRaises(CoreError) as e:
                self.own.control_set(self.p, "one", SID, {**body, "idempotency_key": "k-00000002"})
            self.assertEqual(e.exception.reason, reason)

    def test_a_change_needs_a_recent_passkey_sign_in(self):
        self.read()
        for p in (passkey(self.w, minutes_ago=31), passkey(self.w, method="pairing")):
            with self.subTest(p=p.method), self.assertRaises(CoreError) as e:
                self.own.control_set(p, "one", SID, {"reasoning": "high", "idempotency_key": "k-00000003"})
            self.assertEqual(e.exception.reason, "passkey_needed")
        self.assertFalse(self.own.control(passkey(self.w, minutes_ago=31), "one", SID)["can_change"])

    def test_a_change_is_handed_out_answered_and_replayed_never_twice(self):
        self.read()
        body = {"model": {"provider": "llamacpp", "model": "qwen3-8b.gguf"}, "reasoning": "high",
                "idempotency_key": "k-00000004"}
        out = self.own.control_set(self.p, "one", SID, body)
        self.assertEqual(out["status"], "queued")
        self.assertEqual(self.own.control_set(self.p, "one", SID, body), {**out, "replayed": True})
        with self.assertRaises(CoreError) as e:
            self.own.control_set(self.p, "one", SID, {"reasoning": "low", "idempotency_key": "k-00000004"})
        self.assertEqual(e.exception.reason, "key_reused")
        ask = self.hand()[0]
        self.assertEqual(ask["set"], {"model": {"provider": "llamacpp", "model": "qwen3-8b.gguf"}, "reasoning": "high"})
        self.answer(ask, {**STATE, "model": "qwen3-8b.gguf", "provider": "llamacpp", "reasoning": "high"}, applied=True)
        got = self.own.control(self.p, "one", SID)
        self.assertEqual((got["change"]["status"], got["state"]["model"]), ("done", "qwen3-8b.gguf"))
        # What was asked for is recorded, never any words of the chat.
        ev = self.w.store.db.execute("SELECT body_json FROM events WHERE kind = 'own.control'").fetchall()
        self.assertEqual(len(ev), 1)
        self.assertEqual(json.loads(ev[0][0])["reasoning"], "high")

    def test_a_refused_or_deferred_change_says_so(self):
        self.read()
        self.own.control_set(self.p, "one", SID, {"reasoning": "high", "idempotency_key": "k-00000005"})
        self.answer(self.hand()[0], applied=False, reason="Model not switched: it needs a confirmation")
        self.assertEqual(self.own.control(self.p, "one", SID)["change"]["status"], "refused")
        self.own.control_set(self.p, "one", SID, {"model": {"provider": "llamacpp", "model": "qwen3-8b.gguf"},
                                                  "idempotency_key": "k-00000006"})
        self.answer(self.hand()[0], applied=True, reason="a turn is running; Hermes switches this chat when it ends")
        c = self.own.control(self.p, "one", SID)["change"]
        self.assertEqual((c["status"], c["reason"]), ("deferred", "a turn is running; Hermes switches this chat when it ends"))

    def test_the_newest_change_wins_and_an_unanswered_one_lapses(self):
        self.read()
        self.own.control_set(self.p, "one", SID, {"reasoning": "high", "idempotency_key": "k-00000007"})
        self.own.control_set(self.p, "one", SID, {"reasoning": "low", "idempotency_key": "k-00000008"})
        asks = self.hand()
        self.assertEqual([a["set"] for a in asks], [{"reasoning": "low"}])
        self.w.clock.advance(seconds=121)
        c = self.own.control(self.p, "one", SID)["change"]
        self.assertEqual(c["status"], "lapsed")
        with self.assertRaises(CoreError) as e:
            self.answer(asks[0])
        self.assertEqual(e.exception.reason, "not_asked")

    def test_tokens_switches_and_unlisted_chats(self):
        with self.assertRaises(CoreError) as e:
            self.own.control(self.p, "one", "20261101_000000_ffffff")
        self.assertEqual(e.exception.reason, "not_listed")
        self.own.control(self.p, "one", SID)
        ask = self.hand()[0]
        with self.assertRaises(CoreError) as e:
            self.own.control_post(self.w.ws, "one", self.g.tok["me"], {"id": ask["id"], "state": STATE})
        self.assertEqual(e.exception.status, 401)
        with self.assertRaises(CoreError) as e:
            self.own.control_post(self.w.ws, "me", self.g.tok["me"], {"id": ask["id"], "state": STATE})
        self.assertEqual(e.exception.reason, "not_asked")
        self.w.store.set_flag(self.w.ws, "own_chat_write", False, by="tests")
        for call in (lambda: self.own.control(self.p, "one", SID),
                     lambda: self.own.control_post(self.w.ws, "one", self.g.tok["one"], {"id": ask["id"], "state": STATE})):
            with self.assertRaises(CoreError) as e:
                call()
            self.assertEqual(e.exception.reason, "own_chat_off")

    def test_the_routes_over_http(self):
        s = Served(self)
        on(s.w, "own_chat_write", "mobile_read")
        g = Gateways(s.w)
        g.hello("one")
        g.feed("one")
        cookie, _ = s.phone()
        status, _, body = s.call("GET", f"/own/one/chats/{SID}/control", headers={"Cookie": cookie})
        self.assertEqual((status, body["pending"]), (200, True))
        ask = g.hello("one")["controls"][0]
        status, _, _ = s.call("POST", "/own/one/control", headers={"Authorization": f"Bearer {g.tok['one']}"},
                              body={"id": ask["id"], "state": STATE, "applied": False, "reason": ""})
        self.assertEqual(status, 200)
        status, _, body = s.call("GET", f"/own/one/chats/{SID}/control?wait=1", headers={"Cookie": cookie})
        self.assertEqual((status, body["state"]["model"]), (200, "gpt-5.5"))
        # A phone paired without a passkey sign-in reads, and cannot change.
        self.assertFalse(body["can_change"])


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

    def test_the_asks_are_run_through_the_helper_and_posted_back(self):
        script = self.dir / "helper.py"
        script.write_text("import json, sys, pathlib\nreq = json.loads(sys.stdin.read())\n"
                          f"pathlib.Path({str(self.dir / 'req.json')!r}).write_text(json.dumps(req))\n"
                          f"print(json.dumps({{'state': {STATE!r}, 'applied': 'set' in req, 'reason': ''}}))\n",
                          encoding="utf-8")
        hermes = own_connector.Hermes(["hermes"], [sys.executable, "-I", str(script)], self.dir, "One")
        said, core = [], FakeCore()
        conn = own_connector.OwnConnector.__new__(own_connector.OwnConnector)
        conn.hermes, conn.core, conn.say = hermes, core, said.append
        sent = conn.controls([{"id": "ctl_aaaaaaaaaa", "session_id": SID, "set": {"reasoning": "high"}},
                              {"id": "ctl_bbbbbbbbbb", "session_id": "../bad"}, "junk"])
        self.assertEqual(sent, 1)
        self.assertEqual(json.loads((self.dir / "req.json").read_text()),
                         {"op": "control", "profile": "", "name": "One", "session_id": SID, "set": {"reasoning": "high"}})
        self.assertEqual(core.posts, [("control", {"id": "ctl_aaaaaaaaaa", "state": STATE, "applied": True, "reason": ""})])
        self.assertEqual(said, ["PASS chat settings: changed"])


if __name__ == "__main__":
    unittest.main()
