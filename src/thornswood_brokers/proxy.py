"""tw-proxy: the admission proxy for routes 2 and 4 (06 sections 4 and 6.3).

Hermes cannot say "same model, second credential", "low-risk work only" or
"one Qwen request at a time across six profiles". So route 2 (Max's included
API credits) and route 4 (Qwen on One) are entries in a Bot's chain that point
at this loopback proxy, never at Anthropic or One. A Bot's .env holds only its
own proxy token (TW_PROXY_TOKEN). The real keys are in /etc/thornswood/
proxy.env, readable by tw-proxy alone.

For each request the proxy:
  1. names the Bot from its token (the file holds SHA-256 hashes only),
  2. asks tw-policy whether that Bot's admitted turn may use the route now
     (route_admit: Bramble only and low-risk only for credits, low-risk only
     for Qwen, the route open, a ticket held),
  3. checks the body (the one model, no fast mode or priority tier, a bounded
     Qwen packet with no tools),
  4. holds the single Qwen lease for a Qwen request until One's answer ends,
  5. forwards with the real key, streaming the answer back as it arrives,
  6. reports what happened: usage priced into the credit estimate, a limit
     error closing only its own route's window, "credit balance is too low"
     closing route 2 until the next credit date.

A wait (route closed, lease held, One down) is answered 429 with Retry-After,
which Hermes reads as a limit and moves down the chain. A refusal (wrong Bot,
high-risk work, no ticket, a body it may not send) is 403. Nothing else is
reachable: any other path is 404.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import logging
import os
import socket
import ssl
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterator

from thornswood_guard.policy import Policy

from .wire import Refused

logger = logging.getLogger(__name__)

ROUTES = {
    # path prefix: (route, the one upstream path it may reach)
    "/credits": ("credits", "/v1/messages"),
    "/qwen": ("qwen", "/v1/chat/completions"),
}
CREDIT_WORDS = ("credit balance is too low", "credit balance too low", "insufficient credit")
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "content-length", "content-encoding"}


class UpstreamDown(Exception):
    """The upstream could not be reached, or dropped before answering."""


@dataclass
class Answer:
    status: int
    headers: dict
    body: Iterator[bytes]
    on_close: list[Callable[[], None]] = field(default_factory=list)

    def close(self) -> None:
        for fn in self.on_close:
            try:
                fn()
            except Exception:
                logger.exception("closing an answer failed")


def http_upstream(method: str, url: str, headers: dict, body: bytes, timeout: float):
    """(status, headers, chunks) from the upstream, streaming. Raises UpstreamDown."""
    u = urllib.parse.urlsplit(url)
    try:
        if u.scheme == "https":
            conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout,
                                               context=ssl.create_default_context())
        elif u.scheme == "http":
            conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
        else:
            raise UpstreamDown(f"{u.scheme} is not an upstream scheme")
        path = u.path + (f"?{u.query}" if u.query else "")
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
    except (OSError, http.client.HTTPException) as e:
        raise UpstreamDown(f"{type(e).__name__}: {e}") from e

    def chunks():
        try:
            while True:
                piece = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                if not piece:
                    return
                yield piece
        except (OSError, http.client.HTTPException) as e:
            raise UpstreamDown(f"{type(e).__name__}: {e}") from e
        finally:
            conn.close()

    return resp.status, {k.lower(): v for k, v in resp.getheaders()}, chunks()


def load_env(path: Path | str) -> dict[str, str]:
    out = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[name] = value
    return out


def load_tokens(path: Path | str, policy: Policy) -> dict[str, str]:
    """{bot: sha256 hex of its proxy token}; only the six Bots."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    out = {}
    for bot, digest in raw.items():
        if not policy.is_bot(bot):
            raise ValueError(f"{bot} in the token file is not one of the six Bots")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError(f"{bot}'s entry is not a SHA-256 hex digest")
        out[bot] = digest.lower()
    return out


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def price(usage: dict, prices: dict) -> float:
    """The estimate for one answer, in dollars."""
    m = 1_000_000
    cache = usage.get("cache_creation") or {}
    w5 = cache.get("ephemeral_5m_input_tokens")
    w1 = cache.get("ephemeral_1h_input_tokens")
    if w5 is None and w1 is None:
        w5, w1 = 0, usage.get("cache_creation_input_tokens", 0)
    return (usage.get("input_tokens", 0) * prices["input"] / m
            + usage.get("output_tokens", 0) * prices["output"] / m
            + usage.get("cache_read_input_tokens", 0) * prices["cache_read"] / m
            + (w5 or 0) * prices["cache_write_5m"] / m
            + (w1 or 0) * prices["cache_write_1h"] / m)


