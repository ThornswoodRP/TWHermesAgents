"""Step 1 of the decision core: cards, answers, receipts, immutability, the event log.

Covers 07's T05 to T08 (same card, revisions, double tap, racing devices),
the authorization half of T24 (a specialist cannot answer as Patrick), T27 to
T29 (one logical delivery, the outboxes) and the crash windows of an answer.
"""

from __future__ import annotations

import sqlite3
import threading
import unittest

from decisions_helpers import OPTIONS, World

from thornswood_decisions.schema import FLAGS
from thornswood_decisions.store import CoreError, Store

REPO = "ThornswoodRP/Thornswood"
SHA = "0123456789abcdef0123456789abcdef01234567"
MAIN_OPTIONS = [
    {"key": "authorize", "label": "Authorize", "consequence": "Bramble starts the main run at this commit."},
    {"key": "not_now", "label": "Not now", "consequence": "Nothing runs; Bramble asks again with a new commit."},
]


def main_card(w, *, link=None, by=None, **kw):
    args = dict(kind="main", title="Main release at 0123456", options=MAIN_OPTIONS, recommendation="authorize",
                scope_of_yes=f"One main run of {REPO} at {SHA}.", links=[("release", link or f"main:{REPO}@{SHA}")])
    args.update(kw)
    return w.card(by=by, **args)


class Flags(unittest.TestCase):
    def test_every_flag_starts_off(self):
        w = World(writes=False)
        self.addCleanup(w.close)
        for f in FLAGS:
            self.assertFalse(w.store.flag(w.ws, f), f)

    def test_with_writes_off_nothing_is_written(self):
        w = World(writes=False)
        self.addCleanup(w.close)
        with self.assertRaises(CoreError) as e:
            w.card()
        self.assertEqual(e.exception.status, 423)
        self.assertEqual(w.store.db.execute("SELECT count(*) FROM decisions").fetchone()[0], 0)

    def test_a_flag_is_per_workspace(self):
        w = World()
        self.addCleanup(w.close)
        real = w.store.ensure_workspace("thornswood", is_test=False, by="tests")
        self.assertTrue(w.store.flag(w.ws, "decision_write"))
        self.assertFalse(w.store.flag(real, "decision_write"))


class Cards(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)

    def test_open_card_is_pending_with_its_hash_and_a_queued_push(self):
        c = self.w.card()
        self.assertEqual((c["status"], c["revision"], c["attention_revision"]), ("pending", 1, 1))
        art = self.w.core.artifact(self.w.patrick(), c["id"])
        self.assertEqual(art["sha256"], c["artifact_sha256"])
        pushes = self.w.store.db.execute("SELECT status FROM push_outbox WHERE decision_id = ?", (c["id"],)).fetchall()
        self.assertEqual([r[0] for r in pushes], ["queued"])

    def test_board_review_never_pushes_and_weekly_only_when_urgent(self):
        b = self.w.card(kind="board_review")
        q = self.w.card(kind="weekly_review")
        u = self.w.card(kind="weekly_review", urgent=True)
        n = {r[0]: r[1] for r in self.w.store.db.execute(
            "SELECT decision_id, count(*) FROM push_outbox GROUP BY decision_id")}
        self.assertNotIn(b["id"], n)
        self.assertNotIn(q["id"], n)
        self.assertEqual(n[u["id"]], 1)

    def test_a_card_that_is_not_main_carries_no_release_link(self):
        with self.assertRaises(CoreError) as e:
            self.w.card(links=[("release", f"main:{REPO}@{SHA}")])
        self.assertEqual(e.exception.reason, "release_link")

    def test_same_source_key_merges(self):
        a = self.w.card(source_key="rift:task-9:which-port")
        b = self.w.card(source_key="rift:task-9:which-port", title="Asked again")
        self.assertEqual(a["id"], b["id"])
        self.assertTrue(b["merged"])
        self.assertEqual(self.w.store.db.execute("SELECT count(*) FROM decisions").fetchone()[0], 1)

    def test_a_card_needs_real_options_and_a_recommendation_among_them(self):
        for bad in (dict(options=OPTIONS[:1]), dict(recommendation="z"),
                    dict(options=[OPTIONS[0], dict(OPTIONS[0])]), dict(scope_of_yes=" ")):
            with self.assertRaises(CoreError, msg=str(bad)):
                self.w.card(**bad)

    def test_who_may_open(self):
        with self.assertRaises(CoreError) as e:
            self.w.card(by=self.w.patrick())
        self.assertEqual(e.exception.status, 403)
        with self.assertRaises(CoreError) as e:
            self.w.card(by=self.w.rift, kind="change", task="t1")
        self.assertEqual(e.exception.reason, "specialist_question_only")
        with self.assertRaises(CoreError) as e:
            self.w.card(by=self.w.rift, task="t1")
        self.assertEqual(e.exception.reason, "not_your_task")
        self.w.core.assign_task(self.w.bramble, "t1", "rift")
        c = self.w.card(by=self.w.rift, task="t1")
        self.assertEqual(c["task"], "t1")

    def test_a_specialist_reads_only_its_own_cards(self):
        mine_task = "t2"
        self.w.core.assign_task(self.w.bramble, mine_task, "rift")
        mine = self.w.card(by=self.w.rift, task=mine_task)
        other = self.w.card()
        self.assertEqual(self.w.core.get_card(self.w.rift, mine["id"])["id"], mine["id"])
        with self.assertRaises(CoreError) as e:
            self.w.core.get_card(self.w.rift, other["id"])
        self.assertEqual(e.exception.status, 404)

    def test_needs_you_counts_only_pending(self):
        a = self.w.card()
        self.w.card()
        self.w.core.answer(self.w.patrick(), **World.answer_args(a))
        self.assertEqual(len(self.w.core.needs_you(self.w.patrick())), 1)


