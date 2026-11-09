"""The phone, read only (step 3): passkeys, the app's routes and files, and the
Work and Room feeds.

Passkey tests run where py_webauthn and its dependencies are installed (the
core's venv on the VM, from deploy/decisions/requirements.txt) and skip
elsewhere, saying so. Everything else is standard library.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from decisions_helpers import World
from test_decisions_api import Served

from thornswood_decisions import feed_reader, passkey
from thornswood_decisions.feeds import Feeds
from thornswood_decisions.passkey import Passkeys
from thornswood_decisions.schema import MIGRATIONS
from thornswood_decisions.store import CoreError

ORIGIN = "https://hermes.example.ts.net"
GOOGLE_PM = "ea9b8d66-4d01-1d21-3ce4-b6b48cb575d4"
BITWARDEN = "d548826e-79b4-db40-a3d8-11116f7e8349"
RP_ID = "hermes.example.ts.net"
ROOM = "54b3ad46-3419-4adb-b60d-c79cb347fcee"
BOTS = ("bramble", "rift", "sprocket", "echo", "magpie", "wisp")

try:
    import cbor2  # noqa: F401
    import cryptography  # noqa: F401
    HAVE_PASSKEY_LIB = passkey.available()
except ImportError:
    HAVE_PASSKEY_LIB = False
NEEDS_LIB = unittest.skipUnless(HAVE_PASSKEY_LIB, "py_webauthn is not installed here (it is in the core's venv on the VM)")


class Migrations(unittest.TestCase):
    # A migration that has run on a store must never change. These are the
    # hashes of the migrations as merged; a new change is a new migration.
    PINNED = {
        1: "65f4ceb6d80118fd6da4be92a326909e3bfe7c6a8271899f2563bd4c8f423a63",
        2: "0410fe14fec9508e85fb8d9e40eb1887d2d5450309fadf59bcc2ff6c5c55ad61",
        3: "8a79c74a0938504cd8e43e758be0dbbe607cd53ca3e7bae273424c55ad3632da",
        4: "7d599a7b92702912c989d590afdfb21235025cf7fda9f24dcc091fc11027c95c",
        5: "d36394b9f9ba7c25fc560c05a7834d52955de09312aed15256602e6a05ce1406",
        6: "720327f6d99e5e74175c44b603b525df3cdae7ae67c1a6aa56778e8d882c9b63",
        7: "3f7658050982a903a9df24690b2f1c2a4d4759c525f86af3b5d7855c222175e0",
        8: "0451456cdb9ec93fba6391b472b3f5e04d6a8a84487e44fb9be54226b4e11404",
    }

    def test_merged_migrations_never_change(self):
        for n, want in self.PINNED.items():
            self.assertEqual(hashlib.sha256(MIGRATIONS[n - 1].encode()).hexdigest(), want, f"migration {n}")


def phone(w: World):
    """A paired phone and its fresh principal."""
    code = w.core.auth.operator_pairing_code(w.ws, "phone", "Pixel", by="tests")["code"]
    dev, s = w.core.auth.redeem_pairing_code(w.ws, code)
    return dev, s, w.core.auth.authenticate(s.token)


@NEEDS_LIB
class PasskeyCeremonies(unittest.TestCase):
    def setUp(self):
        from thornswood_decisions.softkey import SoftKey
        self.SoftKey = SoftKey
        self.w = World()
        self.addCleanup(self.w.close)
        self.dev, self.s, self.p = phone(self.w)
        self.pk = Passkeys(self.w.store, ORIGIN)
        self.key = SoftKey(ORIGIN, RP_ID)

    def enrol(self, key=None):
        key = key or self.key
        return self.pk.register(self.p, key.create(self.pk.register_options(self.p)), "Pixel")

    def test_add_then_sign_in(self):
        self.enrol()
        dev, s = self.pk.sign_in(self.w.ws, self.key.get(self.pk.sign_in_options(self.w.ws)))
        p = self.w.core.auth.authenticate(s.token)
        self.assertEqual((dev, p.device_id, p.fresh, p.kind), (self.dev, self.dev, True, "patrick"))
        self.assertEqual(len(self.pk.list(p)), 1)

    def test_adding_needs_a_fresh_sign_in(self):
        self.w.clock.advance(minutes=6)
        p = self.w.core.auth.authenticate(self.s.token)
        with self.assertRaises(CoreError) as cm:
            self.pk.register_options(p)
        # No passkey yet, so no "sign in again with your passkey": the pairing's 5 minutes ran out.
        self.assertEqual(cm.exception.reason, "setup_expired")

    def test_adding_straight_after_pairing_works_and_then_the_old_words_hold(self):
        self.assertFalse(self.w.core.auth.has_passkey(self.w.ws))
        self.enrol()
        self.assertTrue(self.w.core.auth.has_passkey(self.w.ws))
        _, s, _ = phone(self.w)
        self.w.clock.advance(minutes=6)
        with self.assertRaises(CoreError) as cm:
            self.pk.register_options(self.w.core.auth.authenticate(s.token))
        self.assertEqual(cm.exception.reason, "sign_in_again")

    def test_a_challenge_works_once(self):
        self.enrol()
        answer = self.key.get(self.pk.sign_in_options(self.w.ws))
        self.pk.sign_in(self.w.ws, answer)
        with self.assertRaises(CoreError) as cm:
            self.pk.sign_in(self.w.ws, answer)
        self.assertEqual(cm.exception.reason, "bad_challenge")

    def test_a_challenge_expires(self):
        self.enrol()
        opts = self.pk.sign_in_options(self.w.ws)
        self.w.clock.advance(minutes=6)
        with self.assertRaises(CoreError) as cm:
            self.pk.sign_in(self.w.ws, self.key.get(opts))
        self.assertEqual(cm.exception.reason, "bad_challenge")

    def test_a_registration_challenge_belongs_to_its_session(self):
        opts = self.pk.register_options(self.p)
        _, _, other = phone(self.w)
        with self.assertRaises(CoreError) as cm:
            self.pk.register(other, self.key.create(opts), "Pixel")
        self.assertEqual(cm.exception.reason, "bad_challenge")

    def test_another_origin_is_refused_and_counted(self):
        self.enrol()
        for _ in range(5):
            with self.assertRaises(CoreError) as cm:
                self.pk.sign_in(self.w.ws, self.key.get(self.pk.sign_in_options(self.w.ws),
                                                        origin="https://evil.example"))
            self.assertEqual(cm.exception.reason, "passkey_refused")
        with self.assertRaises(CoreError) as cm:
            self.pk.sign_in_options(self.w.ws)
        self.assertEqual(cm.exception.reason, "too_many_tries")

    def test_another_relying_party_is_refused(self):
        self.enrol()
        with self.assertRaises(CoreError) as cm:
            self.pk.sign_in(self.w.ws, self.key.get(self.pk.sign_in_options(self.w.ws), rp_id="evil.example"))
        self.assertEqual(cm.exception.reason, "passkey_refused")

    def test_user_verification_is_required(self):
        lazy = self.SoftKey(ORIGIN, RP_ID, verified=False)
        with self.assertRaises(CoreError) as cm:
            self.enrol(lazy)
        self.assertEqual(cm.exception.reason, "passkey_refused")

    def test_an_unknown_passkey_is_refused(self):
        self.enrol()
        stranger = self.SoftKey(ORIGIN, RP_ID)
        stranger.user_handle = b"x"
        with self.assertRaises(CoreError) as cm:
            self.pk.sign_in(self.w.ws, stranger.get(self.pk.sign_in_options(self.w.ws)))
        self.assertEqual(cm.exception.reason, "unknown_passkey")

    def test_a_sign_in_names_only_the_keys_the_core_takes(self):
        # 9 Nov: the phone offered a key the core never had, and the sign-in was refused. The request now
        # names the live keys, so a passkey app offers only those.
        self.assertIsNone(self.pk.sign_in_options(self.w.ws).get("allowCredentials") or None)
        self.enrol()
        [row] = self.w.core.s.db.execute("SELECT credential_id FROM passkeys").fetchall()
        opts = self.pk.sign_in_options(self.w.ws)
        self.assertEqual([c["id"] for c in opts["allowCredentials"]], [row["credential_id"]])
        self.assertNotIn("hints", opts)
        self.w.core.auth.revoke_device(self.p, self.dev)
        self.assertIsNone(self.pk.sign_in_options(self.w.ws).get("allowCredentials") or None)

    def test_a_refused_sign_in_records_which_key_it_was_offered(self):
        from thornswood_decisions import cli
        self.enrol()
        stranger = self.SoftKey(ORIGIN, RP_ID)
        stranger.user_handle = b"x"
        offered = stranger.get(self.pk.sign_in_options(self.w.ws))
        with self.assertRaises(CoreError):
            self.pk.sign_in(self.w.ws, offered)
        name = self.w.core.s.db.execute("SELECT name FROM workspaces WHERE id = ?", (self.w.ws,)).fetchone()["name"]
        out = cli.cmd_passkeys(self.w.core.s, type("A", (), {"workspace": name, "last": 5})())
        [key] = out["passkeys"]
        self.assertEqual(len(key["credential"]), 16)
        self.assertNotIn("public_key", key)
        self.assertEqual(out["refused"][0]["why"], "unknown")
        self.assertEqual(out["refused"][0]["credential"], offered["rawId"][:16])
        self.assertNotEqual(out["refused"][0]["credential"], key["credential"])

    def test_a_sign_in_the_phone_never_answered_is_recorded_and_not_counted(self):
        from thornswood_decisions import cli
        self.enrol()
        name = self.w.core.s.db.execute("SELECT name FROM workspaces WHERE id = ?", (self.w.ws,)).fetchone()["name"]
        args = type("A", (), {"workspace": name, "last": 5})()
        unanswered = self.pk.sign_in_options(self.w.ws)
        [req] = cli.cmd_passkeys(self.w.core.s, args)["sign_in_requests"]
        self.assertIsNone(req["used_at"])
        self.pk.browser_failed(self.w.ws, {"challenge": unanswered["challenge"], "name": "NotAllowedError",
                                           "message": "No passkeys available"})
        out = cli.cmd_passkeys(self.w.core.s, args)
        self.assertEqual(out["refused"][0], {**out["refused"][0], "why": "browser", "error": "NotAllowedError",
                                             "message": "No passkeys available"})
        self.assertIsNotNone(out["sign_in_requests"][0]["used_at"])
        # Each request is noted once, and a note is no wrong try: the lock never closes on them.
        with self.assertRaises(CoreError) as cm:
            self.pk.browser_failed(self.w.ws, {"challenge": unanswered["challenge"], "name": "NotAllowedError"})
        self.assertEqual(cm.exception.reason, "bad_challenge")
        for _ in range(8):
            self.pk.browser_failed(self.w.ws, {"challenge": self.pk.sign_in_options(self.w.ws)["challenge"]})
        self.pk.sign_in(self.w.ws, self.key.get(self.pk.sign_in_options(self.w.ws)))

    def test_a_used_sign_in_request_is_recorded_with_why(self):
        from thornswood_decisions import cli
        self.enrol()
        answer = self.key.get(self.pk.sign_in_options(self.w.ws))
        self.pk.sign_in(self.w.ws, answer)
        with self.assertRaises(CoreError) as cm:
            self.pk.sign_in(self.w.ws, answer)
        self.assertEqual(cm.exception.reason, "bad_challenge")
        name = self.w.core.s.db.execute("SELECT name FROM workspaces WHERE id = ?", (self.w.ws,)).fetchone()["name"]
        out = cli.cmd_passkeys(self.w.core.s, type("A", (), {"workspace": name, "last": 5})())
        self.assertEqual(out["refused"][0]["why"], "request_used")

    def test_revoking_the_device_stops_its_passkey(self):
        self.enrol()
        self.w.core.auth.revoke_device(self.p, self.dev)
        with self.assertRaises(CoreError) as cm:
            self.pk.sign_in(self.w.ws, self.key.get(self.pk.sign_in_options(self.w.ws)))
        # Named as removed, with its id, so the app can tell his passkey app to drop it.
        self.assertEqual(cm.exception.reason, "removed_passkey")
        self.assertEqual(cm.exception.detail["rp_id"], RP_ID)
        row = self.w.core.s.db.execute("SELECT credential_id FROM passkeys").fetchone()
        self.assertEqual(cm.exception.detail["credential_id"], row["credential_id"])

    def test_a_removed_passkey_is_named_and_left_out_of_the_accepted(self):
        self.enrol()
        second = self.SoftKey(ORIGIN, RP_ID)
        p = self.w.core.auth.authenticate(self.s.token)
        added = self.pk.register(p, second.create(self.pk.register_options(p)), "second")
        dev, _ = self.pk.sign_in(self.w.ws, second.get(self.pk.sign_in_options(self.w.ws)))
        both = self.pk.accepted(self.w.ws, dev)
        self.assertEqual(both["rp_id"], RP_ID)
        self.assertEqual(len(both["credential_ids"]), 2)
        self.assertIn(added["credential_id"], both["credential_ids"])
        first = next(k for k in self.pk.list(p) if k["id"] != added["passkey_id"])
        self.pk.revoke(self.w.core.auth.authenticate(self.s.token), first["id"])
        with self.assertRaises(CoreError) as cm:
            self.pk.sign_in(self.w.ws, self.key.get(self.pk.sign_in_options(self.w.ws)))
        self.assertEqual(cm.exception.reason, "removed_passkey")
        self.assertEqual(self.pk.accepted(self.w.ws, dev)["credential_ids"], [added["credential_id"]])

    def test_a_counter_that_goes_back_is_refused(self):
        key = self.SoftKey(ORIGIN, RP_ID, counter=5)
        self.enrol(key)
        self.pk.sign_in(self.w.ws, key.get(self.pk.sign_in_options(self.w.ws)))  # counter 6
        key.counter = 3  # a cloned key replaying an older counter
        with self.assertRaises(CoreError) as cm:
            self.pk.sign_in(self.w.ws, key.get(self.pk.sign_in_options(self.w.ws)))
        self.assertEqual(cm.exception.reason, "passkey_refused")

    def test_the_same_passkey_is_not_added_twice(self):
        self.enrol()
        p = self.w.core.auth.authenticate(self.s.token)
        with self.assertRaises(CoreError) as cm:
            self.pk.register(p, self.key.create(self.pk.register_options(p)), "again")
        self.assertIn(cm.exception.reason, ("passkey_exists", "passkey_refused"))



def desktop(w: World):
    """A paired Desktop and its fresh principal."""
    code = w.core.auth.operator_pairing_code(w.ws, "desktop", "One", by="tests")["code"]
    dev, s = w.core.auth.redeem_pairing_code(w.ws, code, hermes_principal="nous:patrick")
    return dev, s, w.core.auth.authenticate(s.token, hermes_principal="nous:patrick")


@NEEDS_LIB
class PhoneFingerprint(unittest.TestCase):
    """The phone's own key (Patrick, 2 Nov): made by its platform authenticator,
    and named in the sign-in so the phone answers with its fingerprint sheet
    instead of Android handing the request to a passkey app."""

    def setUp(self):
        from thornswood_decisions.softkey import SoftKey
        self.SoftKey = SoftKey
        self.w = World()
        self.addCleanup(self.w.close)
        self.dev, self.s, self.p = phone(self.w)
        self.pk = Passkeys(self.w.store, ORIGIN)

    def key(self, aaguid: str = GOOGLE_PM):
        return self.SoftKey(ORIGIN, RP_ID, aaguid=aaguid)

    def add(self, key):
        return self.pk.register(self.p, key.create(self.pk.register_options(self.p, on_phone=True)),
                                "Fingerprint on this phone", on_phone=True)

    def test_the_phone_key_asks_for_the_phone_itself_with_its_lock(self):
        opts = self.pk.register_options(self.p, on_phone=True)
        sel = opts["authenticatorSelection"]
        self.assertEqual((sel["authenticatorAttachment"], sel["userVerification"]), ("platform", "required"))
        self.assertEqual(opts["hints"], ["client-device"])
        # The phone's passkey app holds its passkey; naming it would refuse the phone's own key.
        self.pk.register(self.p, self.SoftKey(ORIGIN, RP_ID).create(self.pk.register_options(self.p)), "Bitwarden")
        self.assertEqual(self.pk.register_options(self.p, on_phone=True).get("excludeCredentials", []), [])
        plain = self.pk.register_options(self.p)
        self.assertEqual(len(plain["excludeCredentials"]), 1)
        self.assertNotIn("authenticatorAttachment", plain["authenticatorSelection"])
        self.assertEqual(plain["authenticatorSelection"]["residentKey"], "required")
        self.assertNotIn("hints", plain)

    def test_a_desktop_keeps_its_passkey(self):
        _, _, p = desktop(self.w)
        with self.assertRaises(CoreError) as cm:
            self.pk.register_options(p, on_phone=True)
        self.assertEqual((cm.exception.status, cm.exception.reason), (403, "not_a_phone"))
        self.pk.register_options(p)  # the passkey is still offered there

    def test_a_sign_in_names_the_phone_key_and_is_checked_as_any_passkey(self):
        key = self.key()
        added = self.add(key)
        self.assertEqual(added["credential_id"], passkey.b64url(key.credential_id))
        opts = self.pk.sign_in_options(self.w.ws, added["credential_id"])
        self.assertEqual([c["id"] for c in opts["allowCredentials"]], [added["credential_id"]])
        self.assertEqual(opts["allowCredentials"][0]["transports"], ["internal"])
        self.assertEqual((opts["hints"], opts["userVerification"]), (["client-device"], "required"))
        dev, s = self.pk.sign_in(self.w.ws, key.get(opts))
        p = self.w.core.auth.authenticate(s.token)
        self.assertEqual((dev, p.fresh, p.method), (self.dev, True, "passkey"))

    def test_without_the_phone_key_any_passkey_may_answer(self):
        opts = self.pk.sign_in_options(self.w.ws)
        self.assertEqual(opts.get("allowCredentials", []), [])
        self.assertNotIn("hints", opts)

    def test_no_user_verification_is_refused_for_the_phone_key_too(self):
        key = self.key()
        added = self.add(key)
        key.verified = False
        with self.assertRaises(CoreError) as cm:
            self.pk.sign_in(self.w.ws, key.get(self.pk.sign_in_options(self.w.ws, added["credential_id"])))
        self.assertEqual(cm.exception.reason, "passkey_refused")

    def test_a_removed_or_unknown_phone_key_is_not_named(self):
        key = self.key()
        added = self.add(key)
        for cred in (passkey.b64url(b"\x01" * 16), "not base64 at all!"):
            with self.assertRaises(CoreError) as cm:
                self.pk.sign_in_options(self.w.ws, cred)
            self.assertIn(cm.exception.reason, ("no_phone_key", "bad_credential"))
        p = self.w.core.auth.authenticate(self.s.token)
        self.pk.revoke(p, added["passkey_id"])
        with self.assertRaises(CoreError) as cm:
            self.pk.sign_in_options(self.w.ws, added["credential_id"])
        self.assertEqual((cm.exception.status, cm.exception.reason), (404, "no_phone_key"))

    def test_a_revoked_phone_is_not_named(self):
        added = self.add(self.key())
        self.w.core.auth.revoke_device(self.p, self.dev)
        with self.assertRaises(CoreError) as cm:
            self.pk.sign_in_options(self.w.ws, added["credential_id"])
        self.assertEqual(cm.exception.reason, "no_phone_key")

    def test_a_desktop_passkey_is_never_named_as_a_phone_key(self):
        _, _, p = desktop(self.w)
        key = self.SoftKey(ORIGIN, RP_ID)
        added = self.pk.register(p, key.create(self.pk.register_options(p)), "One")
        with self.assertRaises(CoreError) as cm:
            self.pk.sign_in_options(self.w.ws, added["credential_id"])
        self.assertEqual(cm.exception.reason, "no_phone_key")


    # 8 Nov: the fingerprint button opened Bitwarden. The key added on 2 Nov had been saved in
    # Bitwarden, and the core took it as the phone's own without looking at which app made it.

    def test_a_phone_key_saved_in_a_passkey_app_is_kept_as_a_passkey_not_the_fingerprint(self):
        # 9 Nov: refusing such a key left it in the passkey app (which may keep it in place of the key it had),
        # and the phone came to hold no key the core took. It is kept, but never as the phone's own key.
        own = self.add(self.key())
        for aaguid, name in ((BITWARDEN, "Bitwarden"), (passkey.NO_AAGUID, "did not say its name"),
                             ("12345678-1234-1234-1234-123456789abc", "a passkey app")):
            key = self.key(aaguid)
            out = self.add(key)
            self.assertFalse(out["phone_key"])
            self.assertIn(name, out["maker"])
            row = self.w.store.db.execute("SELECT phone_key, label, revoked_at FROM passkeys WHERE id = ?",
                                          (out["passkey_id"],)).fetchone()
            self.assertEqual((row["phone_key"], row["revoked_at"]), (0, None))
            self.assertIn(name, row["label"])
            # Listed as kept from a fingerprint try, so the Decisions list stops offering it on this phone.
            listed = next(k for k in self.pk.list(self.p) if k["id"] == out["passkey_id"])
            self.assertEqual((listed["fingerprint_try"], listed["phone_key"], listed["device_id"]),
                             (True, False, self.p.device_id))
            # It signs in, and the phone's own key is left as it was.
            self.pk.sign_in(self.w.ws, key.get(self.pk.sign_in_options(self.w.ws)))
        mine = self.w.store.db.execute("SELECT phone_key, revoked_at FROM passkeys WHERE id = ?",
                                       (own["passkey_id"],)).fetchone()
        self.assertEqual((mine["phone_key"], mine["revoked_at"]), (1, None))

    def test_a_plain_passkey_from_a_passkey_app_is_still_taken(self):
        key = self.key(BITWARDEN)
        added = self.pk.register(self.p, key.create(self.pk.register_options(self.p)), "Bitwarden")
        listed = {k["id"]: k for k in self.pk.list(self.p)}
        self.assertEqual((listed[added["passkey_id"]]["maker"], listed[added["passkey_id"]]["phone_key"]),
                         ("Bitwarden", False))
        # and it is never named as this phone's fingerprint
        with self.assertRaises(CoreError) as cm:
            self.pk.sign_in_options(self.w.ws, added["credential_id"])
        self.assertEqual(cm.exception.reason, "no_phone_key")

    def test_the_phone_key_records_its_maker(self):
        added = self.add(self.key())
        row = self.w.store.db.execute("SELECT aaguid, phone_key FROM passkeys WHERE id = ?",
                                      (added["passkey_id"],)).fetchone()
        self.assertEqual(tuple(row), (GOOGLE_PM, 1))
        listed = {k["id"]: k for k in self.pk.list(self.p)}[added["passkey_id"]]
        self.assertEqual((listed["maker"], listed["phone_key"]), ("Google Password Manager", True))
        body = json.loads(self.w.store.db.execute(
            "SELECT body_json FROM events WHERE kind = 'passkey.added' AND subject = ?", (added["passkey_id"],)).fetchone()[0])
        self.assertEqual(body["maker"], "Google Password Manager")

    def test_a_phone_key_of_unknown_maker_is_not_asked_for_and_says_why(self):
        key = self.key()
        added = self.add(key)
        # As the key made on 2 Nov stands after migration 15: a phone key with no recorded maker.
        self.w.store.db.execute("UPDATE passkeys SET aaguid = NULL WHERE id = ?", (added["passkey_id"],))
        with self.assertRaises(CoreError) as cm:
            self.pk.sign_in_options(self.w.ws, added["credential_id"])
        self.assertEqual((cm.exception.status, cm.exception.reason), (409, "phone_key_in_app"))
        # the passkey way in still works, and the same key may answer it there
        opts = self.pk.sign_in_options(self.w.ws)
        self.assertEqual(self.pk.sign_in(self.w.ws, key.get(opts))[0], self.dev)

    def test_a_new_phone_key_retires_the_old_one_and_leaves_the_passkeys(self):
        plain = self.pk.register(self.p, self.key(BITWARDEN).create(self.pk.register_options(self.p)), "Bitwarden")
        first = self.add(self.key())
        second = self.add(self.key())
        live = {k["id"] for k in self.pk.list(self.p)}
        self.assertEqual(live, {plain["passkey_id"], second["passkey_id"]})
        body = json.loads(self.w.store.db.execute(
            "SELECT body_json FROM events WHERE kind = 'passkey.revoked' AND subject = ?", (first["passkey_id"],)).fetchone()[0])
        self.assertEqual(body["replaced_by"], second["passkey_id"])
        with self.assertRaises(CoreError) as cm:
            self.pk.sign_in_options(self.w.ws, first["credential_id"])
        self.assertEqual(cm.exception.reason, "no_phone_key")

    def test_a_desktop_cannot_register_a_phone_key(self):
        _, _, p = desktop(self.w)
        opts = self.pk.register_options(p)
        with self.assertRaises(CoreError) as cm:
            self.pk.register(p, self.key().create(opts), "Fingerprint on this phone", on_phone=True)
        self.assertEqual((cm.exception.status, cm.exception.reason), (403, "not_a_phone"))


class PhoneKeyMigration(unittest.TestCase):
    def test_schema_15_marks_the_phone_keys_the_app_made_before_it(self):
        con = sqlite3.connect(":memory:", isolation_level=None)
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        from thornswood_decisions.store import _statements
        for m in range(1, 15):
            for stmt in _statements(MIGRATIONS[m - 1]):
                con.execute(stmt)
        cols = [r[1] for r in con.execute("PRAGMA table_info(passkeys)")]
        self.assertNotIn("aaguid", cols)
        con.execute("PRAGMA foreign_keys = OFF")
        for i, label in enumerate(("passkey", "Fingerprint on this phone", "One")):
            con.execute("INSERT INTO passkeys(id, workspace_id, principal_id, device_id, credential_id, public_key, "
                        "label, created_at) VALUES(?, 'ws', 'pr', 'dv', ?, x'00', ?, 'now')", (f"pk{i}", f"c{i}", label))
        for stmt in _statements(MIGRATIONS[14]):
            con.execute(stmt)
        rows = dict(con.execute("SELECT label, phone_key FROM passkeys").fetchall())
        self.assertEqual(rows, {"passkey": 0, "Fingerprint on this phone": 1, "One": 0})
        self.assertEqual(con.execute("SELECT count(*) FROM passkeys WHERE aaguid IS NOT NULL").fetchone()[0], 0)


    def test_schema_18_marks_the_keys_kept_from_a_fingerprint_try(self):
        con = sqlite3.connect(":memory:", isolation_level=None)
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        from thornswood_decisions.store import _statements
        for m in range(1, 18):
            for stmt in _statements(MIGRATIONS[m - 1]):
                con.execute(stmt)
        con.execute("PRAGMA foreign_keys = OFF")
        for i, (label, phone) in enumerate((("passkey", 0), ("Fingerprint on this phone", 1),
                                            ("Passkey in Bitwarden", 0), ("This phone", 0))):
            con.execute("INSERT INTO passkeys(id, workspace_id, principal_id, device_id, credential_id, public_key, "
                        "label, created_at, phone_key) VALUES(?, 'ws', 'pr', 'dv', ?, x'00', ?, 'now', ?)",
                        (f"pk{i}", f"c{i}", label, phone))
        for stmt in _statements(MIGRATIONS[17]):
            con.execute(stmt)
        rows = dict(con.execute("SELECT label, fingerprint_try FROM passkeys").fetchall())
        self.assertEqual(rows, {"passkey": 0, "Fingerprint on this phone": 0, "Passkey in Bitwarden": 1, "This phone": 0})


class PasskeyWithoutLibraryOrOrigin(unittest.TestCase):
    def test_no_https_origin_means_no_passkeys(self):
        w = World()
        self.addCleanup(w.close)
        for origin in (None, "http://hermes.example.ts.net"):
            with self.assertRaises(CoreError) as cm:
                Passkeys(w.store, origin).sign_in_options(w.ws)
            self.assertEqual(cm.exception.reason, "no_origin")

    def test_no_library_is_503_not_a_weaker_sign_in(self):
        w = World()
        self.addCleanup(w.close)
        with mock.patch.dict(sys.modules, {"webauthn": None}):
            with self.assertRaises(CoreError) as cm:
                Passkeys(w.store, ORIGIN).sign_in_options(w.ws)
        self.assertEqual((cm.exception.status, cm.exception.reason), (503, "passkey_unavailable"))


class AppRoutes(unittest.TestCase):
    def test_csrf_rotates_with_the_cookie_and_the_exact_origin(self):
        s = Served(self)
        cookie, paired = s.phone()
        status, _, body = s.call("POST", "/csrf", headers={"Cookie": cookie})
        self.assertEqual((status, body["error"]), (403, "origin"))
        status, _, body = s.call("POST", "/csrf", headers={"Cookie": cookie, "Origin": ORIGIN})
        self.assertEqual(status, 200)
        new = body["csrf"]
        old_h = {"Cookie": cookie, "Origin": ORIGIN, "X-CSRF-Token": paired["csrf"]}
        status, _, body = s.call("POST", "/sign-out", headers=old_h)
        self.assertEqual((status, body["error"]), (403, "csrf"))
        self.assertEqual(s.call("POST", "/sign-out", headers={**old_h, "X-CSRF-Token": new})[0], 200)

    def test_csrf_rotation_is_only_for_the_app(self):
        s = Served(self)
        d = s.desktop()
        status, _, body = s.call("POST", "/csrf", headers={"Authorization": f"Bearer {d['token']}",
                                                           "X-TW-Hermes-Principal": "hermes-subject:patrick"})
        self.assertEqual((status, body["error"]), (400, "not_the_app"))

    def test_a_recovery_code_signs_in_as_a_new_phone(self):
        s = Served(self)
        p = s.w.patrick(fresh=True)
        codes = s.w.core.auth.new_recovery_codes(p)
        status, headers, body = s.call("POST", "/recovery-sign-in", headers={"Origin": ORIGIN},
                                       body={"code": codes[0], "label": "New phone"})
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        cookie = headers["Set-Cookie"].split(";")[0]
        status, _, me = s.call("GET", "/me", headers={"Cookie": cookie})
        self.assertEqual((me["device_kind"], me["fresh"]), ("phone", True))
        status, _, body = s.call("POST", "/recovery-sign-in", headers={"Origin": ORIGIN}, body={"code": codes[0]})
        self.assertEqual((status, body["error"]), (401, "wrong_code"))

    def test_with_mobile_read_off_the_phone_signs_in_but_reads_nothing(self):
        s = Served(self)
        cookie, _ = s.phone()
        self.assertEqual(s.call("GET", "/me", headers={"Cookie": cookie})[0], 200)
        for path in ("/cards", "/work", "/room", "/changes?since=0"):
            status, _, body = s.call("GET", path, headers={"Cookie": cookie})
            self.assertEqual((status, body["error"]), (423, "mobile_read_off"), path)

    @NEEDS_LIB
    def test_the_phone_key_routes(self):
        s = Served(self)
        cookie, paired = s.phone()
        h = {"Cookie": cookie, "Origin": ORIGIN, "X-CSRF-Token": paired["csrf"]}
        status, _, body = s.call("POST", "/passkeys/register/options", headers=h, body={"on_phone": True})
        self.assertEqual((status, body["authenticatorSelection"]["authenticatorAttachment"]), (200, "platform"))
        if HAVE_PASSKEY_LIB:
            # The app's register sends on_phone; a key saved in Bitwarden is kept as a passkey, with its maker.
            from thornswood_decisions.softkey import SoftKey
            cred = SoftKey(ORIGIN, RP_ID, aaguid=BITWARDEN).create(body)
            status, _, out = s.call("POST", "/passkeys/register", headers=h,
                                    body={"credential": cred, "label": "Fingerprint on this phone", "on_phone": True})
            self.assertEqual((status, out["phone_key"], out["maker"]), (200, False, "Bitwarden"))
        status, _, body = s.call("POST", "/passkeys/sign-in/options", headers={"Origin": ORIGIN}, body={"phone_key": 5})
        self.assertEqual((status, body["error"]), (400, "bad_field"))
        status, _, body = s.call("POST", "/passkeys/sign-in/options", headers={"Origin": ORIGIN},
                                 body={"phone_key": passkey.b64url(b"\x02" * 16)})
        self.assertEqual((status, body["error"]), (404, "no_phone_key"))

    def test_the_sign_in_screen_asks_only_whether_a_passkey_exists(self):
        s = Served(self)
        status, _, body = s.call("GET", "/sign-in/state")
        self.assertEqual((status, body), (200, {"passkeys": False}))
        s.w.store.db.execute("INSERT INTO passkeys(id, workspace_id, principal_id, credential_id, public_key, label, "
                             "created_at) VALUES('pk1', ?, ?, 'cred', x'00', 'Pixel', 'now')", (s.w.ws, s.w.patrick_id))
        status, _, body = s.call("GET", "/sign-in/state")
        self.assertEqual((status, body), (200, {"passkeys": True}))
        s.w.store.db.execute("UPDATE passkeys SET revoked_at = 'now'")
        self.assertEqual(s.call("GET", "/sign-in/state")[2], {"passkeys": False})

    def test_passkey_routes_need_the_exact_origin(self):
        s = Served(self)
        for path in ("/passkeys/sign-in/options", "/passkeys/sign-in", "/passkeys/sign-in/failed", "/recovery-sign-in"):
            status, _, body = s.call("POST", path, headers={"Origin": "https://evil.example"})
            self.assertEqual((status, body["error"]), (403, "origin"), path)
            status, _, body = s.call("POST", path)
            self.assertEqual((status, body["error"]), (403, "origin"), path)


class AppFiles(unittest.TestCase):
    def setUp(self):
        self.s = Served(self)

    def raw(self, method: str, path: str):
        import http.client
        con = http.client.HTTPConnection("127.0.0.1", self.s.port, timeout=10)
        self.addCleanup(con.close)
        con.request(method, path)
        resp = con.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()

    def test_the_shell_and_its_deep_links(self):
        # Decisions, Board and Rooms, and the tabs' first names, which old links still use.
        for path in ("/m/", "/m/decisions", "/m/board", "/m/rooms", "/m/rooms/00000000-0000-4000-8000-000000000001",
                     "/m/needs-you", "/m/work", "/m/room", "/m/decisions/dec_abc-123", "/m/hermes/one",
                     "/m/hermes/me/s_plan", "/m/hidden/one", "/m/hidden/me"):
            status, headers, body = self.raw("GET", path)
            self.assertEqual(status, 200, path)
            self.assertIn(b'<script src="/m/app.js" defer>', body)
            self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
            self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])

    def test_only_the_app_files(self):
        for path in ("/m/../thornswood_decisions/store.py", "/m/app.py", "/m/decisions/a/b", "/m/%2e%2e/x",
                     "/m/hidden/bots", "/m/hidden/one/s_plan"):
            self.assertEqual(self.raw("GET", path)[0], 404, path)
        self.assertEqual(self.raw("POST", "/m/")[0], 405)

    def test_types_and_the_worker_policy(self):
        _, h, _ = self.raw("GET", "/m/app.js")
        self.assertTrue(h["Content-Type"].startswith("text/javascript"))
        _, h, body = self.raw("GET", "/m/sw.js")
        self.assertEqual(h["Content-Security-Policy"], "default-src 'none'; connect-src 'self'; img-src 'self'")
        files = body.split(b"const FILES = [", 1)[1].split(b"]", 1)[0]
        self.assertNotIn(b"/api/", files, "the worker keeps the shell only")
        self.assertIn(b"!url.pathname.startsWith('/m/')", body, "the worker leaves the API to the network")
        _, h, body = self.raw("GET", "/m/manifest.webmanifest")
        m = json.loads(body)
        self.assertEqual((m["scope"], m["display"]), ("/m/", "standalone"))
        self.assertTrue({"192x192", "512x512"} <= {i["sizes"] for i in m["icons"]})
        status, h, _ = self.raw("GET", "/")
        self.assertEqual((status, h["Location"]), (302, "/m/"))

    def test_the_app_never_writes_markup_from_the_core(self):
        js = (Path(__file__).resolve().parent.parent / "src" / "thornswood_decisions" / "app" / "app.js").read_text(encoding="utf-8")
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(bad, js)
        # What this browser's storage keeps, each under its one key: the phone's copy of the chats it
        # has shown (Patrick, 2 Nov: every open waited for One), deleted at sign-out; the public id of
        # this phone's fingerprint key, which its sign-in names (an id the core sends in the open,
        # never the private key, which never leaves the phone's authenticator); and whether he put
        # the fingerprint offer off (cleared when the core no longer has the phone's key, so it is offered
        # again); and whether "Hey Hermes" is on for this phone (2 Nov). Never a token, a
        # private key or the CSRF value.
        uses = [ln.strip() for ln in js.splitlines() if "localStorage" in ln]
        self.assertEqual(len(uses), 12, uses)
        for ln in uses:
            self.assertRegex(ln, r"localStorage\.(getItem|setItem|removeItem)\((KEPT|PHONE_KEY|FINGERPRINT_LATER|WAKE_KEY)\b", ln)
        self.assertIn("localStorage.setItem(PHONE_KEY, JSON.stringify(k))", js)
        self.assertIn("keepPhoneKey({ credential_id: done.body.credential_id, passkey_id: done.body.passkey_id })", js)
        self.assertIn("forgetKept()\n      csrf = null", js)


class FeedsIn(unittest.TestCase):
    def setUp(self):
        self.s = Served(self)
        self.w = self.s.w
        self.w.core.bind_room(self.w.ws, ROOM, "Thornswood Workbench", by="tests")
        for bot in BOTS:
            self.w.core.bind_bot(self.w.ws, bot, bot, by="tests")
        f = Feeds(self.w.store)
        self.tokens = {n: f.new_token(self.w.ws, n, by="tests") for n in ("work", "room")}

    def put(self, name, body, token=None, headers=None):
        h = {"Authorization": f"Bearer {token or self.tokens[name]}", **(headers or {})}
        return self.s.call("POST", f"/feeds/{name}", headers=h, body=body)

    def test_a_snapshot_replaces_the_last_and_keeps_only_what_is_shown(self):
        body = {"source_time": "2026-11-01T18:00:00+00:00", "secret": "x",
                "doing": [{"bot": "rift", "title": "A" * 500, "state": "tool", "tool": "terminal",
                           "since": "2026-11-01T17:58:00+00:00", "platform": "cron", "session_id": "s1",
                           "args": "private"}]}
        self.assertEqual(self.put("work", body)[0], 200)
        snap = Feeds(self.w.store).get(self.w.ws, "work")
        self.assertEqual(snap["as_of"], "2026-11-01T18:00:00+00:00")
        row = snap["work"]["doing"][0]
        self.assertEqual(set(row), {"bot", "title", "state", "tool", "since", "platform", "session_id"})
        self.assertEqual((len(row["title"]), row["state"], row["tool"], row["session_id"]), (200, "tool", "terminal", "s1"))
        self.assertNotIn("secret", snap["work"])
        self.put("work", {"source_time": "2026-11-01T18:01:00+00:00", "doing": []})
        self.assertEqual(Feeds(self.w.store).get(self.w.ws, "work")["work"]["doing"], [])

    def test_the_board_keeps_only_its_shapes(self):
        odd = {"bot": "echo", "state": "tool", "tool": "<b>x</b>", "since": 5}
        self.assertEqual(self.put("work", {"source_time": "2026-11-01T18:00:00+00:00", "doing": [odd]})[0], 200)
        row = Feeds(self.w.store).get(self.w.ws, "work")["work"]["doing"][0]
        # A tool name not in the shape Hermes uses is dropped, and the row says thinking.
        self.assertEqual((row["tool"], row["state"], row["since"]), ("", "thinking", "5"))
        for bad in ({"source_time": "2026-11-01T18:00:00+00:00"},
                    {"source_time": "2026-11-01T18:00:00+00:00", "doing": [odd] * 61},
                    {"source_time": "2026-11-01T18:00:00+00:00", "doing": ["rift"]},
                    {"source_time": "2026-11-01T18:00:00+00:00", "tasks": []}):
            self.assertEqual(self.put("work", bad)[2]["error"], "bad_feed", bad)
        # Only the workspace's own Bots; one Bot may run several turns at once.
        status, _, out = self.put("work", {"source_time": "2026-11-01T18:00:00+00:00",
                                           "doing": [{"bot": "patrick", "state": "thinking"}]})
        self.assertEqual((status, out["error"]), (409, "not_the_bots"))
        two = {"source_time": "2026-11-01T18:00:00+00:00", "doing": [{"bot": "rift", "state": "thinking"}] * 2}
        self.assertEqual(self.put("work", two)[0], 200)

    def test_the_board_says_whether_it_is_live(self):
        self.put("work", {"source_time": "2026-11-01T18:00:00+00:00", "doing": []})
        self.assertTrue(Feeds(self.w.store).get(self.w.ws, "work")["live"])
        later = self.w.store.clock() + timedelta(seconds=91)
        with mock.patch.object(self.w.store, "clock", lambda: later):
            self.assertFalse(Feeds(self.w.store).get(self.w.ws, "work")["live"])
        self.assertNotIn("live", Feeds(self.w.store).get(self.w.ws, "room"))

    def test_the_wrong_token_or_a_browser_is_refused(self):
        body = {"source_time": "2026-11-01T18:00:00+00:00", "doing": []}
        self.assertEqual(self.put("work", body, token=self.tokens["room"])[2]["error"], "bad_feed_token")
        status, _, out = self.put("work", body, headers={"Origin": ORIGIN})
        self.assertEqual((status, out["error"]), (403, "not_a_feed"))
        status, _, out = self.put("work", body, headers={"Cookie": "tw_session=x"})
        self.assertEqual((status, out["error"]), (403, "not_a_feed"))

    def test_the_room_must_be_the_bound_one(self):
        body = {"source_time": "2026-11-01T18:00:00+00:00", "room_id": "another", "events": []}
        status, _, out = self.put("room", body)
        self.assertEqual((status, out["error"]), (409, "not_the_room"))

    def test_sizes(self):
        big = {"source_time": "2026-11-01T18:00:00+00:00", "room_id": ROOM,
               "events": [{"seq": i + 1, "text": "x" * 1900} for i in range(150)]}
        self.assertEqual(self.put("room", big)[0], 200)  # about 290 KB: over the 64 KB limit for everything else
        self.assertEqual(self.s.call("POST", "/pair", body={"code": "x" * 70000})[0], 413)

    def test_the_views_read_them(self):
        self.put("room", {"source_time": "2026-11-01T18:00:00+00:00", "room_id": ROOM,
                          "events": [{"seq": 4, "kind": "message.member", "actor": "Bramble", "text": "hi", "at": ""}]})
        d = self.s.desktop()
        status, _, body = self.s.call("GET", "/room", headers={"Authorization": f"Bearer {d['token']}",
                                                               "X-TW-Hermes-Principal": "hermes-subject:patrick"})
        self.assertEqual((status, body["room"]["rooms"][0]["events"][0]["text"]), (200, "hi"))


class FeedReader(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        r = sqlite3.connect(self.dir / "shared-state.db")
        r.execute("CREATE TABLE hosted_room_events (room_id TEXT, seq INTEGER, event_id TEXT, kind TEXT, "
                  "actor_json TEXT, authority_epoch INTEGER, payload_json TEXT, created_at REAL)")
        r.executemany("INSERT INTO hosted_room_events VALUES(?,?,?,?,?,?,?,?)", [
            (ROOM, 1, "e1", "message.member", json.dumps({"kind": "member", "id": "b", "display_name": "Bramble"}),
             1, json.dumps({"text": "Starting #12"}), 1761998400.0),
            (ROOM, 2, "e2", "turn.started", json.dumps({"kind": "gateway", "id": "g"}), 1, "{}", 1761998401.0),
            ("other", 1, "e3", "message.user", json.dumps({"kind": "user", "id": "p"}), 1,
             json.dumps({"text": "elsewhere"}), 1761998402.0)])
        r.commit()
        r.close()

    def test_snapshots_hold_only_what_the_views_show(self):
        from datetime import datetime, timezone
        now = datetime(2026, 11, 1, 18, 0, tzinfo=timezone.utc)
        room = feed_reader.room_snapshot(self.dir / "shared-state.db", ROOM, now)
        self.assertEqual([(e["seq"], e["actor"], e["text"]) for e in room["events"]], [(1, "Bramble", "Starting #12")])
        both = feed_reader.rooms_snapshot(self.dir / "shared-state.db", [ROOM, "other"], now)
        self.assertEqual([(r["room_id"], [e["text"] for e in r["events"]]) for r in both["rooms"]],
                         [(ROOM, ["Starting #12"]), ("other", ["elsewhere"])])

    def test_it_never_writes_to_hermes(self):
        before = {p.name: p.read_bytes() for p in self.dir.iterdir()}
        from datetime import datetime, timezone
        feed_reader.room_snapshot(self.dir / "shared-state.db", ROOM, datetime.now(timezone.utc))
        self.assertEqual({p.name: p.read_bytes() for p in self.dir.iterdir() if p.name in before}, before)

    @unittest.skipIf(os.name == "nt", "file modes are POSIX: the feed reader runs on the VM and checks its tokens are 0600")
    def test_end_to_end_into_the_core(self):
        s = Served(self)
        s.w.core.bind_room(s.w.ws, ROOM, "Thornswood Workbench", by="tests")
        for bot in BOTS:
            s.w.core.bind_bot(s.w.ws, bot, bot, by="tests")
        tokens = self.dir / "tokens"
        tokens.mkdir()
        f = Feeds(s.w.store)
        for name in ("room", "bots"):
            path = tokens / f"feed-{name}.token"
            path.write_text(f.new_token(s.w.ws, name, by="tests"), encoding="utf-8")
            os.chmod(path, 0o600)
        out = []
        with mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))):
            code = feed_reader.main(["--room", ROOM, "--hermes-home", str(self.dir), "--tokens", str(tokens),
                                     "--core", f"http://127.0.0.1:{s.port}/api/v1"])
        self.assertEqual(code, 0, out)
        self.assertIsNone(Feeds(s.w.store).get(s.w.ws, "work")["work"])
        self.assertEqual(Feeds(s.w.store).get(s.w.ws, "room")["room"]["rooms"][0]["events"][0]["text"], "Starting #12")

    @unittest.skipIf(os.name == "nt", "file modes are POSIX")
    def test_a_token_others_can_read_is_refused(self):
        path = self.dir / "feed-work.token"
        path.write_text("t", encoding="utf-8")
        os.chmod(path, 0o644)
        with self.assertRaises(SystemExit):
            feed_reader.read_token(path)


if __name__ == "__main__":
    unittest.main()
