"""Step 5: Web Push, with our own keys and nothing paid.

Checks the encryption against RFC 8291's own example, the VAPID header of RFC
8292, the seal that keeps a subscription unreadable in the core's store, the
push service allowlist, the push outbox (the push_send flag, the recheck at
lease time, a revoked phone and a "no" getting nothing, 404 and 410 retiring a
subscription, Retry-After, the last try, a lapsed lease left uncertain), the
payload carrying nothing private, the push worker against a stand-in push
service, its key files, and the API routes. Everything runs in a scratch test
workspace; nothing reaches a real push service.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

from decisions_helpers import World
from test_decisions_api import ORIGIN, Served, answer_body
from test_decisions_phone import phone

from thornswood_decisions.feeds import Feeds
from thornswood_decisions.store import CoreError

try:
    from cryptography.exceptions import InvalidSignature, InvalidTag
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    from thornswood_decisions import push_worker, webpush
    from thornswood_decisions.push import Push
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

NEEDS_CRYPTO = unittest.skipUnless(HAVE_CRYPTO, "cryptography is not installed here (it is in the core's venv on the VM)")
FCM = "https://fcm.googleapis.com/fcm/send/abc123-test-endpoint"


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    text = "".join(text.split())
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Browser:
    """A phone's browser, as far as Web Push goes: its key pair and auth secret."""

    def __init__(self, endpoint: str = FCM):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = os.urandom(16)
        self.endpoint = endpoint

    def subscription(self) -> dict:
        point = self.key.public_key().public_bytes(serialization.Encoding.X962,
                                                   serialization.PublicFormat.UncompressedPoint)
        return {"endpoint": self.endpoint, "expirationTime": None, "keys": {"p256dh": b64u(point), "auth": b64u(self.auth)}}

    def read(self, body: bytes) -> dict:
        return json.loads(webpush.decrypt(body, self.key, self.auth))


def worker_keys():
    vapid = webpush.new_vapid_key()
    seal = X25519PrivateKey.generate()
    return vapid, seal


def verify_vapid(header: str, endpoint: str) -> dict:
    """Checks a VAPID header the way a push service does, and returns its claims."""
    assert header.startswith("vapid t="), header
    t, k = header[len("vapid t="):].split(", k=")
    head, body, sig = t.split(".")
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64u(k))
    raw = unb64u(sig)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    pub.verify(der, f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))
    assert json.loads(unb64u(head)) == {"typ": "JWT", "alg": "ES256"}
    claims = json.loads(unb64u(body))
    from urllib.parse import urlsplit
    u = urlsplit(endpoint)
    assert claims["aud"] == f"{u.scheme}://{u.hostname}", claims
    return claims


@NEEDS_CRYPTO
class Rfc8291(unittest.TestCase):
    # RFC 8291 section 5 and Appendix A, the "watermelon" example.
    PLAINTEXT = b"When I grow up, I want to be a watermelon"
    AS_PRIVATE = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
    UA_PUBLIC = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
    UA_PRIVATE = "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"
    SALT = "DGv6ra1nlYgDCS1FRnbzlw"
    AUTH = "BTBZMqHH6r4Tts7J_aSIgg"
    HEADER = ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
              "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8")
    CIPHERTEXT = "8pfeW0KbunFT06SuDKoJH9Ql87S1QUrdirN6GcG7sFz1y1sqLgVi1VhjVkHsUoEsbI_0LpXMuGvnzQ"

    def test_the_rfc_example_byte_for_byte(self):
        as_private = ec.derive_private_key(int.from_bytes(unb64u(self.AS_PRIVATE), "big"), ec.SECP256R1())
        body = webpush.encrypt(self.PLAINTEXT, unb64u(self.UA_PUBLIC), unb64u(self.AUTH), salt=unb64u(self.SALT),
                               as_private=as_private)
        self.assertEqual(body, unb64u(self.HEADER) + unb64u(self.CIPHERTEXT))
        ua_private = ec.derive_private_key(int.from_bytes(unb64u(self.UA_PRIVATE), "big"), ec.SECP256R1())
        self.assertEqual(webpush.decrypt(body, ua_private, unb64u(self.AUTH)), self.PLAINTEXT)

    def test_a_fresh_salt_and_key_each_time(self):
        b = Browser()
        sub = b.subscription()
        one = webpush.encrypt(b'{"v":1}', unb64u(sub["keys"]["p256dh"]), b.auth)
        two = webpush.encrypt(b'{"v":1}', unb64u(sub["keys"]["p256dh"]), b.auth)
        self.assertNotEqual(one[:16], two[:16])
        self.assertNotEqual(one[21:86], two[21:86])
        self.assertEqual(b.read(one), {"v": 1})

    def test_refuses_wrong_sized_keys(self):
        for pub, auth in ((b"\x04" + b"x" * 63, os.urandom(16)), (b"\x02" + b"x" * 64, os.urandom(16)),
                          (Browser().key.public_key().public_bytes(serialization.Encoding.X962,
                           serialization.PublicFormat.UncompressedPoint), os.urandom(8))):
            with self.assertRaises(ValueError):
                webpush.encrypt(b"x", pub, auth)


