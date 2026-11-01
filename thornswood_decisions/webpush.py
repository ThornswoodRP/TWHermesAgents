"""Standard Web Push, with our own keys and nothing paid (07 section 8, step 5).

Two pieces, each from its RFC, on the `cryptography` library the core's venv
already pins by hash:

- `encrypt`: the message encryption of RFC 8291 (aes128gcm, RFC 8188), so only
  the phone's browser can read what the push service carries;
- `vapid_header`: the application server identification of RFC 8292, an ES256
  JWT signed with the VAPID private key, which lives only on the VM in the push
  worker's own folder.

`seal` and `unseal` keep a phone's subscription (its endpoint, which is a
capability URL, and its keys) encrypted at rest in the core's store: the core
seals with the push worker's public X25519 key and only the worker can open
it, so the store and its backups never hold a usable subscription.

The endpoint must be one of the browsers' push services (`push_host_ok`), so a
subscription can never point the worker at anything else.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import time
from urllib.parse import urlsplit

RECORD_SIZE = 4096
PUSH_HOSTS = ("fcm.googleapis.com", "android.googleapis.com")
PUSH_SUFFIXES = (".push.services.mozilla.com", ".notify.windows.com", ".push.apple.com")
SEAL_INFO = b"thornswood push subscription seal v1"


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64u(text: str) -> bytes:
    text = "".join(str(text).split())
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def push_host_ok(endpoint: str) -> bool:
    try:
        u = urlsplit(endpoint)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    if u.scheme != "https" or u.username or u.password or u.port not in (None, 443) or not host:
        return False
    return host in PUSH_HOSTS or any(host.endswith(s) for s in PUSH_SUFFIXES)


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    from cryptography.hazmat.primitives import hashes, hmac

    h = hmac.HMAC(salt, hashes.SHA256())
    h.update(ikm)
    prk = h.finalize()
    h = hmac.HMAC(prk, hashes.SHA256())
    h.update(info + b"\x01")
    return h.finalize()[:length]


def _point(public_key) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def encrypt(plaintext: bytes, ua_public: bytes, auth_secret: bytes, *, salt: bytes | None = None,
            as_private=None) -> bytes:
    """One aes128gcm record for the browser that holds `ua_public` (65 bytes) and `auth_secret` (16)."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if len(ua_public) != 65 or ua_public[0] != 4 or len(auth_secret) != 16:
        raise ValueError("a push subscription has a 65-byte p256dh key and a 16-byte auth secret")
    if len(plaintext) > RECORD_SIZE - 86 - 17:
        raise ValueError("a push message fits in one record")
    salt = salt if salt is not None else os.urandom(16)
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    as_public = _point(as_private.public_key())
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    ecdh = as_private.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(auth_secret, ecdh, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    header = salt + struct.pack(">IB", RECORD_SIZE, len(as_public)) + as_public
    return header + AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)


def decrypt(body: bytes, ua_private, auth_secret: bytes) -> bytes:
    """The browser's side, for the tests: one aes128gcm record back to its plaintext."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt, (rs, idlen) = body[:16], struct.unpack(">IB", body[16:21])
    as_public = body[21:21 + idlen]
    ua_public = _point(ua_private.public_key())
    ecdh = ua_private.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
    ikm = _hkdf(auth_secret, ecdh, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    padded = AESGCM(cek).decrypt(nonce, body[21 + idlen:], None)
    return padded.rstrip(b"\x00")[:-1]


# ── VAPID (RFC 8292) ─────────────────────────────────────────────────────────

def new_vapid_key():
    from cryptography.hazmat.primitives.asymmetric import ec

    return ec.generate_private_key(ec.SECP256R1())


def vapid_public(private_key) -> str:
    """The applicationServerKey the phone subscribes with: the raw public point, base64url."""
    return b64u(_point(private_key.public_key()))


def vapid_header(endpoint: str, private_key, subject: str, *, now: float | None = None,
                 lifetime: int = 12 * 3600) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    u = urlsplit(endpoint)
    claims = {"aud": f"{u.scheme}://{u.hostname}", "exp": int((now or time.time()) + lifetime), "sub": subject}
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body = b64u(json.dumps(claims, separators=(",", ":")).encode())
    r, s = decode_dss_signature(private_key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256())))
    sig = b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={head}.{body}.{sig}, k={vapid_public(private_key)}"


# ── sealing a subscription at rest ──────────────────────────────────────────

def seal(subscription: dict, seal_public_raw: bytes, aad: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    eph = X25519PrivateKey.generate()
    shared = eph.exchange(X25519PublicKey.from_public_bytes(seal_public_raw))
    eph_raw = eph.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key = _hkdf(eph_raw + seal_public_raw, shared, SEAL_INFO, 32)
    nonce = os.urandom(12)
    return eph_raw + nonce + AESGCM(key).encrypt(nonce, json.dumps(subscription).encode("utf-8"), aad)


def unseal(sealed: bytes, seal_private, aad: bytes) -> dict:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    eph_raw, nonce, box = sealed[:32], sealed[32:44], sealed[44:]
    mine = seal_private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    shared = seal_private.exchange(X25519PublicKey.from_public_bytes(eph_raw))
    key = _hkdf(eph_raw + mine, shared, SEAL_INFO, 32)
    return json.loads(AESGCM(key).decrypt(nonce, box, aad).decode("utf-8"))