class UsageReader:
    """Reads Anthropic's usage from the answer while it streams past."""

    def __init__(self, streaming: bool):
        self.streaming = streaming
        self.usage: dict = {}
        self._buf = b""
        self._whole = bytearray()

    def feed(self, piece: bytes) -> None:
        if not self.streaming:
            if len(self._whole) < 16 * 1024 * 1024:
                self._whole.extend(piece)
            return
        self._buf += piece
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            line = line.strip()
            if line.startswith(b"data:"):
                self._event(line[5:].strip())

    def _event(self, data: bytes) -> None:
        try:
            ev = json.loads(data)
        except ValueError:
            return
        if not isinstance(ev, dict):
            return
        u = (ev.get("message") or {}).get("usage") if ev.get("type") == "message_start" else ev.get("usage")
        if isinstance(u, dict):
            self._merge(u)

    def _merge(self, u: dict) -> None:
        for k, v in u.items():
            if isinstance(v, (int, float)):
                self.usage[k] = max(self.usage.get(k, 0), v)
            elif k == "cache_creation" and isinstance(v, dict):
                cur = self.usage.setdefault("cache_creation", {})
                for kk, vv in v.items():
                    if isinstance(vv, (int, float)):
                        cur[kk] = max(cur.get(kk, 0), vv)

    def finish(self) -> dict:
        if not self.streaming and self._whole:
            try:
                body = json.loads(bytes(self._whole))
                if isinstance(body, dict) and isinstance(body.get("usage"), dict):
                    self._merge(body["usage"])
            except ValueError:
                pass
        return self.usage


