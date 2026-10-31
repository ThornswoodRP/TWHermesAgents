"""Step 1 of the decision core: sign-in (07 section 5.1) and the sign-in half of T24.

Sessions are stored only as hashes, expire, can be revoked, and die with their
device. CSRF tokens bind to one session. Recovery codes work once. A pairing
code works once, expires, and too many wrong tries stop all of them for a
while. A Bot never holds a session.
"""

from __future__ import annotations

import unittest

from decisions_helpers import World

from thornswood_decisions.auth import FAILURE_LIMIT, RECOVERY_CODES
from thornswood_decisions.store import CoreError, sha256_hex


class Sessions(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.a = self.w.core.auth

    def test_only_hashes_are_stored(self):
        s = self.a.issue_session(self.w.ws, method="test", device_id=None)
        rows = self.w.store.db.execute("SELECT id_hash, csrf_hash FROM sessions").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(sha256_hex(s.token), sha256_hex(s.csrf))])
        dump = "\n".join(self.w.store.db.iterdump())
        self.assertNotIn(s.token, dump)
        self.assertNotIn(s.csrf, dump)

    def test_authenticate_and_csrf(self):
        s = self.a.issue_session(self.w.ws, method="test", device_id=None)
        p = self.a.authenticate(s.token)
        self.assertEqual((p.kind, p.id), ("patrick", self.w.patrick_id))
        self.assertTrue(p.fresh)
        self.a.check_csrf(p, s.csrf)
        other = self.a.issue_session(self.w.ws, method="test", device_id=None)
        for bad in (None, "", other.csrf, s.token):
            with self.assertRaises(CoreError) as e:
                self.a.check_csrf(p, bad)
            self.assertEqual(e.exception.status, 403)

    def test_unknown_token_is_refused(self):
        for bad in (None, "", "guessed"):
            with self.assertRaises(CoreError) as e:
                self.a.authenticate(bad)
            self.assertEqual(e.exception.status, 401)

    def test_fresh_wears_off_idle_and_absolute_expiry(self):
        s = self.a.issue_session(self.w.ws, method="test", device_id=None)
        self.w.clock.advance(minutes=6)
        self.assertFalse(self.a.authenticate(s.token).fresh)
        self.w.clock.advance(hours=13)
        with self.assertRaises(CoreError) as e:
            self.a.authenticate(s.token)
        self.assertEqual(e.exception.reason, "session_expired")
        s2 = self.a.issue_session(self.w.ws, method="test", device_id=None)
        for _ in range(17):  # used every 11 hours, it still ends at 7 days
            self.w.clock.advance(hours=11)
            try:
                self.a.authenticate(s2.token)
            except CoreError as exc:
                self.assertEqual(exc.reason, "session_expired")
                break
        else:
            self.fail("a session outlived its 7 days")

    def test_revoke_session_and_device(self):
        dev = self.w.device()
        s1 = self.a.issue_session(self.w.ws, method="test", device_id=dev)
        s2 = self.a.issue_session(self.w.ws, method="test", device_id=dev)
        p = self.a.authenticate(s1.token)
        self.a.revoke_session(p)
        with self.assertRaises(CoreError):
            self.a.authenticate(s1.token)
        p2 = self.a.authenticate(s2.token)
        self.a.revoke_device(p2, dev)
        with self.assertRaises(CoreError):
            self.a.authenticate(s2.token)
        with self.assertRaises(CoreError) as e:
            self.a.issue_session(self.w.ws, method="test", device_id=dev)
        self.assertEqual(e.exception.reason, "device_revoked")

    def test_a_bot_never_holds_a_session(self):
        with self.assertRaises(CoreError):
            self.a.ensure_service(self.w.ws, "specialist", "bramble")
        with self.assertRaises(CoreError):
            self.a.ensure_service(self.w.ws, "patrick")
        with self.assertRaises(CoreError) as e:
            self.a.revoke_device(self.w.bramble, "dv_x")
        self.assertEqual(e.exception.reason, "not_patrick")
        with self.assertRaises(CoreError):
            self.a.new_pairing_code(self.w.rift, "desktop", "Rift's own")


