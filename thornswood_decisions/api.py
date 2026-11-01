"""The decision core's HTTP API on loopback (07 sections 2 and 5).

It listens on 127.0.0.1 only. Tailscale Serve puts the phone in front of it
(step 3); Hermes Desktop reaches it through the thornswood-decisions plugin's
backend, which runs in Patrick's Hermes on the same VM. The feeds and the
connector, on the same VM, each carry a token that does only their one job.

Two ways to carry a session, never both:
- a cookie (the phone): every write needs the exact Origin of the app and the
  CSRF token in `X-CSRF-Token`;
- `Authorization: Bearer` (the Desktop plugin's backend, a server-side caller):
  every write needs the CSRF token too, and any Origin it sends must be exact.
  The plugin passes the Hermes sign-in it saw in `X-TW-Hermes-Principal`, and a
  Desktop device answers only through the sign-in that paired it.

GET never changes a card. Every answer goes through `DecisionCore.answer`.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from pathlib import Path

from .auth import Principal
from .chat import Chat
from .core import DecisionCore
from .feeds import MAX_FEED_BYTES, Feeds
from .passkey import Passkeys
from .push import Push
from .store import CoreError, Store, now_utc

PREFIX = "/api/v1"
MAX_REQUEST_BYTES = 64 * 1024
COOKIE = "tw_session"
DEFAULT_PORT = 9135
APP_DIR = Path(__file__).with_name("app")
APP_FILES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "sw.js": "text/javascript; charset=utf-8",
    "manifest.webmanifest": "application/manifest+json",
    "icon.svg": "image/svg+xml",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
}
# The app's own views, each a deep link that loads the shell. needs-you, work
# and room are the tabs' first names (before Decisions, Board and Rooms), kept
# so an old alert or home screen link still opens.
APP_VIEWS = re.compile(r"^(|decisions|board|rooms|bots|bramble|settings|needs-you|work|room|"
                       r"decisions/[A-Za-z0-9_-]{1,64}|bots/[a-z0-9][a-z0-9_-]{0,63}|rooms/[A-Za-z0-9_-]{1,64})$")
APP_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
           "manifest-src 'self'; worker-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
# The service worker's own policy: it fetches only this origin, to keep the shell,
# and its alerts' icon is this origin's too.
SW_CSP = "default-src 'none'; connect-src 'self'; img-src 'self'"
# Data the phone reads only while mobile_read is on. Signing in, adding a
# passkey and signing out work with it off, so the phone can be set up first.
APP_DATA = {("GET", "cards"), ("GET", "changes"), ("GET", "work"), ("GET", "room"), ("GET", "chat"), ("GET", "push")}
SECURITY_HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
}


class Api:
    """Turns one request into one response. Each thread keeps its own store connection."""

    def __init__(self, db_path, workspace: str, *, origin: str | None = None,
                 clock: Callable[[], datetime] = now_utc):
        self.db_path = db_path
        self.workspace = workspace
        self.origin = origin
        self.clock = clock
        self._local = threading.local()
        self._stores: list[Store] = []
        self._lock = threading.Lock()
        core = self._core()
        row = core.s.db.execute("SELECT id FROM workspaces WHERE name = ?", (workspace,)).fetchone()
        if not row:
            raise CoreError(500, "no_workspace", f"no workspace named {workspace}; run the init command first")
        self.ws = row["id"]

    def _core(self) -> DecisionCore:
        core = getattr(self._local, "core", None)
        if core is None:
            store = Store(self.db_path, clock=self.clock)
            with self._lock:
                self._stores.append(store)
            core = self._local.core = DecisionCore(store)
        return core

    def close(self) -> None:
        with self._lock:
            for s in self._stores:
                s.close()
            self._stores.clear()

    # ── one request ───────────────────────────────────────────────────────

    def handle(self, method: str, target: str, headers: dict[str, str], body: bytes) -> tuple[int, dict, bytes]:
        headers = {k.lower(): v for k, v in headers.items()}
        extra: dict[str, str] = {}
        try:
            url = urlsplit(target)
            if len(body) > body_limit(url.path):
                raise CoreError(413, "too_big", "the request body is too big")
            if url.path == "/" and method == "GET":
                return 302, {**SECURITY_HEADERS, "Location": "/m/"}, b"{}"
            if url.path.startswith("/m/"):
                return self._static(method, url.path[3:])
            parts = [p for p in url.path.split("/") if p]
            if parts[:2] != ["api", "v1"]:
                raise CoreError(404, "no_route", "no such route")
            route = parts[2:]
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            data = self._json(body) if method == "POST" else {}
            status, out = self._route(method, route, query, headers, data, extra)
        except CoreError as exc:
            return _refusal(exc.status, exc.reason, str(exc), exc.detail)
        return status, {**SECURITY_HEADERS, **extra}, json.dumps(out).encode("utf-8")

    def _static(self, method: str, rest: str) -> tuple[int, dict, bytes]:
        if method != "GET":
            return _refusal(405, "method", "the app's files are read with GET")
        name = rest if rest in APP_FILES else ("index.html" if APP_VIEWS.match(rest.rstrip("/")) else None)
        if name is None:
            return _refusal(404, "no_route", "no such file")
        body = (APP_DIR / name).read_bytes()
        headers = {"Content-Type": APP_FILES[name], "Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff",
                   "Referrer-Policy": "no-referrer", "Cross-Origin-Opener-Policy": "same-origin",
                   "Content-Security-Policy": {"index.html": APP_CSP, "sw.js": SW_CSP}.get(name, "default-src 'none'")}
        return 200, headers, body

    def _passkeys(self, core: DecisionCore) -> Passkeys:
        pk = getattr(self._local, "passkeys", None)
        if pk is None:
            pk = self._local.passkeys = Passkeys(core.s, self.origin)
        return pk

    def _exact_origin(self, headers: dict) -> None:
        if not self.origin or headers.get("origin") != self.origin:
            raise CoreError(403, "origin", "this is only for the app, from its exact origin")

    def _app_session(self, s, extra: dict) -> None:
        extra["Set-Cookie"] = f"{COOKIE}={s.token}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age={7 * 86400}"

    @staticmethod
    def _json(body: bytes) -> dict:
        if not body:
            return {}
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise CoreError(400, "bad_json", "the body is not JSON") from None
        if not isinstance(data, dict):
            raise CoreError(400, "bad_json", "the body is a JSON object")
        return data

    def _route(self, method, route, query, headers, data, extra):
        core = self._core()
        if method == "POST" and route == ["pair"]:
            return self._pair(core, headers, data, extra)
        if method not in ("GET", "POST"):
            raise CoreError(405, "method", "GET or POST only")
        if method == "POST" and len(route) == 2 and route[0] in ("feeds", "connector", "push-worker"):
            if "cookie" in headers or headers.get("origin") is not None:
                raise CoreError(403, "not_a_feed", "a feed, the connector or the push worker comes from the VM, "
                                                   "not a browser")
            token = _bearer(headers.get("authorization", ""))
            if route[0] == "feeds":
                return 200, Feeds(core.s).put(self.ws, route[1], token, data)
            if route[0] == "push-worker":
                push = Push(core.s)
                if route[1] == "hello":
                    return 200, push.hello(self.ws, token, str(data.get("vapid_public", "")),
                                           str(data.get("seal_public", "")))
                if route[1] == "claim":
                    return 200, push.claim(self.ws, token, data.get("limit", 20))
                if route[1] == "result":
                    return 200, push.result(self.ws, token, str(data.get("id", "")), data.get("results"))
                raise CoreError(404, "no_route", "no such route")
            if route[1] == "claim":
                return 200, Chat(core.s).claim(self.ws, token, data.get("limit", 7))
            if route[1] == "result":
                return 200, Chat(core.s).result(self.ws, token, str(data.get("id", "")), str(data.get("outcome", "")),
                                                str(data.get("ref", "")), str(data.get("reason", "")))
            raise CoreError(404, "no_route", "no such route")
        if method == "POST" and route == ["passkeys", "sign-in", "options"]:
            self._exact_origin(headers)
            return 200, self._passkeys(core).sign_in_options(self.ws)
        if method == "POST" and route == ["passkeys", "sign-in"]:
            self._exact_origin(headers)
            dev, sess = self._passkeys(core).sign_in(self.ws, data.get("credential"))
            self._app_session(sess, extra)
            return 200, {"device_id": dev, "csrf": sess.csrf, "expires_at": sess.expires_at}
        if method == "POST" and route == ["recovery-sign-in"]:
            self._exact_origin(headers)
            label = str(data.get("label", ""))[:80] or "phone (recovery code)"
            sess = core.auth.sign_in_with_recovery_code(self.ws, str(data.get("code", "")), None, new_phone=label)
            self._app_session(sess, extra)
            return 200, {"csrf": sess.csrf, "expires_at": sess.expires_at}
        # A new CSRF token is the one write that cannot carry the old one (the app lost
        # it on a reload); it still needs the cookie and the exact Origin.
        rotate = method == "POST" and route == ["csrf"]
        p = self._principal(core, headers, write=(method == "POST"), csrf=not rotate)
        app = "cookie" in headers or p.device_kind == "phone"
        if app and (method, route[0] if route else "") in APP_DATA and not core.s.flag(self.ws, "mobile_read"):
            raise CoreError(423, "mobile_read_off", "the phone app is off for now")
        if method == "GET" and route == ["me"]:
            return 200, {"kind": p.kind, "device_id": p.device_id, "device_kind": p.device_kind,
                         "fresh": p.fresh, "workspace": self.workspace}
        if method == "GET" and route == ["cards"]:
            return 200, {"cards": core.needs_you(p), "answered": core.answered(p), "seq": self._seq(core)}
        if method == "GET" and len(route) == 2 and route[0] == "cards":
            return 200, core.get_card(p, route[1])
        if method == "GET" and len(route) == 3 and route[0] == "cards" and route[2] == "artifact":
            rev = query.get("revision")
            return 200, core.artifact(p, route[1], int(rev) if rev and rev.isdigit() else None)
        if method == "GET" and route == ["changes"]:
            since = query.get("since", "0")
            return 200, self._changes(core, int(since) if since.isdigit() else 0)
        if method == "POST" and len(route) == 3 and route[0] == "cards" and route[2] == "answer":
            # The same command from Desktop and the phone (07 step 4): DecisionCore.answer checks it all.
            return 200, core.answer(
                p, decision_id=route[1], option_key=str(data.get("option_key", "")),
                revision=_int(data, "revision"), attention_revision=_int(data, "attention_revision"),
                artifact_sha256=str(data.get("artifact_sha256", "")),
                idempotency_key=str(data.get("idempotency_key", "")), note=str(data.get("note", ""))[:2000])
        if method == "GET" and route in (["work"], ["room"]):
            return 200, Feeds(core.s).get(self.ws, route[0])
        if method == "GET" and route == ["push"]:
            return 200, Push(core.s).state(p)
        if method == "POST" and route == ["push", "subscribe"]:
            self._exact_origin(headers)
            return 200, Push(core.s).subscribe(p, data.get("subscription"))
        if method == "POST" and route == ["push", "unsubscribe"]:
            return 200, Push(core.s).unsubscribe(p)
        if method == "POST" and route == ["push", "denied"]:
            return 200, Push(core.s).denied(p)
        if method == "GET" and route == ["chat"]:
            return 200, Chat(core.s).summary(p)
        if len(route) == 2 and route[0] == "chat":
            if method == "GET":
                return 200, Chat(core.s).view(p, route[1])
            return 200, Chat(core.s).send(p, route[1], data.get("text"), data.get("idempotency_key"))
        if method == "POST" and route == ["csrf"]:
            if "cookie" not in headers:
                raise CoreError(400, "not_the_app", "only the app's cookie session asks for a new CSRF token")
            return 200, {"csrf": core.auth.rotate_csrf(p)}
        if method == "GET" and route == ["passkeys"]:
            return 200, {"passkeys": self._passkeys(core).list(p)}
        if method == "POST" and route == ["passkeys", "register", "options"]:
            self._exact_origin(headers)
            return 200, self._passkeys(core).register_options(p)
        if method == "POST" and route == ["passkeys", "register"]:
            self._exact_origin(headers)
            return 200, self._passkeys(core).register(p, data.get("credential"), str(data.get("label", ""))[:80])
        if method == "POST" and len(route) == 3 and route[0] == "passkeys" and route[2] == "revoke":
            self._passkeys(core).revoke(p, route[1])
            return 200, {"revoked": route[1]}
        if method == "POST" and route == ["sign-out"]:
            core.auth.revoke_session(p)
            if "cookie" in headers:
                extra["Set-Cookie"] = f"{COOKIE}=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Lax"
            return 200, {"signed_out": True}
        if method == "POST" and route == ["pairing-codes"]:
            kind = str(data.get("device_kind", ""))
            if kind not in ("phone", "desktop"):
                raise CoreError(400, "device_kind", "a device is a phone or a desktop")
            return 200, core.auth.new_pairing_code(p, kind, str(data.get("label", ""))[:80] or kind)
        if method == "POST" and route == ["recovery-codes"]:
            return 200, {"codes": core.auth.new_recovery_codes(p)}
        raise CoreError(404, "no_route", "no such route")

    # ── who is asking ─────────────────────────────────────────────────────

    def _principal(self, core: DecisionCore, headers: dict, *, write: bool, csrf: bool = True) -> Principal:
        bearer = _bearer(headers.get("authorization", ""))
        cookie = _cookie(headers.get("cookie", ""))
        if bearer and cookie:
            raise CoreError(400, "two_sessions", "send a session one way, not two")
        origin = headers.get("origin")
        if cookie is not None:
            if write and (not self.origin or origin != self.origin):
                raise CoreError(403, "origin", "a write from the app needs its exact origin")
        elif origin is not None and origin != self.origin:
            raise CoreError(403, "origin", "that origin may not call the core")
        p = core.auth.authenticate(bearer or cookie, hermes_principal=headers.get("x-tw-hermes-principal"))
        if p.workspace_id != self.ws:
            raise CoreError(401, "signed_out", "that session is for another workspace")
        if write and csrf:
            core.auth.check_csrf(p, headers.get("x-csrf-token"))
        return p

    def _pair(self, core: DecisionCore, headers: dict, data: dict, extra: dict):
        origin = headers.get("origin")
        if origin is not None and (not self.origin or origin != self.origin):
            raise CoreError(403, "origin", "that origin may not pair a device")
        dev, s = core.auth.redeem_pairing_code(self.ws, str(data.get("code", "")),
                                               hermes_principal=headers.get("x-tw-hermes-principal"))
        if origin is not None:  # the phone: the session goes only in the cookie
            self._app_session(s, extra)
            return 200, {"device_id": dev, "csrf": s.csrf, "expires_at": s.expires_at}
        return 200, {"device_id": dev, "token": s.token, "csrf": s.csrf, "expires_at": s.expires_at}

    # ── polling ───────────────────────────────────────────────────────────

    def _seq(self, core: DecisionCore) -> int:
        row = core.s.db.execute("SELECT max(seq) FROM events").fetchone()
        return int(row[0] or 0)

    def _changes(self, core: DecisionCore, since: int) -> dict:
        rows = core.s.db.execute("SELECT DISTINCT subject FROM events WHERE seq > ? AND workspace_id = ? "
                                 "AND (kind LIKE 'decision.%' OR kind = 'work.done')", (since, self.ws)).fetchall()
        return {"seq": self._seq(core), "changed": [r[0] for r in rows]}


def body_limit(path: str) -> int:
    return MAX_FEED_BYTES if path.startswith(PREFIX + "/feeds/") else MAX_REQUEST_BYTES


def _refusal(status: int, reason: str, message: str, detail: dict | None = None) -> tuple[int, dict, bytes]:
    out = {"error": reason, "message": message, "detail": detail or {}}
    return status, dict(SECURITY_HEADERS), json.dumps(out).encode("utf-8")


def _int(data: dict, key: str) -> int:
    v = data.get(key)
    if not isinstance(v, int) or isinstance(v, bool):
        raise CoreError(400, "bad_field", f"{key} is a whole number")
    return v


def _bearer(value: str) -> str | None:
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


def _cookie(value: str) -> str | None:
    for part in value.split(";"):
        name, _, v = part.strip().partition("=")
        if name == COOKIE and v:
            return v
    return None


class _Handler(BaseHTTPRequestHandler):
    api: Api
    server_version = "tw-decide"
    sys_version = ""

    def _do(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= body_limit(urlsplit(self.path).path):
            # The body is not read, so the connection cannot be used again.
            self.close_connection = True
            status, headers, out = _refusal(413, "too_big", "the request body is too big")
        else:
            body = self.rfile.read(length) if length else b""
            status, headers, out = self.api.handle(self.command, self.path, dict(self.headers.items()), body)
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = _do
    do_POST = _do
    do_PUT = _do
    do_DELETE = _do
    do_PATCH = _do

    def log_message(self, fmt, *args) -> None:
        # Logs nothing: a request line can carry ids, and a header a session.
        pass


def make_server(api: Api, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "::1"):
        raise CoreError(500, "not_loopback", f"the core listens on loopback only, not {host}")
    handler = type("Handler", (_Handler,), {"api": api})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