@NEEDS_CRYPTO
class Vapid(unittest.TestCase):
    def test_the_header_verifies_and_names_the_push_service(self):
        key = webpush.new_vapid_key()
        h = webpush.vapid_header(FCM, key, "https://thornswood-bots.example.ts.net", now=1_800_000_000)
        claims = verify_vapid(h, FCM)
        self.assertEqual(claims, {"aud": "https://fcm.googleapis.com", "exp": 1_800_000_000 + 12 * 3600,
                                  "sub": "https://thornswood-bots.example.ts.net"})
        self.assertTrue(h.endswith(", k=" + webpush.vapid_public(key)))
        self.assertEqual(len(webpush.vapid_public(key)), 87)

    def test_another_key_does_not_verify(self):
        h = webpush.vapid_header(FCM, webpush.new_vapid_key(), "https://x.example")
        t = h.split(", k=")[0]
        forged = t + ", k=" + webpush.vapid_public(webpush.new_vapid_key())
        with self.assertRaises(InvalidSignature):
            verify_vapid(forged, FCM)


@NEEDS_CRYPTO
class Seal(unittest.TestCase):
    def test_only_the_worker_opens_a_subscription_and_only_under_its_id(self):
        _, seal = worker_keys()
        pub = seal.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        sub = {"endpoint": FCM, "p256dh": "a", "auth": "b"}
        sealed = webpush.seal(sub, pub, b"ps_one")
        self.assertNotIn(b"fcm.googleapis.com", sealed)
        self.assertEqual(webpush.unseal(sealed, seal, b"ps_one"), sub)
        with self.assertRaises(InvalidTag):
            webpush.unseal(sealed, seal, b"ps_two")
        with self.assertRaises(InvalidTag):
            webpush.unseal(sealed, X25519PrivateKey.generate(), b"ps_one")


class Allowlist(unittest.TestCase):
    def test_only_browser_push_services(self):
        from thornswood_decisions.webpush import push_host_ok
        for ok in (FCM, "https://android.googleapis.com/gcm/send/x",
                   "https://updates.push.services.mozilla.com/wpush/v2/x",
                   "https://wns2-par02p.notify.windows.com/w/?token=x", "https://web.push.apple.com/x",
                   "https://fcm.googleapis.com:443/fcm/send/x"):
            self.assertTrue(push_host_ok(ok), ok)
        for bad in ("http://fcm.googleapis.com/fcm/send/x", "https://fcm.googleapis.com.evil.example/x",
                    "https://evilfcm.googleapis.com/x", "https://user@fcm.googleapis.com/x",
                    "https://fcm.googleapis.com:8443/x", "https://127.0.0.1/x", "https://localhost/x",
                    "https://push.services.mozilla.com.example/x", "https://example.com/push.apple.com",
                    "ftp://fcm.googleapis.com/x", "", "https://[::1]/x", "https://100.100.1.10/x"):
            self.assertFalse(push_host_ok(bad), bad)


