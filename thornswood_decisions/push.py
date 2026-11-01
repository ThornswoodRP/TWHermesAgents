"""Web Push in the core: subscriptions, consent and the push outbox (07 section 8, step 5).

What the phone shows is always the same: "Thornswood needs you", no body, the
tag `decision:<id>`, quiet, no buttons. The payload is only
`{v, event_id, decision_id, attention_revision, path}`: no question, repo,
answer or token. A tap opens the card; nothing a notification does answers it.

- A phone subscribes only after a deliberate "Turn on alerts" tap. Its
  subscription is sealed to the push worker's key before it is stored
  (`webpush.seal`), and its endpoint must be a browser push service.
- A "no" is kept (`push_consent`), so the app never asks again in a loop.
- The core keeps the SHA-256 of each endpoint, never the endpoint, and tells
  the phone which one it has. Each time the app opens it compares that with
  the browser's own subscription and sends the browser's when they differ.
  A subscription the push service retired (404 or 410) or the operator's key
  reset took is "lapsed": the phone still said yes, so the app renews it by
  itself. One Patrick turned off, a "no", or a revoked device is never renewed.
- A push row is written by the card's own commit (`core.py`). The worker leases
  it only while `push_send` is on, and the lease re-checks, right then, that
  the card is still pending at that attention revision and that a live phone
  is subscribed; otherwise the row is cancelled. A revoked device gets nothing.
- The worker reports per subscription: 2xx is "push service accepted" (never
  "shown" or "read"); 404 and 410 retire the subscription; 429 and 5xx come
  back after Retry-After (at least a minute), and after the last try the row is
  dead. A lease that runs out is uncertain and is not sent again.
"""

from __future__ import annotations

import base64
from datetime import timedelta

from .auth import Principal
from .feeds import Feeds
from .store import CoreError, Store, canonical, new_id, parse, sha256_hex
from .webpush import push_host_ok, seal, unb64u

LEASE = timedelta(minutes=2)
MAX_ATTEMPTS = 5
MIN_RETRY = 60
MAX_RETRY = 3600
MAX_CLAIM = 20
TTL = 24 * 3600
# Retired with these, a subscription lapsed: Patrick did not turn it off.
LAPSED = ("the push service answered 404", "the push service answered 410", "push keys reset")


def payload(row) -> dict:
    return {"v": 1, "event_id": row["id"], "decision_id": row["decision_id"],
            "attention_revision": row["attention_revision"], "path": f"/m/decisions/{row['decision_id']}"}