class Proxy:
    def __init__(self, policy: Policy, ask: Callable[[str, dict], dict], env: dict[str, str],
                 tokens: dict[str, str], *, upstream=http_upstream, clock: Callable[[], float] = time.time):
        self.policy = policy
        self.cfg = policy.data["admission_proxy"]
        self.ask = ask
        self.env = env
        self.tokens = tokens
        self.upstream = upstream
        self.clock = clock
        self.route_cfg = {r["id"]: r for r in policy.data["routes"]}

    # Answers

    @staticmethod
    def _json(status: int, message: str, extra_headers: dict | None = None, kind: str = "error") -> Answer:
        body = json.dumps({"type": "error", "error": {"type": kind, "message": message}}).encode()
        headers = {"content-type": "application/json", **(extra_headers or {})}
        return Answer(status, headers, iter([body]))

    def _wait(self, message: str, until: str | None = None, seconds: int | None = None) -> Answer:
        if seconds is None:
            seconds = 60
            if until:
                try:
                    t = datetime.fromisoformat(until)
                    seconds = max(1, int(t.timestamp() - self.clock()))
                except ValueError:
                    pass
        return self._json(429, f"rate limit: {message}", {"retry-after": str(seconds)}, kind="rate_limit_error")

    def _refuse(self, message: str) -> Answer:
        return self._json(403, f"refused by the Thornswood admission proxy: {message}", kind="permission_error")

    # The request

    def _bot(self, headers: dict) -> str | None:
        token = headers.get("x-api-key") or ""
        auth = headers.get("authorization") or ""
        if not token and auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        if not token:
            return None
        digest = token_digest(token)
        found = None
        for bot, want in self.tokens.items():
            if hmac.compare_digest(digest, want):
                found = bot
        return found

    def handle(self, method: str, path: str, headers: dict, body: bytes) -> Answer:
        headers = {k.lower(): v for k, v in headers.items()}
        prefix = next((p for p in ROUTES if path == p + ROUTES[p][1] or path.startswith(p + ROUTES[p][1] + "?")), None)
        if prefix is None or method != "POST":
            return self._json(404, f"the admission proxy carries only POST {', '.join(p + u for p, (_, u) in ROUTES.items())}",
                              kind="not_found_error")
        route, upstream_path = ROUTES[prefix]
        bot = self._bot(headers)
        if bot is None:
            return self._json(401, "no Bot's proxy token", kind="authentication_error")
        limit = self.cfg["max_request_bytes"][route]
        if len(body) > limit:
            return self._refuse(f"the request is {len(body)} bytes; route {route} takes at most {limit}")
        try:
            req = json.loads(body)
            if not isinstance(req, dict):
                raise ValueError("not an object")
        except ValueError as e:
            return self._json(400, f"the body is not a JSON object: {e}", kind="invalid_request_error")
        problem = self._body_problem(route, req, headers)
        if problem:
            return self._refuse(problem)
        try:
            admit = self.ask("route_admit", {"bot": bot, "route": route})
        except Refused as e:
            return self._refuse(str(e))
        if not admit.get("admitted"):
            return self._wait(admit.get("reason") or f"route {route} is closed", admit.get("until"))
        lease = None
        # Each request is its own holder: a second request in the same turn
        # waits like any other.
        holder = f"{admit['ticket']}/{uuid.uuid4().hex[:12]}"
        if route == "qwen":
            try:
                lease = self.ask("lease_acquire", {"name": "qwen", "holder": holder,
                                                   "ttl_seconds": self.cfg["qwen_lease_seconds"]})
            except Refused as e:
                return self._wait(f"one Qwen request at a time across the fleet ({e})", seconds=30)
        try:
            answer = self._forward(bot, route, upstream_path, req, headers, body)
        except BaseException:
            if lease:
                self._release(holder, lease)
            raise
        if lease:
            answer.on_close.append(lambda: self._release(holder, lease))
        return answer

    def _release(self, holder: str, lease: dict) -> None:
        self.ask("lease_release", {"name": "qwen", "holder": holder, "generation": lease["generation"]})

    def _body_problem(self, route: str, req: dict, headers: dict) -> str | None:
        if route == "credits":
            want = self.route_cfg["credits"]["model"]
            if req.get("model") != want:
                return f"route 2 carries {want} only, not {req.get('model')!r}"
            if "speed" in req:
                return "fast mode bills at twice the price; route 2 never sends it"
            if req.get("service_tier") not in (None, "standard_only"):
                return f"service_tier {req.get('service_tier')!r} is not sent on route 2"
            betas = (headers.get("anthropic-beta") or "").lower()
            if "fast-mode" in betas:
                return "fast mode bills at twice the price; route 2 never sends it"
            return None
        if req.get("tools") or req.get("functions") or req.get("tool_choice") not in (None, "none"):
            return "Qwen gets a sealed task packet, never tools"
        cap = self.cfg.get("qwen_max_tokens", 8192)
        asked = req.get("max_tokens") or req.get("max_completion_tokens")
        if asked is not None and (not isinstance(asked, int) or asked > cap):
            return f"a Qwen answer is at most {cap} tokens"
        return None

    def _forward(self, bot: str, route: str, upstream_path: str, req: dict, headers: dict, body: bytes) -> Answer:
        up = self.cfg["upstreams"][route]
        base = up.get("url") or self.env.get(up.get("url_env", ""), "")
        key = self.env.get(up["key_env"], "")
        if not base or not key:
            return self._json(503, f"route {route} is not set up on this box (proxy.env lacks its address or key)",
                              kind="api_error")
        out_headers = {"content-type": "application/json", "accept": headers.get("accept", "application/json")}
        if route == "credits":
            out_headers["x-api-key"] = key
            out_headers["anthropic-version"] = headers.get("anthropic-version", "2023-06-01")
            if headers.get("anthropic-beta"):
                out_headers["anthropic-beta"] = headers["anthropic-beta"]
        else:
            out_headers["authorization"] = f"Bearer {key}"
        url = base.rstrip("/") + upstream_path
        try:
            status, up_headers, chunks = self.upstream("POST", url, out_headers, body,
                                                      self.cfg["upstream_timeout_seconds"])
        except UpstreamDown as e:
            logger.warning("route %s for %s: upstream down: %s", route, bot, e)
            self._report_down(route)
            return self._wait(f"route {route}'s upstream did not answer ({e})", seconds=60)
        if status == 200:
            return self._stream_ok(bot, route, req, up_headers, chunks)
        raw = b"".join(chunks)
        self._report_error(route, status, up_headers, raw)
        return Answer(status, _pass_headers(up_headers), iter([raw]))

    def _stream_ok(self, bot, route, req, up_headers, chunks) -> Answer:
        reader = UsageReader(bool(req.get("stream")) or "text/event-stream" in up_headers.get("content-type", ""))
        done = threading.Event()

        def body():
            try:
                for piece in chunks:
                    reader.feed(piece)
                    yield piece
            except UpstreamDown as e:
                logger.warning("route %s for %s: the answer broke off: %s", route, bot, e)
            finally:
                if not done.is_set():
                    done.set()
                    self._account(route, reader.finish())

        return Answer(200, _pass_headers(up_headers), body())

    def _account(self, route: str, usage: dict) -> None:
        try:
            self.ask("route_ok", {"route": route})
            if route == "credits" and usage:
                usd = price(usage, self.route_cfg["credits"]["price_usd_per_mtok"])
                self.ask("credit_spend", {"usd": usd})
        except Refused:
            logger.exception("could not record route %s's answer", route)

    def _report_down(self, route: str) -> None:
        try:
            self.ask("route_limit", {"route": route, "window": "one" if route == "qwen" else "upstream"})
        except Refused:
            logger.exception("could not close route %s", route)

    def _report_error(self, route: str, status: int, headers: dict, raw: bytes) -> None:
        text = raw[:4096].decode("utf-8", "replace").lower()
        try:
            if route == "credits" and status in (400, 402, 403) and any(w in text for w in CREDIT_WORDS):
                self.ask("credit_refused", {})
            elif status == 429:
                window, reset = _limit_window(headers)
                self.ask("route_limit", {"route": route, "window": window, "reset_at": reset})
            elif status in (500, 502, 503, 504, 529) and route == "qwen":
                self.ask("route_limit", {"route": route, "window": "one"})
        except Refused:
            logger.exception("could not record route %s's error", route)


