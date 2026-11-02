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

    def test_revoking_the_device_stops_its_passkey(self):
        self.enrol()
        self.w.core.auth.revoke_device(self.p, self.dev)
        with self.assertRaises(CoreError) as cm:
            self.pk.sign_in(self.w.ws, self.key.get(self.pk.sign_in_options(self.w.ws)))
        self.assertEqual(cm.exception.reason, "unknown_passkey")

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

    def add(self, key):
        return self.pk.register(self.p, key.create(self.pk.register_options(self.p, on_phone=True)),
                                "Fingerprint on this phone")

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
        key = self.SoftKey(ORIGIN, RP_ID)
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
        key = self.SoftKey(ORIGIN, RP_ID)
        added = self.add(key)
        key.verified = False
        with self.assertRaises(CoreError) as cm:
            self.pk.sign_in(self.w.ws, key.get(self.pk.sign_in_options(self.w.ws, added["credential_id"])))
        self.assertEqual(cm.exception.reason, "passkey_refused")

    def test_a_removed_or_unknown_phone_key_is_not_named(self):
        key = self.SoftKey(ORIGIN, RP_ID)
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
        added = self.add(self.SoftKey(ORIGIN, RP_ID))
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
        for path in ("/passkeys/sign-in/options", "/passkeys/sign-in", "/recovery-sign-in"):
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
        js = (Path(__file__).resolve().parent.parent / "thornswood_decisions" / "app" / "app.js").read_text(encoding="utf-8")
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(bad, js)
        # What this browser's storage keeps, each under its one key: the phone's copy of the chats it
        # has shown (Patrick, 2 Nov: every open waited for One), deleted at sign-out; the public id of
        # this phone's fingerprint key, which its sign-in names (an id the core sends in the open,
        # never the private key, which never leaves the phone's authenticator); and whether he put
        # the fingerprint offer off. Never a token, a private key or the CSRF value.
        uses = [ln.strip() for ln in js.splitlines() if "localStorage" in ln]
        self.assertEqual(len(uses), 8, uses)
        for ln in uses:
            self.assertRegex(ln, r"localStorage\.(getItem|setItem|removeItem)\((KEPT|PHONE_KEY|FINGERPRINT_LATER)\b", ln)
        self.assertIn("localStorage.setItem(PHONE_KEY, JSON.stringify(k))", js)
        self.assertIn("keepPhoneKey({ credential_id: done.body.credential_id, passkey_id: done.body.passkey_id })", js)
        self.assertIn("forgetKept()\n      csrf = null", js)


