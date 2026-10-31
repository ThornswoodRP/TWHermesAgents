"""The decision core's HTTP API on loopback (07 sections 2 and 5).

It listens on 127.0.0.1 only. Tailscale Serve puts the phone in front of it
(step 3); Hermes Desktop reaches it through the thornswood-decisions plugin's
backend, which runs in Patrick's Hermes on the same VM.

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
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from .auth import Principal
from .core import DecisionCore
from .store import CoreError, Store, now_utc

PREFIX = "/api/v1"
MAX_REQUEST_BYTES = 64 * 1024
COOKIE = "tw_session"
DEFAULT_PORT = 9135
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
            if len(body) > MAX_REQUEST_BYTES:
                raise CoreError(413, "too_big", "the request body is too big")
            url = urlsplit(target)
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
        p = self._principal(core, headers, write=(method == "POST"))
        if method == "GET" and route == ["me"]:
            return 200, {"kind": p.kind, "device_id": p.device_id, "device_kind": p.device_kind,
                         "fresh": p.fresh, "workspace": self.workspace}
        if method == "GET" and route == ["cards"]:
            return 200, {"cards": core.needs_you(p), "seq": self._seq(core)}
        if method == "GET" and len(route) == 2 and route[0] == "cards":
            return 200, core.get_card(p, route[1])
        if method == "GET" and len(route) == 3 and route[0] == "cards" and route[2] == "artifact":
            rev = query.get("revision")
            return 200, core.artifact(p, route[1], int(rev) if rev and rev.isdigit() else None)
        if method == "GET" and route == ["changes"]:
            since = query.get("since", "0")
            return 200, self._changes(core, int(since) if since.isdigit() else 0)
        if method == "POST" and len(route) == 3 and route[0] == "cards" and route[2] == "answer":
            if p.device_kind == "phone":
                raise CoreError(403, "phone_read_only", "the phone answers from step 4; answer on Desktop for now")
            return 200, core.answer(
                p, decision_id=route[1], option_key=str(data.get("option_key", "")),
                revision=_int(data, "revision"), attention_revision=_int(data, "attention_revision"),
                artifact_sha256=str(data.get("artifact_sha256", "")),
                idempotency_key=str(data.get("idempotency_key", "")), note=str(data.get("note", ""))[:2000])
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

    def _principal(self, core: DecisionCore, headers: dict, *, write: bool) -> Principal:
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
        if p.device_kind == "phone" and not core.s.flag(self.ws, "mobile_read"):
            raise CoreError(423, "mobile_read_off", "the phone app is off for now")
        if write:
            core.auth.check_csrf(p, headers.get("x-csrf-token"))
        return p

    def _pair(self, core: DecisionCore, headers: dict, data: dict, extra: dict):
        origin = headers.get("origin")
        if origin is not None and (not self.origin or origin != self.origin):
            raise CoreError(403, "origin", "that origin may not pair a device")
        dev, s = core.auth.redeem_pairing_code(self.ws, str(data.get("code", "")),
                                               hermes_principal=headers.get("x-tw-hermes-principal"))
        if origin is not None:  # the phone: the session goes only in the cookie
            extra["Set-Cookie"] = f"{COOKIE}={s.token}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age={7 * 86400}"
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
        if not 0 <= length <= MAX_REQUEST_BYTES:
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
