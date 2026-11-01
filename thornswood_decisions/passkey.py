"""Patrick's passkeys: the phone's sign-in (07 section 5.1, step 3).

The WebAuthn checks are py_webauthn's (Duo Labs), pinned with hashes in
deploy/decisions/requirements.txt and installed in the core's own venv on the
VM. This file only keeps the records around it:

- a challenge is random, used once, lives 5 minutes and only its hash is kept;
- a passkey is added only from a fresh Patrick session on a paired device, and
  belongs to that device: revoking the device stops the passkey;
- a sign-in needs user verification (the phone's lock), checks the counter
  where the passkey keeps one, and gives a fresh session on the passkey's device;
- five failures in 15 minutes stop passkey sign-in for 15 minutes.

The relying party is the app's exact origin (`https://<name>.ts.net`) and its
host name. Without the library installed every route here answers 503
`passkey_unavailable`; nothing falls back to a weaker sign-in.
"""

from __future__ import annotations

import base64
import json
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from .auth import Auth, IssuedSession, Principal
from .store import CoreError, Store, new_id, parse, sha256_hex

CHALLENGE_LIFETIME = timedelta(minutes=5)
RP_NAME = "Thornswood"
MAX_PASSKEYS = 5


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64url(text: str) -> bytes:
    if not isinstance(text, str) or len(text) > 4096:
        raise CoreError(400, "bad_credential", "that is not a passkey answer")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError):
        raise CoreError(400, "bad_credential", "that is not a passkey answer") from None


def available() -> bool:
    try:
        import webauthn  # noqa: F401
    except ImportError:
        return False
    return True


def _lib():
    try:
        import webauthn
        from webauthn.helpers import options_to_json
        from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                              ResidentKeyRequirement, UserVerificationRequirement)
    except ImportError:
        raise CoreError(503, "passkey_unavailable",
                        "the passkey library is not installed for the core; see deploy/decisions/requirements.txt") from None
    return webauthn, options_to_json, AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor, \
        ResidentKeyRequirement, UserVerificationRequirement