class FeedsIn(unittest.TestCase):
    def setUp(self):
        self.s = Served(self)
        self.w = self.s.w
        self.w.core.bind_room(self.w.ws, ROOM, "Thornswood Workbench", by="tests")
        f = Feeds(self.w.store)
        self.tokens = {n: f.new_token(self.w.ws, n, by="tests") for n in ("work", "room")}

    def put(self, name, body, token=None, headers=None):
        h = {"Authorization": f"Bearer {token or self.tokens[name]}", **(headers or {})}
        return self.s.call("POST", f"/feeds/{name}", headers=h, body=body)

    def test_a_snapshot_replaces_the_last_and_keeps_only_what_is_shown(self):
        body = {"source_time": "2026-11-01T18:00:00+00:00", "secret": "x",
                "tasks": [{"id": "t1", "title": "A" * 500, "assignee": "rift", "status": "running", "body": "private",
                           "priority": 3}]}
        self.assertEqual(self.put("work", body)[0], 200)
        snap = Feeds(self.w.store).get(self.w.ws, "work")
        self.assertEqual(snap["as_of"], "2026-11-01T18:00:00+00:00")
        task = snap["work"]["tasks"][0]
        self.assertEqual(set(task), {"id", "title", "assignee", "status", "updated_at", "priority"})
        self.assertEqual(len(task["title"]), 200)
        self.assertNotIn("secret", snap["work"])
        self.put("work", {"source_time": "2026-11-01T18:01:00+00:00", "tasks": []})
        self.assertEqual(Feeds(self.w.store).get(self.w.ws, "work")["work"]["tasks"], [])

    def test_the_bots_github_work_keeps_only_what_the_board_shows(self):
        item = {"repo": "ThornswoodRP/Thornswood", "number": 5033, "title": "T" * 500, "bot": "echo", "stage": "pr_open",
                "pr": 5367, "pr_repo": "ThornswoodRP/Thornswood", "pr_title": "P", "checks": "failing",
                "checks_line": "18 of 20 passed, 2 failed", "auto_merge": True, "updated_at": "2026-11-01T17:30:00Z",
                "body": "private", "url": "javascript:alert(1)"}
        odd = {"repo": "ThornswoodRP/Thornswood-Ops", "number": "12", "title": "x", "bot": "rift", "stage": "shipped",
               "pr": True, "pr_repo": "evil.example/../x", "checks": "green", "auto_merge": "yes"}
        body = {"source_time": "2026-11-01T18:00:00+00:00", "tasks": [], "items": [item, odd]}
        self.assertEqual(self.put("work", body)[0], 200)
        got = Feeds(self.w.store).get(self.w.ws, "work")["work"]["items"]
        self.assertEqual(set(got[0]), {"repo", "number", "title", "bot", "stage", "pr", "pr_repo", "pr_title", "checks",
                                       "checks_line", "auto_merge", "updated_at"})
        self.assertEqual((got[0]["stage"], got[0]["pr"], got[0]["checks"], got[0]["auto_merge"], len(got[0]["title"])),
                         ("pr_open", 5367, "failing", True, 200))
        # Anything not in the shapes the Board knows is cut back to nothing, never passed on.
        self.assertEqual({k: got[1][k] for k in ("number", "stage", "pr", "pr_repo", "checks", "auto_merge")},
                         {"number": 0, "stage": "claimed", "pr": 0, "pr_repo": "", "checks": "", "auto_merge": False})
        for repo in ("javascript:alert(1)", "https://evil.example/x", "a/b/c", ""):
            bad = {"source_time": "2026-11-01T18:00:00+00:00", "tasks": [], "items": [{**item, "repo": repo}]}
            self.assertEqual(self.put("work", bad)[2]["error"], "bad_feed", repo)
        many = {"source_time": "2026-11-01T18:00:00+00:00", "tasks": [], "items": [item] * 201}
        self.assertEqual(self.put("work", many)[2]["error"], "bad_feed")
        # A snapshot from before the GitHub items is still taken, without them.
        self.put("work", {"source_time": "2026-11-01T18:05:00+00:00", "tasks": []})
        self.assertNotIn("items", Feeds(self.w.store).get(self.w.ws, "work")["work"])

    def test_the_wrong_token_or_a_browser_is_refused(self):
        body = {"source_time": "2026-11-01T18:00:00+00:00", "tasks": []}
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
        many = {"source_time": "2026-11-01T18:00:00+00:00", "tasks": [{"id": str(i)} for i in range(501)]}
        self.assertEqual(self.put("work", many)[2]["error"], "bad_feed")
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
        k = sqlite3.connect(self.dir / "kanban.db")
        k.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT, "
                  "status TEXT NOT NULL, priority INTEGER DEFAULT 0, created_at INTEGER NOT NULL, "
                  "started_at INTEGER, completed_at INTEGER)")
        k.executemany("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?)", [
            ("t1", "Fix the log", "private body", "sprocket", "running", 2, 1761998400, 1761998500, None),
            ("t2", "Old", None, None, "archived", 0, 1761990000, None, None)])
        k.commit()
        k.close()
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
        work = feed_reader.work_snapshot(self.dir / "kanban.db", now)
        self.assertEqual([t["id"] for t in work["tasks"]], ["t1"])
        self.assertNotIn("body", work["tasks"][0])
        room = feed_reader.room_snapshot(self.dir / "shared-state.db", ROOM, now)
        self.assertEqual([(e["seq"], e["actor"], e["text"]) for e in room["events"]], [(1, "Bramble", "Starting #12")])
        both = feed_reader.rooms_snapshot(self.dir / "shared-state.db", [ROOM, "other"], now)
        self.assertEqual([(r["room_id"], [e["text"] for e in r["events"]]) for r in both["rooms"]],
                         [(ROOM, ["Starting #12"]), ("other", ["elsewhere"])])

    def test_it_never_writes_to_hermes(self):
        before = {p.name: p.read_bytes() for p in self.dir.iterdir()}
        from datetime import datetime, timezone
        feed_reader.work_snapshot(self.dir / "kanban.db", datetime.now(timezone.utc))
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
        for name in ("work", "room", "bots"):
            path = tokens / f"feed-{name}.token"
            path.write_text(f.new_token(s.w.ws, name, by="tests"), encoding="utf-8")
            os.chmod(path, 0o600)
        out = []
        with mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))):
            with mock.patch.object(feed_reader, "broker_reader", lambda path: lambda repo, p: (
                    {"check_runs": []} if "/check-runs" in p else [])):
                code = feed_reader.main(["--room", ROOM, "--hermes-home", str(self.dir), "--tokens", str(tokens),
                                         "--core", f"http://127.0.0.1:{s.port}/api/v1", "--github", "always"])
        self.assertEqual(code, 0, out)
        self.assertEqual(Feeds(s.w.store).get(s.w.ws, "work")["work"]["items"], [])
        self.assertEqual(Feeds(s.w.store).get(s.w.ws, "room")["room"]["rooms"][0]["events"][0]["text"], "Starting #12")

    @unittest.skipIf(os.name == "nt", "file modes are POSIX")
    def test_a_token_others_can_read_is_refused(self):
        path = self.dir / "feed-work.token"
        path.write_text("t", encoding="utf-8")
        os.chmod(path, 0o644)
        with self.assertRaises(SystemExit):
            feed_reader.read_token(path)


