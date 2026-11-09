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

The phone's fingerprint (Patrick, 2 Nov) is one more passkey of the same kind,
held by the phone's own authenticator instead of a password manager. A sign-in
that asks for any passkey lets Android hand the request to the chosen passkey
provider (Bitwarden), which opens its own app and then asks for the finger; a
request that names this phone's key (`allowCredentials`, hint `client-device`)
is answered by whichever app holds that key. So the key itself must be the
phone's own: Android still lets a passkey app save it, and on 2 Nov Bitwarden
did, so the fingerprint button opened Bitwarden (8 Nov). The core now reads
which app made each key (its AAGUID) and takes a phone key only from Google
Password Manager, the Pixel's own, whose sign-in is the fingerprint sheet over
the app. The core checks the signed challenge, the counter and user
verification exactly as for any passkey, so it is no weaker. The passkey stays
the way in on a Desktop.

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

# Which app made a key, by the AAGUID it reports with a "none" attestation (the community list,
# github.com/passkeydeveloper/passkey-authenticator-aaguids). A phone key comes only from the phone's
# own authenticator; the others are named so a refusal says where the key went.
PHONE_AUTHENTICATORS = {"ea9b8d66-4d01-1d21-3ce4-b6b48cb575d4": "Google Password Manager"}
PASSKEY_APPS = {
    "d548826e-79b4-db40-a3d8-11116f7e8349": "Bitwarden",
    "bada5566-a7aa-401f-bd96-45619a55120d": "1Password",
    "531126d6-e717-415c-9320-3d9aa6981239": "Dashlane",
    "50726f74-6f6e-5061-7373-50726f746f6e": "Proton Pass",
    "53414d53-554e-4700-0000-000000000000": "Samsung Pass",
}
NO_AAGUID = "00000000-0000-0000-0000-000000000000"


def maker(aaguid: str | None) -> str:
    """The app's name for an AAGUID, or what is known about it."""
    if not aaguid or aaguid == NO_AAGUID:
        return "an app that did not say its name"
    return PHONE_AUTHENTICATORS.get(aaguid) or PASSKEY_APPS.get(aaguid) or "a passkey app"


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