class Answers(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.c = self.w.card()

    def test_answer_gives_a_receipt_and_tells_the_requester(self):
        r = self.w.core.answer(self.w.patrick(device=self.w.device()), **World.answer_args(self.c))
        self.assertEqual(r["option_key"], "a")
        card = self.w.core.get_card(self.w.patrick(), self.c["id"])
        self.assertEqual(card["status"], "resolved")
        self.assertEqual(card["receipt"]["response_id"], r["response_id"])
        cx = self.w.store.db.execute("SELECT target, status, idempotency_key FROM connector_outbox").fetchall()
        self.assertEqual([tuple(x) for x in cx], [("bramble", "queued", f"answer:{r['response_id']}")])
        push = self.w.store.db.execute("SELECT status, reason FROM push_outbox").fetchone()
        self.assertEqual(tuple(push), ("cancelled", "answered"))

    def test_t07_double_tap_same_key_same_receipt(self):
        args = World.answer_args(self.c)
        r1 = self.w.core.answer(self.w.patrick(), **args)
        r2 = self.w.core.answer(self.w.patrick(), **args)
        self.assertEqual(r1["response_id"], r2["response_id"])
        self.assertTrue(r2["replayed"])
        self.assertEqual(self.w.store.db.execute("SELECT count(*) FROM responses").fetchone()[0], 1)
        self.assertEqual(self.w.store.db.execute("SELECT count(*) FROM connector_outbox").fetchone()[0], 1)

    def test_same_key_changed_body_is_refused(self):
        self.w.core.answer(self.w.patrick(), **World.answer_args(self.c))
        with self.assertRaises(CoreError) as e:
            self.w.core.answer(self.w.patrick(), **World.answer_args(self.c, option="b"))
        self.assertEqual((e.exception.status, e.exception.reason), (409, "key_reused"))

    def test_t06_a_stale_revision_is_refused_and_approves_nothing(self):
        old = dict(self.c)
        new = self.w.core.revise_card(self.w.bramble, self.c["id"], expected_revision=1, title="Pick one, again",
                                      body="The evidence changed.", options=OPTIONS, recommendation="b",
                                      scope_of_yes="Only this one change.")
        self.assertEqual((new["revision"], new["attention_revision"]), (2, 2))
        with self.assertRaises(CoreError) as e:
            self.w.core.answer(self.w.patrick(), **World.answer_args(old))
        self.assertEqual((e.exception.status, e.exception.reason), (409, "stale_revision"))
        self.assertEqual(e.exception.detail["revision"], 2)
        self.assertEqual(self.w.store.db.execute("SELECT count(*) FROM responses").fetchone()[0], 0)
        r = self.w.core.answer(self.w.patrick(), **World.answer_args(new, key="key-00000002"))
        self.assertEqual(r["revision"], 2)

    def test_a_revision_that_changes_nothing_is_not_a_new_one(self):
        """2 Nov: two overlapping Bramble sweeps revised #5458's card to revision
        3 in three minutes. The same words again leave the shown revision
        answerable."""
        same = self.w.core.revise_card(self.w.bramble, self.c["id"], expected_revision=1, title="Pick one",
                                       body="What is wrong, what was measured, each option.", options=OPTIONS,
                                       recommendation="a", scope_of_yes="Only this one change.")
        self.assertTrue(same["unchanged"])
        self.assertEqual((same["revision"], same["attention_revision"]), (1, 1))
        events = self.w.store.db.execute("SELECT count(*) FROM events WHERE kind = 'decision.revised'").fetchone()[0]
        self.assertEqual(events, 0)
        pushes = self.w.store.db.execute("SELECT status FROM push_outbox").fetchall()
        self.assertEqual([r[0] for r in pushes], ["queued"], "the shown revision's push stands")
        self.assertEqual(self.w.core.answer(self.w.patrick(), **World.answer_args(self.c))["revision"], 1)

    def test_any_changed_part_is_a_new_revision(self):
        base = dict(title="Pick one", body="What is wrong, what was measured, each option.", options=OPTIONS,
                    recommendation="a", scope_of_yes="Only this one change.")
        changed = [dict(base, title="Pick one, again"), dict(base, body="New evidence."),
                   dict(base, recommendation="b"), dict(base, scope_of_yes="Only this, and the test."),
                   dict(base, options=[dict(o, label=o["label"] + "!") for o in OPTIONS]),
                   dict(base, options=list(reversed(OPTIONS)))]
        rev = 1
        for kw in changed:
            out = self.w.core.revise_card(self.w.bramble, self.c["id"], expected_revision=rev, **kw)
            self.assertNotIn("unchanged", out, kw)
            self.assertEqual(out["revision"], rev + 1, kw)
            rev = out["revision"]
            out = self.w.core.revise_card(self.w.bramble, self.c["id"], expected_revision=rev, **base)
            rev = out["revision"]

    def test_a_wrong_hash_is_refused(self):
        args = World.answer_args(self.c)
        args["artifact_sha256"] = "f" * 64
        with self.assertRaises(CoreError) as e:
            self.w.core.answer(self.w.patrick(), **args)
        self.assertEqual(e.exception.reason, "stale_revision")

    def test_revising_cancels_the_old_push_and_queues_one_for_the_new_revision(self):
        self.w.core.revise_card(self.w.bramble, self.c["id"], expected_revision=1, title="t", body="b2",
                                options=OPTIONS, recommendation="a", scope_of_yes="s")
        rows = self.w.store.db.execute("SELECT attention_revision, status FROM push_outbox ORDER BY attention_revision")
        self.assertEqual([tuple(r) for r in rows], [(1, "cancelled"), (2, "queued")])

    def test_t08_racing_devices_one_answer_one_receipt(self):
        cores = [self.w.other() for _ in range(6)]
        devices = [self.w.device(label=f"device {i}") for i in range(len(cores))]
        barrier = threading.Barrier(len(cores))
        results, errors = [], []

        def tap(i, core):
            barrier.wait()
            try:
                results.append(core.answer(self.w.patrick(device=devices[i]),
                                           **World.answer_args(self.c, option="ab"[i % 2], key=f"device-{i:04d}")))
            except CoreError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=tap, args=(i, c)) for i, c in enumerate(cores)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), len(cores) - 1)
        winner = results[0]["response_id"]
        for exc in errors:
            self.assertEqual(exc.reason, "already_answered")
            self.assertEqual(exc.detail["receipt"]["response_id"], winner)
        self.assertEqual(self.w.store.db.execute("SELECT count(*) FROM responses").fetchone()[0], 1)
        self.assertEqual(self.w.store.db.execute("SELECT count(*) FROM connector_outbox").fetchone()[0], 1)
        self.assertTrue(self.w.store.verify_events()[0])

    def test_t08_racing_same_key_from_two_handlers_one_receipt(self):
        cores = [self.w.other() for _ in range(4)]
        barrier = threading.Barrier(len(cores))
        results = []

        def tap(core):
            barrier.wait()
            results.append(core.answer(self.w.patrick(), **World.answer_args(self.c)))

        threads = [threading.Thread(target=tap, args=(c,)) for c in cores]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len({r["response_id"] for r in results}), 1)
        self.assertEqual(sum(1 for r in results if not r.get("replayed")), 1)

    def test_t24_only_patrick_answers(self):
        for who in (self.w.bramble, self.w.rift):
            with self.assertRaises(CoreError) as e:
                self.w.core.answer(who, **World.answer_args(self.c))
            self.assertEqual(e.exception.status, 403)

    def test_another_workspace_cannot_see_or_answer(self):
        w2 = self.w.store.ensure_workspace("test-2", is_test=True, by="tests")
        self.w.store.set_flag(w2, "decision_write", True, by="tests")
        pid = self.w.core.auth.ensure_patrick(w2)
        from thornswood_decisions.auth import Principal
        stranger = Principal(pid, w2, "patrick", fresh=True)
        with self.assertRaises(CoreError) as e:
            self.w.core.answer(stranger, **World.answer_args(self.c))
        self.assertEqual(e.exception.status, 404)

    def test_expired_card_refuses_and_sweeps(self):
        self.w.clock.advance(days=4)
        with self.assertRaises(CoreError) as e:
            self.w.core.answer(self.w.patrick(), **World.answer_args(self.c))
        self.assertEqual(e.exception.status, 410)
        self.assertEqual(self.w.core.expire_due(self.w.ws), 1)
        self.assertEqual(self.w.core.get_card(self.w.patrick(), self.c["id"])["status"], "expired")

    def test_high_risk_needs_a_fresh_sign_in(self):
        c = self.w.card(risk="high")
        with self.assertRaises(CoreError) as e:
            self.w.core.answer(self.w.patrick(fresh=False), **World.answer_args(c))
        self.assertEqual(e.exception.reason, "sign_in_again")
        self.w.core.answer(self.w.patrick(fresh=True), **World.answer_args(c))

    def test_superseded_card_points_to_its_replacement(self):
        new = self.w.card(title="Replacement")
        self.w.core.close_card(self.w.bramble, self.c["id"], status="superseded", reason="new evidence",
                               superseded_by=new["id"])
        with self.assertRaises(CoreError) as e:
            self.w.core.answer(self.w.patrick(), **World.answer_args(self.c))
        self.assertEqual(e.exception.detail["superseded_by"], new["id"])

    def test_four_states_stay_apart(self):
        r = self.w.core.answer(self.w.patrick(), **World.answer_args(self.c))
        s = self.w.core.states(self.c["id"])
        self.assertEqual(s, {"decision_accepted": True, "bot_told": False, "work_done": False,
                             "release_authorized": None})
        cx = self.w.store.db.execute("SELECT id FROM connector_outbox").fetchone()[0]
        self.w.core.confirm_effect(cx, outbox="connector", outcome="confirmed", detail={"run": "r1"})
        self.assertTrue(self.w.core.states(self.c["id"])["bot_told"])
        self.assertFalse(self.w.core.states(self.c["id"])["work_done"])
        self.w.core.report_work_done(self.w.bramble, self.c["id"], "merged")
        self.assertTrue(self.w.core.states(self.c["id"])["work_done"])
        self.assertIsNotNone(r)

    def test_t27_an_uncertain_effect_is_kept_as_uncertain(self):
        self.w.core.answer(self.w.patrick(), **World.answer_args(self.c))
        cx = self.w.store.db.execute("SELECT id FROM connector_outbox").fetchone()[0]
        self.w.core.confirm_effect(cx, outbox="connector", outcome="uncertain", detail={"why": "timeout"})
        self.assertEqual(self.w.store.db.execute("SELECT status FROM connector_outbox").fetchone()[0], "uncertain")
        self.assertFalse(self.w.core.states(self.c["id"])["bot_told"])


