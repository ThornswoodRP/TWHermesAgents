"""A chat's details in the app: its model and thinking, read and changed for that chat only (plan Part 6).

Checks:

- room_send.py op `control`, against stand-in Hermes modules: the read (session.resume to open the chat,
  then session.info, config.get reasoning, busy and yolo, and model.options); only models his sign-ins, a
  free model or a local one covers are offered, with Anthropic offered only when its route carries his
  Claude login; a model change and a thinking change each go through Hermes's own PreparedMutations
  (session.mutate fenced to the chat's revision and generation); a pick not offered is refused before
  anything is sent; a model Hermes's selection guard holds for a confirmation is not switched; a pick
  while a turn runs is said to land after it; the model is the one the chat's agent is on now, with
  Hermes's own word when that is a fallback (session.usage fallback_model); a Bot's Bot Chat through its own
  gateway, offered only its own routes and changed in its model and thinking only;
- the core: a read asked for, handed to that gateway's connector and answered; a change checked against
  the last read's offer, needing a passkey sign-in of the last 30 minutes, the newest change winning, the
  same idempotency key replayed and never a second change; an unanswered change given up after 2 minutes;
  the routes over HTTP; a Bot's chat asked through the Bots' one connector, each ask naming its Bot;
- One's connector and the Bots' connector: the asks the core handed out, run through the helper and posted
  back.

Everything runs on stand-ins and a scratch workspace; nothing reaches a real Hermes.
"""

from __future__ import annotations

import json
from dataclasses import replace
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import unittest.mock
from datetime import timedelta
from pathlib import Path

from decisions_helpers import World
from test_decisions_api import Served
from test_decisions_own import SID, Gateways, on, passkey

from thornswood_decisions import connector, own_connector, own_control
from thornswood_decisions.chat import Chat
from thornswood_decisions.feeds import Feeds
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
    STATE = {"model": "gpt-5.5", "provider": "openai-codex", "effort": "medium", "revision": 7, "generation": 3,
             "busy": "steer", "approvals": "manual"}
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
            if method.startswith("config.") and params.get("key") == "approvals.mode" and "session_id" not in params:
                # gateway/session_config.py config_get and session_tui_config.py _set_approval_mode: the
                # profile's own mode, read and written with no session, as Desktop's approval chip does.
                if method == "config.set":
                    if params["value"] not in ("manual", "smart", "off"):
                        raise GatewayRPCError("invalid_params")
                    STATE["approvals"] = params["value"]
                return {"key": "approvals.mode", "value": STATE["approvals"]}
            if params.get("session_id") != "20261101_201500_ab12cd":
                raise GatewayRPCError("not_found")
            if method == "session.resume":
                return {"session_id": params["session_id"], "revision": STATE["revision"],
                        "execution_generation": STATE["generation"]}
            if method == "session.info":
                if os.environ.get("TW_STUB_FALLBACK"):
                    # The chat's main model, as its row holds it, while its agent answers on a fallback.
                    return {"model": "claude-opus-5-5", "provider": "anthropic", "source": "tui", "lazy": False}
                return {"model": STATE["model"], "provider": STATE["provider"], "source": "tui", "lazy": False,
                        "cwd": "/home/patrick/thornswood"}
            if method == "config.get":
                return {"reasoning": {"value": STATE["effort"], "display": "show"},
                        "busy": {"key": "busy", "value": STATE["busy"], "scope": "session"},
                        "yolo": {"key": "yolo", "value": "0", "scope": "session"}}[params["key"]]
            if method == "config.set" and params["key"] == "busy":
                # gateway/session_busy_controls.py busy_config with write=True.
                if params["value"] not in ("interrupt", "steer", "queue"):
                    raise GatewayRPCError("invalid_params")
                STATE["busy"] = params["value"]
                return {"key": "busy", "value": STATE["busy"], "scope": "session"}
            if method == "model.options":
                return OPTIONS
            if method == "session.usage":
                # tui_gateway/methods_session.py session.usage: no agent built yet means no context figures.
                # gateway/session_usage.py usage_stats: the live agent's model and provider, and fallback_model
                # set only while _fallback_activated.
                fell = bool(os.environ.get("TW_STUB_FALLBACK"))
                return {"model": STATE["model"], "provider": STATE["provider"] if fell else "", "input": 12345,
                        "output": 4100, "total": 16445, "calls": 3, "compressions": 1,
                        "fallback_model": STATE["model"] if fell else None}
            if method == "session.context_breakdown":
                if os.environ.get("TW_STUB_NOCTX"):
                    raise GatewayRPCError("Could not compute context breakdown: no prompt")
                return {"categories": [{"id": "system", "label": "System", "color": "x", "tokens": 9000}],
                        "context_max": 200000, "context_percent": 41, "context_used": 82000,
                        "estimated_total": 82000, "context_estimated": True, "context_source": "estimate",
                        "model": STATE["model"]}
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