def _pass_headers(h: dict) -> dict:
    return {k: v for k, v in h.items() if k not in HOP}


def _limit_window(headers: dict) -> tuple[str, str | None]:
    """The window a 429 names and when it resets, from Anthropic's headers."""
    for name in ("requests", "input-tokens", "output-tokens", "tokens"):
        reset = headers.get(f"anthropic-ratelimit-{name}-reset")
        remaining = headers.get(f"anthropic-ratelimit-{name}-remaining")
        if reset and remaining == "0":
            return name, reset
    ra = headers.get("retry-after")
    if ra and ra.isdigit():
        t = datetime.fromtimestamp(time.time() + int(ra), tz=timezone.utc)
        return "default", t.isoformat()
    return "default", None


# The HTTP server

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "tw-proxy"
    sys_version = ""

    def log_message(self, fmt, *args):
        logger.info("%s %s", self.address_string(), fmt % args)

    def do_POST(self):
        self._serve("POST")

    def do_GET(self):
        self._serve("GET")

    def _serve(self, method: str):
        proxy: Proxy = self.server.proxy  # type: ignore[attr-defined]
        if self.headers.get("transfer-encoding"):
            self._send(proxy._json(411, "send a Content-Length", kind="invalid_request_error"))
            return
        n = int(self.headers.get("content-length") or 0)
        cap = max(proxy.cfg["max_request_bytes"].values())
        if n > cap:
            self._send(proxy._refuse(f"the request is {n} bytes; the most any route takes is {cap}"))
            return
        body = self.rfile.read(n) if n else b""
        try:
            answer = proxy.handle(method, self.path, dict(self.headers.items()), body)
        except Refused as e:
            answer = proxy._refuse(str(e))
        except Exception as e:
            logger.exception("the proxy failed")
            answer = proxy._json(500, f"the proxy failed: {type(e).__name__}", kind="api_error")
        self._send(answer)

    def _send(self, answer: Answer):
        try:
            self.send_response(answer.status)
            for k, v in answer.headers.items():
                self.send_header(k, v)
            self.send_header("transfer-encoding", "chunked")
            self.send_header("connection", "close")
            self.end_headers()
            for piece in answer.body:
                if piece:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
                    self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            logger.info("the caller went away mid-answer")
        finally:
            close = getattr(answer.body, "close", None)
            if close:
                close()
            answer.close()
            self.close_connection = True


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, listen: str, proxy: Proxy, fd: int | None = None):
        """fd: the socket systemd holds on `listen` (tw-proxy.socket, #162),
        so a model call made while the proxy starts again waits for it."""
        host, _, port = listen.rpartition(":")
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError(f"the admission proxy listens on loopback only, not {host}")
        self.proxy = proxy
        if host == "::1":
            self.address_family = socket.AF_INET6
        if fd is None:
            super().__init__((host, int(port)), _Handler)
            return
        super().__init__((host, int(port)), _Handler, bind_and_activate=False)
        self.socket.close()
        self.socket = socket.socket(fileno=fd)
        got = self.socket.getsockname()
        if self.socket.family not in (socket.AF_INET, socket.AF_INET6) or got[1] != int(port) \
                or got[0] not in ("127.0.0.1", "::1"):
            self.socket.detach()
            raise ValueError(f"the socket systemd passed listens on {got!r}, not {listen}; "
                             "tw-proxy.socket and the policy disagree")
        self.server_address = got[:2]
        self.server_name, self.server_port = got[0], got[1]


def check_environment(environ: dict) -> list[str]:
    """What must not be in tw-proxy's own environment (06 section 6.2)."""
    problems = []
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY"):
        if name in environ:
            problems.append(f"{name} is set in tw-proxy's environment; the credits key lives only in proxy.env under its own name")
    return problems
