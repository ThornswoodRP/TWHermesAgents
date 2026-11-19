"""A completed router sweep is not evidence that an idle worker got work."""

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from test_owed_wake import BUSY, Idle, NOON, POLICY, wakes
from decisions_helpers import World
from thornswood_decisions.bots import BotRoute
from thornswood_guard import owed_wake, worker_slot
from thornswood_guard.brokers import BrokerRefused


def stamp(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


def receipt(name, at, status="sent", issue=None):
    return {"id": "cx_" + name, "at": stamp(at), "status": status, "issue": issue}


class UndispatchedOpportunity(unittest.TestCase):
    def test_a_finished_sweep_cannot_drop_still_idle_workers_with_ready_work(self):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        owed_wake._save(home.joinpath(*owed_wake.IDLE_FILE),
                        {"morning": "2026-11-09", "alert_checked": NOON})
        broker = Idle(claimed=dict(BUSY, **{"claimed: Echo": 0, "claimed: Sprocket": 0}),
                      open_={'"area: Launcher","area: Packaging"': 2,
                             '"area: Website","area: Discord","area: Voice"': 2})
        first = owed_wake.check(home, "bramble", broker, now=NOON, policy=POLICY)
        self.assertTrue(wakes("\n".join(first)))
        self.assertIn("echo", "\n".join(first))
        self.assertIn("sprocket", "\n".join(first))
        # The whole sweep ended but neither worker got a message, ticket or
        # a later completed turn. The ready issues remain available.
        broker.turns = {"bramble": {"running": False,
                                   "sweep_done": datetime.fromtimestamp(NOON + 100, timezone.utc).isoformat()},
                        "echo": {"running": False, "waiting": False},
                        "sprocket": {"running": False, "waiting": False}}
        broker.queued = {}
        again = owed_wake.check(home, "bramble", broker, now=NOON + 300, policy=POLICY)
        self.assertTrue(wakes("\n".join(again)), "a completed sweep cannot acknowledge work that was never dispatched")
        self.assertIn("echo", "\n".join(again))
        self.assertIn("sprocket", "\n".join(again))


class CronQuietWindow(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.home = Path(home.name)
        self.now = NOON
        self.elapsed = 0
        self.webhook_ends = (45,)
        self.capacity = True
        self.block_after = None
        self.enabled = True
        self.ended = True
        self.probes = []
        self.b = Idle(claimed=dict(BUSY, **{"claimed: Echo": 0}), open_={'"area: Website"': 1})

    def ask(self, broker, op, args):
        webhook = next((i for i, end in enumerate(self.webhook_ends) if self.elapsed < end), None)
        if op == "turn_room":
            self.probes.append((self.elapsed, bool(args.get("sweep")), webhook))
            return {"room": self.capacity and (self.block_after is None or self.elapsed < self.block_after)
                    and not (args.get("sweep") and webhook is not None)}
        if op == "bot_turns":
            return {"bots": {"bramble": {"running": webhook is not None, "waiting": False},
                             "echo": {"running": False, "waiting": False,
                                      "last": stamp(self.now - (60 if self.ended else 600))}}}
        return self.b(broker, op, args)

    def run_check(self):
        owed_wake._save(self.home.joinpath(*owed_wake.JOBS_FILE), {"jobs": [
            {"name": owed_wake.OWED_WAKE_JOB, "enabled": self.enabled, "state": "scheduled"},
        ]})
        owed_wake._save(self.home.joinpath(*owed_wake.IDLE_FILE), {
            "morning": "2026-11-09", "alert_checked": self.now, "idle_checked": self.now - 300,
        })
        return "\n".join(owed_wake._cron_check(self.home, "bramble", self.ask, POLICY,
                                             clock=lambda: self.now + self.elapsed, monotonic=lambda: self.elapsed,
                                             sleep=self.advance))

    def advance(self, seconds):
        self.elapsed += seconds

    def test_the_same_scheduled_check_routes_workers_after_the_active_sweep_ends(self):
        self.assertIn("Checked next issue for echo:", self.run_check())
        self.assertGreaterEqual(self.elapsed, self.webhook_ends[-1])
        self.assertEqual(sum(op == "board" for _, op, _ in self.b.asked), 1)

    def test_consecutive_webhooks_do_not_restart_or_consume_the_deadline(self):
        # The first webhook ended, but the replacement was already admitted
        # when the next probe ran. A later check sees the queue clear.
        self.webhook_ends = (22, 79)
        self.assertIn("Checked next issue for echo:", self.run_check())
        self.assertEqual({webhook for _, sweep, webhook in self.probes if sweep}, {0, 1, None})
        self.assertGreaterEqual(self.elapsed, 79)
        self.assertLessEqual(self.elapsed, 90)
        self.assertEqual(sum(op == "board" for _, op, _ in self.b.asked), 1)

    def test_persistent_occupancy_times_out_without_a_board_read_or_agent_turn(self):
        self.webhook_ends = (40, 120)
        self.assertFalse(wakes(self.run_check()))
        self.assertEqual(self.elapsed, 90)
        self.assertFalse(any(b == "git" for b, _, _ in self.b.asked))
        now = self.now + self.elapsed
        path = self.home.joinpath(*worker_slot.WORKER_SLOT_FILE)
        initial = owed_wake._load(path)
        self.assertTrue(worker_slot.active(self.home, POLICY, now))
        later = now + worker_slot.WORKER_SLOT_SECONDS
        worker_slot.reserve(self.home, later)
        self.assertEqual(owed_wake._load(path), initial, "the same board watermark does not extend the reservation")
        self.assertFalse(worker_slot.active(self.home, POLICY, later))

    def test_a_checked_board_keeps_the_slot_until_its_cron_turn_can_admit(self):
        self.webhook_ends = (120,)
        self.assertFalse(wakes(self.run_check()))
        self.elapsed = 390
        got = owed_wake._cron_check(self.home, "bramble", self.ask, POLICY,
                                    clock=lambda: self.now + self.elapsed, monotonic=lambda: self.elapsed,
                                    sleep=self.advance)
        self.assertIn("Checked next issue for echo: ThornswoodRP/Thornswood#9000.", got)
        self.assertTrue(worker_slot.active(self.home, POLICY, self.now + self.elapsed))
        worker_slot.consume(self.home)
        self.assertFalse(worker_slot.active(self.home, POLICY, self.now + self.elapsed))

    def test_a_checked_board_without_work_consumes_the_slot_without_a_turn(self):
        self.webhook_ends = (120,)
        self.assertFalse(wakes(self.run_check()))
        self.elapsed = 390
        self.b.open.clear()
        got = owed_wake._cron_check(self.home, "bramble", self.ask, POLICY,
                                    clock=lambda: self.now + self.elapsed, monotonic=lambda: self.elapsed,
                                    sleep=self.advance)
        self.assertFalse(wakes("\n".join(got)))
        self.assertFalse(worker_slot.active(self.home, POLICY, self.now + self.elapsed))

    def test_reservation_write_failure_still_runs_the_normal_cron_check(self):
        from unittest.mock import patch

        self.webhook_ends = (120,)
        normal_save = worker_slot.save
        slot_path = self.home.joinpath(*worker_slot.WORKER_SLOT_FILE)

        def save(path, value):
            if path == slot_path:
                raise OSError("local marker cannot be written")
            return normal_save(path, value)

        with (patch.object(worker_slot, "save", side_effect=save),
              patch.object(owed_wake, "check", wraps=owed_wake.check) as check):
            self.assertFalse(wakes(self.run_check()))
            check.assert_called_once()
        self.assertFalse(slot_path.exists())

    def test_a_global_hold_stops_waiting_even_while_a_sweep_is_running(self):
        self.block_after = 8
        self.assertFalse(wakes(self.run_check()))
        self.assertEqual(self.elapsed, 8)
        self.assertFalse(any(b == "git" for b, _, _ in self.b.asked))
        self.assertFalse(self.home.joinpath(*worker_slot.WORKER_SLOT_FILE).exists())

    def test_a_queued_delivery_is_not_unsettled_worker_demand(self):
        self.b.queued = {"echo": 1}
        self.assertFalse(wakes(self.run_check()))
        self.assertEqual(self.elapsed, 0)

    def test_no_wait_without_canonical_demand_or_an_enabled_workday(self):
        for knob, value in (("ended", False), ("enabled", False), ("capacity", False), ("now", NOON + 12 * 3600)):
            with self.subTest(knob=knob):
                old = getattr(self, knob)
                setattr(self, knob, value)
                self.assertFalse(wakes(self.run_check()))
                self.assertEqual(self.elapsed, 0)
                setattr(self, knob, old)

    def test_the_work_window_is_rechecked_after_waiting_across_midnight(self):
        self.now = NOON + 11.5 * 3600 - 8
        self.assertFalse(wakes(self.run_check()))
        self.assertEqual(self.elapsed, 8)
        self.assertFalse(any(b == "git" for b, _, _ in self.b.asked))


class DispatchReceipts(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.addCleanup(self.world.close)
        self.route = BotRoute(self.world.path, "test", clock=self.world.clock)
        self.addCleanup(self.route.close)

    def test_only_exact_new_assignment_outbox_rows_acknowledge_dispatch(self):
        self.world.core.assign_task(self.world.bramble, "old", "echo", "Old brief.")
        self.world.clock.advance(seconds=1)
        since = self.world.store.now()
        self.world.clock.advance(seconds=1)
        self.world.core.assign_task(self.world.bramble, "new", "echo", "Private brief stays private.")
        row = self.world.store.db.execute("SELECT id,created_at FROM connector_outbox WHERE target='echo' "
                                          "ORDER BY created_at DESC LIMIT 1").fetchone()
        got = self.route.queued_for(self.world.bramble, {"since": since})
        self.assertEqual(got["dispatches"], {"echo": {"id": row["id"], "at": row["created_at"], "status": "queued"}})
        self.assertNotIn("brief", json.dumps(got))
        self.world.clock.advance(seconds=1)
        after = self.world.store.now()
        self.world.clock.advance(seconds=1)
        again = self.world.core.assign_task(self.world.bramble, "new", "echo", "Private brief stays private.")
        self.assertFalse(again["queued"])
        self.assertEqual(self.route.queued_for(self.world.bramble, {"since": after})["dispatches"], {},
                         "a repeated brief changes assignment bookkeeping but is not a new delivery")

    def test_dispatch_projection_is_workspace_scoped_and_bramble_only(self):
        since = self.world.store.now()
        other = self.world.store.ensure_workspace("other", is_test=True, by="tests")
        principal = self.world.core.auth.ensure_service(other, "bramble", "bramble")
        self.world.clock.advance(seconds=1)
        self.world.core.assign_task(principal, "other", "echo", "Other workspace.")
        self.assertEqual(self.route.queued_for(self.world.bramble, {"since": since})["dispatches"], {})
        with self.assertRaisesRegex(Exception, "only Bramble"):
            self.route.queued_for(self.world.rift, {"since": since})


    def test_only_trusted_canonical_task_metadata_is_projected(self):
        since = self.world.store.now()
        for task, expected in (("ThornswoodRP/Thornswood#12", {"repo": "ThornswoodRP/Thornswood", "number": 12}),
                               ("please resume ThornswoodRP/Thornswood#12", None), ("12", None)):
            self.world.clock.advance(seconds=1)
            self.world.core.assign_task(self.world.bramble, task, "echo", "Never export private brief.")
            got = self.route.queued_for(self.world.bramble, {"since": since})
            self.assertEqual(got["attempts"]["echo"]["issue"], expected)
            self.assertNotIn("private brief", json.dumps(got))
        self.assertEqual(set(self.route.queued_for(self.world.bramble, {})), {"bots"})

    def test_receipt_hold_refresh_is_exact_scoped_and_superseded_only_by_same_task(self):
        self.world.core.assign_task(self.world.bramble, "ThornswoodRP/Thornswood#12", "echo", "Original.")
        row = self.world.store.db.execute("SELECT id FROM connector_outbox WHERE target='echo'").fetchone()[0]
        self.world.store.db.execute("UPDATE connector_outbox SET status='uncertain' WHERE id=?", (row,))
        self.world.clock.advance(seconds=1)
        self.world.core.assign_task(self.world.bramble, "ThornswoodRP/Thornswood#13", "echo", "Different.")
        got = self.route.queued_for(self.world.bramble, {"receipts": [row]})["receipts"][row]
        self.assertFalse(got["superseded"])
        self.assertEqual(got["status"], "uncertain")
        self.world.clock.advance(seconds=1)
        self.world.core.assign_task(self.world.bramble, "ThornswoodRP/Thornswood#12", "sprocket", "Explicit new brief.")
        self.assertTrue(self.route.queued_for(self.world.bramble, {"receipts": [row]})["receipts"][row]["superseded"])
        for bad in ("cx_one", ["bad"], ["cx_a"] * 33):
            with self.subTest(bad=bad), self.assertRaises(Exception):
                self.route.queued_for(self.world.bramble, {"receipts": bad})

    def test_a_newer_cancelled_same_task_transfers_the_hold_instead_of_releasing_it(self):
        task = "ThornswoodRP/Thornswood#12"
        self.world.core.assign_task(self.world.bramble, task, "echo", "Original.")
        first = self.world.store.db.execute("SELECT id FROM connector_outbox WHERE target='echo'").fetchone()[0]
        self.world.store.db.execute("UPDATE connector_outbox SET status='uncertain' WHERE id=?", (first,))
        state = {"review_receipts": {first: {"id": first}}}
        self.world.clock.advance(seconds=1)
        self.world.core.assign_task(self.world.bramble, task, "sprocket", "Explicit new brief.")
        latest = self.world.store.db.execute("SELECT id FROM connector_outbox WHERE target='sprocket'").fetchone()[0]
        self.world.store.db.execute("UPDATE connector_outbox SET status='cancelled' WHERE id=?", (latest,))
        ask = lambda broker, op, args: self.route.queued_for(self.world.bramble, args)
        owed_wake._refresh_receipts("bramble", ask, state, POLICY)
        self.assertEqual(set(state["review_receipts"]), {latest})
        self.assertEqual(state["review_receipts"][latest]["status"], "cancelled")
        self.world.store.db.execute("UPDATE connector_outbox SET status='sent' WHERE id=?", (latest,))
        owed_wake._refresh_receipts("bramble", ask, state, POLICY)
        self.assertEqual(state["review_receipts"], {})

    def test_every_unseen_failure_is_scanned_even_when_one_worker_has_two_between_ticks(self):
        ids = []
        for n, status in ((12, "uncertain"), (13, "cancelled")):
            self.world.core.assign_task(self.world.bramble, f"ThornswoodRP/Thornswood#{n}", "echo", f"Brief {n}.")
            row = self.world.store.db.execute("SELECT id FROM connector_outbox ORDER BY rowid DESC LIMIT 1").fetchone()[0]
            self.world.store.db.execute("UPDATE connector_outbox SET status=? WHERE id=?", (status, row))
            ids.append(row)
        scan = self.route.queued_for(self.world.bramble, {"scan_after": 0})
        self.assertEqual([r["id"] for r in scan["outcomes"]], ids)
        self.assertEqual([r["issue"]["number"] for r in scan["outcomes"]], [12, 13])
        self.assertFalse(scan["scan_more"])
        self.assertEqual(self.route.queued_for(self.world.bramble, {"scan_after": scan["scan_after"]})["outcomes"], [])

    def test_dispatch_timestamp_requires_an_unambiguous_zone(self):
        for bad in (None, 1, True, "no", "2026-11-18T08:00:00", "9999-12-31T23:59:59-23:00"):
            with self.subTest(bad=bad), self.assertRaises(Exception):
                self.route.queued_for(self.world.bramble, {"since": bad})

    def test_a_later_failed_attempt_cannot_hide_an_earlier_current_dispatch(self):
        since = self.world.store.now()
        self.world.clock.advance(seconds=1)
        self.world.core.assign_task(self.world.bramble, "accepted", "echo", "Accepted.")
        first = self.route.queued_for(self.world.bramble, {"since": since})["dispatches"]["echo"]
        self.world.store.db.execute("UPDATE connector_outbox SET status='sent' WHERE id=?", (first["id"],))
        self.world.clock.advance(seconds=1)
        self.world.core.assign_task(self.world.bramble, "later", "echo", "Later attempt.")
        latest = self.world.store.db.execute("SELECT id FROM connector_outbox WHERE target='echo' "
                                             "ORDER BY created_at DESC LIMIT 1").fetchone()[0]
        for status in ("dead", "uncertain", "cancelled"):
            self.world.store.db.execute("UPDATE connector_outbox SET status=? WHERE id=?", (status, latest))
            got = self.route.queued_for(self.world.bramble, {"since": since})
            self.assertEqual(got["dispatches"]["echo"]["id"], first["id"])
            self.assertEqual(got["attempts"]["echo"]["status"], status)
        # Cancelling the accepted receipt itself removes that proof; a
        # different cancelled attempt never cancels the accepted one by inference.
        self.world.store.db.execute("UPDATE connector_outbox SET status='cancelled' WHERE id=?", (first["id"],))
        self.assertEqual(self.route.queued_for(self.world.bramble, {"since": since})["dispatches"], {})


class OpportunityLifecycle(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.path = self.home.joinpath(*owed_wake.IDLE_FILE)
        owed_wake._save(self.path, {"morning": "2026-11-09", "alert_checked": NOON,
                                   "idle_checked": NOON - 3600})
        self.b = Idle(claimed=dict(BUSY, **{"claimed: Echo": 0, "claimed: Sprocket": 0}),
                      open_={'"area: Website"': 2, '"area: Launcher"': 2})
        self.b.turns = {w: {"running": False, "waiting": False} for w in POLICY["bots"]}

    def check(self, at=NOON, broker=None):
        return "\n".join(owed_wake.check(self.home, "bramble", broker or self.b, now=at, policy=POLICY))

    def test_finished_workers_get_the_first_quiet_check_and_alert_gets_the_next(self):
        self.check()
        for worker in ("echo", "sprocket"):
            self.b.dispatches[worker] = receipt(worker, NOON + 30)
            self.b.turns[worker]["last"] = stamp(NOON + 180)

        def owed(broker, op, args):
            if op == "wake_due":
                if self.b.turns["bramble"]["running"]:
                    return {"owed": True, "due": False, "why_not": "another sweep is running"}
                return {"owed": True, "due": True, "refused": 1}
            return self.b(broker, op, args)

        self.b.turns["bramble"]["running"] = True
        self.b.asked.clear()
        for seconds in (900, 1200):
            self.assertFalse(wakes(self.check(NOON + seconds, owed)))
        self.assertFalse(any(b == "git" for b, _, _ in self.b.asked))
        self.assertEqual(owed_wake._load(self.path)["idle_checked"], NOON)

        self.b.turns["bramble"] = {"running": False, "sweep_done": stamp(NOON + 1210)}
        first = self.check(NOON + 1500, owed)
        for worker in ("echo", "sprocket"):
            self.assertIn(f"Checked next issue for {worker}:", first)
        self.assertEqual(owed_wake._load(self.path)["alert_checked"], NOON)
        self.assertEqual(sum(op == "board" for _, op, _ in self.b.asked), 1)
        self.assertFalse(any(a["repo"] == POLICY["repos"]["ops"] for a in self.b.reads()))

        for worker in ("echo", "sprocket"):
            self.b.dispatches[worker] = receipt(worker + "_next", NOON + 1540)
            self.b.turns[worker]["last"] = stamp(NOON + 1680)
        self.b.asked.clear()
        self.check(NOON + 1800)
        self.assertEqual(owed_wake._load(self.path)["alert_checked"], NOON + 1800)
        self.assertTrue(any(a["repo"] == POLICY["repos"]["ops"] for a in self.b.reads()))
        self.assertFalse(any(op == "board" for _, op, _ in self.b.asked))

        again = self.check(NOON + 2100)
        for worker in ("echo", "sprocket"):
            self.assertIn(f"Checked next issue for {worker}:", again)

    def test_full_board_check_still_detects_a_known_worker_completion(self):
        self.check()
        for worker in ("echo", "sprocket"):
            self.b.dispatches[worker] = receipt(worker, NOON + 30)
            self.b.turns[worker]["last"] = stamp(NOON + 180)
        self.b.asked.clear()

        out = self.check(NOON + owed_wake.IDLE_EVERY)
        self.assertIn("Checked next issue for echo:", out)
        self.assertEqual(owed_wake._load(self.path)["alert_checked"], NOON)
        self.assertFalse(any(a["repo"] == POLICY["repos"]["ops"] for a in self.b.reads()))

    def test_partial_delivery_settles_only_that_worker_without_repeating_it(self):
        self.check()
        self.b.dispatches["echo"] = receipt("echo", NOON + 10)
        self.b.turns["bramble"]["sweep_done"] = stamp(NOON + 20)
        out = self.check(NOON + 300)
        self.assertTrue(wakes(out))
        self.assertIn("in the field of sprocket", out)
        self.assertNotIn("in the field of echo", out)
        state = owed_wake._load(self.path)
        self.assertEqual(state["pending_workers"], {"sprocket": NOON})
        # Refresh does not move the observation past a raced assignment.
        self.b.dispatches["sprocket"] = receipt("sprocket", NOON + 200)
        self.assertFalse(wakes(self.check(NOON + 600)))
        self.assertNotIn("said_at", owed_wake._load(self.path))

    def test_inflight_assignment_can_become_uncertain_or_cancelled_without_losing_observation(self):
        for status in ("uncertain", "cancelled"):
            with self.subTest(status=status):
                self.setUp()
                self.check()
                self.b.dispatches["echo"] = receipt("echo", NOON + 10)
                queued = receipt("inflight", NOON + 11, "queued", {"repo": POLICY["repos"]["app"], "number": 12})
                self.b.dispatches["sprocket"] = queued
                self.b.queued["sprocket"] = 1
                self.assertFalse(wakes(self.check(NOON + 300)))
                self.assertEqual(owed_wake._load(self.path)["pending_workers"], {"sprocket": NOON})
                self.b.queued.clear()
                self.b.dispatches.pop("sprocket")
                self.b.attempts["sprocket"] = dict(queued, status=status)
                out = self.check(NOON + 600)
                self.assertTrue(wakes(out))
                self.assertIn(f"receipt cx_inflight is {status}", out)

    def test_queued_then_sent_settles_only_after_confirmation(self):
        self.check()
        self.b.dispatches = {w: receipt(w, NOON + 10, "leased") for w in ("echo", "sprocket")}
        self.b.queued = {"echo": 1, "sprocket": 1}
        self.assertFalse(wakes(self.check(NOON + 300)))
        self.assertEqual(set(owed_wake._load(self.path)["pending_workers"]), {"echo", "sprocket"})
        self.b.dispatches = {w: dict(r, status="sent") for w, r in self.b.dispatches.items()}
        self.b.queued.clear()
        self.assertFalse(wakes(self.check(NOON + 600)))
        self.assertNotIn("said_at", owed_wake._load(self.path))

    def test_old_accepted_delivery_does_not_erase_new_unrelated_cancelled_task_hold(self):
        self.check()
        self.b.dispatches["echo"] = receipt("old_accepted", NOON + 5)
        self.b.attempts["echo"] = receipt("different_cancelled", NOON + 6, "cancelled",
                                          {"repo": POLICY["repos"]["app"], "number": 12})
        self.check(NOON + 300)
        self.assertNotIn("echo", owed_wake._load(self.path)["pending_workers"])
        self.assertIn("cx_different_cancelled", owed_wake._load(self.path)["review_receipts"])
        grouped = [a for _, op, a in self.b.asked if op == "board"][-1]
        self.assertIn({"repo": POLICY["repos"]["app"], "number": 12}, grouped["excluded_issues"],
                      "an Echo hold still applies on a Sprocket-only snapshot")

    def test_preexisting_uncertain_receipt_is_reviewed_without_replaying_active_claim(self):
        self.b.attempts["echo"] = receipt("historical", NOON - 100, "uncertain",
                                          {"repo": POLICY["repos"]["app"], "number": 300})
        self.b.claimed["claimed: Echo"] = 1
        out = self.check()
        self.assertIn("receipt cx_historical is uncertain", out)
        self.assertNotIn("echo hold an active issue", out)

    def test_more_than_thirty_two_holds_reconcile_in_bounded_batches_without_stopping_other_workers(self):
        held = {}
        for i in range(33):
            row = dict(receipt(f"held_{i:02}", NOON - 100, "uncertain",
                               {"repo": POLICY["repos"]["app"], "number": i + 1}), target="echo")
            held[row["id"]] = row
        state = owed_wake._load(self.path)
        state["review_receipts"] = held
        owed_wake._save(self.path, state)
        self.b.receipt_rows = dict(held)
        self.b.receipt_rows["cx_held_32"]["status"] = "sent"
        out = self.check()
        self.assertIn("in the field of sprocket, echo", out)
        self.check(NOON + 300)
        self.assertNotIn("cx_held_32", owed_wake._load(self.path)["review_receipts"])
        requests = [a["receipts"] for _, op, a in self.b.asked if op == "queued_for" and "receipts" in a]
        self.assertTrue(requests)
        self.assertTrue(all(len(ids) <= 32 for ids in requests))

    def test_two_unseen_failed_tasks_are_both_excluded(self):
        rows = [dict(receipt(f"unseen_{n}", NOON - 10, status,
                             {"repo": POLICY["repos"]["app"], "number": n}), target="echo")
                for n, status in ((12, "uncertain"), (13, "cancelled"))]
        self.b.scan_rows = rows
        self.b.receipt_rows = {r["id"]: r for r in rows}
        self.b.attempts["echo"] = rows[-1]
        self.check()
        grouped = [a for _, op, a in self.b.asked if op == "board"][-1]
        self.assertEqual(grouped["excluded_issues"], [{"repo": POLICY["repos"]["app"], "number": n} for n in (12, 13)])

    def test_historical_ambiguous_notices_do_not_pause_any_of_the_five_workers(self):
        workers = owed_wake._workers(POLICY)
        self.b.claimed = {POLICY["bots"][w]["claim_label"]: 0 for w in workers}
        self.b.open = {f'"area: {owed_wake.AREAS[w][0]}"': 1 for w in workers}
        self.b.scan_rows = [dict(receipt(f"historical_{w}", NOON - 86400, "uncertain"), target=w) for w in workers]
        self.b.receipt_rows = {r["id"]: r for r in self.b.scan_rows}
        out = self.check()
        for w in workers:
            self.assertIn(f"Checked next issue for {w}:", out)
        self.assertIn("Historical ambiguity is not a worker pause", out)
        self.assertNotIn("otherwise tell it to carry on", out)

    def test_ambiguous_workers_remain_named_when_canonical_notices_use_the_display_limit(self):
        rows = [dict(receipt(f"canonical_{n}", NOON - 100, "uncertain",
                             {"repo": POLICY["repos"]["app"], "number": n}), target="sprocket") for n in range(1, 7)]
        rows.append(dict(receipt("ambiguous_echo", NOON - 100, "uncertain"), target="echo"))
        self.b.scan_rows = rows
        self.b.receipt_rows = {r["id"]: r for r in rows}
        out = self.check()
        self.assertIn("Historical ambiguous task notices need review for: echo.", out)
        self.assertIn("Checked next issue for echo:", out)

    def test_source_proven_closed_hold_is_not_readded_by_its_latest_attempt_on_next_check(self):
        self.b.attempts["echo"] = receipt("closed", NOON - 100, "uncertain",
                                          {"repo": POLICY["repos"]["app"], "number": 12})
        board_requests = []
        def broker(kind, op, args):
            reply = self.b(kind, op, args)
            if op == "board":
                board_requests.append(args)
                reply["closed_exclusions"] = args.get("excluded_issues", [])
            return reply
        self.check(broker=broker)
        self.assertEqual(owed_wake._load(self.path)["review_receipts"], {})
        out = self.check(NOON + 300, broker)
        self.assertNotIn("receipt cx_closed", out)
        self.assertNotIn("excluded_issues", board_requests[-1])

    def test_fresh_hint_names_only_safe_checked_candidate_even_when_ordinary_board_first_is_held(self):
        from test_grouped_board_projection import issue, page, projection, Snapshot
        state = owed_wake._load(self.path)
        row = dict(receipt("echo_hold", NOON - 10, "cancelled",
                           {"repo": POLICY["repos"]["app"], "number": 12}), target="echo")
        state.update(review_receipts={row["id"]: row}, pending_workers={"sprocket": NOON - 10},
                     said_at=NOON - 10, said=["old"], all_workers_checked=NOON)
        owed_wake._save(self.path, state)
        self.b.receipt_rows = {row["id"]: row}
        board = projection()()
        board.policy = type("Policy", (), {"data": POLICY, "is_bot": lambda _, name: name in POLICY["bots"],
                                           "is_router": lambda _, name: name == "bramble"})()
        board.gh = Snapshot([page([issue(12, "Launcher"), issue(13, "Launcher")])])
        board._waits_on = lambda *args: None
        def broker(kind, op, args):
            return board.board("gateway", args) if op == "board" else self.b(kind, op, args)
        out = self.check(broker=broker)
        ordinary = board.board("gateway", {"bot": "bramble", "eligible": True, "areas": ["Launcher"]})
        self.assertEqual(ordinary["issues"][0]["number"], 12)
        checked = [line for line in out.splitlines() if line.startswith("Checked next issue")]
        self.assertEqual(checked, [f"Checked next issue for sprocket: {POLICY['repos']['app']}#13."])
        self.assertIn("Only pick these checked issue refs", out)

    def test_each_specialist_gets_ordered_alternatives_from_one_guarded_board_read(self):
        from test_grouped_board_projection import issue, page, projection, Snapshot

        self.b.claimed = {}
        rows = [
            issue(3721, "GitHub & CI"), issue(3722, "GitHub & CI"),
            issue(3723, "GitHub & CI"), issue(3724, "GitHub & CI"),
            issue(3725, "GitHub & CI"), issue(3726, "GitHub & CI"),
            issue(3727, "GitHub & CI"), issue(3728, "GitHub & CI"),
            issue(5041, "Launcher"), issue(5043, "Launcher"),
            issue(4602, "Engine"), issue(4603, "Engine"),
            issue(4500, "Shared"), issue(4501, "Shared"),
        ]
        rows[1]["content"]["labels"]["nodes"].append({"name": "claimed: Magpie"})
        rows[3]["order"]["number"] = 3730
        board = projection()()
        board.policy = type("Policy", (), {"data": POLICY, "is_bot": lambda _, name: name in POLICY["bots"],
                                           "is_router": lambda _, name: name == "bramble"})()
        board.gh = Snapshot([page(list(reversed(rows)))])
        board._waits_on = lambda repo, number: "ThornswoodRP/Thornswood#99" if number == 5043 else None
        board_requests = []

        def broker(kind, op, args):
            if op == "board":
                board_requests.append(args)
                return board.board("gateway", args)
            return self.b(kind, op, args)

        out = self.check(broker=broker)
        repo = POLICY["repos"]["app"]
        for worker, expected in (("magpie", (3721, 3723, 3725, 3726, 3727, 3728)),
                                 ("sprocket", (5041,)), ("wisp", (4602, 4603))):
            picks = [line for line in out.splitlines()
                     if line.startswith((f"Checked next issue for {worker}:", f"Checked alternative for {worker}:"))]
            first, *alternatives = expected
            self.assertEqual(picks, [f"Checked next issue for {worker}: {repo}#{first}.",
                                     *(f"Checked alternative for {worker}: {repo}#{n}." for n in alternatives)])
        self.assertEqual(len(board_requests), 1)
        self.assertEqual(board_requests[0]["limit"], 6)
        self.assertEqual(len(board.gh.calls), 1)

    def test_two_failures_between_scan_and_latest_read_suppress_the_stale_hint(self):
        self.b.open['"area: Website"'] = 3
        injected = False
        def broker(kind, op, args):
            nonlocal injected
            if op == "queued_for" and set(args) == {"bot"} and not injected:
                injected = True
                rows = [dict(receipt(f"raced_{n}", NOON, status,
                                     {"repo": POLICY["repos"]["app"], "number": n}), target="echo")
                        for n, status in ((9000, "uncertain"), (9001, "cancelled"))]
                self.b.scan_rows.extend(rows)
                self.b.receipt_rows.update({r["id"]: r for r in rows})
                self.b.attempts["echo"] = rows[-1]
            return self.b(kind, op, args)
        out = self.check(broker=broker)
        self.assertFalse(wakes(out))
        self.assertNotIn("Checked next issue", out)
        self.assertTrue(owed_wake._load(self.path)["receipt_scan_pending"])
        out = self.check(NOON + 300, broker)
        self.assertIn(f"Checked next issue for echo: {POLICY['repos']['app']}#9002.", out)

    def test_257_closed_holds_do_not_stop_unrelated_work_and_prune_only_with_board_proof(self):
        from test_grouped_board_projection import issue, page, projection, Snapshot
        held = {f"cx_old_{n}": dict(receipt(f"old_{n}", NOON - 100, "uncertain",
                                            {"repo": POLICY["repos"]["app"], "number": n}), target="echo")
                for n in range(1, 258)}
        state = owed_wake._load(self.path)
        state["review_receipts"] = held
        owed_wake._save(self.path, state)
        self.b.receipt_rows = held
        nodes = [issue(n, state="CLOSED") for n in range(1, 258)] + [issue(9000, "Launcher")]
        board = projection()()
        board.policy = type("Policy", (), {"data": POLICY, "is_bot": lambda _, name: name in POLICY["bots"],
                                           "is_router": lambda _, name: name == "bramble"})()
        board.gh = Snapshot([page(nodes[:100], True, "100"), page(nodes[100:200], True, "200"), page(nodes[200:])])
        board._waits_on = lambda *args: None
        def broker(kind, op, args):
            return board.board("gateway", args) if op == "board" else self.b(kind, op, args)
        out = self.check(broker=broker)
        self.assertIn("in the field of sprocket", out)
        self.assertEqual(owed_wake._load(self.path)["review_receipts"], {})
        self.assertEqual(len(board.gh.calls), 3, "one whole snapshot, not one query per held issue")

    def test_held_claim_does_not_suppress_guidance_for_a_different_inactive_claim(self):
        for w in ("rift", "wisp", "magpie"):
            self.b.turns[w]["running"] = True
        self.b.claimed["claimed: Sprocket"] = 2  # #200 held, #201 unrelated active
        self.b.attempts["sprocket"] = receipt("held", NOON - 100, "cancelled",
                                             {"repo": POLICY["repos"]["app"], "number": 200})
        out = self.check()
        self.assertIn("sprocket hold an active issue", out)
        self.assertIn("never to an exact receipt task held for review", out)

    def test_canonical_no_work_settles_without_an_assignment_or_model_turn(self):
        self.check()
        self.b.open.clear()
        out = self.check(NOON + 300)
        self.assertFalse(wakes(out))
        self.assertNotIn("pending_workers", owed_wake._load(self.path))

    def test_source_failure_retains_observation_and_bounds_github_retries(self):
        self.check()
        calls = []
        def down(broker, op, args):
            if op == "board":
                calls.append(args)
                raise BrokerRefused("secondary rate limit")
            return self.b(broker, op, args)
        self.assertFalse(wakes(self.check(NOON + 300, down)))
        pending = owed_wake._load(self.path)["pending_workers"]
        for t in (600, 900, 1200):
            self.check(NOON + t, down)
        self.assertEqual(len(calls), 1)
        self.assertEqual(owed_wake._load(self.path)["pending_workers"], pending)

    def test_busy_router_queued_worker_and_guarded_cap_do_not_spawn_duplicate_sweeps(self):
        self.check()
        self.b.turns["bramble"]["running"] = True
        before = len(self.b.reads())
        self.assertFalse(wakes(self.check(NOON + 300)))
        self.assertEqual(len(self.b.reads()), before)
        self.b.turns["bramble"]["running"] = False
        self.b.queued = {"echo": 1, "sprocket": 1}
        self.assertFalse(wakes(self.check(NOON + 600)))
        self.b.queued.clear()
        self.b.room = False
        self.assertFalse(wakes(self.check(NOON + 700)))
        self.assertEqual(len(self.b.reads()), before)
        self.assertEqual(set(owed_wake._load(self.path)["pending_workers"]), {"echo", "sprocket"})

    def test_exact_cancelled_claim_is_reviewed_while_different_work_remains_available(self):
        self.check()
        self.b.dispatches["echo"] = receipt("echo", NOON + 5)
        self.b.attempts["sprocket"] = receipt("cancelled", NOON + 6, "cancelled",
                                             {"repo": POLICY["repos"]["app"], "number": 200})
        self.b.claimed["claimed: Sprocket"] = 1  # fixture's exact claimed #200
        out = self.check(NOON + 300)
        self.assertTrue(wakes(out))
        self.assertIn("receipt cx_cancelled is cancelled", out)
        self.assertIn("in the field of sprocket", out)
        self.assertNotIn("otherwise tell it to carry on", out)
        grouped = [a for _, op, a in self.b.asked if op == "board"][-1]
        self.assertEqual(grouped["excluded_issues"], [{"repo": POLICY["repos"]["app"], "number": 200}])

    def test_no_work_settles_opportunity_but_exact_task_hold_survives_next_hour(self):
        self.check()
        self.b.dispatches["echo"] = receipt("echo", NOON + 5)
        self.b.attempts["sprocket"] = receipt("held", NOON + 6, "cancelled",
                                             {"repo": POLICY["repos"]["app"], "number": 12})
        self.b.open.clear()
        self.check(NOON + 300)
        self.b.turns["bramble"]["sweep_done"] = stamp(NOON + 400)
        self.check(NOON + 600)
        self.assertNotIn("pending_workers", owed_wake._load(self.path))
        self.assertIn("cx_held", owed_wake._load(self.path)["review_receipts"])
        self.b.open['"area: Launcher"'] = 1
        # Separate the alert look from the hourly free-worker read.
        self.check(NOON + 3900)
        out = self.check(NOON + 4200)
        self.assertIn("in the field of sprocket", out)
        grouped = [a for _, op, a in self.b.asked if op == "board"][-1]
        self.assertEqual(grouped["excluded_issues"], [{"repo": POLICY["repos"]["app"], "number": 12}])

    def test_ambiguous_receipt_does_not_globally_stop_another_ready_worker(self):
        self.check()
        self.b.attempts["echo"] = receipt("legacy", NOON + 5, "uncertain")
        out = self.check(NOON + 300)
        self.assertIn("unidentified legacy task", out)
        self.assertIn("Checked next issue for sprocket:", out)
        out = self.check(NOON + 600)
        self.assertIn("Checked next issue for sprocket:", out)
        self.assertIn("Checked next issue for echo:", out, "an ambiguous old notice is not a worker pause")
        self.assertIn("Historical ambiguity is not a worker pause", out)

    def test_cached_multiworker_hint_cannot_ride_an_unrelated_alert_or_midnight(self):
        self.check()
        self.b.dispatches["echo"] = receipt("echo", NOON + 5)
        self.b.attempts["sprocket"] = receipt("cancelled", NOON + 6, "cancelled",
                                             {"repo": POLICY["repos"]["app"], "number": 2})
        self.b.turns["bramble"]["running"] = True
        def own_alert(broker, op, args):
            if op == "read" and args.get("repo") == POLICY["repos"]["ops"]:
                return {"body": {"items": [{"number": 99, "updated_at": stamp(NOON + 900)}]}}
            return self.b(broker, op, args)
        out = self.check(NOON + 900, own_alert)
        self.assertTrue(wakes(out))
        self.assertIn("Thornswood-Ops#99", out)
        self.assertNotIn("pick for each free worker", out)
        self.assertNotIn("in the field of echo", out)
        self.assertEqual(owed_wake._carried(self.home, NOON + 12 * 3600, POLICY), [])

    def test_missing_activity_and_malformed_receipts_keep_pending_without_git_reads(self):
        self.check()
        self.b.turns.pop("echo")
        before = len(self.b.reads())
        self.assertFalse(wakes(self.check(NOON + 300)))
        self.assertEqual(len(self.b.reads()), before)
        self.assertIn("echo", owed_wake._load(self.path)["pending_workers"])

    def test_legacy_observation_refreshes_once_instead_of_inventing_delivery(self):
        owed_wake._save(self.path, {"morning": "2026-11-09", "alert_checked": NOON,
                                   "idle_checked": NOON, "said_at": NOON - 300,
                                   "said": ["echo hold no active issue: pick for each free worker"]})
        self.b.turns["bramble"]["sweep_done"] = stamp(NOON - 100)
        self.assertTrue(wakes(self.check()))
        self.assertEqual(set(owed_wake._load(self.path)["pending_workers"]), {"echo", "sprocket"})