# hermes_state_registry.py acquire: Hermes's own SessionDB, as far as get_session's row goes.
STUB_REGISTRY = textwrap.dedent('''
    class _DB:
        def get_session(self, sid):
            if sid != "20261101_201500_ab12cd":
                return None
            return {"id": sid, "message_count": 42, "started_at": 1794251520.0}
    def acquire(path=None):
        return _DB()
    def release_or_close(db):
        pass
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


class ContextAndTokens(unittest.TestCase):
    """What the core keeps of a read's context and token use (own_control._use)."""

    def test_only_whole_counts_reach_the_app(self):
        st = own_control.clean_state({"context": {"context_used": 150, "context_max": 100, "context_estimated": "yes"},
                                      "usage": {"input": 1, "output": 2, "total": 3, "calls": True}})
        self.assertEqual(st["context"], {"used": 150, "max": 100, "percent": 100, "estimated": False})
        self.assertIsNone(st["usage"], "a count that is not a whole number is not shown")
        for bad in ({"context_used": -1, "context_max": 100}, {"context_used": 5, "context_max": 0}, "full"):
            self.assertIsNone(own_control.clean_state({"context": bad})["context"], bad)
        self.assertNotIn("context_why", own_control.clean_state({"context_why": "Hermes said no"} | {
            "context": {"context_used": 1, "context_max": 10}}))


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
                           ("hermes_constants.py", STUB_CONSTANTS), ("hermes_state_registry.py", STUB_REGISTRY)):
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
        self.assertEqual((st["model"], st["provider"], st["reasoning"], st["busy"], st["yolo"], st["approvals"]),
                         ("gpt-5.5", "openai-codex", "medium", "steer", False, "manual"))
        self.assertEqual(st["levels"], ["low", "medium", "high", "xhigh"])
        # Opened as Desktop opens a chat, then only reads.
        self.assertEqual([r["method"] for r in self.rpcs()],
                         ["session.resume", "session.info", "config.get", "config.get", "config.get", "config.get",
                          "model.options",
                          "session.usage", "session.context_breakdown"])

    def test_the_read_says_how_big_the_chat_is_when_it_started_and_its_folder(self):
        _, out = self.run_helper({"session_id": SID})
        st = out["state"]
        self.assertEqual((st["messages"], st["started"], st["folder"]), (42, 1794251520.0, "/home/patrick/thornswood"))
        clean = own_control.clean_state(st)
        self.assertEqual((clean["messages"], clean["started"], clean["folder"]), (42, 1794251520.0, "/home/patrick/thornswood"))
        odd = own_control.clean_state({"messages": -3, "started": "yesterday", "folder": 7})
        self.assertEqual((odd["messages"], odd["started"], odd["folder"]), (None, None, None))

    def test_the_read_says_how_full_the_context_is_and_the_tokens_used(self):
        _, out = self.run_helper({"session_id": SID})
        st = out["state"]
        self.assertEqual(st["context"], {"context_used": 82000, "context_max": 200000, "context_percent": 41,
                                         "context_estimated": True})
        self.assertEqual(st["usage"], {"input": 12345, "output": 4100, "total": 16445, "calls": 3, "compressions": 1})
        self.assertEqual(own_control.clean_state(st)["context"], {"used": 82000, "max": 200000, "percent": 41, "estimated": True})

    def test_a_context_hermes_would_not_give_leaves_the_rest_of_the_read(self):
        _, out = self.run_helper({"session_id": SID}, TW_STUB_NOCTX="1")
        st = out["state"]
        self.assertEqual((st["model"], st["context"]), ("gpt-5.5", None))
        self.assertEqual(st["context_why"],
                         "Hermes did not say (Could not compute context breakdown: no prompt)")
        self.assertEqual(st["usage"]["calls"], 3)
        clean = own_control.clean_state(st)
        self.assertEqual((clean["context"], clean["context_why"]),
                         (None, "Hermes did not say (Could not compute context breakdown: no prompt)"))

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

    def test_the_busy_setting_and_the_approval_mode_change_as_desktop_changes_them(self):
        _, out = self.run_helper({"session_id": SID, "set": {"busy": "queue", "approvals": "smart"}})
        self.assertEqual((out["applied"], out["reason"]), (True, ""))
        self.assertEqual((out["state"]["busy"], out["state"]["approvals"]), ("queue", "smart"))
        sets = [r["params"] for r in self.rpcs() if r["method"] == "config.set"]
        # The busy setting is the chat's; the approval mode is the profile's, sent with no session.
        self.assertEqual(sets, [{"session_id": SID, "key": "busy", "value": "queue"},
                                {"key": "approvals.mode", "value": "smart"}])
        self.assertNotIn("session.mutate", [r["method"] for r in self.rpcs()])
        for bad in ({"busy": "later"}, {"approvals": "yolo"}, {"busy": None, "approvals": None}):
            with self.subTest(bad=bad):
                self.assertEqual(self.run_helper({"session_id": SID, "set": bad})[1],
                                 {"state": None, "applied": False, "reason": "bad request"})

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

    def test_a_fallback_shows_the_model_that_answers_and_says_it_fell_back(self):
        """Patrick, 11 Nov: the app said a Bot was on Claude while ChatGPT answered. The model is the live
        agent's, and Hermes's fallback_model is the word that it fell back."""
        _, out = self.run_helper({"session_id": SID}, TW_STUB_FALLBACK="1")
        st = out["state"]
        self.assertEqual((st["model"], st["provider"], st["fallback"]), ("gpt-5.5", "openai-codex", True))
        self.assertIs(own_control.clean_state(st)["fallback"], True)
        _, out = self.run_helper({"session_id": SID})
        self.assertIs(out["state"]["fallback"], False)
        self.assertIs(own_control.clean_state({"fallback": "yes"})["fallback"], False)

    def test_a_bots_chat_is_read_and_changed_within_its_own_routes(self):
        allowed = [["anthropic", "claude-opus-5-5"], ["openai-codex", "gpt-5.5"], ["openai-codex", "gpt-5.5-mini"]]
        bot = {"session_id": SID, "profile": "rift", "name": "Rift", "allowed": allowed}
        _, out = self.run_helper(bot)
        self.assertEqual({(o["provider"], o["model"]) for o in out["state"]["offered"]},
                         {(p, m) for p, m in allowed})
        self.assertEqual({o["why"] for o in out["state"]["offered"]}, {"subscription"})
        _, out = self.run_helper({**bot, "set": {"model": {"provider": "anthropic", "model": "claude-opus-5-5"},
                                                 "reasoning": "high"}})
        self.assertEqual((out["applied"], out["state"]["model"], out["state"]["reasoning"]),
                         (True, "claude-opus-5-5", "high"))
        # A model off its own list, a free or local one included, is refused before anything is sent.
        before = len([r for r in self.rpcs() if r["method"] == "session.mutate"])
        for pick in ({"provider": "llamacpp", "model": "qwen3-8b.gguf"}, {"provider": "openrouter", "model": "priced/free"}):
            with self.subTest(pick=pick):
                _, out = self.run_helper({**bot, "set": {"model": pick}})
                self.assertEqual((out["applied"], out["reason"]), (False, "that model is not one of this Bot's own routes"))
        self.assertEqual(len([r for r in self.rpcs() if r["method"] == "session.mutate"]), before)
        # Its approvals and its busy rule are its policy's.
        for want in ({"busy": "queue"}, {"approvals": "off"}):
            with self.subTest(want=want):
                self.assertEqual(self.run_helper({**bot, "set": want})[1], {
                    "state": None, "applied": False, "reason": "a Bot's chat takes a model and a thinking level only"})
        self.assertNotIn("config.set", [r["method"] for r in self.rpcs()])

    def test_bad_requests_a_bot_and_a_stopped_gateway(self):
        for req in ({"session_id": ""}, {"session_id": SID, "set": {}}, {"session_id": SID, "set": {"model": "x"}},
                    {"session_id": SID, "set": {"reasoning": "high", "other": 1}},
                    # A Bot's ask carries its own models; one without them, or a bad profile, is not run.
                    {"session_id": SID, "profile": "rift", "set": {"reasoning": "high"}},
                    {"session_id": SID, "profile": "../x", "allowed": []},
                    {"session_id": SID, "profile": "rift", "allowed": [["anthropic"]]}):
            with self.subTest(req=req):
                self.assertEqual(self.run_helper(req)[1], {"state": None, "applied": False, "reason": "bad request"})
        _, out = self.run_helper({"session_id": SID}, TW_STUB_DOWN="1")
        self.assertEqual(out, {"state": None, "applied": False, "reason": "One's gateway is absent"})
        self.assertEqual(self.rpcs(), [])


STATE = {"model": "gpt-5.5", "provider": "openai-codex", "reasoning": "medium", "levels": ["low", "medium", "high"],
         "yolo": False, "busy": "steer", "approvals": "manual",
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

    def test_the_busy_setting_and_the_approval_mode_are_checked(self):
        self.read()
        self.assertEqual(self.own.control(self.p, "one", SID)["state"]["approvals"], "manual")
        for body, reason in (({"busy": "later"}, "bad_busy"), ({"approvals": "yolo"}, "bad_approvals")):
            with self.subTest(body=body), self.assertRaises(CoreError) as e:
                self.own.control_set(self.p, "one", SID, {**body, "idempotency_key": "k-00000004"})
            self.assertEqual(e.exception.reason, reason)
        out = self.own.control_set(self.p, "one", SID, {"busy": "queue", "approvals": "smart",
                                                        "idempotency_key": "k-00000005"})
        self.assertEqual(out["status"], "queued")
        self.assertEqual(self.hand()[0]["set"], {"busy": "queue", "approvals": "smart"})

    def test_approvals_off_needs_a_sign_in_from_the_last_5_minutes(self):
        self.read()
        stale = replace(passkey(self.w, minutes_ago=10), fresh=False)
        with self.assertRaises(CoreError) as e:
            self.own.control_set(stale, "one", SID, {"approvals": "off", "idempotency_key": "k-00000006"})
        self.assertEqual((e.exception.status, e.exception.reason), (403, "sign_in_again"))
        self.assertIn("last 5 minutes", str(e.exception))
        self.assertEqual(self.hand(), [], "nothing was handed to the connector")
        # Manual and Smart take the 30 minutes any change takes.
        self.own.control_set(stale, "one", SID, {"approvals": "smart", "idempotency_key": "k-00000007"})
        fresh = replace(passkey(self.w), fresh=True)
        out = self.own.control_set(fresh, "one", SID, {"approvals": "off", "idempotency_key": "k-00000008"})
        self.assertEqual(out["status"], "queued")

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


BOTS = ("bramble", "rift", "sprocket", "echo", "magpie", "wisp")
BOT_STATE = {"model": "gpt-5.5", "provider": "openai-codex", "fallback": True, "reasoning": "medium",
             "levels": ["low", "medium", "high"],
             "offered": [{"provider": "anthropic", "model": "claude-opus-5-5", "name": "Anthropic", "why": "subscription"},
                         {"provider": "openai-codex", "model": "gpt-5.5", "name": "OpenAI Codex", "why": "subscription"}]}


class BotChatCore(unittest.TestCase):
    """A Bot's Bot Chat details (Patrick, 11 Nov): asked through the Bots' one connector, each ask naming its
    Bot, and a change of its model or thinking only."""

    def setUp(self):
        for d in (own_control.BOT_CONTROLS.read, own_control.BOT_CONTROLS.asked, own_control.BOT_CONTROLS.changes):
            d.clear()
        self.w = World()
        self.addCleanup(self.w.close)
        for bot in BOTS:
            self.w.core.bind_bot(self.w.ws, bot, bot, by="tests")
        for flag in ("mobile_chat_write", "connector_effects"):
            self.w.store.set_flag(self.w.ws, flag, True, by="tests")
        self.chat = Chat(self.w.store)
        self.tok = Feeds(self.w.store).new_token(self.w.ws, "connector", by="tests")
        feeds = Feeds(self.w.store)
        feeds.put(self.w.ws, "bots", feeds.new_token(self.w.ws, "bots", by="tests"), {
            "source_time": self.w.store.clock().isoformat(),
            "bots": [{"bot": "rift", "session_id": SID, "messages": [],
                      "route": {"model": "gpt-5.5", "provider": "openai-codex", "fallback": True, "at": ""}}]})
        self.p = self.w.patrick()

    def hand(self) -> list:
        return self.chat.claim(self.w.ws, self.tok)["controls"]

    def answer(self, ask, state=BOT_STATE, applied=False, reason=""):
        return self.chat.control_post(self.w.ws, self.tok, {"id": ask["id"], "state": state, "applied": applied,
                                                            "reason": reason})

    def test_the_list_and_the_chat_say_the_model_and_that_it_fell_back(self):
        rift = next(b for b in self.chat.summary(self.p)["bots"] if b["bot"] == "rift")
        self.assertEqual((rift["route"]["model"], rift["route"]["fallback"]), ("gpt-5.5", True))
        self.assertEqual(self.chat.view(self.p, "rift")["route"]["model"], "gpt-5.5")
        self.assertIsNone(next(b for b in self.chat.summary(self.p)["bots"] if b["bot"] == "echo")["route"])

    def test_a_read_goes_to_the_bots_connector_naming_its_bot(self):
        got = self.chat.control(self.p, "rift")
        self.assertEqual((got["pending"], got["state"], got["can_change"]), (True, None, True))
        asks = self.hand()
        self.assertEqual([(a["bot"], a["session_id"], "set" in a) for a in asks], [("rift", SID, False)])
        self.answer(asks[0])
        got = self.chat.control(self.p, "rift")
        self.assertEqual((got["pending"], got["state"]["model"], got["state"]["fallback"]), (False, "gpt-5.5", True))

    def test_a_change_is_its_model_or_thinking_and_goes_once(self):
        self.chat.control(self.p, "rift")
        self.answer(self.hand()[0])
        for body, reason in (({"busy": "queue", "idempotency_key": "key-00000001"}, "bad_control"),
                             ({"approvals": "off", "idempotency_key": "key-00000001"}, "bad_control"),
                             ({"reasoning": "high"}, "bad_key"),
                             ({"model": {"provider": "llamacpp", "model": "qwen3-8b.gguf"},
                               "idempotency_key": "key-00000001"}, "model_not_offered")):
            with self.subTest(body=body):
                with self.assertRaises(CoreError) as e:
                    self.chat.control_set(self.p, "rift", body)
                self.assertEqual(e.exception.reason, reason)
        want = {"model": {"provider": "anthropic", "model": "claude-opus-5-5"}, "idempotency_key": "key-00000002"}
        self.assertEqual(self.chat.control_set(self.p, "rift", want)["status"], "queued")
        self.assertTrue(self.chat.control_set(self.p, "rift", want).get("replayed"))
        asks = self.hand()
        self.assertEqual([(a["bot"], a["set"]) for a in asks],
                         [("rift", {"model": {"provider": "anthropic", "model": "claude-opus-5-5"}})])
        self.answer(asks[0], {**BOT_STATE, "model": "claude-opus-5-5", "provider": "anthropic", "fallback": False},
                    applied=True)
        got = self.chat.control(self.p, "rift")
        self.assertEqual((got["change"]["status"], got["state"]["model"], got["state"]["fallback"]),
                         ("done", "claude-opus-5-5", False))
        self.assertEqual(self.hand(), [])

    def test_no_bot_chat_no_such_bot_and_the_switch(self):
        for bot, reason in (("echo", "no_bot_chat"), ("nobody", "no_chat")):
            with self.subTest(bot=bot):
                with self.assertRaises(CoreError) as e:
                    self.chat.control(self.p, bot)
                self.assertEqual(e.exception.reason, reason)
        with self.assertRaises(CoreError) as e:
            self.chat.control_post(self.w.ws, "nope", {"id": "ctl_aaaaaaaaaa"})
        self.assertEqual(e.exception.status, 401)
        self.w.store.set_flag(self.w.ws, "mobile_chat_write", False, by="tests")
        with self.assertRaises(CoreError) as e:
            self.chat.control(self.p, "rift")
        self.assertEqual(e.exception.reason, "chat_write_off")


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


    def test_the_bots_connector_asks_each_bots_own_gateway_with_its_own_models(self):
        script = self.dir / "helper.py"
        script.write_text("import json, sys, pathlib\nreq = json.loads(sys.stdin.read())\n"
                          f"pathlib.Path({str(self.dir / 'req.json')!r}).write_text(json.dumps(req))\n"
                          f"print(json.dumps({{'state': {BOT_STATE!r}, 'applied': 'set' in req, 'reason': ''}}))\n",
                          encoding="utf-8")
        hermes = connector.Hermes.__new__(connector.Hermes)
        hermes._helper = lambda req, seconds: json.loads(subprocess.run(
            [sys.executable, "-I", str(script)], input=json.dumps(req).encode(), capture_output=True).stdout)
        said, core = [], FakeCore()
        conn = connector.Connector.__new__(connector.Connector)
        conn.hermes, conn.core, conn.say = hermes, core, said.append
        models = [["anthropic", "claude-opus-5-5"], ["openai-codex", "gpt-5.5"]]
        with unittest.mock.patch.object(connector, "bot_models", return_value=models) as bm:
            sent = conn.controls([{"id": "ctl_aaaaaaaaaa", "bot": "rift", "session_id": SID, "set": {"reasoning": "high"}},
                                  {"id": "ctl_bbbbbbbbbb", "bot": "../x", "session_id": SID},
                                  {"id": "ctl_cccccccccc", "session_id": SID}, "junk"])
        self.assertEqual(sent, 1)
        bm.assert_called_once_with("rift")
        self.assertEqual(json.loads((self.dir / "req.json").read_text()),
                         {"op": "control", "profile": "rift", "name": "Rift", "session_id": SID, "allowed": models,
                          "set": {"reasoning": "high"}})
        self.assertEqual(core.posts, [("/connector/control", {"id": "ctl_aaaaaaaaaa", "state": BOT_STATE,
                                                              "applied": True, "reason": ""})])

    def test_a_bots_models_are_its_own_routes_it_reaches_with_its_own_sign_in(self):
        from thornswood_guard.policy import Policy
        from thornswood_guard.routes import chain_for
        policy = Policy.load()
        for bot in BOTS:
            with self.subTest(bot=bot):
                got = connector.bot_models(bot)
                self.assertTrue(got, "every Bot has a route of its own")
                chain = [[r["provider"], r["model"]] for r in chain_for(policy, bot, proxy_ready=False)]
                self.assertTrue(all(m in chain for m in got))
        self.assertEqual(connector.bot_models("nobody"), [])


if __name__ == "__main__":
    unittest.main()