@NEEDS_CRYPTO
class Outbox(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.push = Push(self.w.store)
        self.token = Feeds(self.w.store).new_token(self.w.ws, "push", by="tests")
        self.vapid, self.seal = worker_keys()
        self.dev, _, self.p = phone(self.w)

    def hello(self):
        return self.push.hello(self.w.ws, self.token, webpush.vapid_public(self.vapid),
                               push_worker.seal_public(self.seal))

    def on(self):
        self.w.store.set_flag(self.w.ws, "push_send", True, by="tests")

    def subscribed(self, browser=None):
        self.hello()
        b = browser or Browser()
        self.push.subscribe(self.p, b.subscription())
        return b

    def status(self, outbox_id):
        return self.w.store.db.execute("SELECT status, attempts FROM push_outbox WHERE id = ?", (outbox_id,)).fetchone()

    def test_push_send_starts_off_and_nothing_is_leased(self):
        self.subscribed()
        self.w.card()
        self.assertFalse(self.w.store.flag(self.w.ws, "push_send"))
        with self.assertRaises(CoreError) as e:
            self.push.claim(self.w.ws, self.token)
        self.assertEqual((e.exception.status, e.exception.reason), (423, "push_send_off"))
        self.assertEqual(self.w.store.db.execute("SELECT status FROM push_outbox").fetchone()["status"], "queued")

    def test_only_the_push_token(self):
        for bad in (None, "", "x" * 43, Feeds(self.w.store).new_token(self.w.ws, "connector", by="tests")):
            with self.assertRaises(CoreError) as e:
                self.push.claim(self.w.ws, bad)
            self.assertEqual(e.exception.status, 401)

    def test_the_keys_are_said_once(self):
        self.assertEqual(self.hello(), {"set": True})
        self.assertEqual(self.hello(), {"set": False})
        other, _ = worker_keys()
        with self.assertRaises(CoreError) as e:
            self.push.hello(self.w.ws, self.token, webpush.vapid_public(other), push_worker.seal_public(self.seal))
        self.assertEqual((e.exception.status, e.exception.reason), (409, "keys_differ"))
        with self.assertRaises(CoreError):
            self.push.hello(self.w.ws, self.token, "AAAA", push_worker.seal_public(self.seal))

    def test_a_subscription_waits_for_the_worker_and_must_be_a_push_service(self):
        with self.assertRaises(CoreError) as e:
            self.push.subscribe(self.p, Browser().subscription())
        self.assertEqual(e.exception.reason, "push_not_ready")
        self.hello()
        for endpoint in ("https://evil.example/x", "http://fcm.googleapis.com/x", "https://127.0.0.1/x"):
            with self.assertRaises(CoreError) as e:
                self.push.subscribe(self.p, Browser(endpoint).subscription())
            self.assertEqual(e.exception.reason, "bad_endpoint")
        sub = Browser().subscription()
        sub["keys"]["auth"] = b64u(b"short")
        with self.assertRaises(CoreError):
            self.push.subscribe(self.p, sub)
        with self.assertRaises(CoreError):
            self.push.subscribe(self.p, "not a subscription")

    def test_alerts_are_for_a_paired_phone_only(self):
        self.hello()
        desk = self.w.core.auth.operator_pairing_code(self.w.ws, "desktop", "Desk", by="tests")["code"]
        _, s = self.w.core.auth.redeem_pairing_code(self.w.ws, desk, hermes_principal="patrick")
        dp = self.w.core.auth.authenticate(s.token, hermes_principal="patrick")
        for bot in (self.w.bramble, self.w.rift, dp):
            with self.assertRaises(CoreError) as e:
                self.push.subscribe(bot, Browser().subscription())
            self.assertEqual(e.exception.reason, "phone_only")

    def test_the_store_never_holds_a_readable_subscription(self):
        b = self.subscribed()
        self.w.store.db.commit()
        raw = Path(self.w.path).read_bytes() + (Path(str(self.w.path) + "-wal").read_bytes()
                                                if Path(str(self.w.path) + "-wal").exists() else b"")
        self.assertNotIn(b"fcm.googleapis.com", raw)
        self.assertNotIn(b.subscription()["keys"]["auth"].encode(), raw)
        row = self.w.store.db.execute("SELECT id, sealed FROM push_subscriptions").fetchone()
        self.assertEqual(webpush.unseal(bytes(row["sealed"]), self.seal, row["id"].encode())["endpoint"], FCM)

    def test_a_card_is_leased_with_a_payload_that_says_nothing_private(self):
        b = self.subscribed()
        card = self.w.card(title="Delete the secret branch?", body="The repo EvilPatrick06/Secret, token abc.")
        self.on()
        items = self.push.claim(self.w.ws, self.token)["items"]
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(set(it["payload"]), {"v", "event_id", "decision_id", "attention_revision", "path"})
        self.assertEqual(it["payload"]["decision_id"], card["id"])
        self.assertEqual(it["payload"]["path"], "/m/decisions/" + card["id"])
        self.assertEqual(it["payload"]["attention_revision"], card["attention_revision"])
        text = json.dumps(it["payload"])
        for private in ("Delete", "secret", "Secret", "EvilPatrick06", "abc", FCM, self.token):
            self.assertNotIn(private, text)
        self.assertLessEqual(len(it["topic"]), 32)
        self.assertEqual(len(it["subscriptions"]), 1)
        opened = webpush.unseal(base64.b64decode(it["subscriptions"][0]["sealed"]), self.seal,
                                it["subscriptions"][0]["id"].encode())
        self.assertEqual(opened["auth"], b.subscription()["keys"]["auth"])
        self.assertEqual(self.push.claim(self.w.ws, self.token)["items"], [], "a leased push is not leased twice")

    def test_board_findings_never_push(self):
        self.subscribed()
        self.w.card(kind="board_review", source_key="board-1")
        self.assertEqual(self.w.store.db.execute("SELECT count(*) FROM push_outbox").fetchone()[0], 0)

    def test_the_lease_rechecks_the_card(self):
        self.subscribed()
        card = self.w.card()
        with self.w.store.tx() as db:  # an alert for an older attention revision, still queued
            self.w.core._queue_push(db, self.w.ws, card["id"], card["attention_revision"] + 7)
        self.on()
        items = self.push.claim(self.w.ws, self.token)["items"]
        self.assertEqual([i["payload"]["attention_revision"] for i in items], [card["attention_revision"]])
        stale = self.w.store.db.execute("SELECT status, reason FROM push_outbox WHERE attention_revision = ?",
                                        (card["attention_revision"] + 7,)).fetchone()
        self.assertEqual(stale["status"], "cancelled")

    def test_an_answered_card_is_not_pushed(self):
        self.subscribed()
        card = self.w.card()
        self.w.core.answer(self.w.patrick(), **self.w.answer_args(card))
        self.on()
        self.assertEqual(self.push.claim(self.w.ws, self.token)["items"], [])
        self.assertEqual(self.w.store.db.execute("SELECT status FROM push_outbox").fetchone()["status"], "cancelled")

    def test_no_phone_with_alerts_on_means_no_push(self):
        self.hello()
        self.w.card()
        self.on()
        self.assertEqual(self.push.claim(self.w.ws, self.token)["items"], [])
        row = self.w.store.db.execute("SELECT status, reason FROM push_outbox").fetchone()
        self.assertEqual((row["status"], row["reason"]), ("cancelled", "no phone has alerts on"))

    def test_a_no_is_kept_and_gets_nothing(self):
        self.subscribed()
        self.assertEqual(self.push.state(self.p)["consent"], "granted")
        self.push.denied(self.p)
        st = self.push.state(self.p)
        self.assertEqual((st["consent"], st["subscribed"]), ("denied", False))
        self.w.card()
        self.on()
        self.assertEqual(self.push.claim(self.w.ws, self.token)["items"], [])

    def test_a_revoked_phone_gets_nothing(self):
        self.subscribed()
        self.w.core.auth.revoke_device(self.w.patrick(), self.dev)
        row = self.w.store.db.execute("SELECT revoked_at, retired_reason FROM push_subscriptions").fetchone()
        self.assertEqual(row["retired_reason"], "device revoked")
        self.w.card()
        self.on()
        self.assertEqual(self.push.claim(self.w.ws, self.token)["items"], [])

    def test_a_revoked_phone_gets_nothing_even_if_its_subscription_row_were_live(self):
        self.subscribed()
        self.w.core.auth.revoke_device(self.w.patrick(), self.dev)
        with self.w.store.tx() as db:  # the join on devices is the second lock
            db.execute("UPDATE push_subscriptions SET revoked_at = NULL")
        self.w.card()
        self.on()
        self.assertEqual(self.push.claim(self.w.ws, self.token)["items"], [])

    def test_turning_alerts_off(self):
        self.subscribed()
        self.assertTrue(self.push.state(self.p)["subscribed"])
        self.push.unsubscribe(self.p)
        self.assertFalse(self.push.state(self.p)["subscribed"])

    def test_a_new_subscription_replaces_the_old(self):
        self.subscribed()
        self.push.subscribe(self.p, Browser().subscription())
        live = self.w.store.db.execute("SELECT count(*) FROM push_subscriptions WHERE revoked_at IS NULL").fetchone()[0]
        self.assertEqual(live, 1)

    def leased(self):
        self.subscribed()
        self.w.card()
        self.on()
        it = self.push.claim(self.w.ws, self.token)["items"][0]
        return it, it["subscriptions"][0]["id"]

    def test_accepted_is_sent_with_its_receipt(self):
        it, sid = self.leased()
        self.assertEqual(self.push.result(self.w.ws, self.token, it["id"], [{"subscription_id": sid, "status": 201}]),
                         {"id": it["id"], "status": "sent"})
        rec = self.w.store.db.execute("SELECT outcome FROM effect_receipts WHERE outbox_id = ?", (it["id"],)).fetchone()
        self.assertEqual(rec["outcome"], "confirmed")
        again = self.push.result(self.w.ws, self.token, it["id"], [{"subscription_id": sid, "status": 201}])
        self.assertTrue(again["replayed"])

    def test_gone_retires_the_subscription(self):
        for code in (404, 410):
            with self.subTest(code=code):
                self.setUp()
                it, sid = self.leased()
                self.assertEqual(self.push.result(self.w.ws, self.token, it["id"],
                                                  [{"subscription_id": sid, "status": code}])["status"], "dead")
                self.assertFalse(self.push.state(self.p)["subscribed"])

    def test_retry_after_is_waited_out(self):
        it, sid = self.leased()
        out = self.push.result(self.w.ws, self.token, it["id"],
                               [{"subscription_id": sid, "status": 429, "retry_after": 120}])
        self.assertEqual(out["status"], "queued")
        self.assertEqual(self.push.claim(self.w.ws, self.token)["items"], [])
        self.w.clock.advance(seconds=119)
        self.assertEqual(self.push.claim(self.w.ws, self.token)["items"], [])
        self.w.clock.advance(seconds=2)
        again = self.push.claim(self.w.ws, self.token)["items"]
        self.assertEqual([i["id"] for i in again], [it["id"]])
        self.assertEqual(self.status(it["id"])["attempts"], 2)

    def test_a_short_or_huge_retry_after_is_bounded(self):
        it, sid = self.leased()
        self.push.result(self.w.ws, self.token, it["id"], [{"subscription_id": sid, "status": 503, "retry_after": 1}])
        self.w.clock.advance(seconds=30)
        self.assertEqual(self.push.claim(self.w.ws, self.token)["items"], [], "at least a minute")
        self.w.clock.advance(seconds=31)
        it = self.push.claim(self.w.ws, self.token)["items"][0]
        self.push.result(self.w.ws, self.token, it["id"],
                         [{"subscription_id": sid, "status": 503, "retry_after": 10 ** 9}])
        self.w.clock.advance(seconds=3601)
        self.assertEqual(len(self.push.claim(self.w.ws, self.token)["items"]), 1, "at most an hour")

    def test_dead_after_the_last_try(self):
        it, sid = self.leased()
        for n in range(1, 6):
            out = self.push.result(self.w.ws, self.token, it["id"], [{"subscription_id": sid, "status": 503}])
            if n < 5:
                self.assertEqual(out["status"], "queued")
                self.w.clock.advance(seconds=61)
                it = self.push.claim(self.w.ws, self.token)["items"][0]
        self.assertEqual(out["status"], "dead")
        self.assertEqual(self.status(it["id"])["attempts"], 5)
        rec = self.w.store.db.execute("SELECT outcome FROM effect_receipts WHERE outbox_id = ?", (it["id"],)).fetchone()
        self.assertEqual(rec["outcome"], "failed")

    def test_a_lapsed_lease_is_uncertain_and_never_sent_again(self):
        it, _ = self.leased()
        self.w.clock.advance(minutes=3)
        self.assertEqual(self.push.claim(self.w.ws, self.token)["items"], [])
        self.assertEqual(self.status(it["id"])["status"], "uncertain")
        self.w.clock.advance(hours=2)
        self.assertEqual(self.push.claim(self.w.ws, self.token)["items"], [])

    def test_a_bad_result_is_refused(self):
        it, sid = self.leased()
        for bad in (None, [], "201", [201]):
            with self.assertRaises(CoreError):
                self.push.result(self.w.ws, self.token, it["id"], bad)
        with self.assertRaises(CoreError) as e:
            self.push.result(self.w.ws, self.token, "px_nope", [{"subscription_id": sid, "status": 201}])
        self.assertEqual(e.exception.status, 404)

    def test_push_reset_forgets_the_keys_and_every_subscription(self):
        self.subscribed()
        self.push.reset_keys(self.w.ws, by="operator")
        self.assertFalse(self.push.state(self.p)["subscribed"])
        self.assertIsNone(self.push.state(self.p)["vapid_public"])
        self.vapid, self.seal = worker_keys()
        self.assertEqual(self.hello(), {"set": True})


class FakePushService:
    def __init__(self, answer=(201, None)):
        self.sent = []
        self.answer = answer

    def __call__(self, endpoint, body, headers):
        self.sent.append((endpoint, body, headers))
        return self.answer


@NEEDS_CRYPTO
class Worker(unittest.TestCase):
    def setUp(self):
        self.s = Served(self)
        self.w = self.s.w
        self.token = Feeds(self.w.store).new_token(self.w.ws, "push", by="tests")
        self.vapid, self.seal = worker_keys()
        self.service = FakePushService()
        self.said = []
        self.worker = push_worker.Worker(push_worker.Core(f"http://127.0.0.1:{self.s.port}/api/v1", self.token),
                                         self.vapid, self.seal, ORIGIN, send=self.service, say=self.said.append)

    def test_the_whole_way_to_a_phone(self):
        self.worker.hello()
        _, _, p = phone(self.w)
        b = Browser()
        Push(self.w.store).subscribe(p, b.subscription())
        card = self.w.card()
        self.assertEqual(self.worker.once(), -1, "nothing while push_send is off")
        self.assertEqual(self.service.sent, [])
        self.w.store.set_flag(self.w.ws, "push_send", True, by="tests")
        self.assertEqual(self.worker.once(), 1)
        (endpoint, body, headers), = self.service.sent
        self.assertEqual(endpoint, FCM)
        msg = b.read(body)
        self.assertEqual(msg["decision_id"], card["id"])
        self.assertEqual(msg["path"], "/m/decisions/" + card["id"])
        self.assertEqual(headers["Content-Encoding"], "aes128gcm")
        self.assertEqual(headers["TTL"], "86400")
        self.assertEqual(headers["Urgency"], "normal")
        self.assertRegex(headers["Topic"], r"^[A-Za-z0-9_-]{1,32}$")
        self.assertEqual(verify_vapid(headers["Authorization"], FCM)["sub"], ORIGIN)
        self.assertEqual(self.w.store.db.execute("SELECT status FROM push_outbox").fetchone()["status"], "sent")
        log = " ".join(self.said)
        for private in ("fcm", "abc123", b.subscription()["keys"]["auth"], self.token):
            self.assertNotIn(private, log)
        self.assertEqual(self.worker.once(), 0)
        self.assertEqual(len(self.service.sent), 1)

    def test_an_unreachable_push_service_comes_back_later(self):
        self.worker.hello()
        _, _, p = phone(self.w)
        Push(self.w.store).subscribe(p, Browser().subscription())
        self.w.card()
        self.w.store.set_flag(self.w.ws, "push_send", True, by="tests")
        self.service.answer = (0, None)
        self.worker.once()
        self.assertEqual(self.w.store.db.execute("SELECT status FROM push_outbox").fetchone()["status"], "queued")
        self.assertEqual(self.worker.once(), 0, "not before the minute is up")

    def test_other_keys_at_the_core_stop_the_worker(self):
        self.worker.hello()
        other = push_worker.Worker(self.worker.core, webpush.new_vapid_key(), self.seal, ORIGIN, send=self.service)
        with self.assertRaises(SystemExit) as e:
            other.hello()
        self.assertIn("push-reset", str(e.exception))

    def test_a_wrong_token_stops_the_worker(self):
        self.worker.core.token = "not-the-token"
        with self.assertRaises(SystemExit):
            self.worker.hello()

    def test_a_subscription_it_cannot_open_is_retired(self):
        self.worker.hello()
        _, _, p = phone(self.w)
        Push(self.w.store).subscribe(p, Browser().subscription())
        with self.w.store.tx() as db:
            db.execute("UPDATE push_subscriptions SET sealed = ?", (os.urandom(120),))
        self.w.card()
        self.w.store.set_flag(self.w.ws, "push_send", True, by="tests")
        self.worker.once()
        self.assertEqual(self.service.sent, [])
        self.assertFalse(Push(self.w.store).state(p)["subscribed"])

    def test_the_worker_routes_never_take_a_browser(self):
        for headers in ({"Origin": ORIGIN}, {"Cookie": "tw_session=x"}):
            for route in ("hello", "claim", "result"):
                status, _, body = self.s.call("POST", "/push-worker/" + route,
                                              headers=dict(headers, Authorization="Bearer " + self.token), body={})
                self.assertEqual((status, body.get("error")), (403, "not_a_feed"), (route, headers))


@NEEDS_CRYPTO
class PhoneRoutes(unittest.TestCase):
    def setUp(self):
        self.s = Served(self)
        self.w = self.s.w
        token = Feeds(self.w.store).new_token(self.w.ws, "push", by="tests")
        vapid, seal = worker_keys()
        Push(self.w.store).hello(self.w.ws, token, webpush.vapid_public(vapid), push_worker.seal_public(seal))
        self.cookie, paired = self.s.phone()
        self.h = {"Cookie": self.cookie, "Origin": ORIGIN, "X-CSRF-Token": paired["csrf"]}

    def test_the_alert_state_is_phone_data(self):
        status, _, body = self.s.call("GET", "/push", headers={"Cookie": self.cookie})
        self.assertEqual((status, body.get("error")), (423, "mobile_read_off"))
        self.w.store.set_flag(self.w.ws, "mobile_read", True, by="tests")
        status, _, body = self.s.call("GET", "/push", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["vapid_public"]), 87)
        self.assertEqual((body["subscribed"], body["consent"], body["sending"]), (False, None, False))

    def test_subscribe_needs_the_exact_origin_and_csrf(self):
        sub = {"subscription": Browser().subscription()}
        status, _, _ = self.s.call("POST", "/push/subscribe", headers={"Cookie": self.cookie}, body=sub)
        self.assertEqual(status, 403)
        status, _, _ = self.s.call("POST", "/push/subscribe", headers=dict(self.h, Origin="https://evil.example"),
                                   body=sub)
        self.assertEqual(status, 403)
        status, _, _ = self.s.call("POST", "/push/subscribe", headers=dict(self.h, **{"X-CSRF-Token": "x"}), body=sub)
        self.assertEqual(status, 403)
        status, _, body = self.s.call("POST", "/push/subscribe", headers=self.h, body=sub)
        self.assertEqual((status, body), (200, {"subscribed": True}))

    def test_deny_then_turn_off(self):
        status, _, body = self.s.call("POST", "/push/denied", headers=self.h, body={})
        self.assertEqual((status, body), (200, {"consent": "denied"}))
        status, _, body = self.s.call("POST", "/push/unsubscribe", headers=self.h, body={})
        self.assertEqual((status, body), (200, {"subscribed": False}))