class CrashWindows(unittest.TestCase):
    """A crash anywhere inside the answer leaves nothing half written."""

    def test_each_point_rolls_back_and_the_retry_succeeds(self):
        for point in ("after_response", "after_cancel_pushes", "before_commit"):
            w = World()
            self.addCleanup(w.close)
            c = w.card()

            def fault(at, point=point):
                if at == point:
                    raise RuntimeError(f"killed at {at}")

            broken = w.other(fault=fault)
            with self.assertRaises(RuntimeError):
                broken.answer(w.patrick(), **World.answer_args(c))
            db = w.store.db
            self.assertEqual(db.execute("SELECT count(*) FROM responses").fetchone()[0], 0, point)
            self.assertEqual(db.execute("SELECT count(*) FROM commands").fetchone()[0], 0, point)
            self.assertEqual(db.execute("SELECT count(*) FROM connector_outbox").fetchone()[0], 0, point)
            self.assertEqual(db.execute("SELECT status FROM push_outbox").fetchone()[0], "queued", point)
            self.assertEqual(w.core.get_card(w.patrick(), c["id"])["status"], "pending", point)
            r = w.core.answer(w.patrick(), **World.answer_args(c))
            self.assertFalse(r.get("replayed"), point)
            self.assertTrue(w.store.verify_events()[0], point)


