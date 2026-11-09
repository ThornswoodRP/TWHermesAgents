"""Who is asking: Patrick's sessions, devices, recovery codes and pairing (07 section 5.1).

Only hashes are stored. A session id and its CSRF token are random 32-byte
values handed out once; the store keeps their SHA-256. Recovery codes are kept
as scrypt hashes. A pairing code is one-time, short-lived, and refused after
too many wrong tries.

The passkey ceremony (WebAuthn) comes with the phone in step 3, because its
relying party is the phone's ts.net origin. It ends by calling
`issue_session(..., method="passkey")`, the same door every sign-in uses.

A Bot is never a person: Bramble, the specialists and the push worker are
service principals, reached over the VM's local socket (step 2), and none of
them can hold a session.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta

from .store import CoreError, Store, new_id, parse, sha256_hex

SESSION_LIFETIME = timedelta(days=7)
SESSION_IDLE = timedelta(hours=12)
FRESH_FOR = timedelta(minutes=5)
PAIRING_LIFETIME = timedelta(minutes=10)
FAILURE_WINDOW = timedelta(minutes=15)
FAILURE_LIMIT = 5
RECOVERY_CODES = 10
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O or U
_SCRYPT = dict(n=2 ** 14, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)


@dataclass(frozen=True)
class Principal:
    id: str
    workspace_id: str
    kind: str                 # patrick, bramble, specialist, push_worker
    bot: str | None = None
    device_id: str | None = None
    session_hash: str | None = None
    fresh: bool = False
    device_kind: str | None = None   # phone or desktop, from the paired device
    method: str | None = None        # how this session was signed in: passkey, pairing or recovery_code
    signed_in_at: str | None = None  # when it was

    @property
    def label(self) -> str:
        return self.bot or self.kind


@dataclass(frozen=True)
class IssuedSession:
    token: str                # goes in the Secure, HttpOnly, SameSite=Lax cookie
    csrf: str                 # sent back by the page in a header on every write
    expires_at: str


def _code(n: int) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def _norm(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


class Auth:
    def __init__(self, store: Store):
        self.s = store

    # ── principals ────────────────────────────────────────────────────────

    def ensure_patrick(self, workspace_id: str) -> str:
        return self._ensure_principal(workspace_id, "patrick", None, "Patrick")

    def ensure_service(self, workspace_id: str, kind: str, bot: str | None = None) -> Principal:
        if kind not in ("bramble", "specialist", "push_worker"):
            raise CoreError(400, "not_a_service", f"{kind} is not a service principal")
        if kind == "bramble" and bot != "bramble":
            raise CoreError(400, "not_bramble", "the bramble principal is Bramble's alone")
        if kind == "specialist" and bot in (None, "bramble"):
            raise CoreError(400, "not_a_specialist", f"{bot!r} is not a specialist Bot")
        pid = self._ensure_principal(workspace_id, kind, bot, bot.title() if bot else "Push worker")
        return Principal(pid, workspace_id, kind, bot)

    def _ensure_principal(self, workspace_id, kind, bot, name) -> str:
        with self.s.tx() as db:
            if bot is None:
                row = db.execute("SELECT id FROM principals WHERE workspace_id = ? AND kind = ?",
                                 (workspace_id, kind)).fetchone()
            else:
                row = db.execute("SELECT id, kind FROM principals WHERE workspace_id = ? AND bot = ?",
                                 (workspace_id, bot)).fetchone()
                if row and row["kind"] != kind:
                    raise CoreError(409, "principal_kind", f"{bot} is already a {row['kind']}")
            if row:
                return row["id"]
            pid = new_id("pr")
            db.execute("INSERT INTO principals(id, workspace_id, kind, bot, display_name, created_at) "
                       "VALUES(?,?,?,?,?,?)", (pid, workspace_id, kind, bot, name, self.s.now()))
            self.s.event(db, workspace_id, "principal.created", pid, "core", {"kind": kind, "bot": bot})
            return pid

    def _patrick(self, workspace_id: str) -> str:
        row = self.s.db.execute("SELECT id FROM principals WHERE workspace_id = ? AND kind = 'patrick' "
                                "AND disabled_at IS NULL", (workspace_id,)).fetchone()
        if not row:
            raise CoreError(404, "no_patrick", "this workspace has no Patrick principal yet")
        return row["id"]

    # ── sessions ──────────────────────────────────────────────────────────

    def issue_session(self, workspace_id: str, *, method: str, device_id: str | None,
                      db=None) -> IssuedSession:
        """Only for Patrick, only after a sign-in method succeeded."""
        pid = self._patrick(workspace_id)
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        now = self.s.clock()
        expires = now + SESSION_LIFETIME
        row = (sha256_hex(token), sha256_hex(csrf), workspace_id, pid, device_id, method,
               self.s.now(), expires.isoformat(), (now + SESSION_IDLE).isoformat(),
               (now + FRESH_FOR).isoformat())

        def write(d):
            if device_id is not None:
                dev = d.execute("SELECT revoked_at FROM devices WHERE id = ? AND principal_id = ?",
                                (device_id, pid)).fetchone()
                if not dev or dev["revoked_at"]:
                    raise CoreError(403, "device_revoked", "that device is revoked or not Patrick's")
            d.execute("INSERT INTO sessions(id_hash, csrf_hash, workspace_id, principal_id, device_id, method, "
                      "created_at, expires_at, idle_expires_at, fresh_until) VALUES(?,?,?,?,?,?,?,?,?,?)", row)
            self.s.event(d, workspace_id, "session.issued", pid, "patrick", {"method": method, "device": device_id})

        if db is not None:
            write(db)
        else:
            with self.s.tx() as d:
                write(d)
        return IssuedSession(token, csrf, expires.isoformat())

    def authenticate(self, token: str | None, *, touch: bool = True,
                     hermes_principal: str | None = None) -> Principal:
        """The session's principal. A Desktop device paired from a Hermes sign-in
        answers only through that same sign-in."""
        if not token:
            raise CoreError(401, "signed_out", "sign in first")
        h = sha256_hex(token)
        row = self.s.db.execute(
            "SELECT s.*, p.kind, p.disabled_at, d.revoked_at AS device_revoked, d.kind AS device_kind, "
            "d.hermes_principal FROM sessions s "
            "JOIN principals p ON p.id = s.principal_id LEFT JOIN devices d ON d.id = s.device_id "
            "WHERE s.id_hash = ?", (h,)).fetchone()
        now = self.s.clock()
        if not row or row["revoked_at"] or row["disabled_at"] or row["device_revoked"]:
            raise CoreError(401, "signed_out", "that session is not valid; sign in again")
        if now >= parse(row["expires_at"]) or now >= parse(row["idle_expires_at"]):
            raise CoreError(401, "session_expired", "that session expired; sign in again")
        if row["kind"] != "patrick":
            raise CoreError(401, "signed_out", "only Patrick holds sessions")
        if row["hermes_principal"] is not None and not hmac.compare_digest(
                row["hermes_principal"], hermes_principal or ""):
            raise CoreError(401, "wrong_hermes_sign_in", "this Desktop was paired through another Hermes sign-in")
        if touch:
            idle = min(now + SESSION_IDLE, parse(row["expires_at"]))
            with self.s.tx() as db:
                db.execute("UPDATE sessions SET idle_expires_at = ? WHERE id_hash = ?", (idle.isoformat(), h))
        return Principal(row["principal_id"], row["workspace_id"], "patrick", None, row["device_id"], h,
                         now < parse(row["fresh_until"]), row["device_kind"], row["method"], row["created_at"])

    def check_csrf(self, principal: Principal, csrf: str | None) -> None:
        row = self.s.db.execute("SELECT csrf_hash FROM sessions WHERE id_hash = ?",
                                (principal.session_hash,)).fetchone()
        if not row or not csrf or not hmac.compare_digest(row["csrf_hash"], sha256_hex(csrf)):
            raise CoreError(403, "csrf", "the request's CSRF token does not match this session")

    def rotate_csrf(self, principal: Principal) -> str:
        """A new CSRF token for this session (the app asks after a reload; only the
        cookie and an exact Origin get here). The old one stops working."""
        csrf = secrets.token_urlsafe(32)
        with self.s.tx() as db:
            db.execute("UPDATE sessions SET csrf_hash = ? WHERE id_hash = ? AND revoked_at IS NULL",
                       (sha256_hex(csrf), principal.session_hash))
        return csrf

    def revoke_session(self, principal: Principal, reason: str = "signed out") -> None:
        with self.s.tx() as db:
            db.execute("UPDATE sessions SET revoked_at = ?, revoked_reason = ? WHERE id_hash = ? "
                       "AND revoked_at IS NULL", (self.s.now(), reason, principal.session_hash))
            self.s.event(db, principal.workspace_id, "session.revoked", principal.id, "patrick", {"reason": reason})

    def revoke_device(self, principal: Principal, device_id: str) -> None:
        self._need_patrick(principal)
        with self.s.tx() as db:
            n = db.execute("UPDATE devices SET revoked_at = ? WHERE id = ? AND principal_id = ? "
                           "AND revoked_at IS NULL", (self.s.now(), device_id, principal.id)).rowcount
            if not n:
                raise CoreError(404, "no_device", "no such live device")
            db.execute("UPDATE sessions SET revoked_at = ?, revoked_reason = 'device revoked' "
                       "WHERE device_id = ? AND revoked_at IS NULL", (self.s.now(), device_id))
            db.execute("UPDATE push_subscriptions SET revoked_at = ?, retired_reason = 'device revoked' "
                       "WHERE device_id = ? AND revoked_at IS NULL", (self.s.now(), device_id))
            self.s.event(db, principal.workspace_id, "device.revoked", device_id, "patrick", {})

    def revoke_all(self, workspace_id: str, reason: str, db) -> int:
        return db.execute("UPDATE sessions SET revoked_at = ?, revoked_reason = ? WHERE workspace_id = ? "
                          "AND revoked_at IS NULL", (self.s.now(), reason, workspace_id)).rowcount

    def _need_patrick(self, p: Principal, *, fresh: bool = False) -> None:
        if p.kind != "patrick":
            raise CoreError(403, "not_patrick", f"{p.label} is not Patrick")
        if fresh and not p.fresh:
            if p.method == "pairing" and not self.has_passkey(p.workspace_id):
                # The 5 minutes after a pairing ran out before the first passkey was added: there is no
                # passkey to sign in again with, so the way on is a new pairing code.
                raise CoreError(403, "setup_expired", "setting up this phone ran out: a pairing gives 5 minutes "
                                "to add the first passkey; pair again with a new code and add it straight after")
            raise CoreError(403, "sign_in_again", "this needs a fresh sign-in")

    def has_passkey(self, workspace_id: str) -> bool:
        return self.s.db.execute("SELECT 1 FROM passkeys WHERE workspace_id = ? AND revoked_at IS NULL LIMIT 1",
                                 (workspace_id,)).fetchone() is not None

    # ── wrong tries ───────────────────────────────────────────────────────

    def _locked(self, workspace_id: str, method: str) -> bool:
        since = (self.s.clock() - FAILURE_WINDOW).isoformat()
        n = self.s.db.execute("SELECT count(*) FROM sign_in_failures WHERE workspace_id = ? AND method = ? "
                              "AND at > ?", (workspace_id, method, since)).fetchone()[0]
        return n >= FAILURE_LIMIT

    def _failed(self, workspace_id: str, method: str) -> None:
        with self.s.tx() as db:
            db.execute("INSERT INTO sign_in_failures(workspace_id, method, at) VALUES(?,?,?)",
                       (workspace_id, method, self.s.now()))
            self.s.event(db, workspace_id, "sign_in.failed", method, "unknown", {})

    # ── recovery codes ────────────────────────────────────────────────────

    def new_recovery_codes(self, principal: Principal) -> list[str]:
        """Replaces every unused code. Shown once; only hashes are kept."""
        self._need_patrick(principal, fresh=True)
        codes = [f"{_code(4)}-{_code(4)}-{_code(4)}" for _ in range(RECOVERY_CODES)]
        with self.s.tx() as db:
            db.execute("UPDATE recovery_codes SET replaced_at = ? WHERE principal_id = ? AND used_at IS NULL "
                       "AND replaced_at IS NULL", (self.s.now(), principal.id))
            for c in codes:
                salt = secrets.token_bytes(16)
                db.execute("INSERT INTO recovery_codes(id, workspace_id, principal_id, salt, code_hash, created_at) "
                           "VALUES(?,?,?,?,?,?)", (new_id("rc"), principal.workspace_id, principal.id, salt,
                                                   hashlib.scrypt(_norm(c).encode(), salt=salt, **_SCRYPT),
                                                   self.s.now()))
            self.s.event(db, principal.workspace_id, "recovery_codes.replaced", principal.id, "patrick",
                         {"count": len(codes)})
        return codes

    def sign_in_with_recovery_code(self, workspace_id: str, code: str, device_id: str | None, *,
                                   new_phone: str | None = None) -> IssuedSession:
        """A code works once. With `new_phone`, the sign-in is a new phone device of
        that label (the phone app's way back in after a lost passkey)."""
        if self._locked(workspace_id, "recovery"):
            raise CoreError(429, "too_many_tries", "too many wrong codes; wait 15 minutes")
        pid = self._patrick(workspace_id)
        want = _norm(code).encode()
        rows = self.s.db.execute("SELECT id, salt, code_hash FROM recovery_codes WHERE principal_id = ? "
                                 "AND used_at IS NULL AND replaced_at IS NULL", (pid,)).fetchall()
        match = None
        for r in rows:  # every row is hashed, so the time taken does not say which matched
            if hmac.compare_digest(hashlib.scrypt(want, salt=r["salt"], **_SCRYPT), r["code_hash"]):
                match = r["id"]
        if not match:
            self._failed(workspace_id, "recovery")
            raise CoreError(401, "wrong_code", "that recovery code is not valid")
        with self.s.tx() as db:
            n = db.execute("UPDATE recovery_codes SET used_at = ? WHERE id = ? AND used_at IS NULL",
                           (self.s.now(), match)).rowcount
            if not n:
                raise CoreError(401, "wrong_code", "that recovery code was just used")
            self.s.event(db, workspace_id, "recovery_code.used", match, "patrick", {})
            if new_phone is not None:
                device_id = new_id("dv")
                db.execute("INSERT INTO devices(id, workspace_id, principal_id, kind, label, created_at) "
                           "VALUES(?,?,?,?,?,?)", (device_id, workspace_id, pid, "phone", new_phone[:80] or "phone",
                                                   self.s.now()))
                self.s.event(db, workspace_id, "device.paired", device_id, "patrick",
                             {"kind": "phone", "label": new_phone[:80], "by": "recovery_code"})
            return self.issue_session(workspace_id, method="recovery_code", device_id=device_id, db=db)

    # ── pairing a new device ──────────────────────────────────────────────

    def new_pairing_code(self, principal: Principal, device_kind: str, label: str) -> dict:
        """Patrick, freshly signed in, asks for a code to type into the new device."""
        self._need_patrick(principal, fresh=True)
        code = _code(8)
        expires = (self.s.clock() + PAIRING_LIFETIME).isoformat()
        with self.s.tx() as db:
            pc = new_id("pc")
            db.execute("INSERT INTO pairing_codes(id, workspace_id, principal_id, code_hash, device_kind, label, "
                       "created_at, expires_at) VALUES(?,?,?,?,?,?,?,?)",
                       (pc, principal.workspace_id, principal.id, sha256_hex(code), device_kind, label,
                        self.s.now(), expires))
            self.s.event(db, principal.workspace_id, "pairing.offered", pc, "patrick",
                         {"device_kind": device_kind, "label": label})
        return {"code": f"{code[:4]}-{code[4:]}", "expires_at": expires}

    def operator_pairing_code(self, workspace_id: str, device_kind: str, label: str, *, by: str) -> dict:
        """The first device's code, made on the VM by the operator over SSH (only
        Patrick's key reaches it). Every later device is paired from a signed-in one."""
        pid = self._patrick(workspace_id)
        code = _code(8)
        expires = (self.s.clock() + PAIRING_LIFETIME).isoformat()
        with self.s.tx() as db:
            pc = new_id("pc")
            db.execute("INSERT INTO pairing_codes(id, workspace_id, principal_id, code_hash, device_kind, label, "
                       "created_at, expires_at) VALUES(?,?,?,?,?,?,?,?)",
                       (pc, workspace_id, pid, sha256_hex(code), device_kind, label, self.s.now(), expires))
            self.s.event(db, workspace_id, "pairing.offered", pc, by,
                         {"device_kind": device_kind, "label": label, "from": "operator"})
        return {"code": f"{code[:4]}-{code[4:]}", "expires_at": expires}

    def redeem_pairing_code(self, workspace_id: str, code: str, *,
                            hermes_principal: str | None = None) -> tuple[str, IssuedSession]:
        """The new device sends the code once. It gets its device id and a session.
        A Desktop device must come through a Hermes sign-in, and is bound to it."""
        if self._locked(workspace_id, "pairing"):
            raise CoreError(429, "too_many_tries", "too many wrong codes; wait 15 minutes")
        h = sha256_hex(_norm(code))
        now = self.s.clock()
        with self.s.tx() as db:
            row = db.execute("SELECT * FROM pairing_codes WHERE code_hash = ? AND workspace_id = ?",
                             (h, workspace_id)).fetchone()
            ok = row and not row["used_at"] and not row["cancelled_at"] and now < parse(row["expires_at"])
            if ok and row["device_kind"] == "desktop" and not hermes_principal:
                raise CoreError(403, "no_hermes_sign_in", "a Desktop pairs only through a signed-in Hermes")
            if ok:
                db.execute("UPDATE pairing_codes SET used_at = ? WHERE id = ?", (self.s.now(), row["id"]))
                dev = new_id("dv")
                db.execute("INSERT INTO devices(id, workspace_id, principal_id, kind, label, created_at, "
                           "hermes_principal) VALUES(?,?,?,?,?,?,?)",
                           (dev, workspace_id, row["principal_id"], row["device_kind"], row["label"], self.s.now(),
                            hermes_principal if row["device_kind"] == "desktop" else None))
                self.s.event(db, workspace_id, "device.paired", dev, "patrick",
                             {"kind": row["device_kind"], "label": row["label"]})
                session = self.issue_session(workspace_id, method="pairing", device_id=dev, db=db)
        if not ok:
            self._failed(workspace_id, "pairing")
            raise CoreError(401, "wrong_code", "that pairing code is not valid or has expired")
        return dev, session