class Push:
    def __init__(self, store: Store):
        self.s = store

    # ── the worker says its keys, once ────────────────────────────────────

    def hello(self, workspace_id: str, token: str | None, vapid_public: str, seal_public: str) -> dict:
        Feeds(self.s).check_token(workspace_id, "push", token)
        try:
            vp, sp = unb64u(vapid_public), unb64u(seal_public)
        except (ValueError, TypeError):
            raise CoreError(400, "bad_keys", "the keys are base64url") from None
        if len(vp) != 65 or vp[0] != 4 or len(sp) != 32:
            raise CoreError(400, "bad_keys", "a VAPID key is a 65-byte P-256 point and a seal key 32 bytes")
        with self.s.tx() as db:
            row = db.execute("SELECT vapid_public, seal_public FROM push_keys WHERE workspace_id = ?",
                             (workspace_id,)).fetchone()
            if row:
                if (row["vapid_public"], bytes(row["seal_public"])) != (vapid_public, sp):
                    raise CoreError(409, "keys_differ", "the push keys are already set; only the operator resets them")
                return {"set": False}
            db.execute("INSERT INTO push_keys(workspace_id, vapid_public, seal_public, set_at) VALUES(?,?,?,?)",
                       (workspace_id, vapid_public, sp, self.s.now()))
            self.s.event(db, workspace_id, "push.keys_set", workspace_id, "push worker", {"vapid_public": vapid_public})
        return {"set": True}

    def reset_keys(self, workspace_id: str, *, by: str) -> None:
        """The operator's step when the worker's keys are lost: every subscription goes with them."""
        with self.s.tx() as db:
            db.execute("DELETE FROM push_keys WHERE workspace_id = ?", (workspace_id,))
            db.execute("UPDATE push_subscriptions SET revoked_at = ?, retired_reason = 'push keys reset' "
                       "WHERE workspace_id = ? AND revoked_at IS NULL", (self.s.now(), workspace_id))
            self.s.event(db, workspace_id, "push.keys_reset", workspace_id, by, {})

    def _keys(self, workspace_id: str):
        return self.s.db.execute("SELECT vapid_public, seal_public FROM push_keys WHERE workspace_id = ?",
                                 (workspace_id,)).fetchone()

    # ── the phone ─────────────────────────────────────────────────────────

    @staticmethod
    def _phone(p: Principal) -> None:
        if p.kind != "patrick" or p.device_kind != "phone" or not p.device_id:
            raise CoreError(403, "phone_only", "alerts are for Patrick's paired phone")

    def state(self, p: Principal) -> dict:
        self._phone(p)
        keys = self._keys(p.workspace_id)
        last = self.s.db.execute("SELECT revoked_at, retired_reason, endpoint_sha256 FROM push_subscriptions "
                                 "WHERE device_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                                 (p.device_id,)).fetchone()
        consent = self.s.db.execute("SELECT state FROM push_consent WHERE device_id = ?", (p.device_id,)).fetchone()
        consent = consent["state"] if consent else None
        live = last is not None and last["revoked_at"] is None
        lapsed = (last is not None and not live and consent == "granted" and last["retired_reason"] in LAPSED)
        return {"vapid_public": keys["vapid_public"] if keys else None, "subscribed": live,
                "endpoint_sha256": last["endpoint_sha256"] if live else None,
                "lapsed": lapsed, "lapsed_endpoint_sha256": last["endpoint_sha256"] if lapsed else None,
                "consent": consent, "sending": self.s.flag(p.workspace_id, "push_send")}

    def subscribe(self, p: Principal, subscription) -> dict:
        self._phone(p)
        keys = self._keys(p.workspace_id)
        if not keys:
            raise CoreError(503, "push_not_ready", "the push worker has not started yet")
        if not isinstance(subscription, dict):
            raise CoreError(400, "bad_subscription", "a subscription is the browser's PushSubscription as JSON")
        endpoint = subscription.get("endpoint")
        k = subscription.get("keys") if isinstance(subscription.get("keys"), dict) else {}
        if not isinstance(endpoint, str) or len(endpoint) > 1024 or not push_host_ok(endpoint):
            raise CoreError(400, "bad_endpoint", "the endpoint is not a browser push service")
        try:
            p256dh, auth = unb64u(k.get("p256dh", "")), unb64u(k.get("auth", ""))
        except (ValueError, TypeError):
            raise CoreError(400, "bad_subscription", "the subscription's keys are base64url") from None
        if len(p256dh) != 65 or p256dh[0] != 4 or len(auth) != 16:
            raise CoreError(400, "bad_subscription", "the subscription's keys are the wrong size")
        sid = new_id("ps")
        sealed = seal({"endpoint": endpoint, "p256dh": k["p256dh"], "auth": k["auth"]},
                      bytes(keys["seal_public"]), sid.encode())
        with self.s.tx() as db:
            now = self.s.now()
            db.execute("UPDATE push_subscriptions SET revoked_at = ?, retired_reason = 'replaced' "
                       "WHERE device_id = ? AND revoked_at IS NULL", (now, p.device_id))
            db.execute("INSERT INTO push_subscriptions(id, workspace_id, principal_id, device_id, sealed, "
                       "endpoint_sha256, created_at) VALUES(?,?,?,?,?,?,?)",
                       (sid, p.workspace_id, p.id, p.device_id, sealed, sha256_hex(endpoint), now))
            self._consent(db, p, "granted")
            self.s.event(db, p.workspace_id, "push.subscribed", sid, "patrick", {"device": p.device_id})
        return {"subscribed": True}

    def unsubscribe(self, p: Principal) -> dict:
        self._phone(p)
        with self.s.tx() as db:
            n = db.execute("UPDATE push_subscriptions SET revoked_at = ?, retired_reason = 'turned off' "
                           "WHERE device_id = ? AND revoked_at IS NULL", (self.s.now(), p.device_id)).rowcount
            self.s.event(db, p.workspace_id, "push.unsubscribed", p.device_id, "patrick", {"count": n})
        return {"subscribed": False}

    def denied(self, p: Principal) -> dict:
        """The phone said no. Kept, so the app never asks again by itself."""
        self._phone(p)
        with self.s.tx() as db:
            db.execute("UPDATE push_subscriptions SET revoked_at = ?, retired_reason = 'permission denied' "
                       "WHERE device_id = ? AND revoked_at IS NULL", (self.s.now(), p.device_id))
            self._consent(db, p, "denied")
            self.s.event(db, p.workspace_id, "push.denied", p.device_id, "patrick", {})
        return {"consent": "denied"}

    def _consent(self, db, p: Principal, state: str) -> None:
        db.execute("INSERT INTO push_consent(device_id, workspace_id, state, at) VALUES(?,?,?,?) "
                   "ON CONFLICT(device_id) DO UPDATE SET state = excluded.state, at = excluded.at",
                   (p.device_id, p.workspace_id, state, self.s.now()))

    # ── the worker ────────────────────────────────────────────────────────

    def claim(self, workspace_id: str, token: str | None, limit: int = MAX_CLAIM) -> dict:
        Feeds(self.s).check_token(workspace_id, "push", token)
        if not self.s.flag(workspace_id, "push_send"):
            raise CoreError(423, "push_send_off", "the push worker sends nothing while push_send is off")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_CLAIM:
            raise CoreError(400, "bad_limit", f"the limit is 1 to {MAX_CLAIM}")
        now = self.s.clock()
        items = []
        with self.s.tx() as db:
            for r in db.execute("SELECT id, lease_until FROM push_outbox WHERE workspace_id = ? AND status = 'leased'",
                                (workspace_id,)).fetchall():
                if r["lease_until"] and now >= parse(r["lease_until"]):
                    self._finish(db, workspace_id, r["id"], "uncertain", "the worker stopped before it said")
            rows = db.execute("SELECT o.*, d.status AS card_status, d.attention_revision AS card_attention "
                              "FROM push_outbox o JOIN decisions d ON d.id = o.decision_id "
                              "WHERE o.workspace_id = ? AND o.status = 'queued' ORDER BY o.created_at, o.rowid",
                              (workspace_id,)).fetchall()
            subs = db.execute("SELECT s.id, s.sealed FROM push_subscriptions s JOIN devices d ON d.id = s.device_id "
                              "LEFT JOIN push_consent c ON c.device_id = s.device_id "
                              "WHERE s.workspace_id = ? AND s.revoked_at IS NULL AND d.revoked_at IS NULL "
                              "AND d.kind = 'phone' AND (c.state IS NULL OR c.state = 'granted')",
                              (workspace_id,)).fetchall()
            until = (now + LEASE).isoformat()
            for r in rows:
                if len(items) >= limit:
                    break
                if r["lease_until"] and now < parse(r["lease_until"]):
                    continue  # waiting out a Retry-After
                if r["card_status"] != "pending" or r["card_attention"] != r["attention_revision"]:
                    self._finish(db, workspace_id, r["id"], "cancelled", "the card no longer needs this alert")
                    continue
                if not subs:
                    self._finish(db, workspace_id, r["id"], "cancelled", "no phone has alerts on")
                    continue
                db.execute("UPDATE push_outbox SET status = 'leased', lease_until = ?, attempts = attempts + 1, "
                           "updated_at = ? WHERE id = ?", (until, self.s.now(), r["id"]))
                items.append({"id": r["id"], "payload": payload(r), "topic": r["decision_id"][-32:], "ttl": TTL,
                              "subscriptions": [{"id": s["id"], "sealed": base64.b64encode(bytes(s["sealed"])).decode()}
                                                for s in subs]})
        return {"items": items}

    def result(self, workspace_id: str, token: str | None, outbox_id: str, results) -> dict:
        Feeds(self.s).check_token(workspace_id, "push", token)
        if not isinstance(results, list) or not results:
            raise CoreError(400, "bad_results", "results are one entry per subscription")
        with self.s.tx() as db:
            row = db.execute("SELECT status, attempts FROM push_outbox WHERE id = ? AND workspace_id = ?",
                             (outbox_id, workspace_id)).fetchone()
            if not row:
                raise CoreError(404, "no_outbox_row", "no such push")
            if row["status"] != "leased":
                return {"id": outbox_id, "status": row["status"], "replayed": True}
            codes, retry_after = [], MIN_RETRY
            for r in results:
                if not isinstance(r, dict):
                    raise CoreError(400, "bad_results", "each result is an object")
                code = r.get("status")
                code = code if isinstance(code, int) and not isinstance(code, bool) else 0
                codes.append(code)
                if code in (404, 410):
                    db.execute("UPDATE push_subscriptions SET revoked_at = ?, retired_reason = ? WHERE id = ? "
                               "AND workspace_id = ? AND revoked_at IS NULL",
                               (self.s.now(), f"the push service answered {code}", str(r.get("subscription_id")),
                                workspace_id))
                ra = r.get("retry_after")
                if isinstance(ra, int) and not isinstance(ra, bool):
                    retry_after = max(retry_after, min(ra, MAX_RETRY))
            if any(200 <= c < 300 for c in codes):
                state, why = "sent", "push service accepted"
            elif any(c == 0 or c == 429 or c >= 500 for c in codes) and row["attempts"] < MAX_ATTEMPTS:
                when = (self.s.clock() + timedelta(seconds=retry_after)).isoformat()
                db.execute("UPDATE push_outbox SET status = 'queued', lease_until = ?, reason = ?, updated_at = ? "
                           "WHERE id = ?", (when, f"try again after {retry_after}s", self.s.now(), outbox_id))
                self.s.event(db, workspace_id, "push.retry_later", outbox_id, "push worker", {"codes": codes})
                return {"id": outbox_id, "status": "queued", "not_before": when}
            else:
                state, why = "dead", "no push service took it: " + ",".join(map(str, codes))
            self._finish(db, workspace_id, outbox_id, state, why, codes=codes)
        return {"id": outbox_id, "status": state}

    def _finish(self, db, workspace_id: str, outbox_id: str, state: str, why: str, codes=None) -> None:
        db.execute("UPDATE push_outbox SET status = ?, lease_until = NULL, reason = ?, updated_at = ? WHERE id = ?",
                   (state, why, self.s.now(), outbox_id))
        outcome = {"sent": "confirmed", "dead": "failed", "uncertain": "uncertain"}.get(state)
        if outcome:
            db.execute("INSERT INTO effect_receipts(id, workspace_id, outbox, outbox_id, outcome, detail_json, at) "
                       "VALUES(?,?,?,?,?,?,?)", (new_id("fx"), workspace_id, "push", outbox_id, outcome,
                                                 canonical({"reason": why, "codes": codes or []}), self.s.now()))
        self.s.event(db, workspace_id, f"push.{state}", outbox_id, "push worker", {"reason": why})