class History(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.c = self.w.card()
        self.w.core.answer(self.w.patrick(), **World.answer_args(self.c))

    def test_history_rows_cannot_change(self):
        db = self.w.store.db
        for sql in ("UPDATE artifact_revisions SET body = x'00'", "DELETE FROM artifact_revisions",
                    "UPDATE responses SET option_key = 'b'", "DELETE FROM responses",
                    "UPDATE events SET body_json = '{}'", "DELETE FROM events",
                    "UPDATE decision_options SET label = 'x'"):
            with self.assertRaises(sqlite3.IntegrityError, msg=sql):
                db.execute(sql)

    def test_the_event_chain_holds_and_a_tamper_is_found(self):
        ok, why = self.w.store.verify_events()
        self.assertTrue(ok, why)
        db = self.w.store.db
        db.execute("DROP TRIGGER events_no_update")
        db.execute("UPDATE events SET body_json = '{\"forged\":true}' WHERE seq = 3")
        ok, why = self.w.store.verify_events()
        self.assertFalse(ok)
        self.assertIn("event 3", why)

    def test_a_removed_event_is_found(self):
        db = self.w.store.db
        db.execute("DROP TRIGGER events_no_delete")
        db.execute("DELETE FROM events WHERE seq = 2")
        self.assertIn("missing", self.w.store.verify_events()[1])

    def test_a_newer_store_refuses_to_open(self):
        self.w.store.db.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
        with self.assertRaises(CoreError) as e:
            Store(self.w.path).close()
        self.assertEqual(e.exception.reason, "store_too_new")


if __name__ == "__main__":
    unittest.main()


class MainCards(unittest.TestCase):
    """Step 6: Patrick's tap on a Main card is what the brokers record as his yes."""

    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)

    def test_a_main_card_is_high_risk_and_names_what_it_is_for(self):
        c = main_card(self.w, risk="normal")
        self.assertEqual((c["kind"], c["risk"], c["status"]), ("main", "high", "pending"))
        self.assertIn({"kind": "release", "ref": f"main:{REPO}@{SHA}"}, c["links"])
        self.assertIsNone(self.w.core.main_answer(self.w.ws, c["id"]))
        self.assertFalse(self.w.core.states(c["id"])["release_authorized"])

    def test_only_bramble_opens_one(self):
        with self.assertRaises(CoreError) as e:
            main_card(self.w, by=self.w.rift, task="t1")
        self.assertEqual(e.exception.reason, "not_bramble")

    def test_it_needs_one_good_release_link(self):
        for links, why in (([], "none"), ([("release", f"main:{REPO}@{SHA}"), ("release", f"publish:{REPO}@{SHA}")], "two"),
                           ([("release", f"main:{REPO}@{SHA[:12]}")], "short commit"),
                           ([("release", f"deploy:{REPO}@{SHA}")], "unknown kind")):
            with self.subTest(why), self.assertRaises(CoreError) as e:
                self.w.card(kind="main", options=MAIN_OPTIONS, recommendation="authorize",
                            scope_of_yes=f"One main run at {SHA}.", links=links)
            self.assertEqual(e.exception.reason, "release_link")

    def test_it_offers_authorize_and_its_scope_names_the_commit(self):
        with self.assertRaises(CoreError) as e:
            main_card(self.w, options=OPTIONS, recommendation="a")
        self.assertEqual(e.exception.reason, "main_options")
        with self.assertRaises(CoreError) as e:
            main_card(self.w, scope_of_yes="One main run of the latest commit.")
        self.assertEqual(e.exception.reason, "main_scope")
        with self.assertRaises(CoreError) as e:
            main_card(self.w, source_key="bramble:main")
        self.assertEqual(e.exception.reason, "main_source_key")

    def test_his_yes_needs_a_fresh_sign_in(self):
        c = main_card(self.w)
        with self.assertRaises(CoreError) as e:
            self.w.core.answer(self.w.patrick(fresh=False), **World.answer_args(c, "authorize"))
        self.assertEqual(e.exception.reason, "sign_in_again")

    def test_authorize_is_read_back_with_the_binding(self):
        c = main_card(self.w, link=f"publish:{REPO}@{SHA}#5582")
        self.w.core.answer(self.w.patrick(), **World.answer_args(c, "authorize"))
        a = self.w.core.main_answer(self.w.ws, c["id"])
        self.assertEqual({k: a[k] for k in ("for", "repo", "commit", "pr", "revision", "option", "restored")},
                         {"for": "publish", "repo": REPO, "commit": SHA, "pr": 5582, "revision": 1,
                          "option": "authorize", "restored": False})
        self.assertTrue(self.w.core.states(c["id"])["release_authorized"])

    def test_not_now_authorizes_nothing(self):
        c = main_card(self.w)
        self.w.core.answer(self.w.patrick(), **World.answer_args(c, "not_now"))
        self.assertEqual(self.w.core.main_answer(self.w.ws, c["id"])["option"], "not_now")
        self.assertFalse(self.w.core.states(c["id"])["release_authorized"])

    def test_a_revision_keeps_authorize_and_the_commit(self):
        c = main_card(self.w)
        with self.assertRaises(CoreError) as e:
            self.w.core.revise_card(self.w.bramble, c["id"], expected_revision=1, title="t", body="b",
                                    options=OPTIONS, recommendation="a", scope_of_yes=f"at {SHA}")
        self.assertEqual(e.exception.reason, "main_options")
        r = self.w.core.revise_card(self.w.bramble, c["id"], expected_revision=1, title="Main release, checks green",
                                    body="Now green.", options=MAIN_OPTIONS, recommendation="authorize",
                                    scope_of_yes=f"One main run of {REPO} at {SHA}.")
        self.assertEqual(r["revision"], 2)

    def test_an_answer_from_before_a_restore_authorizes_nothing(self):
        from thornswood_decisions import backup
        c = main_card(self.w)
        self.w.core.answer(self.w.patrick(), **World.answer_args(c, "authorize"))
        dest = self.w.dir / "b.db"
        backup.backup(self.w.store, dest)
        back = self.w.dir / "restored.db"
        backup.restore(dest, back, clock=self.w.clock)
        w2 = Store(back, clock=self.w.clock)
        self.addCleanup(w2.close)
        from thornswood_decisions.core import DecisionCore
        core2 = DecisionCore(w2)
        self.assertTrue(core2.main_answer(self.w.ws, c["id"])["restored"])
        self.assertFalse(core2.states(c["id"])["release_authorized"])

    def test_main_answer_is_for_main_cards_only(self):
        c = self.w.card()
        with self.assertRaises(CoreError) as e:
            self.w.core.main_answer(self.w.ws, c["id"])
        self.assertEqual(e.exception.reason, "no_main_card")
