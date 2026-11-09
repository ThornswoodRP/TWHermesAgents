"""A software passkey, for the tests and the install's self-check only.

It makes the same answers a phone's authenticator makes (a "none" attestation,
an ES256 key, user present and verified), so the core's passkey records and
the installed py_webauthn are checked end to end without a phone. It is never
used to sign in for real: the key lives in memory and is thrown away.
"""

from __future__ import annotations

import hashlib
import json
import secrets

from .passkey import b64url, unb64url


class SoftKey:
    def __init__(self, origin: str, rp_id: str, *, verified: bool = True, counter: int = 0,
                 aaguid: str = "00000000-0000-0000-0000-000000000000"):
        import cbor2
        from cryptography.hazmat.primitives.asymmetric import ec
        self._cbor = cbor2
        self._ec = ec
        self.origin = origin
        self.rp_id = rp_id
        self.verified = verified
        self.counter = counter
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(16)
        self.user_handle = b""
        # Which app it says made it, as a phone's authenticator or a passkey app says.
        self.aaguid = bytes.fromhex(aaguid.replace("-", ""))

    def _flags(self, attested: bool) -> int:
        return 0x01 | (0x04 if self.verified else 0) | (0x40 if attested else 0)

    def _cose(self) -> bytes:
        nums = self.key.public_key().public_numbers()
        return self._cbor.dumps({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})

    def _client(self, kind: str, challenge: str, origin: str | None) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin or self.origin,
                           "crossOrigin": False}).encode()

    def create(self, options: dict, *, origin: str | None = None) -> dict:
        self.user_handle = unb64url(options["user"]["id"])
        rp_hash = hashlib.sha256((options["rp"]["id"] or self.rp_id).encode()).digest()
        cred = self.aaguid + len(self.credential_id).to_bytes(2, "big") + self.credential_id + self._cose()
        auth_data = rp_hash + bytes([self._flags(True)]) + self.counter.to_bytes(4, "big") + cred
        att = self._cbor.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        client = self._client("webauthn.create", options["challenge"], origin)
        cid = b64url(self.credential_id)
        return {"id": cid, "rawId": cid, "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": b64url(client), "attestationObject": b64url(att),
                             "transports": ["internal"]}}

    def get(self, options: dict, *, origin: str | None = None, rp_id: str | None = None) -> dict:
        from cryptography.hazmat.primitives import hashes
        self.counter += 1 if self.counter else 0
        rp_hash = hashlib.sha256((rp_id or options.get("rpId") or self.rp_id).encode()).digest()
        auth_data = rp_hash + bytes([self._flags(False)]) + self.counter.to_bytes(4, "big")
        client = self._client("webauthn.get", options["challenge"], origin)
        sig = self.key.sign(auth_data + hashlib.sha256(client).digest(), self._ec.ECDSA(hashes.SHA256()))
        cid = b64url(self.credential_id)
        return {"id": cid, "rawId": cid, "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": b64url(client), "authenticatorData": b64url(auth_data),
                             "signature": b64url(sig), "userHandle": b64url(self.user_handle)}}