def _pr(number, ref, *, repo="ThornswoodRP/Thornswood", state="open", draft=False, merged_at=None, closed_at=None,
        auto=False, updated="2026-11-01T17:00:00Z", title=None):
    return {"number": number, "title": title or f"PR {number}", "state": state, "draft": draft, "merged_at": merged_at,
            "closed_at": closed_at, "auto_merge": {"merge_method": "squash"} if auto else None, "updated_at": updated,
            "head": {"ref": ref, "sha": f"sha{number}", "repo": {"full_name": repo}}}


def _issue(number, title, updated="2026-11-01T16:00:00Z", pr=False):
    out = {"number": number, "title": title, "updated_at": updated}
    if pr:
        out["pull_request"] = {}
    return out


class GitHubWork(unittest.TestCase):
    """The Board's GitHub half: the Bots' claims and pull requests, read the
    way the guard rails record them (claim labels and <bot>/<issue>-<what>)."""

    NOW = None

    def setUp(self):
        from datetime import datetime, timezone
        from thornswood_guard.policy import Policy
        self.now = datetime(2026, 11, 1, 18, 0, tzinfo=timezone.utc)
        self.policy = Policy.load()
        self.asked = []
        app, ops, fork = "ThornswoodRP/Thornswood", "ThornswoodRP/Thornswood-Ops", "ThornswoodRP/skymp"
        self.answers = {
            f"/repos/{app}/pulls?state=open&per_page=100": [
                _pr(5367, "echo/5033-hub-current-page", auto=True, updated="2026-11-01T17:30:00Z"),
                _pr(5400, "wisp/5343-gamemode-parts", draft=True),
                _pr(5401, "rift/77-not-claimed", title="Rift's own fix"),
                _pr(5434, "claude/project-thread-wdia3e"),                       # not a Bot's branch
                _pr(5435, "magpie/5191-tables", repo="someone/Thornswood"),      # a fork's branch, not the Bot's
                _pr(5436, "sprocket/BAD-name")],                                 # not the Bot branch pattern
            f"/repos/{app}/pulls?state=closed&sort=updated&direction=desc&per_page=50": [
                _pr(5300, "echo/5033-hub-first-try", state="closed", closed_at="2026-11-01T10:00:00Z"),
                _pr(5299, "magpie/5291-push-checks", state="closed", merged_at="2026-11-01T12:00:00Z",
                    closed_at="2026-11-01T12:00:00Z", updated="2026-11-01T12:00:00Z"),
                _pr(5100, "magpie/5100-old", state="closed", merged_at="2026-10-30T12:00:00Z",
                    closed_at="2026-10-30T12:00:00Z")],                          # more than a day ago
            f"/repos/{ops}/pulls?state=open&per_page=100": [],
            f"/repos/{ops}/pulls?state=closed&sort=updated&direction=desc&per_page=50": [],
            f"/repos/{fork}/pulls?state=open&per_page=100": [_pr(148, "wisp/5343-engine-side", repo=fork)],
            f"/repos/{fork}/pulls?state=closed&sort=updated&direction=desc&per_page=50": [],
            f"/repos/{app}/commits/sha5367/check-runs?per_page=100": {"check_runs": [
                {"status": "completed", "conclusion": "success"}, {"status": "completed", "conclusion": "skipped"},
                {"status": "completed", "conclusion": "failure"}, {"status": "in_progress", "conclusion": None}]},
            f"/repos/{app}/commits/sha5400/check-runs?per_page=100": {"check_runs": []},
            f"/repos/{app}/commits/sha5401/check-runs?per_page=100": {"check_runs": [
                {"status": "completed", "conclusion": "success"}]},
            f"/repos/{fork}/commits/sha148/check-runs?per_page=100": {"check_runs": [
                {"status": "queued", "conclusion": None}]},
        }
        for repo in (app, ops):
            for bot in BOTS:
                label = "claimed%3A%20" + bot.capitalize()
                self.answers[f"/repos/{repo}/issues?state=open&labels={label}&per_page=100"] = []
        self.answers[f"/repos/{app}/issues?state=open&labels=claimed%3A%20Echo&per_page=100"] = [
            _issue(5033, "The site hub says which page is open"), _issue(5367, "a labelled PR", pr=True)]
        self.answers[f"/repos/{app}/issues?state=open&labels=claimed%3A%20Magpie&per_page=100"] = [
            _issue(5191, "Generated content tables are built from their sources", updated="2026-11-01T17:10:00Z"),
            _issue(5291, "A push runs the parse and writing checks")]
        self.answers[f"/repos/{app}/issues?state=open&labels=claimed%3A%20Wisp&per_page=100"] = [
            _issue(5343, "The gamemode is written as TypeScript parts")]

    def ask(self, repo, path):
        self.asked.append((repo, path))
        self.assertTrue(path.startswith(f"/repos/{repo}/"), path)
        return self.answers[path]

    def work(self):
        return feed_reader.github_work(self.policy, self.ask, self.now)

    def test_each_claim_and_bot_pr_is_one_item_with_its_progress(self):
        got = {(i["bot"], i["number"]): i for i in self.work()}
        echo = got[("echo", 5033)]
        self.assertEqual((echo["stage"], echo["pr"], echo["checks"], echo["checks_line"], echo["auto_merge"]),
                         ("pr_open", 5367, "failing", "2 of 4 passed, 1 failed, 1 running", True))
        self.assertEqual(echo["updated_at"], "2026-11-01T17:30:00Z")      # the PR's, newer than the issue's
        self.assertEqual(echo["title"], "The site hub says which page is open")
        self.assertEqual((got[("magpie", 5191)]["stage"], got[("magpie", 5191)]["pr"]), ("claimed", 0))
        self.assertEqual((got[("magpie", 5291)]["stage"], got[("magpie", 5291)]["pr"]), ("merged", 5299))
        # An open PR beats a draft one; here Wisp's skymp PR is open and names a Thornswood issue.
        wisp = got[("wisp", 5343)]
        self.assertEqual((wisp["stage"], wisp["pr"], wisp["pr_repo"], wisp["checks"]),
                         ("pr_open", 148, "ThornswoodRP/skymp", "running"))
        # A Bot's PR for an issue it has not claimed is its own item.
        rift = got[("rift", 77)]
        self.assertEqual((rift["title"], rift["stage"], rift["pr"], rift["checks"]),
                         ("Rift's own fix", "pr_open", 5401, "passed"))
        # Not a Bot's branch, a fork's branch, a bad name, a labelled PR, a PR closed days ago: none of them.
        self.assertEqual(set(got), {("echo", 5033), ("magpie", 5191), ("magpie", 5291), ("wisp", 5343), ("rift", 77)})

    def test_items_go_in_the_bots_order_then_open_work_first(self):
        order = [(i["bot"], i["number"]) for i in self.work()]
        self.assertEqual(order, [("rift", 77), ("echo", 5033), ("magpie", 5191), ("magpie", 5291), ("wisp", 5343)])

    def test_it_only_reads_and_only_inside_the_three_work_repos(self):
        self.work()
        repos = {r for r, _ in self.asked}
        self.assertEqual(repos, {"ThornswoodRP/Thornswood", "ThornswoodRP/Thornswood-Ops", "ThornswoodRP/skymp"})
        self.assertFalse(any("/issues" in p for r, p in self.asked if r == "ThornswoodRP/skymp"))
        self.assertLessEqual(len(self.asked), 30)

    def test_a_refused_read_sends_nothing(self):
        def ask(repo, path):
            raise RuntimeError("tw-git is not answering")
        kanban = Path(tempfile.mkdtemp()) / "kanban.db"
        k = sqlite3.connect(kanban)
        k.execute("CREATE TABLE tasks (id TEXT, title TEXT, assignee TEXT, status TEXT, priority INTEGER, "
                  "created_at INTEGER, started_at INTEGER, completed_at INTEGER)")
        k.commit()
        k.close()
        with self.assertRaises(feed_reader.GitHubUnread):
            feed_reader.work_with_github(kanban, "/x", self.now, ask=ask, policy=self.policy)
        body = feed_reader.work_with_github(kanban, "/x", self.now, ask=self.ask, policy=self.policy)
        self.assertEqual((body["tasks"], len(body["items"])), ([], 5))

    def test_github_is_read_once_in_each_five_minutes(self):
        from datetime import datetime, timedelta, timezone
        start = datetime(2026, 11, 1, 18, 0, 3, tzinfo=timezone.utc)
        runs = [start + timedelta(seconds=62 * n) for n in range(60)]   # the timer's real pace
        due = [r for r in runs if feed_reader.github_due(r)]
        windows = {int(r.timestamp() // 300) for r in runs}
        self.assertEqual({int(r.timestamp() // 300) for r in due}, windows)  # every window reads it at least once
        self.assertLessEqual(len(due), 2 * len(windows))

    def test_the_feed_reads_through_tw_gits_read_op_as_bramble(self):
        fake = mock.Mock()
        fake.call.return_value = {"body": [1]}
        with mock.patch.dict(sys.modules, {"thornswood_brokers.wire": fake}), \
                mock.patch("thornswood_brokers.wire", fake, create=True):
            ask = feed_reader.broker_reader("/run/tw/git/git.sock")
            self.assertEqual(ask("ThornswoodRP/Thornswood", "/repos/ThornswoodRP/Thornswood/pulls"), [1])
        fake.call.assert_called_once_with("/run/tw/git/git.sock", "read", {
            "bot": "bramble", "repo": "ThornswoodRP/Thornswood", "path": "/repos/ThornswoodRP/Thornswood/pulls"},
            timeout=30)
        self.assertEqual(feed_reader.GIT_SOCKET, self.policy.data["brokers"]["sockets"]["git"]["path"])

    def test_main_skips_the_board_between_reads_and_keeps_the_last_one_on_a_failure(self):
        sent = []
        tokens = Path(tempfile.mkdtemp())
        with mock.patch.object(feed_reader, "send", lambda core, name, token, body: sent.append(name) or 200), \
                mock.patch.object(feed_reader, "read_token", lambda p: "t"), \
                mock.patch.object(feed_reader, "rooms_snapshot", lambda *a: {}), \
                mock.patch.object(feed_reader, "bots_snapshot", lambda *a: {}), \
                mock.patch.object(feed_reader, "github_due", lambda now: False), \
                mock.patch("builtins.print"):
            self.assertEqual(feed_reader.main(["--room", ROOM, "--tokens", str(tokens)]), 0)
            self.assertEqual(sent, ["room", "bots"])
            sent.clear()
            with mock.patch.object(feed_reader, "work_with_github",
                                   mock.Mock(side_effect=feed_reader.GitHubUnread("Refused: no"))):
                self.assertEqual(feed_reader.main(["--room", ROOM, "--tokens", str(tokens), "--github", "always"]), 1)
            self.assertEqual(sent, ["room", "bots"])


if __name__ == "__main__":
    unittest.main()