class RecoveryCodes(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.a = self.w.core.auth

    def test_codes_work_once_and_are_hashed(self):
        codes = self.a.new_recovery_codes(self.w.patrick(fresh=True))
        self.assertEqual(len(codes), RECOVERY_CODES)
        dump = "\n".join(self.w.store.db.iterdump())
        for c in codes:
            self.assertNotIn(c, dump)
        s = self.a.sign_in_with_recovery_code(self.w.ws, codes[3].lower(), None)
        self.assertEqual(self.a.authenticate(s.token).kind, "patrick")
        with self.assertRaises(CoreError):
            self.a.sign_in_with_recovery_code(self.w.ws, codes[3], None)

    def test_new_codes_replace_the_old_and_need_a_fresh_sign_in(self):
        with self.assertRaises(CoreError):
            self.a.new_recovery_codes(self.w.patrick(fresh=False))
        old = self.a.new_recovery_codes(self.w.patrick(fresh=True))
        self.a.new_recovery_codes(self.w.patrick(fresh=True))
        with self.assertRaises(CoreError):
            self.a.sign_in_with_recovery_code(self.w.ws, old[0], None)

    def test_too_many_wrong_codes_lock_for_a_while(self):
        codes = self.a.new_recovery_codes(self.w.patrick(fresh=True))
        for _ in range(FAILURE_LIMIT):
            with self.assertRaises(CoreError):
                self.a.sign_in_with_recovery_code(self.w.ws, "0000-0000-0000", None)
        with self.assertRaises(CoreError) as e:
            self.a.sign_in_with_recovery_code(self.w.ws, codes[0], None)
        self.assertEqual(e.exception.status, 429)
        self.w.clock.advance(minutes=16)
        self.a.sign_in_with_recovery_code(self.w.ws, codes[0], None)


class Pairing(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.a = self.w.core.auth

    def test_pair_desktop_once(self):
        offer = self.a.new_pairing_code(self.w.patrick(fresh=True), "desktop", "Hermes Desktop on One")
        dev, s = self.a.redeem_pairing_code(self.w.ws, offer["code"])
        p = self.a.authenticate(s.token)
        self.assertEqual(p.device_id, dev)
        kind = self.w.store.db.execute("SELECT kind FROM devices WHERE id = ?", (dev,)).fetchone()[0]
        self.assertEqual(kind, "desktop")
        with self.assertRaises(CoreError):
            self.a.redeem_pairing_code(self.w.ws, offer["code"])

    def test_a_code_expires(self):
        offer = self.a.new_pairing_code(self.w.patrick(fresh=True), "desktop", "d")
        self.w.clock.advance(minutes=11)
        with self.assertRaises(CoreError) as e:
            self.a.redeem_pairing_code(self.w.ws, offer["code"])
        self.assertEqual(e.exception.reason, "wrong_code")

    def test_a_stale_sign_in_cannot_offer_a_code(self):
        with self.assertRaises(CoreError) as e:
            self.a.new_pairing_code(self.w.patrick(fresh=False), "desktop", "d")
        self.assertEqual(e.exception.reason, "sign_in_again")

    def test_guessing_locks_every_code(self):
        offer = self.a.new_pairing_code(self.w.patrick(fresh=True), "phone", "p")
        for i in range(FAILURE_LIMIT):
            with self.assertRaises(CoreError):
                self.a.redeem_pairing_code(self.w.ws, f"ZZZZ-ZZ{i:02d}")
        with self.assertRaises(CoreError) as e:
            self.a.redeem_pairing_code(self.w.ws, offer["code"])
        self.assertEqual(e.exception.status, 429)

    def test_another_workspace_cannot_redeem(self):
        offer = self.a.new_pairing_code(self.w.patrick(fresh=True), "phone", "p")
        other = self.w.store.ensure_workspace("test-2", is_test=True, by="tests")
        self.a.ensure_patrick(other)
        with self.assertRaises(CoreError):
            self.a.redeem_pairing_code(other, offer["code"])


if __name__ == "__main__":
    unittest.main()
