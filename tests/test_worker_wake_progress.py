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
from thornswood_guard import owed_wake
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
        self.assertIn("in the field of sprocket", out)
        out = self.check(NOON + 600)
        self.assertIn("in the field of sprocket", out)
        self.assertNotIn("in the field of echo", out)
        self.assertNotIn("receipt cx_legacy", out, "one worker's ambiguity is not repeated every five minutes")

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