def _phone_structs():
    from webauthn.helpers.structs import AuthenticatorAttachment, AuthenticatorTransport, PublicKeyCredentialHint
    return AuthenticatorAttachment, AuthenticatorTransport, PublicKeyCredentialHint


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

    def register_options(self, p: Principal, *, on_phone: bool = False) -> dict:
        """on_phone: the key is made by the phone's own authenticator (its screen lock and
        fingerprint), not a password manager, so a sign-in that names it never leaves the app."""
        self._ready()
        self.auth._need_patrick(p, fresh=True)
        if not p.device_id:
            raise CoreError(403, "no_device", "a passkey is added from a paired device")
        if on_phone and p.device_kind != "phone":
            raise CoreError(403, "not_a_phone", "the fingerprint sign-in is for a paired phone; a Desktop keeps its passkey")
        webauthn, options_to_json, Selection, Descriptor, Resident, UV = _lib()
        have = self.s.db.execute("SELECT credential_id FROM passkeys WHERE principal_id = ? AND revoked_at IS NULL",
                                 (p.id,)).fetchall()
        if len(have) >= MAX_PASSKEYS:
            raise CoreError(409, "too_many_passkeys", f"{MAX_PASSKEYS} passkeys at most; remove one first")
        challenge = self._challenge(p.workspace_id, "register", p.session_hash)
        if on_phone:
            Attachment, _, Hint = _phone_structs()
            # The phone's platform authenticator. Sign-in names this key, so it need not be
            # discoverable; the platform decides, and user verification stays required.
            selection = Selection(authenticator_attachment=Attachment.PLATFORM, resident_key=Resident.PREFERRED,
                                  user_verification=UV.REQUIRED)
            hints = [Hint.CLIENT_DEVICE]
            # No exclude list: the passkey this phone already has lives in a passkey app on the same
            # phone, and Android checks an exclude list against every provider, so naming it would
            # refuse the phone's own key as "already there". The app offers this once per phone.
            exclude = []
        else:
            selection = Selection(resident_key=Resident.REQUIRED, user_verification=UV.REQUIRED)
            hints = None
            exclude = [Descriptor(id=unb64url(r["credential_id"])) for r in have]
        opts = webauthn.generate_registration_options(
            rp_id=self.rp_id, rp_name=RP_NAME, user_name="Patrick", user_display_name="Patrick",
            user_id=p.id.encode("utf-8"), challenge=challenge, timeout=int(CHALLENGE_LIFETIME.total_seconds() * 1000),
            authenticator_selection=selection, hints=hints, exclude_credentials=exclude)
        return json.loads(options_to_json(opts))

    def register(self, p: Principal, credential: dict, label: str, *, on_phone: bool = False) -> dict:
        """on_phone: the answer to register_options(on_phone=True). It is kept as this phone's own key
        only when the phone's own authenticator made it, and it then takes the place of any phone key
        this phone had before."""
        self._ready()
        self.auth._need_patrick(p, fresh=True)
        if not p.device_id:
            raise CoreError(403, "no_device", "a passkey is added from a paired device")
        if on_phone and p.device_kind != "phone":
            raise CoreError(403, "not_a_phone", "the fingerprint sign-in is for a paired phone; a Desktop keeps its passkey")
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
        aaguid = str(v.aaguid or "").lower() or None
        if on_phone and aaguid not in PHONE_AUTHENTICATORS:
            # Nothing is kept: a phone key in a passkey app opens that app at every sign-in.
            where = maker(aaguid)
            raise CoreError(409, "saved_in_app",
                            f"the fingerprint key was saved in {where}, so signing in with it would open {where}; "
                            "nothing was added. Add it again and pick Google Password Manager when Android asks "
                            "where to save it", maker=where)
        pk = new_id("pk")
        with self.s.tx() as db:
            if db.execute("SELECT 1 FROM passkeys WHERE credential_id = ?", (b64url(v.credential_id),)).fetchone():
                raise CoreError(409, "passkey_exists", "that passkey is already added")
            replaced = []
            if on_phone:
                replaced = [r["id"] for r in db.execute(
                    "SELECT id FROM passkeys WHERE device_id = ? AND phone_key = 1 AND revoked_at IS NULL",
                    (p.device_id,))]
                for old in replaced:
                    db.execute("UPDATE passkeys SET revoked_at = ? WHERE id = ?", (self.s.now(), old))
                    self.s.event(db, p.workspace_id, "passkey.revoked", old, "patrick",
                                 {"device": p.device_id, "replaced_by": pk})
            db.execute("INSERT INTO passkeys(id, workspace_id, principal_id, device_id, credential_id, public_key, "
                       "sign_count, label, created_at, aaguid, phone_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (pk, p.workspace_id, p.id, p.device_id, b64url(v.credential_id), v.credential_public_key,
                        v.sign_count, (label or "passkey")[:80], self.s.now(), aaguid, 1 if on_phone else 0))
            self.s.event(db, p.workspace_id, "passkey.added", pk, "patrick",
                         {"device": p.device_id, "maker": maker(aaguid), **({"phone_key": True} if on_phone else {})})
        # The credential id goes back to the phone that made it, so its sign-in can name it.
        return {"passkey_id": pk, "credential_id": b64url(v.credential_id)}

    # ── signing in ────────────────────────────────────────────────────────

    def sign_in_options(self, workspace_id: str, phone_key: str | None = None) -> dict:
        """phone_key: the credential id of a paired phone's own key (its fingerprint). The
        request then names only that key, so the phone answers it itself and no passkey
        provider app is opened. Without it, any of Patrick's passkeys may answer."""
        self._ready()
        webauthn, options_to_json, _, Descriptor, _, UV = _lib()
        if self.auth._locked(workspace_id, "passkey"):
            raise CoreError(429, "too_many_tries", "too many failed passkey sign-ins; wait 15 minutes")
        allow = None
        if phone_key is not None:
            cred_id = b64url(unb64url(phone_key))
            row = self.s.db.execute(
                "SELECT k.aaguid FROM passkeys k JOIN devices d ON d.id = k.device_id WHERE k.credential_id = ? "
                "AND k.workspace_id = ? AND k.phone_key = 1 AND k.revoked_at IS NULL AND d.revoked_at IS NULL "
                "AND d.kind = 'phone'", (cred_id, workspace_id)).fetchone()
            if not row:
                raise CoreError(404, "no_phone_key", "this phone's fingerprint sign-in was removed; use the passkey")
            if row["aaguid"] not in PHONE_AUTHENTICATORS:
                # A phone key from before 8 Nov, which a passkey app may hold: asking for it would open that app.
                raise CoreError(409, "phone_key_in_app",
                                "this phone's fingerprint key was not made by the phone itself, so it opens a "
                                "passkey app; use the passkey and add the fingerprint again")
            _, Transport, _ = _phone_structs()
            allow = [Descriptor(id=unb64url(cred_id), transports=[Transport.INTERNAL])]
        challenge = self._challenge(workspace_id, "sign_in", None)
        opts = webauthn.generate_authentication_options(
            rp_id=self.rp_id, challenge=challenge, timeout=int(CHALLENGE_LIFETIME.total_seconds() * 1000),
            user_verification=UV.REQUIRED, allow_credentials=allow)
        out = json.loads(options_to_json(opts))
        if allow:
            out["hints"] = ["client-device"]  # WebAuthn level 3; py_webauthn 2.7 has no hints for a sign-in
        return out

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
        if not row:
            self.auth._failed(workspace_id, "passkey")
            raise CoreError(401, "unknown_passkey", "that passkey is not one of Patrick's here")
        if row["revoked_at"] or row["device_revoked"]:
            # Removed here, but still in his passkey app, which offers it with the live ones (9 Nov: the
            # fingerprint key replaced on 8 Nov stayed in the app and was refused). The app tells the
            # passkey app to drop it (WebAuthn signalUnknownCredential), only for a key the core removed.
            self.auth._failed(workspace_id, "passkey")
            raise CoreError(401, "removed_passkey", "that passkey was removed here; sign in with another one",
                            rp_id=self.rp_id, credential_id=row["credential_id"])
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

    def accepted(self, workspace_id: str, device_id: str) -> dict:
        """The passkeys the core takes now, for the app to tell his passkey app after a sign-in (WebAuthn
        signalAllAcceptedCredentials): one it removed is then no longer offered. Only to Patrick, signed in."""
        row = self.s.db.execute("SELECT principal_id FROM devices WHERE id = ? AND workspace_id = ?",
                                (device_id, workspace_id)).fetchone()
        if not row:
            return {}
        ids = [r["credential_id"] for r in self.s.db.execute(
            "SELECT k.credential_id FROM passkeys k JOIN devices d ON d.id = k.device_id WHERE k.principal_id = ? "
            "AND k.workspace_id = ? AND k.revoked_at IS NULL AND d.revoked_at IS NULL ORDER BY k.created_at",
            (row["principal_id"], workspace_id))]
        return {"rp_id": self.rp_id, "user_id": b64url(row["principal_id"].encode("utf-8")), "credential_ids": ids}

    def list(self, p: Principal) -> list[dict]:
        self.auth._need_patrick(p)
        rows = self.s.db.execute("SELECT id, label, device_id, created_at, last_used_at, aaguid, phone_key FROM passkeys "
                                 "WHERE principal_id = ? AND revoked_at IS NULL ORDER BY created_at", (p.id,))
        out = []
        for r in rows:
            d = dict(r)
            aaguid = d.pop("aaguid")
            d["maker"] = maker(aaguid) if aaguid else None
            d["phone_key"] = bool(d["phone_key"])
            out.append(d)
        return out

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
