"""Cards, answers and receipts (07 sections 5 and 11.1).

Every write is one transaction. An answer is checked against the card revision,
the attention revision and the artifact hash Patrick saw; it writes the
response, the status change, the event, the cancelled pushes, the connector
message and the command result in one commit, and only then returns its
receipt. A retry with the same idempotency key and the same body gets the same
receipt back; the same key with a different body is refused.

Four states are kept apart and never merged: the decision accepted (a
response), the Bot told (a confirmed connector receipt), the work done
(Bramble's report) and a release authorized (Patrick's "authorize" on a Main
card).

A Main card (step 6) is Patrick's tap for what the brokers need his yes for:
a main run while runs.main_without_patrick is false, a collection publish,
the claim label cutover, or the merge of a pull request that changes a
workflow, the deploy gate or a verb script (merge.patrick_paths, Ops #276),
which names that pull request and its head. Only Bramble opens one. It names exactly what it is
for in one release link, release:<main|publish|cutover|merge>:<owner>/<repo>@<the
40-character commit>[#<pull request>], fixed for the card's life; its scope of
a yes names that commit, so the commit is on the card he reads; it offers
"authorize"; and it is high risk, so his answer needs a fresh sign-in. tw-git
reads his answer from main_answer() on its own socket and tw-policy records
it as a decision record (thornswood_guard/decisions.py), used once. An answer
given before the store was restored from a backup authorizes nothing.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from .auth import Auth, Principal
from .schema import KINDS
from .store import CoreError, Store, canonical, iso, new_id, parse, sha256_hex
from .wakes import BOTS_GW

# A task's brief, as long as one chat message (chat.MAX_TEXT).
MAX_BRIEF = 8000
# How a kept message begins when the connector delivers it (chat.Chat._kept_text).
KEPT_HEAD = "[Thornswood queue] This message reached your Bot Chat at "
# The Bots take up new work from 6 AM to 11 PM Mountain, and the Grok lanes
# audit from 11 PM to 6 AM (Patrick, 9 Nov 2026: "have them auto work between
# 6 am and 11 pm so that there is nice transaction and not overload on the VM,
# VPS, One and GitHub"; "If it is in the middle of something by the time it
# may Finnish but not start on a new issue"). A brief told outside it is
# refused, so no worker starts a new issue at night; one already working
# finishes. The hours are policy.yaml's work section (thornswood_guard/hours.py):
# around the clock while Patrick has the lanes' audits paused (11 Nov 2026).
WORK_ZONE = "America/Denver"


def _policy_hours() -> tuple[int, int]:
    from thornswood_guard.hours import work_hours
    from thornswood_guard.policy import Policy
    return work_hours(Policy.load().data)

DEFAULT_EXPIRY = timedelta(days=3)
MAX_BODY_BYTES = 256 * 1024
MAX_OPTIONS = 6
LIVE = ("draft", "pending")
ROOM_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# A Main card's one release link: what Patrick's "authorize" is for.
RELEASE_LINK = re.compile(r"(main|publish|cutover|merge):([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@([0-9a-f]{40})(?:#([1-9][0-9]*))?")
AUTHORIZE = "authorize"


def pushes(kind: str, urgent: bool) -> bool:
    """Only a real need pushes (07 section 8): board review findings never do,
    weekly project review findings only when urgent."""
    if kind == "board_review":
        return False
    if kind == "weekly_review":
        return urgent
    return True


class DecisionCore:
    def __init__(self, store: Store, *, fault: Callable[[str], None] | None = None,
                 work_hours: Callable[[], tuple[int, int]] = _policy_hours):
        self.s = store
        # Read at each brief, so an install that changes the hours needs no restart of its own.
        self.work_hours = work_hours
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

    def assign_task(self, p: Principal, task: str, bot: str, brief: str | None = None) -> dict:
        """Give a task to a specialist. With a brief, the specialist is also told: the brief goes
        to its Bot Chat through the connector, as a card's answer does, and that message starts
        its turn. Until 9 Nov 2026 an assignment only let the specialist ask about the task, and
        nothing reached it, so no worker ever took up an issue Bramble picked: a Hermes turn
        starts only with a message or a schedule. The same brief for the same task and Bot is
        told once."""
        if p.kind != "bramble":
            raise CoreError(403, "not_bramble", "only Bramble assigns tasks")
        if brief is not None:
            if not isinstance(brief, str) or not brief.strip():
                raise CoreError(400, "bad_brief", "a brief says what to do")
            brief = brief.strip()
            if len(brief) > MAX_BRIEF:
                raise CoreError(413, "too_big", f"a brief is at most {MAX_BRIEF} characters")
        if brief is not None:
            local = parse(self.s.now()).astimezone(ZoneInfo(WORK_ZONE))
            start, end = self.work_hours()
            if not start <= local.hour < end:
                from thornswood_guard.hours import says
                raise CoreError(409, "after_hours",
                                f"new work starts {says((start, end))} Mountain; it is "
                                f"{local.strftime('%I:%M %p').lstrip('0')} there, so this waits for the morning")
        told = False
        with self.s.tx() as db:
            now = self.s.now()
            db.execute("INSERT INTO task_assignments(workspace_id, task, bot, assigned_by, assigned_at) "
                       "VALUES(?,?,?,?,?) ON CONFLICT(workspace_id, task) DO UPDATE SET bot = excluded.bot, "
                       "assigned_by = excluded.assigned_by, assigned_at = excluded.assigned_at",
                       (p.workspace_id, task, bot, p.id, now))
            self.s.event(db, p.workspace_id, "task.assigned", task, p.label, {"bot": bot})
            if brief is not None:
                key = f"task:{p.workspace_id}:{task}:{bot}:{sha256_hex(brief)[:16]}"
                cur = db.execute("INSERT OR IGNORE INTO connector_outbox(id, workspace_id, decision_id, kind, "
                                 "target, body_json, idempotency_key, status, created_at, updated_at) "
                                 "VALUES(?,?,NULL,'task.assigned',?,?,?,'queued',?,?)",
                                 (new_id("cx"), p.workspace_id, bot,
                                  canonical({"task": task, "brief": brief, "by": p.label}), key, now, now))
                told = cur.rowcount == 1
                if told:
                    self.s.wake(p.workspace_id, BOTS_GW)
        return {"task": task, "bot": bot, "queued": told}

    def keep_turned_away(self, p: Principal, text: str, reason: str, session: str) -> dict:
        """A message that reached this Bot's Bot Chat but whose turn the fleet queue turned away, kept to be
        delivered once the queue has room. The connector's own messages go back in the queue by themselves
        (connector.py FLEET); this is for the rest, a message another Bot sent with Hermes's message_agent,
        which nothing sent again: 9 Nov 2026, Rift's asks to Sprocket (#5650) and Magpie (#5645) were turned
        away under the old fleet hour cap, each Bot answered that it had not started, and the asks were gone.
        A kept message turned away again is kept again as it first was, not wrapped twice."""
        if not isinstance(text, str) or not text.strip():
            raise CoreError(400, "bad_text", "a kept message has some text")
        text = text.strip()
        if text.startswith(KEPT_HEAD) and "\n\n" in text:
            text = text.split("\n\n", 1)[1].strip()
        text = text[:MAX_BRIEF]
        reason = str(reason or "the fleet queue had no room")[:300]
        with self.s.tx() as db:
            now = self.s.now()
            # Each turned-away turn is its own: a message turned away again is kept again.
            key = f"turned:{p.workspace_id}:{p.bot}:{session}:{sha256_hex(text)[:16]}:{new_id('k')}"
            cur = db.execute("INSERT OR IGNORE INTO connector_outbox(id, workspace_id, decision_id, kind, target, "
                             "body_json, idempotency_key, status, created_at, updated_at) "
                             "VALUES(?,?,NULL,'bot.message',?,?,?,'queued',?,?)",
                             (new_id("cx"), p.workspace_id, p.bot,
                              canonical({"text": text, "reason": reason, "at": now}), key, now, now))
            kept = cur.rowcount == 1
            if kept:
                self.s.event(db, p.workspace_id, "message.kept", p.bot, p.label, {"reason": reason})
                self.s.wake(p.workspace_id, BOTS_GW)
        return {"kept": kept}

    def open_card(self, p: Principal, *, kind: str, title: str, body: str, options: list[dict],
                  recommendation: str, scope_of_yes: str, risk: str = "normal", urgent: bool = False,
                  expires_in: timedelta = DEFAULT_EXPIRY, source_key: str | None = None,
                  task: str | None = None, links: Iterable[tuple[str, str]] = ()) -> dict:
        self._writes_on(p.workspace_id)
        if kind not in KINDS:
            raise CoreError(400, "bad_kind", f"no card kind {kind!r}")
        links = list(links)
        release = self._release_of(kind, links)
        if release:
            if p.kind != "bramble":
                raise CoreError(403, "not_bramble", "only Bramble opens a Main card")
            if source_key:
                raise CoreError(400, "main_source_key", "a Main card is for one commit and is never merged with another")
            # His yes on a Main card always needs a fresh sign-in (answer()).
            risk = "high"
            self._check_main(options, scope_of_yes, release)
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
            if row["kind"] == "main":
                self._check_main(options, scope_of_yes, self._release_row(db, decision_id))
            # Nothing changed is not a new revision: a new one makes the answer
            # Patrick may be giving to this one stale, for nothing.
            sha = sha256_hex(body)
            shown = [tuple(o) for o in db.execute(
                "SELECT key, label, consequence FROM decision_options WHERE decision_id = ? AND revision = ? "
                "ORDER BY position", (decision_id, row["revision"])).fetchall()]
            if (row["title"], row["artifact_sha256"], row["recommendation"], row["scope_of_yes"], shown) == (
                    title, sha, recommendation, scope_of_yes,
                    [(o["key"], o["label"], o["consequence"]) for o in options]):
                return self._card(db, row) | {"unchanged": True}
            rev = row["revision"] + 1
            art_rev = row["artifact_revision"] + 1
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

    def answer(self, p: Principal, *, decision_id: str, option_key: str | None, revision: int,
               attention_revision: int, artifact_sha256: str, idempotency_key: str, note: str = "",
               words: str | None = None) -> dict:
        """Patrick's answer on a card: one of its options (`option_key`, with `note` beside it), or his own
        words instead of any option (`words`, TWHermesAgents #107), which the Bot that asked is told as his
        answer. Words choose no option, so they authorize nothing that waits on one (main_answer, and the
        outside pull request answers tw-git reads, bots.outside_answers)."""
        if p.kind != "patrick":
            raise CoreError(403, "not_patrick", f"only Patrick answers; {p.label} cannot")
        self._writes_on(p.workspace_id)
        if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 200:
            raise CoreError(400, "bad_key", "an answer carries an idempotency key of 8 to 200 characters")
        if words is not None:
            if option_key:
                raise CoreError(400, "bad_answer", "an answer is one of the card's options or his own words, not both")
            if not isinstance(words, str) or not 1 <= len(words.strip()) <= 2000:
                raise CoreError(400, "bad_words", "an answer in his own words says something, up to 2000 characters")
            option_key, note = None, words.strip()
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
            opt = option_key is None or db.execute(
                "SELECT key FROM decision_options WHERE decision_id = ? AND revision = ? AND key = ?",
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
                         {"response": rid, "revision": revision, "option": option_key, "device": p.device_id}
                         | ({"in_words": True} if option_key is None else {}))
            self._cancel_pushes(db, decision_id, "answered")
            self._fault("after_cancel_pushes")
            requester = db.execute("SELECT bot, kind FROM principals WHERE id = ?", (row["requested_by"],)).fetchone()
            db.execute("INSERT INTO connector_outbox(id, workspace_id, decision_id, kind, target, body_json, "
                       "idempotency_key, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,'queued',?,?)",
                       (new_id("cx"), p.workspace_id, decision_id, "decision.answered", requester["bot"],
                        canonical({"decision_id": decision_id, "revision": revision, "option": option_key,
                                   "response": rid}), f"answer:{rid}", now, now))
            self.s.wake(p.workspace_id, BOTS_GW)
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

    def asked_by(self, p: Principal, bot: str) -> list[dict]:
        """The cards waiting on Patrick that this Bot asked, oldest first: shown in its Bot Chat, where
        his message can answer one (chat.py Chat.send)."""
        if p.kind != "patrick":
            raise CoreError(403, "not_patrick", "only Patrick reads the cards in a Bot Chat")
        rows = self.s.db.execute("SELECT d.* FROM decisions d JOIN principals b ON b.id = d.requested_by "
                                 "WHERE d.workspace_id = ? AND d.status = 'pending' AND b.bot = ? AND d.expires_at > ? "
                                 "ORDER BY d.created_at", (p.workspace_id, bot, self.s.now())).fetchall()
        return [self._card(self.s.db, r) for r in rows]

    def asker(self, workspace_id: str, decision_id: str) -> str | None:
        """The Bot that asked a card, or None."""
        r = self.s.db.execute("SELECT b.bot FROM decisions d JOIN principals b ON b.id = d.requested_by "
                              "WHERE d.id = ? AND d.workspace_id = ?", (decision_id, workspace_id)).fetchone()
        return r["bot"] if r else None

    def needs_you(self, p: Principal) -> list[dict]:
        if p.kind not in ("patrick", "bramble"):
            raise CoreError(403, "not_patrick", "only Patrick and Bramble read the Needs you list")
        rows = self.s.db.execute("SELECT * FROM decisions WHERE workspace_id = ? AND status = 'pending' "
                                 "ORDER BY created_at", (p.workspace_id,)).fetchall()
        return [self._card(self.s.db, r) for r in rows]

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

    def main_answer(self, workspace_id: str, decision_id: str) -> dict | None:
        """Patrick's answer on a Main card's current revision, with what the card
        is for, or None while there is none. tw-git reads it on its own socket
        and tw-policy makes it a decision record only when the option is
        "authorize". An answer given before the store was last restored from a
        backup is reported as restored and authorizes nothing."""
        db = self.s.db
        row = db.execute("SELECT * FROM decisions WHERE id = ? AND workspace_id = ? AND kind = 'main'",
                         (decision_id, workspace_id)).fetchone()
        if not row:
            raise CoreError(404, "no_main_card", f"{decision_id} is not a Main card")
        r = db.execute("SELECT r.option_key, r.answered_at, r.revision FROM responses r "
                       "JOIN principals who ON who.id = r.principal_id "
                       "WHERE r.decision_id = ? AND r.revision = ? AND who.kind = 'patrick'",
                       (decision_id, row["revision"])).fetchone()
        if not r:
            return None
        rel = self._release_row(db, decision_id)
        answered = db.execute("SELECT seq FROM events WHERE kind = 'decision.answered' AND subject = ? "
                              "ORDER BY seq DESC LIMIT 1", (decision_id,)).fetchone()
        restored = db.execute("SELECT seq FROM events WHERE kind = 'store.restored' AND workspace_id = ? "
                              "ORDER BY seq DESC LIMIT 1", (workspace_id,)).fetchone()
        return {"decision_id": decision_id, "for": rel["for"], "repo": rel["repo"], "commit": rel["commit"],
                "pr": rel["pr"], "revision": r["revision"], "option": r["option_key"],
                "answered_at": r["answered_at"],
                "restored": bool(restored and (not answered or answered["seq"] < restored["seq"]))}

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
    def _release_of(kind: str, links: list[tuple[str, str]]) -> dict | None:
        """A Main card's one release link, read; any other card has none."""
        refs = [ref for lk, ref in links if lk == "release"]
        if kind != "main":
            if refs:
                raise CoreError(400, "release_link", "only a Main card carries a release link")
            return None
        if len(refs) != 1:
            raise CoreError(400, "release_link", "a Main card names what it is for in exactly one release link, "
                                                 "release:<main|publish|cutover|merge>:<owner>/<repo>@<40-character commit>"
                                                 "[#<pull request>]")
        m = RELEASE_LINK.fullmatch(refs[0])
        if not m:
            raise CoreError(400, "release_link", f"{refs[0]!r} is not <main|publish|cutover|merge>:<owner>/<repo>@"
                                                 "<40-character commit>[#<pull request>]")
        if m.group(1) == "merge" and not m.group(4):
            raise CoreError(400, "release_link", "a merge card names its pull request: "
                                                 "merge:<owner>/<repo>@<its head commit>#<pull request>")
        return {"for": m.group(1), "repo": m.group(2), "commit": m.group(3),
                "pr": int(m.group(4)) if m.group(4) else None}

    def _release_row(self, db, decision_id: str) -> dict:
        r = db.execute("SELECT ref FROM task_links WHERE decision_id = ? AND kind = 'release'",
                       (decision_id,)).fetchone()
        return self._release_of("main", [("release", r["ref"])] if r else [])

    @staticmethod
    def _check_main(options: list[dict], scope_of_yes: str, release: dict) -> None:
        if AUTHORIZE not in [o.get("key") for o in options]:
            raise CoreError(400, "main_options", f"a Main card offers {AUTHORIZE!r}, the one answer that authorizes")
        if release["commit"] not in scope_of_yes:
            raise CoreError(400, "main_scope", "a Main card's scope of a yes names the exact commit, "
                                               f"{release['commit']}, so Patrick reads what he authorizes")

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
                "answered_at": r["answered_at"], "device_id": r["device_id"]} | (
                    {"words": r["note"]} if r["option_key"] is None else {})

    def states(self, decision_id: str) -> dict:
        """The four states, each read from its own record."""
        db = self.s.db
        accepted = db.execute("SELECT 1 FROM responses WHERE decision_id = ?", (decision_id,)).fetchone() is not None
        told = db.execute("SELECT 1 FROM connector_outbox c JOIN effect_receipts e ON e.outbox_id = c.id "
                          "WHERE c.decision_id = ? AND c.kind = 'decision.answered' AND e.outcome = 'confirmed'",
                          (decision_id,)).fetchone() is not None
        done = db.execute("SELECT 1 FROM events WHERE subject = ? AND kind = 'work.done'",
                          (decision_id,)).fetchone() is not None
        release = None
        row = db.execute("SELECT kind, workspace_id FROM decisions WHERE id = ?", (decision_id,)).fetchone()
        if row and row["kind"] == "main":
            a = self.main_answer(row["workspace_id"], decision_id)
            release = bool(a and a["option"] == AUTHORIZE and not a["restored"])
        return {"decision_accepted": accepted, "bot_told": told, "work_done": done, "release_authorized": release}

    def _card(self, db, row) -> dict:
        opts = db.execute("SELECT key, label, consequence FROM decision_options WHERE decision_id = ? "
                          "AND revision = ? ORDER BY position", (row["id"], row["revision"])).fetchall()
        links = db.execute("SELECT kind, ref FROM task_links WHERE decision_id = ?", (row["id"],)).fetchall()
        asker = db.execute("SELECT bot FROM principals WHERE id = ?", (row["requested_by"],)).fetchone()
        return {
            "id": row["id"], "kind": row["kind"], "status": row["status"], "title": row["title"],
            # Which Bot opened it, for the card's "From" line (#107).
            "from": asker["bot"] if asker else None,
            "revision": row["revision"], "attention_revision": row["attention_revision"],
            "artifact_sha256": row["artifact_sha256"], "recommendation": row["recommendation"],
            "scope_of_yes": row["scope_of_yes"], "risk": row["risk"], "expires_at": row["expires_at"],
            "superseded_by": row["superseded_by"], "task": row["task"],
            "options": [dict(o) for o in opts], "links": [dict(x) for x in links],
            "receipt": self._receipt_for(db, row["id"]),
            # When the card's alert went to the push service (Patrick, 2 Nov: a card reached his
            # phone minutes after it was stored), so the card itself says where the time went.
            "alert": self._alert_of(db, row["id"]),
        }

    @staticmethod
    def _alert_of(db, decision_id: str) -> dict | None:
        r = db.execute("SELECT created_at, updated_at, status, attention_revision FROM push_outbox WHERE decision_id = ? "
                       "ORDER BY attention_revision DESC LIMIT 1", (decision_id,)).fetchone()
        if not r:
            return None
        # Which alert this is: a revised card gets a new one, and the phone keeps when each reached it by this.
        return {"queued_at": r["created_at"], "status": r["status"], "attention_revision": r["attention_revision"],
                "sent_at": r["updated_at"] if r["status"] == "sent" else None}
