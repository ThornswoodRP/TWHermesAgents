"""GitHub for tw-git: the two Apps' installation tokens, and plain REST calls.

Tokens are minted from each App's private key, which only tw-git can read
(mode 0400). A token is asked for one repository and only the permissions the
call needs, lives about an hour, and is never written down. Every call logs
X-RateLimit-Remaining, so each App's own limit can be measured (never
Patrick's user token: apply.py refuses a gh login in the Bots' user).

The JWT is signed with the openssl command, so this needs nothing outside the
standard library on the VM.
"""

from __future__ import annotations

import base64
import json
import logging
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)

API = "https://api.github.com"


class GitHubError(Exception):
    def __init__(self, status: int, message: str, body=None):
        super().__init__(f"GitHub answered {status}: {message}")
        self.status = status
        self.body = body


@dataclass
class Response:
    status: int
    body: object
    headers: dict


Transport = Callable[[str, str, dict, bytes | None], Response]


def urllib_transport(method: str, url: str, headers: dict, data: bytes | None) -> Response:
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return Response(r.status, json.loads(raw) if raw else None, dict(r.headers))
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = raw.decode("utf-8", "replace")
        return Response(e.code, body, dict(e.headers or {}))


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(app_id: int, key_file: str, *, now: float | None = None,
            signer: Callable[[bytes, str], bytes] | None = None) -> str:
    now = int(now if now is not None else time.time())
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode()
    sig = (signer or openssl_sign)(signing_input, key_file)
    return f"{header}.{payload}.{b64url(sig)}"


def openssl_sign(data: bytes, key_file: str) -> bytes:
    r = subprocess.run(["openssl", "dgst", "-sha256", "-sign", key_file], input=data, capture_output=True)
    if r.returncode != 0:
        raise GitHubError(0, f"could not sign the App's token: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout


class GitHub:
    """One App's view of GitHub. `app` is "bramble" or "workers"."""

    def __init__(self, apps: dict, *, transport: Transport = urllib_transport,
                 signer: Callable[[bytes, str], bytes] | None = None, clock: Callable[[], float] = time.time):
        self.apps = apps
        self.transport = transport
        self.signer = signer
        self.clock = clock
        self._tokens: dict[tuple, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def token(self, app: str, repo: str, permissions: dict) -> str:
        key = (app, repo, json.dumps(permissions, sort_keys=True))
        with self._lock:
            cached = self._tokens.get(key)
            if cached and cached[1] - self.clock() > 300:
                return cached[0]
            spec = self.apps[app]
            jwt = app_jwt(spec["app_id"], spec["key_file"], now=self.clock(), signer=self.signer)
            body = {"repositories": [repo.split("/", 1)[1]], "permissions": permissions}
            r = self.transport("POST", f"{API}/app/installations/{spec['installation_id']}/access_tokens",
                               {"Authorization": f"Bearer {jwt}", "Accept": "application/vnd.github+json",
                                "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"},
                               json.dumps(body).encode())
            if r.status != 201:
                raise GitHubError(r.status, _message(r.body))
            tok = r.body["token"]
            self._tokens[key] = (tok, self.clock() + 3300)
            return tok

    def call(self, app: str, repo: str, permissions: dict, method: str, path: str, body=None) -> Response:
        tok = self.token(app, repo, permissions)
        url = path if path.startswith("http") else f"{API}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"token {tok}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        r = self.transport(method, url, headers, data)
        remaining = (r.headers or {}).get("X-RateLimit-Remaining") or (r.headers or {}).get("x-ratelimit-remaining")
        logger.info("github %s %s %s as %s: %s, X-RateLimit-Remaining %s", method, path.split("?")[0], repo, app, r.status, remaining)
        return r

    def get(self, app: str, repo: str, path: str, permissions: dict | None = None):
        r = self.call(app, repo, permissions or READ, "GET", path)
        if r.status != 200:
            raise GitHubError(r.status, _message(r.body), r.body)
        return r.body

    def get_all(self, app: str, repo: str, path: str, key: str | None = None, permissions: dict | None = None, cap: int = 3000) -> list:
        out = []
        page = 1
        sep = "&" if "?" in path else "?"
        while True:
            body = self.get(app, repo, f"{path}{sep}per_page=100&page={page}", permissions)
            items = body.get(key, []) if key else body
            out.extend(items)
            if len(items) < 100 or len(out) >= cap:
                return out
            page += 1

    def graphql(self, app: str, repo: str, permissions: dict, query: str, variables: dict) -> dict:
        """One GraphQL call with a token for this repo only. An answer with
        errors is GitHub saying no, the same as a 4xx."""
        r = self.call(app, repo, permissions, "POST", "/graphql", {"query": query, "variables": variables})
        if r.status != 200:
            raise GitHubError(r.status, _message(r.body), r.body)
        body = r.body if isinstance(r.body, dict) else {}
        if body.get("errors"):
            msgs = "; ".join(str(e.get("message") or e) for e in body["errors"] if isinstance(e, dict)) or str(body["errors"])
            raise GitHubError(422, msgs, body)
        return body.get("data") or {}

    def git_auth_header(self, app: str, repo: str, permissions: dict) -> str:
        tok = self.token(app, repo, permissions)
        return "Authorization: Basic " + base64.b64encode(f"x-access-token:{tok}".encode()).decode()


READ = {"contents": "read", "pull_requests": "read", "issues": "read", "actions": "read", "checks": "read", "metadata": "read"}


def _message(body) -> str:
    if isinstance(body, dict):
        return str(body.get("message") or body)
    return str(body)


def quote(s: str) -> str:
    return urllib.parse.quote(s, safe="")
