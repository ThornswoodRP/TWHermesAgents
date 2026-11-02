"""Cards, answers and receipts (07 sections 5 and 11.1).

Every write is one transaction. An answer is checked against the card revision,
the attention revision and the artifact hash Patrick saw; it writes the
response, the status change, the event, the cancelled pushes, the connector
message and the command result in one commit, and only then returns its
receipt. A retry with the same idempotency key and the same body gets the same
receipt back; the same key with a different body is refused.

Four states are kept apart and never merged: the decision accepted (a
response), the Bot told (a confirmed connector receipt), the work done
(Bramble's report) and a release authorized (the Main card, step 6, not built).
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Callable, Iterable

from .auth import Auth, Principal
from .schema import KINDS
from .store import CoreError, Store, canonical, iso, new_id, parse, sha256_hex

DEFAULT_EXPIRY = timedelta(days=3)
MAX_BODY_BYTES = 256 * 1024
MAX_OPTIONS = 6
LIVE = ("draft", "pending")
ROOM_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def pushes(kind: str, urgent: bool) -> bool:
    """Only a real need pushes (07 section 8): board review findings never do,
    weekly project review findings only when urgent."""
    if kind == "board_review":
        return False
    if kind == "weekly_review":
        return urgent
    return True


class DecisionCore:
    def __init__(self, store: Store, *, fault: Callable[[str], None] | None = None):
        self.s = store
        self.auth = Auth(store)
        self._fault = fault or (lambda point: None)

    # ── setup, run on the VM by the operator ──────────────────────────────

    def bind_room(self, workspace_id: str, room_id: str, name: str, *, by: str) -> None:
        # The app opens a room at /m/rooms/<room id>, so the id is kept to what a path can carry.
        if not isinstance(room_id, str) or not ROOM_ID.match(room_id):
            raise CoreError(400, "bad_room", "a room id is 1 to 64 letters, digits, '-' or '_'")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise CoreError(400, "bad_room", "a room's name is 1 to 80 characters")
        name = name.strip()
        with self.s.tx() as db:
            db.execute("INSERT INTO room_bindings(workspace_id, room_id, name, bound_at) VALUES(?,?,?,?) "
                       "ON CONFLICT(workspace_id, room_id) DO UPDATE SET name = excluded.name",
                       (workspace_id, room_id, name, self.s.now()))
            self.s.event(db, workspace_id, "room.bound", room_id, by, {"name": name})

    def bind_bot(self, workspace_id: str, bot: str, profile_id: str, *, by: str) -> None:
        with self.s.tx() as db:
            db.execute("INSERT INTO bot_bindings(workspace_id, bot, profile_id, bound_at) VALUES(?,?,?,?) "
                       "ON CONFLICT(workspace_id, bot) DO UPDATE SET profile_id = excluded.profile_id",
                       (workspace_id, bot, profile_id, self.s.now()))
            self.s.event(db, workspace_id, "bot.bound", bot, by, {"profile": profile_id})

    # ── checks ────────────────────────────────────────────────────────────

    def _writes_on(self, workspace_id: str) -> None:
        if not self.s.flag(workspace_id, "decision_write"):
            raise CoreError(423, "decision_write_off",
                            "decision writes are off in this workspace; the report page is still where Patrick answers")

    def _row(self, db, decision_id: str, workspace_id: str):
        row = db.execute("SELECT * FROM decisions WHERE id = ? AND workspace_id = ?",
                         (decision_id, workspace_id)).fetchone()
        if not row:
            raise CoreError(404, "no_card", "no such card")
        return row

    def _may_read(self, p: Principal, row) -> None:
        if p.kind in ("patrick", "bramble"):
            return
        if p.kind == "specialist" and row["requested_by"] == p.id:
            return
        raise CoreError(404, "no_card", "no such card")

    # ── opening and changing cards ────────────────────────────────────────

    def assign_task(self, p: Principal, task: str, bot: str) -> None:
        if p.kind != "bramble":
            raise CoreError(403, "not_bramble", "only Bramble assigns tasks")
        with self.s.tx() as db:
            db.execute("INSERT INTO task_assignments(workspace_id, task, bot, assigned_by, assigned_at) "
                       "VALUES(?,?,?,?,?) ON CONFLICT(workspace_id, task) DO UPDATE SET bot = excluded.bot, "
                       "assigned_by = excluded.assigned_by, assigned_at = excluded.assigned_at",
                       (p.workspace_id, task, bot, p.id, self.s.now()))
            self.s.event(db, p.workspace_id, "task.assigned", task, p.label, {"bot": bot})

    def open_card(self, p: Principal, *, kind: str, title: str, body: str, options: list[dict],
                  recommendation: str, scope_of_yes: str, risk: str = "normal", urgent: bool = False,
                  expires_in: timedelta = DEFAULT_EXPIRY, source_key: str | None = None,
                  task: str | None = None, links: Iterable[tuple[str, str]] = ()) -> dict:
        self._writes_on(p.workspace_id)
        if kind not in KINDS:
            raise CoreError(400, "bad_kind", f"no card kind {kind!r}")
        if kind == "main":
            raise CoreError(403, "main_waits", "the Main card is step 6 and waits for phase 7")
        if p.kind == "specialist":
            if kind != "question":
                raise CoreError(403, "specialist_question_only", "a specialist opens questions only")
            if not task:
                raise CoreError(403, "specialist_needs_task", "a specialist's question names its assigned task")
        elif p.kind != "bramble":
            raise CoreError(403, "not_a_requester", f"{p.label} does not open cards")
        self._check_card(title, body, options, recommendation, scope_of_yes, risk)
        with self.s.tx() as db:
            if p.kind == "specialist":
                a = db.execute("SELECT bot FROM task_assignments WHERE workspace_id = ? AND task = ?",
                               (p.workspace_id, task)).fetchone()
                if not a or a["bot"] != p.bot:
                    raise CoreError(403, "not_your_task", f"task {task} is not assigned to {p.bot}")
            if source_key:
                req = db.execute("SELECT decision_id FROM decision_requests WHERE workspace_id = ? "
                                 "AND source_key = ?", (p.workspace_id, source_key)).fetchone()
                if req:
                    db.execute("UPDATE decision_requests SET last_at = ?, count = count + 1 WHERE workspace_id = ? "
                               "AND source_key = ?", (self.s.now(), p.workspace_id, source_key))
                    self.s.event(db, p.workspace_id, "decision.request_merged", req["decision_id"], p.label,
                                 {"source_key": source_key})
                    return self._card(db, self._row(db, req["decision_id"], p.workspace_id)) | {"merged": True}
            now = self.s.now()
            art = new_id("art")
            sha = sha256_hex(body)
            db.execute("INSERT INTO artifacts(id, workspace_id, created_by, created_at) VALUES(?,?,?,?)",
                       (art, p.workspace_id, p.id, now))
            db.execute("INSERT INTO artifact_revisions(artifact_id, revision, sha256, media_type, body, created_by, "
                       "created_at) VALUES(?,1,?,?,?,?,?)", (art, sha, "text/markdown", body.encode("utf-8"), p.id, now))
            did = new_id("dec")
            expires = (self.s.clock() + expires_in).isoformat()
            db.execute("INSERT INTO decisions(id, workspace_id, kind, status, revision, attention_revision, title, "
                       "artifact_id, artifact_revision, artifact_sha256, recommendation, scope_of_yes, required_actor, "
                       "risk, urgent, requested_by, task, source_key, expires_at, created_at, updated_at) "
                       "VALUES(?,?,?,'pending',1,1,?,?,1,?,?,?,'patrick',?,?,?,?,?,?,?,?)",
                       (did, p.workspace_id, kind, title, art, sha, recommendation, scope_of_yes, risk, int(urgent),
                        p.id, task, source_key, expires, now, now))
            self._write_options(db, did, 1, options)
            for lk, ref in links:
                db.execute("INSERT INTO task_links(id, decision_id, kind, ref) VALUES(?,?,?,?)",
                           (new_id("ln"), did, lk, ref))
            if source_key:
                db.execute("INSERT INTO decision_requests(workspace_id, source_key, decision_id, first_at, last_at, "
                           "count) VALUES(?,?,?,?,?,1)", (p.workspace_id, source_key, did, now, now))
            self.s.event(db, p.workspace_id, "decision.opened", did, p.label,
                         {"kind": kind, "revision": 1, "artifact_sha256": sha})
            if pushes(kind, urgent):
                self._queue_push(db, p.workspace_id, did, 1)
            return self._card(db, self._row(db, did, p.workspace_id))

    def revise_card(self, p: Principal, decision_id: str, *, expected_revision: int, title: str, body: str,
                    options: list[dict], recommendation: str, scope_of_yes: str) -> dict:
        """A changed card is a new revision; every answer to an old one approves nothing."""
        self._writes_on(p.workspace_id)
        with self.s.tx() as db:
            row = self._row(db, decision_id, p.workspace_id)
            if not (p.kind == "bramble" or (p.kind == "specialist" and row["requested_by"] == p.id)):
                raise CoreError(403, "not_yours", f"{p.label} may not change this card")
            self._check_card(title, body, options, recommendation, scope_of_yes, row["risk"])
            if row["status"] != "pending":
                raise CoreError(409, "not_pending", f"the card is {row['status']}", status_now=row["status"])
            if row["revision"] != expected_revision:
                raise CoreError(409, "stale_revision", f"the card is at revision {row['revision']}",
                                revision=row["revision"])
            rev = row["revision"] + 1
            art_rev = row["artifact_revision"] + 1
            sha = sha256_hex(body)
            now = self.s.now()
            db.execute("INSERT INTO artifact_revisions(artifact_id, revision, sha256, media_type, body, created_by, "
                       "created_at) VALUES(?,?,?,?,?,?,?)",
                       (row["artifact_id"], art_rev, sha, "text/markdown", body.encode("utf-8"), p.id, now))
            self._write_options(db, decision_id, rev, options)
            db.execute("UPDATE decisions SET revision = ?, attention_revision = attention_revision + 1, title = ?, "
                       "artifact_revision = ?, artifact_sha256 = ?, recommendation = ?, scope_of_yes = ?, "
                       "updated_at = ? WHERE id = ?",
                       (rev, title, art_rev, sha, recommendation, scope_of_yes, now, decision_id))
            self._cancel_pushes(db, decision_id, "card revised")
            self.s.event(db, p.workspace_id, "decision.revised", decision_id, p.label,
                         {"revision": rev, "artifact_sha256": sha})
            if pushes(row["kind"], bool(row["urgent"])):
                self._queue_push(db, p.workspace_id, decision_id, row["attention_revision"] + 1)
            return self._card(db, self._row(db, decision_id, p.workspace_id))

    def close_card(self, p: Principal, decision_id: str, *, status: str, reason: str,
                   superseded_by: str | None = None) -> dict:
        """Bramble cancels a card, or replaces it with another."""
        if p.kind != "bramble":
            raise CoreError(403, "not_bramble", "only Bramble cancels or supersedes a card")
        if status not in ("cancelled", "superseded"):
            raise CoreError(400, "bad_status", f"a card is closed as cancelled or superseded, not {status}")
        if (status == "superseded") != bool(superseded_by):
            raise CoreError(400, "superseded_by", "a superseded card names the card that replaces it, and only then")
        self._writes_on(p.workspace_id)
        with self.s.tx() as db:
            row = self._row(db, decision_id, p.workspace_id)
            if row["status"] not in LIVE:
                raise CoreError(409, "not_pending", f"the card is {row['status']}", status_now=row["status"])
            if superseded_by:
                new = self._row(db, superseded_by, p.workspace_id)
                if new["id"] == decision_id or new["status"] not in LIVE:
                    raise CoreError(409, "bad_replacement", "the replacement must be another live card")
            db.execute("UPDATE decisions SET status = ?, superseded_by = ?, updated_at = ? WHERE id = ?",
                       (status, superseded_by, self.s.now(), decision_id))
            self._cancel_pushes(db, decision_id, status)
            self.s.event(db, p.workspace_id, f"decision.{status}", decision_id, p.label,
                         {"reason": reason, "superseded_by": superseded_by})
            return self._card(db, self._row(db, decision_id, p.workspace_id))

    def expire_due(self, workspace_id: str) -> int:
        now = self.s.clock()
        with self.s.tx() as db:
            rows = db.execute("SELECT id, expires_at FROM decisions WHERE workspace_id = ? AND status = 'pending'",
                              (workspace_id,)).fetchall()
            n = 0
            for r in rows:
                if now >= parse(r["expires_at"]):
                    db.execute("UPDATE decisions SET status = 'expired', updated_at = ? WHERE id = ?",
                               (self.s.now(), r["id"]))
                    self._cancel_pushes(db, r["id"], "expired")
                    self.s.event(db, workspace_id, "decision.expired", r["id"], "core", {})
                    n += 1
            return n

    # ── answering ─────────────────────────────────────────────────────────

    def answer(self, p: Principal, *, decision_id: str, option_key: str, revision: int, attention_revision: int,
               artifact_sha256: str, idempotency_key: str, note: str = "") -> dict:
        if p.kind != "patrick":
            raise CoreError(403, "not_patrick", f"only Patrick answers; {p.label} cannot")
        self._writes_on(p.workspace_id)
        if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 200:
            raise CoreError(400, "bad_key", "an answer carries an idempotency key of 8 to 200 characters")
        body = {"decision_id": decision_id, "option_key": option_key, "revision": revision,
                "attention_revision": attention_revision, "artifact_sha256": artifact_sha256, "note": note}
        body_sha = sha256_hex(canonical(body))
        with self.s.tx() as db:
            cmd = db.execute("SELECT op, body_sha256, result_json FROM commands WHERE workspace_id = ? "
                             "AND principal_id = ? AND idempotency_key = ?",
                             (p.workspace_id, p.id, idempotency_key)).fetchone()
            if cmd:
                if cmd["op"] != "answer" or cmd["body_sha256"] != body_sha:
                    raise CoreError(409, "key_reused", "that idempotency key was used for a different request")
                return json.loads(cmd["result_json"]) | {"replayed": True}
            row = self._row(db, decision_id, p.workspace_id)
            if row["status"] == "resolved":
                raise CoreError(409, "already_answered", "this card was already answered",
                                receipt=self._receipt_for(db, decision_id))
            if row["status"] != "pending":
                raise CoreError(409, "not_pending", f"the card is {row['status']}", status_now=row["status"],
                                superseded_by=row["superseded_by"])
            if self.s.clock() >= parse(row["expires_at"]):
                raise CoreError(410, "expired", "this card expired; Bramble opens a new one if it still matters")
            if (row["revision"], row["attention_revision"], row["artifact_sha256"]) != (
                    revision, attention_revision, artifact_sha256):
                raise CoreError(409, "stale_revision", "the card changed since it was shown; read it again",
                                revision=row["revision"], attention_revision=row["attention_revision"])
            opt = db.execute("SELECT key FROM decision_options WHERE decision_id = ? AND revision = ? AND key = ?",
                             (decision_id, revision, option_key)).fetchone()
            if not opt:
                raise CoreError(400, "no_option", f"revision {revision} has no option {option_key!r}")
            if row["risk"] == "high" and not p.fresh:
                raise CoreError(403, "sign_in_again", "a high-risk card needs a fresh sign-in")
            now = self.s.now()
            rid = new_id("rsp")
            db.execute("INSERT INTO responses(id, workspace_id, decision_id, revision, attention_revision, "
                       "artifact_sha256, option_key, note, principal_id, device_id, answered_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (rid, p.workspace_id, decision_id, revision, attention_revision, artifact_sha256,
                        option_key, note, p.id, p.device_id, now))
            self._fault("after_response")
            db.execute("UPDATE decisions SET status = 'resolved', updated_at = ? WHERE id = ?", (now, decision_id))
            self.s.event(db, p.workspace_id, "decision.answered", decision_id, "patrick",
                         {"response": rid, "revision": revision, "option": option_key, "device": p.device_id})
            self._cancel_pushes(db, decision_id, "answered")
            self._fault("after_cancel_pushes")
            requester = db.execute("SELECT bot, kind FROM principals WHERE id = ?", (row["requested_by"],)).fetchone()
            db.execute("INSERT INTO connector_outbox(id, workspace_id, decision_id, kind, target, body_json, "
                       "idempotency_key, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,'queued',?,?)",
                       (new_id("cx"), p.workspace_id, decision_id, "decision.answered", requester["bot"],
                        canonical({"decision_id": decision_id, "revision": revision, "option": option_key,
                                   "response": rid}), f"answer:{rid}", now, now))
            receipt = self._receipt_for(db, decision_id)
            db.execute("INSERT INTO commands(workspace_id, principal_id, idempotency_key, op, body_sha256, "
                       "result_json, created_at) VALUES(?,?,?,?,?,?,?)",
                       (p.workspace_id, p.id, idempotency_key, "answer", body_sha, canonical(receipt), now))
            self._fault("before_commit")
        return receipt

    # ── reading ───────────────────────────────────────────────────────────

    def get_card(self, p: Principal, decision_id: str) -> dict:
        row = self._row(self.s.db, decision_id, p.workspace_id)
        self._may_read(p, row)
        return self._card(self.s.db, row)

    def needs_you(self, p: Principal) -> list[dict]:
        if p.kind not in ("patrick", "bramble"):
            raise CoreError(403, "not_patrick", "only Patrick and Bramble read the Needs you list")
        rows = self.s.db.execute("SELECT * FROM decisions WHERE workspace_id = ? AND status = 'pending' "
                                 "ORDER BY created_at", (p.workspace_id,)).fetchall()
        return [self._card(self.s.db, r) for r in rows]

    def answered(self, p: Principal, *, hours: int = 24, limit: int = 10) -> list[dict]:
        """The cards Patrick answered in the last day, newest first: the app's "Answered" list under Decisions."""
        if p.kind not in ("patrick", "bramble"):
            raise CoreError(403, "not_patrick", "only Patrick and Bramble read the answered list")
        since = iso(self.s.clock() - timedelta(hours=hours))
        rows = self.s.db.execute(
            "SELECT d.id, d.title, d.risk, r.option_key, r.answered_at, o.label FROM decisions d "
            "JOIN responses r ON r.decision_id = d.id "
            "LEFT JOIN decision_options o ON o.decision_id = d.id AND o.revision = r.revision AND o.key = r.option_key "
            "WHERE d.workspace_id = ? AND d.status = 'resolved' AND r.answered_at >= ? "
            "ORDER BY r.answered_at DESC, r.rowid DESC LIMIT ?", (p.workspace_id, since, limit)).fetchall()
        return [{"id": r["id"], "title": r["title"], "risk": r["risk"], "answered_at": r["answered_at"],
                 "option": r["label"] or r["option_key"]} for r in rows]

    def artifact(self, p: Principal, decision_id: str, revision: int | None = None) -> dict:
        row = self._row(self.s.db, decision_id, p.workspace_id)
        self._may_read(p, row)
        rev = revision or row["artifact_revision"]
        a = self.s.db.execute("SELECT * FROM artifact_revisions WHERE artifact_id = ? AND revision = ?",
                              (row["artifact_id"], rev)).fetchone()
        if not a:
            raise CoreError(404, "no_revision", "no such artifact revision")
        body = bytes(a["body"])
        if sha256_hex(body) != a["sha256"]:
            raise CoreError(500, "artifact_corrupt", "the stored artifact does not match its hash")
        return {"revision": rev, "sha256": a["sha256"], "media_type": a["media_type"], "body": body.decode("utf-8")}

    def report_work_done(self, p: Principal, decision_id: str, detail: str) -> None:
        if p.kind != "bramble":
            raise CoreError(403, "not_bramble", "only Bramble reports work done")
        self._writes_on(p.workspace_id)
        with self.s.tx() as db:
            row = self._row(db, decision_id, p.workspace_id)
            if row["status"] != "resolved":
                raise CoreError(409, "not_answered", "work is done only on an answered card")
            self.s.event(db, p.workspace_id, "work.done", decision_id, p.label, {"detail": detail})

    def confirm_effect(self, outbox_id: str, *, outbox: str, outcome: str, detail: dict) -> None:
        """What a delivery worker learned. An uncertain effect blocks any automatic retry."""
        table = {"push": "push_outbox", "connector": "connector_outbox"}[outbox]
        state = {"confirmed": "sent", "failed": "dead", "uncertain": "uncertain"}[outcome]
        with self.s.tx() as db:
            row = db.execute(f"SELECT workspace_id, status FROM {table} WHERE id = ?", (outbox_id,)).fetchone()
            if not row:
                raise CoreError(404, "no_outbox_row", "no such outbox row")
            if row["status"] in ("quarantined", "cancelled"):
                raise CoreError(409, "not_deliverable", f"that row is {row['status']}")
            db.execute(f"UPDATE {table} SET status = ?, updated_at = ? WHERE id = ?", (state, self.s.now(), outbox_id))
            db.execute("INSERT INTO effect_receipts(id, workspace_id, outbox, outbox_id, outcome, detail_json, at) "
                       "VALUES(?,?,?,?,?,?,?)", (new_id("fx"), row["workspace_id"], outbox, outbox_id, outcome,
                                                 canonical(detail), self.s.now()))
            self.s.event(db, row["workspace_id"], f"effect.{outcome}", outbox_id, "core", {"outbox": outbox})

    # ── helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _check_card(title, body, options, recommendation, scope_of_yes, risk) -> None:
        if not title.strip() or not scope_of_yes.strip():
            raise CoreError(400, "incomplete", "a card has a title and the exact scope of a yes")
        if len(body.encode("utf-8")) > MAX_BODY_BYTES:
            raise CoreError(413, "too_big", f"a card's text is at most {MAX_BODY_BYTES} bytes")
        if not 2 <= len(options) <= MAX_OPTIONS:
            raise CoreError(400, "options", f"a card has 2 to {MAX_OPTIONS} options")
        keys = [o.get("key") for o in options]
        if len(set(keys)) != len(keys) or not all(isinstance(k, str) and k for k in keys):
            raise CoreError(400, "options", "each option has its own key")
        if not all(o.get("label") and o.get("consequence") for o in options):
            raise CoreError(400, "options", "each option says what it does")
        if recommendation not in keys:
            raise CoreError(400, "recommendation", "the recommendation is one of the options")
        if risk not in ("normal", "high"):
            raise CoreError(400, "risk", "risk is normal or high")

    @staticmethod
    def _write_options(db, decision_id: str, revision: int, options: list[dict]) -> None:
        for i, o in enumerate(options):
            db.execute("INSERT INTO decision_options(decision_id, revision, key, label, consequence, position) "
                       "VALUES(?,?,?,?,?,?)", (decision_id, revision, o["key"], o["label"], o["consequence"], i))

    def _queue_push(self, db, workspace_id: str, decision_id: str, attention_revision: int) -> None:
        db.execute("INSERT INTO push_outbox(id, workspace_id, decision_id, attention_revision, status, created_at, "
                   "updated_at) VALUES(?,?,?,?,'queued',?,?) ON CONFLICT(decision_id, attention_revision) DO NOTHING",
                   (new_id("px"), workspace_id, decision_id, attention_revision, self.s.now(), self.s.now()))

    def _cancel_pushes(self, db, decision_id: str, reason: str) -> None:
        db.execute("UPDATE push_outbox SET status = 'cancelled', reason = ?, updated_at = ? WHERE decision_id = ? "
                   "AND status IN ('queued', 'leased')", (reason, self.s.now(), decision_id))

    def _receipt_for(self, db, decision_id: str) -> dict | None:
        r = db.execute("SELECT * FROM responses WHERE decision_id = ? ORDER BY revision DESC LIMIT 1",
                       (decision_id,)).fetchone()
        if not r:
            return None
        return {"response_id": r["id"], "decision_id": decision_id, "revision": r["revision"],
                "attention_revision": r["attention_revision"], "option_key": r["option_key"],
                "answered_at": r["answered_at"], "device_id": r["device_id"]}

    def states(self, decision_id: str) -> dict:
        """The four states, each read from its own record."""
        db = self.s.db
        accepted = db.execute("SELECT 1 FROM responses WHERE decision_id = ?", (decision_id,)).fetchone() is not None
        told = db.execute("SELECT 1 FROM connector_outbox c JOIN effect_receipts e ON e.outbox_id = c.id "
                          "WHERE c.decision_id = ? AND c.kind = 'decision.answered' AND e.outcome = 'confirmed'",
                          (decision_id,)).fetchone() is not None
        done = db.execute("SELECT 1 FROM events WHERE subject = ? AND kind = 'work.done'",
                          (decision_id,)).fetchone() is not None
        return {"decision_accepted": accepted, "bot_told": told, "work_done": done, "release_authorized": None}

    def _card(self, db, row) -> dict:
        opts = db.execute("SELECT key, label, consequence FROM decision_options WHERE decision_id = ? "
                          "AND revision = ? ORDER BY position", (row["id"], row["revision"])).fetchall()
        links = db.execute("SELECT kind, ref FROM task_links WHERE decision_id = ?", (row["id"],)).fetchall()
        return {
            "id": row["id"], "kind": row["kind"], "status": row["status"], "title": row["title"],
            "revision": row["revision"], "attention_revision": row["attention_revision"],
            "artifact_sha256": row["artifact_sha256"], "recommendation": row["recommendation"],
            "scope_of_yes": row["scope_of_yes"], "risk": row["risk"], "expires_at": row["expires_at"],
            "superseded_by": row["superseded_by"], "task": row["task"],
            "options": [dict(o) for o in opts], "links": [dict(x) for x in links],
            "receipt": self._receipt_for(db, row["id"]),
        }
