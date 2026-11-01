"""The push worker: sends "Hermes needs you" to Patrick's phone (07 section 8, step 5).

Runs on the VM as its own user, `tw-push` (tw-decide-push.service), with the
core's venv:

    python3 -I -m thornswood_decisions.push_worker --store /srv/tw/push --subject https://<the app's origin>

Its folder (0700, its own) holds what never leaves the VM: the VAPID private
key (`vapid.pem`), the key subscriptions are sealed to (`seal.key`), both made
here the first time and 0600, and its token for the core (`core.token`). It
says its two public keys to the core once, then every few seconds, while
`push_send` is on, leases the pushes due, opens each phone's subscription,
encrypts the payload for that browser alone (RFC 8291), signs a VAPID header
(RFC 8292) and posts it to that browser's push service, which must be one of
the known ones. It tells the core each push service's answer.

It is the only part of the decision system that reaches the internet, and only
the push services. It listens on no port, and it logs only push ids and status
codes: never an endpoint (a capability URL), a key or a payload.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import stat
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import webpush

CORE = "http://127.0.0.1:9135/api/v1"
POLL_SECONDS = 5
SEND_TIMEOUT = 15


def _private_file(path: Path) -> bytes:
    st = path.stat()
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise SystemExit(f"STOP: {path} is readable by others; it must be 0600")
    return path.read_bytes()


def _write_new(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def load_keys(store: Path):
    """The VAPID key and the seal key, made here the first time and never shown."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    if os.name != "nt" and stat.S_IMODE(store.stat().st_mode) & 0o077:
        raise SystemExit(f"STOP: {store} is open to others; it must be 0700")
    vapid_path, seal_path = store / "vapid.pem", store / "seal.key"
    if not vapid_path.exists():
        key = webpush.new_vapid_key()
        _write_new(vapid_path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                 serialization.NoEncryption()))
    if not seal_path.exists():
        _write_new(seal_path, X25519PrivateKey.generate().private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()))
    vapid = serialization.load_pem_private_key(_private_file(vapid_path), password=None)
    seal = X25519PrivateKey.from_private_bytes(_private_file(seal_path))
    return vapid, seal


def seal_public(seal) -> str:
    from cryptography.hazmat.primitives import serialization

    return webpush.b64u(seal.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


class Core:
    def __init__(self, base: str, token: str):
        self.base, self.token = base, token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.token}"})
        try:
            with self.opener.open(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                out = json.loads(exc.read() or b"{}")
            except json.JSONDecodeError:
                out = {}
            exc.close()
            return exc.code, out
        except (urllib.error.URLError, OSError):
            return 0, {}


def send_one(endpoint: str, body: bytes, headers: dict, opener=None) -> tuple[int, int | None]:
    """POST one push. Returns the push service's status (0: no answer) and its Retry-After in seconds."""
    if not webpush.push_host_ok(endpoint):
        return 400, None
    opener = opener or urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(endpoint, data=body, method="POST", headers=headers)
    try:
        with opener.open(req, timeout=SEND_TIMEOUT) as resp:
            return resp.status, None
    except urllib.error.HTTPError as exc:
        ra = exc.headers.get("Retry-After") if exc.headers else None
        exc.close()
        return exc.code, int(ra) if ra and ra.strip().isdigit() else None
    except (urllib.error.URLError, OSError):
        return 0, None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A push service never needs a redirect; following one could carry the push elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def say(line: str) -> None:
    """One line to the journal now: under systemd stdout is a pipe, and `-I` ignores PYTHONUNBUFFERED."""
    print(line, flush=True)


class Worker:
    def __init__(self, core: Core, vapid, seal, subject: str, send=send_one, say=say):
        self.core, self.vapid, self.seal, self.subject, self.send, self.say = core, vapid, seal, subject, send, say
        self.unsaid: list[dict] = []

    def hello(self) -> None:
        status, body = self.core.post("/push-worker/hello", {"vapid_public": webpush.vapid_public(self.vapid),
                                                             "seal_public": seal_public(self.seal)})
        if status == 409:
            raise SystemExit("STOP: the core holds other push keys. If this worker's keys were lost, the operator "
                             "runs push-reset and each phone turns alerts on again")
        if status != 200:
            raise SystemExit(f"STOP: the core did not take the push keys ({status} {body.get('error', '')})")

    def deliver(self, item: dict) -> dict:
        data = json.dumps(item["payload"], separators=(",", ":")).encode("utf-8")
        results = []
        for sub in item["subscriptions"]:
            try:
                opened = webpush.unseal(base64.b64decode(sub["sealed"]), self.seal, sub["id"].encode())
                endpoint = opened["endpoint"]
                body = webpush.encrypt(data, webpush.unb64u(opened["p256dh"]), webpush.unb64u(opened["auth"]))
            except Exception:  # a subscription this worker cannot open is useless: retire it
                results.append({"subscription_id": sub["id"], "status": 410})
                continue
            headers = {"TTL": str(int(item.get("ttl", 86400))), "Content-Encoding": "aes128gcm",
                       "Content-Type": "application/octet-stream", "Urgency": "normal", "Topic": item["topic"],
                       "Authorization": webpush.vapid_header(endpoint, self.vapid, self.subject)}
            code, retry_after = self.send(endpoint, body, headers)
            r = {"subscription_id": sub["id"], "status": code}
            if retry_after is not None:
                r["retry_after"] = retry_after
            results.append(r)
        return {"id": item["id"], "results": results}

    def _report(self) -> None:
        keep = []
        for r in self.unsaid:
            status, body = self.core.post("/push-worker/result", r)
            if status == 200:
                self.say(f"{r['id']}: {body.get('status')} ({','.join(str(x['status']) for x in r['results'])})")
            else:
                keep.append(r)
        self.unsaid = keep

    def once(self) -> int:
        self._report()
        status, body = self.core.post("/push-worker/claim", {"limit": 20})
        if status == 423:
            return -1
        if status == 401:
            raise SystemExit("STOP: the core refused the push worker's token")
        if status != 200:
            return 0
        for item in body.get("items", []):
            self.unsaid.append(self.deliver(item))
        self._report()
        return len(body.get("items", []))

    def forever(self) -> None:
        while True:
            n = self.once()
            time.sleep(POLL_SECONDS * (6 if n < 0 else 1))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="thornswood_decisions.push_worker")
    ap.add_argument("--store", default="/srv/tw/push")
    ap.add_argument("--subject", default="", help="the app's https origin, named in each VAPID header")
    ap.add_argument("--core", default=CORE)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)
    if not args.subject.startswith("https://"):
        raise SystemExit("STOP: the push worker needs the app's https origin (--subject); set it with install.sh --origin")
    store = Path(args.store)
    vapid, seal = load_keys(store)
    token = _private_file(store / "core.token").decode("utf-8").strip()
    w = Worker(Core(args.core, token), vapid, seal, args.subject)
    w.hello()
    say("PASS the core has this worker's push keys")
    if args.once:
        n = w.once()
        say("WAIT push_send is off" if n < 0 else f"PASS {n} pushes")
        return 1 if w.unsaid else 0
    w.forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