class Passkeys:
    def __init__(self, store: Store, origin: str | None):
        self.s = store
        self.auth = Auth(store)
        self.origin = origin
        host = urlsplit(origin).hostname if origin else None
        self.rp_id = host

    def _ready(self) -> None:
        # https, or http on localhost, which browsers also treat as a secure origin (the browser test uses it).
        secure = bool(self.origin) and (self.origin.startswith("https://") or self.rp_id == "localhost")
        if not self.rp_id or not secure:
            raise CoreError(503, "no_origin", "the core has no https origin yet, so it cannot use passkeys")

    # ── challenges ────────────────────────────────────────────────────────

    def _challenge(self, workspace_id: str, purpose: str, session_hash: str | None) -> bytes:
        raw = secrets.token_bytes(32)
        now = self.s.clock()
        with self.s.tx() as db:
            db.execute("INSERT INTO webauthn_challenges(id, workspace_id, purpose, challenge_hash, session_hash, "
                       "created_at, expires_at) VALUES(?,?,?,?,?,?,?)",
                       (new_id("wc"), workspace_id, purpose, sha256_hex(raw), session_hash,
                        now.isoformat(), (now + CHALLENGE_LIFETIME).isoformat()))
        return raw

    def _take_challenge(self, db, workspace_id: str, purpose: str, client_data_b64: str,
                        session_hash: str | None) -> bytes:
        """The challenge the credential answers, used up here, inside the caller's transaction."""
        try:
            client = json.loads(unb64url(client_data_b64).decode("utf-8"))
            raw = unb64url(client["challenge"])
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            raise CoreError(400, "bad_credential", "that is not a passkey answer") from None
        row = db.execute("SELECT * FROM webauthn_challenges WHERE challenge_hash = ? AND workspace_id = ? "
                         "AND purpose = ?", (sha256_hex(raw), workspace_id, purpose)).fetchone()
        if not row or row["used_at"] or self.s.clock() >= parse(row["expires_at"]):
            raise CoreError(401, "bad_challenge", "that passkey request is used or expired; start again")
        if purpose == "register" and row["session_hash"] != session_hash:
            raise CoreError(401, "bad_challenge", "that passkey request belongs to another session")
        db.execute("UPDATE webauthn_challenges SET used_at = ? WHERE id = ?", (self.s.now(), row["id"]))
        return raw

    # ── adding a passkey ──────────────────────────────────────────────────

    def register_options(self, p: Principal) -> dict:
        self._ready()
        self.auth._need_patrick(p, fresh=True)
        if not p.device_id:
            raise CoreError(403, "no_device", "a passkey is added from a paired device")
        webauthn, options_to_json, Selection, Descriptor, Resident, UV = _lib()
        have = self.s.db.execute("SELECT credential_id FROM passkeys WHERE principal_id = ? AND revoked_at IS NULL",
                                 (p.id,)).fetchall()
        if len(have) >= MAX_PASSKEYS:
            raise CoreError(409, "too_many_passkeys", f"{MAX_PASSKEYS} passkeys at most; remove one first")
        challenge = self._challenge(p.workspace_id, "register", p.session_hash)
        opts = webauthn.generate_registration_options(
            rp_id=self.rp_id, rp_name=RP_NAME, user_name="Patrick", user_display_name="Patrick",
            user_id=p.id.encode("utf-8"), challenge=challenge, timeout=int(CHALLENGE_LIFETIME.total_seconds() * 1000),
            authenticator_selection=Selection(resident_key=Resident.REQUIRED, user_verification=UV.REQUIRED),
            exclude_credentials=[Descriptor(id=unb64url(r["credential_id"])) for r in have])
        return json.loads(options_to_json(opts))

    def register(self, p: Principal, credential: dict, label: str) -> dict:
        self._ready()
        self.auth._need_patrick(p, fresh=True)
        if not p.device_id:
            raise CoreError(403, "no_device", "a passkey is added from a paired device")
        webauthn = _lib()[0]
        client_data = _field(credential, "response", "clientDataJSON")
        with self.s.tx() as db:
            challenge = self._take_challenge(db, p.workspace_id, "register", client_data, p.session_hash)
        try:
            v = webauthn.verify_registration_response(
                credential=credential, expected_challenge=challenge, expected_rp_id=self.rp_id,
                expected_origin=self.origin, require_user_verification=True)
        except Exception as exc:  # the library's InvalidRegistrationResponse and parse errors
            raise CoreError(400, "passkey_refused", f"the passkey was not accepted: {type(exc).__name__}") from None
        pk = new_id("pk")
        with self.s.tx() as db:
            if db.execute("SELECT 1 FROM passkeys WHERE credential_id = ?", (b64url(v.credential_id),)).fetchone():
                raise CoreError(409, "passkey_exists", "that passkey is already added")
            db.execute("INSERT INTO passkeys(id, workspace_id, principal_id, device_id, credential_id, public_key, "
                       "sign_count, label, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (pk, p.workspace_id, p.id, p.device_id, b64url(v.credential_id), v.credential_public_key,
                        v.sign_count, (label or "passkey")[:80], self.s.now()))
            self.s.event(db, p.workspace_id, "passkey.added", pk, "patrick", {"device": p.device_id})
        return {"passkey_id": pk}

    # ── signing in ────────────────────────────────────────────────────────

    def sign_in_options(self, workspace_id: str) -> dict:
        self._ready()
        webauthn, options_to_json, _, _, _, UV = _lib()
        if self.auth._locked(workspace_id, "passkey"):
            raise CoreError(429, "too_many_tries", "too many failed passkey sign-ins; wait 15 minutes")
        challenge = self._challenge(workspace_id, "sign_in", None)
        opts = webauthn.generate_authentication_options(
            rp_id=self.rp_id, challenge=challenge, timeout=int(CHALLENGE_LIFETIME.total_seconds() * 1000),
            user_verification=UV.REQUIRED)
        return json.loads(options_to_json(opts))

    def sign_in(self, workspace_id: str, credential: dict) -> tuple[str, IssuedSession]:
        self._ready()
        webauthn = _lib()[0]
        if self.auth._locked(workspace_id, "passkey"):
            raise CoreError(429, "too_many_tries", "too many failed passkey sign-ins; wait 15 minutes")
        client_data = _field(credential, "response", "clientDataJSON")
        cred_id = (credential.get("rawId") or credential.get("id")) if isinstance(credential, dict) else None
        if not isinstance(cred_id, str):
            raise CoreError(400, "bad_credential", "that is not a passkey answer")
        with self.s.tx() as db:
            challenge = self._take_challenge(db, workspace_id, "sign_in", client_data, None)
        row = self.s.db.execute(
            "SELECT k.*, d.revoked_at AS device_revoked FROM passkeys k LEFT JOIN devices d ON d.id = k.device_id "
            "WHERE k.credential_id = ? AND k.workspace_id = ?", (b64url(unb64url(cred_id)), workspace_id)).fetchone()
        if not row or row["revoked_at"] or row["device_revoked"]:
            self.auth._failed(workspace_id, "passkey")
            raise CoreError(401, "unknown_passkey", "that passkey is not one of Patrick's here")
        try:
            v = webauthn.verify_authentication_response(
                credential=credential, expected_challenge=challenge, expected_rp_id=self.rp_id,
                expected_origin=self.origin, credential_public_key=bytes(row["public_key"]),
                credential_current_sign_count=row["sign_count"], require_user_verification=True)
        except Exception as exc:  # InvalidAuthenticationResponse: bad signature, counter, origin, rp, flags
            self.auth._failed(workspace_id, "passkey")
            raise CoreError(401, "passkey_refused", f"the passkey sign-in was not accepted: {type(exc).__name__}") from None
        with self.s.tx() as db:
            n = db.execute("UPDATE passkeys SET sign_count = ?, last_used_at = ? WHERE id = ? AND sign_count = ?",
                           (v.new_sign_count, self.s.now(), row["id"], row["sign_count"])).rowcount
            if not n:
                raise CoreError(409, "passkey_raced", "that passkey was just used; sign in again")
            self.s.event(db, workspace_id, "passkey.used", row["id"], "patrick", {"device": row["device_id"]})
            session = self.auth.issue_session(workspace_id, method="passkey", device_id=row["device_id"], db=db)
        return row["device_id"], session

    def list(self, p: Principal) -> list[dict]:
        self.auth._need_patrick(p)
        rows = self.s.db.execute("SELECT id, label, device_id, created_at, last_used_at FROM passkeys "
                                 "WHERE principal_id = ? AND revoked_at IS NULL ORDER BY created_at", (p.id,))
        return [dict(r) for r in rows]

    def revoke(self, p: Principal, passkey_id: str) -> None:
        self.auth._need_patrick(p, fresh=True)
        with self.s.tx() as db:
            n = db.execute("UPDATE passkeys SET revoked_at = ? WHERE id = ? AND principal_id = ? AND revoked_at IS NULL",
                           (self.s.now(), passkey_id, p.id)).rowcount
            if not n:
                raise CoreError(404, "no_passkey", "no such passkey")
            self.s.event(db, p.workspace_id, "passkey.revoked", passkey_id, "patrick", {})


def _field(obj, *path) -> str:
    for key in path:
        if not isinstance(obj, dict) or key not in obj:
            raise CoreError(400, "bad_credential", "that is not a passkey answer")
        obj = obj[key]
    if not isinstance(obj, str):
        raise CoreError(400, "bad_credential", "that is not a passkey answer")
    return obj