@NEEDS_CRYPTO
class KeyFiles(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.store = self.dir / "push"
        self.store.mkdir(mode=0o700)
        os.chmod(self.store, 0o700)

    @unittest.skipIf(os.name == "nt", "file modes are POSIX")
    def test_keys_are_made_once_and_private(self):
        v1, s1 = push_worker.load_keys(self.store)
        for name in ("vapid.pem", "seal.key"):
            self.assertEqual(stat.S_IMODE((self.store / name).stat().st_mode), 0o600)
        v2, s2 = push_worker.load_keys(self.store)
        self.assertEqual(webpush.vapid_public(v1), webpush.vapid_public(v2))
        self.assertEqual(push_worker.seal_public(s1), push_worker.seal_public(s2))

    @unittest.skipIf(os.name == "nt", "file modes are POSIX")
    def test_refuses_an_open_folder_or_key(self):
        os.chmod(self.store, 0o755)
        with self.assertRaises(SystemExit):
            push_worker.load_keys(self.store)
        os.chmod(self.store, 0o700)
        push_worker.load_keys(self.store)
        os.chmod(self.store / "vapid.pem", 0o644)
        with self.assertRaises(SystemExit):
            push_worker.load_keys(self.store)

    def test_main_needs_an_https_subject(self):
        with self.assertRaises(SystemExit) as e:
            push_worker.main(["--store", str(self.store), "--subject", "http://x.example"])
        self.assertIn("https origin", str(e.exception))

    def test_send_one_never_leaves_for_a_host_off_the_list(self):
        self.assertEqual(push_worker.send_one("https://127.0.0.1:9135/api/v1/cards", b"x", {}), (400, None))

    def test_send_one_reads_retry_after_and_follows_no_redirect(self):
        import http.server
        hits = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                hits.append(self.path)
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path == "/busy":
                    self.send_response(429)
                    self.send_header("Retry-After", "90")
                else:
                    self.send_response(307)
                    self.send_header("Location", "/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        import urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), push_worker._NoRedirect)
        with unittest.mock.patch.object(push_worker.webpush, "push_host_ok", return_value=True):
            self.assertEqual(push_worker.send_one(base + "/busy", b"x", {}, opener=opener), (429, 90))
            self.assertEqual(push_worker.send_one(base + "/moved", b"x", {}, opener=opener), (307, None))
        self.assertEqual(hits, ["/busy", "/moved"])


if __name__ == "__main__":
    unittest.main()
