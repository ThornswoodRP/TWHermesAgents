"""The thornswood-decisions plugin's backend: Hermes Desktop's way to the decision core.

Hermes mounts this router at /api/plugins/thornswood-decisions/ on Patrick's own
`hermes serve` on the VM (user patrick-hermes, never the Bots' user), and only
once the plugin is in plugins.enabled. The pane calls it with ctx.rest, which
cannot set headers, so every route is a POST and the pane's session rides in
the body as {"session": {"token": ..., "csrf": ...}}.

What it does with a call:
- forwards it to the core on 127.0.0.1 with the session as `Authorization:
  Bearer` and, on a write, `X-CSRF-Token`;
- says which Hermes sign-in made the call in `X-TW-Hermes-Principal`, taken from
  what Hermes verified (request.state), never from anything the pane sent; the
  core lets a Desktop answer only through the sign-in that paired it;
- answers 200 with {"status": <the core's status>, "body": <the core's JSON>},
  so the pane can read a 409's detail; status 0 means the core did not answer.

It holds nothing: no token, no key, no file. It logs nothing.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request

from fastapi import APIRouter, Request

CORE = "http://127.0.0.1:9135/api/v1"
TIMEOUT_SECONDS = 15
MAX_BODY_BYTES = 64 * 1024
ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
KEY = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")

router = APIRouter()
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def hermes_principal(request) -> str | None:
    """The Hermes sign-in that made this call, named the way Hermes names it, or None.

    This follows hermes_cli/web_server_sessions.py at the pinned commit: a native
    grant's subject, a verified session through authenticated_subject, a token
    principal, or the loopback serve's own session token as "legacy-token-owner".
    Anything else (a hosted plugin call, no sign-in at all) is None, and the core
    then refuses to pair or serve a Desktop.
    """
    state = getattr(request, "state", None)
    try:
        from gateway.session_identity import authenticated_subject
    except ImportError:
        return None
    native = getattr(state, "native_http_principal", None)
    if isinstance(native, dict) and native.get("subject"):
        return "native:" + str(native["subject"])
    session = getattr(state, "session", None)
    if session is not None and getattr(session, "user_id", ""):
        return authenticated_subject({"user_id": session.user_id, "provider": session.provider,
                                      "issuer": getattr(session, "issuer", "")})
    token = getattr(state, "token_principal", None)
    if token is not None and getattr(token, "principal", ""):
        return "token:" + authenticated_subject({"user_id": token.principal, "provider": token.provider})
    app_state = getattr(getattr(request, "app", None), "state", None)
    if not getattr(app_state, "auth_required", False):
        try:
            from hermes_cli.web_server import _has_valid_session_token
        except ImportError:
            return None
        if _has_valid_session_token(request):
            return authenticated_subject({"user_id": "legacy-token-owner", "provider": "session-token"})
    return None


def _envelope(status: int, body) -> dict:
    return {"status": status, "body": body}


def _refused(reason: str, message: str) -> dict:
    return _envelope(400, {"error": reason, "message": message, "detail": {}})


def call_core(method: str, path: str, *, session: dict | None, principal: str | None,
              write: bool = False, payload: dict | None = None) -> dict:
    """One call to the core; never raises."""
    headers = {"Accept": "application/json"}
    if principal:
        headers["X-TW-Hermes-Principal"] = principal
    if session is not None:
        token = session.get("token")
        if not isinstance(token, str) or not token:
            return _envelope(401, {"error": "signed_out", "message": "pair this Desktop first", "detail": {}})
        headers["Authorization"] = f"Bearer {token}"
        if write:
            csrf = session.get("csrf")
            headers["X-CSRF-Token"] = csrf if isinstance(csrf, str) else ""
    data = None
    if method == "POST":
        data = json.dumps(payload or {}).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(CORE + path, data=data, method=method, headers=headers)
    try:
        with _opener.open(req, timeout=TIMEOUT_SECONDS) as resp:
            return _envelope(resp.status, _read(resp))
    except urllib.error.HTTPError as exc:
        with exc:
            return _envelope(exc.code, _read(exc))
    except (urllib.error.URLError, OSError, ValueError):
        return _envelope(0, {"error": "core_unreachable",
                             "message": "the decision core did not answer; nothing was sent", "detail": {}})


def _read(resp) -> dict:
    raw = resp.read(MAX_BODY_BYTES + 1)
    if len(raw) > MAX_BODY_BYTES:
        return {"error": "too_big", "message": "the core's answer was too big", "detail": {}}
    try:
        out = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"error": "bad_answer", "message": "the core's answer was not JSON", "detail": {}}
    return out if isinstance(out, dict) else {"error": "bad_answer", "message": "not an object", "detail": {}}


def _session(body: dict) -> dict:
    s = body.get("session") if isinstance(body, dict) else None
    return s if isinstance(s, dict) else {}


async def _body(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return {}
    try:
        out = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return out if isinstance(out, dict) else {}


# Handlers are `async` only to read the body; the blocking call runs in a thread
# so a slow core never holds Hermes's event loop.

async def _run(fn, *args, **kw):
    return await asyncio.to_thread(fn, *args, **kw)


@router.post("/pair")
async def pair(request: Request):
    body = await _body(request)
    code = body.get("code")
    if not isinstance(code, str) or not 1 <= len(code) <= 32:
        return _refused("bad_code", "type the code the VM or the phone printed")
    return await _run(call_core, "POST", "/pair", session=None, principal=hermes_principal(request),
                      payload={"code": code})


@router.post("/me")
async def me(request: Request):
    body = await _body(request)
    return await _run(call_core, "GET", "/me", session=_session(body), principal=hermes_principal(request))


@router.post("/cards")
async def cards(request: Request):
    body = await _body(request)
    return await _run(call_core, "GET", "/cards", session=_session(body), principal=hermes_principal(request))


@router.post("/card")
async def card(request: Request):
    body = await _body(request)
    cid = body.get("id")
    if not isinstance(cid, str) or not ID.match(cid):
        return _refused("bad_id", "that is not a card id")
    return await _run(call_core, "GET", f"/cards/{cid}", session=_session(body),
                      principal=hermes_principal(request))


@router.post("/artifact")
async def artifact(request: Request):
    body = await _body(request)
    cid, rev = body.get("id"), body.get("revision")
    if not isinstance(cid, str) or not ID.match(cid):
        return _refused("bad_id", "that is not a card id")
    path = f"/cards/{cid}/artifact"
    if isinstance(rev, int) and not isinstance(rev, bool) and rev >= 1:
        path += f"?revision={rev}"
    return await _run(call_core, "GET", path, session=_session(body), principal=hermes_principal(request))


@router.post("/changes")
async def changes(request: Request):
    body = await _body(request)
    since = body.get("since")
    since = since if isinstance(since, int) and not isinstance(since, bool) and since >= 0 else 0
    return await _run(call_core, "GET", f"/changes?since={since}", session=_session(body),
                      principal=hermes_principal(request))


ANSWER_FIELDS = ("option_key", "revision", "attention_revision", "artifact_sha256", "idempotency_key", "note")


@router.post("/answer")
async def answer(request: Request):
    body = await _body(request)
    cid = body.get("id")
    if not isinstance(cid, str) or not ID.match(cid):
        return _refused("bad_id", "that is not a card id")
    key = body.get("idempotency_key")
    if not isinstance(key, str) or not KEY.match(key):
        return _refused("bad_key", "an answer carries its own idempotency key")
    payload = {k: body[k] for k in ANSWER_FIELDS if k in body}
    return await _run(call_core, "POST", f"/cards/{cid}/answer", session=_session(body),
                      principal=hermes_principal(request), write=True, payload=payload)


@router.post("/sign-out")
async def sign_out(request: Request):
    body = await _body(request)
    return await _run(call_core, "POST", "/sign-out", session=_session(body),
                      principal=hermes_principal(request), write=True)
