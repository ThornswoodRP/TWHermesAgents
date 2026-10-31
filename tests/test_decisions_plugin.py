"""The thornswood-decisions plugin: its backend facade and its Desktop pane (step 2).

The backend runs inside Hermes, which this repo does not install, so the test
puts small stand-ins for `fastapi` and Hermes's `authenticated_subject` in
sys.modules for the length of each test, loads plugin_api.py by path, and points
it at a scratch core served on loopback. The pane's own logic runs under node
when node is there (tests/decisions_pane_check.mjs), and is skipped otherwise.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import subprocess
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_decisions_api import Served

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugins" / "thornswood-decisions"
PREFIX = "hermes-subject:"


def authenticated_subject(identity):
    # The pinned Hermes's naming (gateway/session_identity.py:296), minus the native case.
    return PREFIX + json.dumps([identity.get("provider") or "", identity.get("issuer") or "",
                                str(identity.get("user_id") or "unbound")], separators=(",", ":"))


def stubs(valid_token: bool = False) -> dict:
    fastapi = types.ModuleType("fastapi")

    class APIRouter:
        def __init__(self):
            self.routes = {}

        def post(self, path):
            def deco(fn):
                self.routes[path] = fn
                return fn
            return deco

        get = post

    fastapi.APIRouter = APIRouter
    fastapi.Request = object
    gateway = types.ModuleType("gateway")
    ident = types.ModuleType("gateway.session_identity")
    ident.authenticated_subject = authenticated_subject
    gateway.session_identity = ident
    cli = types.ModuleType("hermes_cli")
    web = types.ModuleType("hermes_cli.web_server")
    web._has_valid_session_token = lambda request: valid_token
    cli.web_server = web
    return {"fastapi": fastapi, "gateway": gateway, "gateway.session_identity": ident,
            "hermes_cli": cli, "hermes_cli.web_server": web}


def load(testcase: unittest.TestCase, **kw):
    patcher = mock.patch.dict(sys.modules, stubs(**kw))
    patcher.start()
    testcase.addCleanup(patcher.stop)
    spec = importlib.util.spec_from_file_location("tw_decisions_plugin_api", PLUGIN / "dashboard" / "plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeRequest:
    def __init__(self, body: dict | None = None, *, session=None, token=None, native=None, auth_required=True):
        self.state = SimpleNamespace()
        if session is not None:
            self.state.session = session
        if token is not None:
            self.state.token_principal = token
        if native is not None:
            self.state.native_http_principal = native
        self.app = SimpleNamespace(state=SimpleNamespace(auth_required=auth_required))
        self._raw = json.dumps(body or {}).encode()

    async def body(self) -> bytes:
        return self._raw


def patrick_session(user_id: str = "patrick-gh-1"):
    return SimpleNamespace(user_id=user_id, provider="github", issuer="https://github.com", email="")


class WhoIsCalling(unittest.TestCase):
    def test_a_verified_session_is_named_as_hermes_names_it(self):
        mod = load(self)
        name = mod.hermes_principal(FakeRequest(session=patrick_session()))
        self.assertEqual(name, PREFIX + '["github","https://github.com","patrick-gh-1"]')

    def test_token_and_native_callers_are_named_apart(self):
        mod = load(self)
        token = mod.hermes_principal(FakeRequest(token=SimpleNamespace(principal="svc", provider="p")))
        native = mod.hermes_principal(FakeRequest(native={"subject": "1000"}))
        self.assertTrue(token.startswith("token:"))
        self.assertEqual(native, "native:1000")

    def test_the_loopback_session_token_owner(self):
        mod = load(self, valid_token=True)
        name = mod.hermes_principal(FakeRequest(auth_required=False))
        self.assertEqual(name, PREFIX + '["session-token","","legacy-token-owner"]')

    def test_no_sign_in_is_none(self):
        mod = load(self, valid_token=False)
        self.assertIsNone(mod.hermes_principal(FakeRequest(auth_required=False)))
        # With the sign-in gate on, a valid loopback token alone is not a sign-in.
        mod = load(self, valid_token=True)
        self.assertIsNone(mod.hermes_principal(FakeRequest(auth_required=True)))

    def test_the_pane_cannot_name_the_caller(self):
        mod = load(self)
        req = FakeRequest({"hermes_principal": "someone"}, session=None)
        req.state.hermes_principal = "someone"
        self.assertIsNone(mod.hermes_principal(req))


class Facade(unittest.TestCase):
    def setUp(self):
        self.s = Served(self)
        self.mod = load(self)
        self.mod.CORE = f"http://127.0.0.1:{self.s.port}/api/v1"

    def run_route(self, name: str, body: dict, **who):
        return asyncio.run(getattr(self.mod, name)(FakeRequest(body, **who)))

    def paired(self, user_id: str = "patrick-gh-1") -> dict:
        code = self.s.code("desktop")
        env = self.run_route("pair", {"code": code}, session=patrick_session(user_id))
        self.assertEqual(env["status"], 200, env)
        return {"token": env["body"]["token"], "csrf": env["body"]["csrf"]}

    def test_pair_read_and_answer_through_the_facade(self):
        session = self.paired()
        card = self.s.w.card()
        env = self.run_route("cards", {"session": session}, session=patrick_session())
        self.assertEqual((env["status"], [c["id"] for c in env["body"]["cards"]]), (200, [card["id"]]))
        env = self.run_route("artifact", {"session": session, "id": card["id"]}, session=patrick_session())
        self.assertEqual(env["status"], 200)
        self.assertEqual(env["body"]["sha256"], card["artifact_sha256"])
        answer = {"session": session, "id": card["id"], "option_key": "a", "revision": card["revision"],
                  "attention_revision": card["attention_revision"], "artifact_sha256": card["artifact_sha256"],
                  "idempotency_key": "ans_0123456789abcdef"}
        env = self.run_route("answer", answer, session=patrick_session())
        self.assertEqual((env["status"], env["body"]["option_key"]), (200, "a"))
        again = self.run_route("answer", answer, session=patrick_session())
        self.assertEqual((again["status"], again["body"]["response_id"]), (200, env["body"]["response_id"]))

    def test_another_hermes_sign_in_cannot_use_the_token(self):
        session = self.paired()
        env = self.run_route("cards", {"session": session}, session=patrick_session("someone-else"))
        self.assertEqual((env["status"], env["body"]["error"]), (401, "wrong_hermes_sign_in"))

    def test_no_hermes_sign_in_cannot_pair(self):
        env = self.run_route("pair", {"code": self.s.code("desktop")})
        self.assertEqual((env["status"], env["body"]["error"]), (403, "no_hermes_sign_in"))

    def test_a_409_comes_back_with_its_detail(self):
        session = self.paired()
        card = self.s.w.card()
        env = self.run_route("answer", {"session": session, "id": card["id"], "option_key": "a",
                                        "revision": card["revision"] + 1,
                                        "attention_revision": card["attention_revision"],
                                        "artifact_sha256": card["artifact_sha256"],
                                        "idempotency_key": "ans_fedcba9876543210"}, session=patrick_session())
        self.assertEqual((env["status"], env["body"]["error"]), (409, "stale_revision"))
        self.assertEqual(env["body"]["detail"]["revision"], card["revision"])

    def test_ids_and_keys_are_checked_before_any_call(self):
        session = self.paired()
        for bad in ("../me", "a/b", "", "x" * 65, 7):
            env = self.run_route("card", {"session": session, "id": bad}, session=patrick_session())
            self.assertEqual((env["status"], env["body"]["error"]), (400, "bad_id"))
        env = self.run_route("answer", {"session": session, "id": "dc_x", "idempotency_key": "short"},
                             session=patrick_session())
        self.assertEqual((env["status"], env["body"]["error"]), (400, "bad_key"))

    def test_no_session_is_signed_out_without_a_call(self):
        env = self.run_route("cards", {}, session=patrick_session())
        self.assertEqual((env["status"], env["body"]["error"]), (401, "signed_out"))

    def test_core_down_is_status_0(self):
        self.mod.CORE = "http://127.0.0.1:9/api/v1"
        env = self.run_route("cards", {"session": {"token": "t"}}, session=patrick_session())
        self.assertEqual((env["status"], env["body"]["error"]), (0, "core_unreachable"))


class Shape(unittest.TestCase):
    def test_the_package_is_laid_out_as_hermes_reads_it(self):
        manifest = json.loads((PLUGIN / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual((manifest["name"], manifest["api"]), ("thornswood-decisions", "plugin_api.py"))
        self.assertTrue((PLUGIN / "desktop" / "plugin.js").is_file())

    def test_the_pane_imports_only_what_desktop_allows(self):
        js = (PLUGIN / "desktop" / "plugin.js").read_text(encoding="utf-8")
        specifiers = set()
        for line in js.splitlines():
            if line.startswith("import "):
                specifiers.add(line.rsplit(" from ", 1)[1].strip().strip("'\""))
        self.assertTrue(specifiers <= {"@hermes/plugin-sdk", "react", "react/jsx-runtime"}, specifiers)
        self.assertNotIn("import(", js)
        self.assertIn("id: 'thornswood-decisions'", js)
        self.assertIn("defaultEnabled: false", js)
        for bare in ("window.setInterval", "window.setTimeout", "innerHTML", "dangerouslySetInnerHTML"):
            self.assertNotIn(bare, js)

    def test_the_backend_never_logs(self):
        src = (PLUGIN / "dashboard" / "plugin_api.py").read_text(encoding="utf-8")
        for word in ("logging", "print(", "logger"):
            self.assertNotIn(word, src)


@unittest.skipUnless(shutil.which("node"), "node is not installed here")
class PaneLogic(unittest.TestCase):
    def test_the_pane_under_node(self):
        script = Path(__file__).with_name("decisions_pane_check.mjs")
        out = subprocess.run([shutil.which("node"), str(script), str(PLUGIN / "desktop" / "plugin.js")],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("ALL PASS", out.stdout)


if __name__ == "__main__":
    unittest.main()
