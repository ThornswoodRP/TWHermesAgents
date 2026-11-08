"""tw-policy: the queue, receipts, decisions, routes, leases and the ledger.

Attack tests 19 (queue and quota), 20 (a crash between a write and its
receipt, at the store) and 21 (restore kills Main records) from 08 section 8.
"""

import sys as _sys
import unittest as _unittest

if not _sys.platform.startswith("linux"):
    # The brokers are Linux code (Unix sockets with SO_PEERCRED, pwd, O_NOFOLLOW
    # walks, systemd): they run only on the Oracle VM, and these tests run in
    # the VM half of scripts/test-at-head.sh.
    raise _unittest.SkipTest("the brokers run only on the Oracle VM; their tests run there")

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from helpers import HEAD, OTHER, policy

from thornswood_brokers.host import HostState
from thornswood_brokers.policy_service import PolicyService
from thornswood_brokers.wire import Refused
from thornswood_guard.decisions import new_decision

APP = "ThornswoodRP/Thornswood"


def roomy() -> HostState:
    return HostState(mem_available_gb=16, swap_used_mb=0, psi_memory_some_avg60=0, psi_memory_full_avg10=0,
                     load5=0.2, srv_free_gb=80, boot_free_gb=80)


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


def pilot_data() -> dict:
    """The policy at the pilot stage (one turn, no checks): the queue's own
    mechanics are tested against a limit of one, whatever the stage is."""
    import copy
    data = copy.deepcopy(policy().data)
    data["admission"]["stage"] = "pilot"
    # The route mechanics are tested with both proxy routes ready; Routes
    # tests a route that is not.
    data["admission_proxy"]["ready"] = {"credits": True, "qwen": True}
    return data


def service(host=None, clock=None, db=None, data=None) -> PolicyService:
    from thornswood_guard.policy import Policy
    p = Policy(data if data else pilot_data())
    db = db or Path(tempfile.mkdtemp()) / "policy.db"
    return PolicyService(db, p, host_reader=host or roomy, clock=clock or Clock())


class Release(unittest.TestCase):
    def test_a_reload_gives_back_every_turn_ticket_and_keeps_sandbox_jobs(self):
        s = service()
        a = s.admit("hermes", {"bot": "rift", "task": "t1"})
        b = s.admit("hermes", {"bot": "wisp", "task": "t2"})
        self.assertEqual((a["state"], b["state"]), ("admitted", "waiting"))
        s.db.execute("INSERT INTO tickets(id, bot, kind, risk, task, source, state, created_at) "
                     "VALUES('sb', 'rift', 'sandbox', 'high', 't3', 'files', 'admitted', '2026-11-01T00:00:00+00:00')")
        handler, callers = s.ops()["release_turns"]
        self.assertEqual(callers, {"root"})
        self.assertEqual(handler("root", {"why": "the Bots were reloaded"}), {"released": 2})
        states = dict(s.db.execute("SELECT id, state FROM tickets").fetchall())
        self.assertEqual((states[a["ticket"]], states[b["ticket"]], states["sb"]), ("done", "done", "admitted"))
        self.assertEqual(s.admit("hermes", {"bot": "echo", "task": "t4"})["state"], "admitted")


class Queue(unittest.TestCase):
    def test_one_model_turn_at_a_time_in_the_pilot(self):
        s = service()
        a = s.admit("hermes", {"bot": "rift", "task": "t1"})
        b = s.admit("hermes", {"bot": "wisp", "task": "t2"})
        self.assertEqual(a["state"], "admitted")
        self.assertEqual(b["state"], "waiting")
        self.assertIn("at most 1", b["reason"])
        s.finish("hermes", {"bot": "rift", "ticket": a["ticket"]})
        again = s.admit("hermes", {"bot": "wisp", "ticket": b["ticket"]})
        self.assertEqual(again["state"], "admitted")

    def test_a_waiting_ticket_nobody_asks_about_lets_the_next_one_by(self):
        clock = Clock()
        s = service(clock=clock)
        a = s.admit("hermes", {"bot": "rift", "task": "t1"})
        b = s.admit("hermes", {"bot": "wisp", "task": "t2"})
        c = s.admit("hermes", {"bot": "echo", "task": "t3"})
        self.assertEqual(c["state"], "waiting")
        s.finish("hermes", {"bot": "rift", "ticket": a["ticket"]})
        clock.advance(minutes=6)
        s.admit("hermes", {"bot": "echo", "ticket": c["ticket"]})
        clock.advance(minutes=5)
        # Wisp's turn went away without a word; Echo, still asking, is next.
        self.assertEqual(s.admit("hermes", {"bot": "echo", "ticket": c["ticket"]})["state"], "admitted")
        self.assertEqual(s.admit("hermes", {"bot": "wisp", "ticket": b["ticket"]})["state"], "waiting")
        clock.advance(hours=25)
        with self.assertRaises(Refused):
            s.admit("hermes", {"bot": "wisp", "ticket": b["ticket"]})

    def test_first_come_first_served(self):
        s = service()
        a = s.admit("hermes", {"bot": "rift", "task": "t1"})
        b = s.admit("hermes", {"bot": "wisp", "task": "t2"})
        c = s.admit("hermes", {"bot": "echo", "task": "t3"})
        s.finish("hermes", {"bot": "rift", "ticket": a["ticket"]})
        self.assertEqual(s.admit("hermes", {"bot": "echo", "ticket": c["ticket"]})["state"], "waiting")
        self.assertEqual(s.admit("hermes", {"bot": "wisp", "ticket": b["ticket"]})["state"], "admitted")

    def test_19_the_101st_card_is_refused(self):
        clock = Clock()
        s = service(clock=clock)
        running = s.admit("hermes", {"bot": "rift", "task": "running"})["ticket"]
        for i in range(100):
            if i % 19 == 0:
                clock.advance(hours=1, minutes=1)
                s.touch("hermes", {"bot": "rift", "ticket": running})
            self.assertEqual(s.admit("hermes", {"bot": "rift", "task": f"t{i}"})["state"], "waiting")
        clock.advance(hours=1, minutes=1)
        s.touch("hermes", {"bot": "rift", "ticket": running})
        with self.assertRaises(Refused) as e:
            s.admit("hermes", {"bot": "rift", "task": "one too many"})
        self.assertIn("at most 100", str(e.exception))

    def test_19_a_burst_of_21_in_an_hour_is_refused(self):
        s = service()
        for i in range(20):
            s.admit("hermes", {"bot": "rift", "task": f"t{i}"})
        with self.assertRaises(Refused) as e:
            s.admit("hermes", {"bot": "rift", "task": "t21"})
        self.assertIn("20 an hour", str(e.exception))

    def test_19_memory_below_the_floor_waits(self):
        low = lambda: HostState(1.2, 0, 0, 0, 0.1, 80, 80)
        s = service(host=low)
        r = s.admit("hermes", {"bot": "rift", "task": "t"})
        self.assertEqual(r["state"], "waiting")
        self.assertIn("draining", r["reason"])

    def test_admission_floors(self):
        for state, words in ((HostState(2.5, 0, 0, 0, 0.1, 80, 80), "memory"),
                             (HostState(16, 900, 0, 0, 0.1, 80, 80), "swap"),
                             (HostState(16, 0, 12, 0, 0.1, 80, 80), "pressure"),
                             (HostState(16, 0, 0, 0, 2.5, 80, 80), "load"),
                             (HostState(16, 0, 0, 0, 0.1, 20, 80), "/srv/tw")):
            r = service(host=lambda st=state: st).admit("hermes", {"bot": "rift", "task": "t"})
            self.assertEqual(r["state"], "waiting", words)
            self.assertIn(words, r["reason"])

    def test_no_sandbox_in_the_pilot(self):
        r = service().admit("hermes", {"bot": "rift", "task": "t", "kind": "sandbox"})
        self.assertEqual(r["state"], "waiting")
        self.assertIn("pilot", r["reason"])

    def test_the_fleet_is_past_the_pilot_with_ceilings_from_their_causes(self):
        # 1 Nov: the pilot's 1 turn and 0 checks queued every Bot behind one
        # other. The live admission floors measure the VM; the counts are
        # one turn per Bot, and as many checks as the sandbox slice holds.
        p = policy().data
        a = p["admission"]
        self.assertEqual(a["stage"], "after")
        self.assertEqual(a["model_turns"]["after"], len(policy().bots))

        def gib(v):
            return int(v[:-1]) * (1024 if v.endswith("G") else 1) if v[-1] in "GM" else int(v)
        slice_mem = gib(p["slices"]["tw-sandbox.slice"]["MemoryMax"])
        check_mem = gib(p["brokers"]["files"]["check_limits"]["MemoryMax"])
        self.assertEqual(a["sandbox_jobs"]["after"], slice_mem // check_mem)

    def test_six_turns_run_at_once_and_a_seventh_waits(self):
        s = service(data=policy().data)
        bots = list(policy().bots)
        got = [s.admit("hermes", {"bot": b, "task": f"t{i}"}) for i, b in enumerate(bots)]
        self.assertEqual([g["state"] for g in got], ["admitted"] * len(bots))
        extra = s.admit("hermes", {"bot": bots[0], "task": "t-extra"})
        self.assertEqual(extra["state"], "waiting")

    def test_16_no_sandbox_without_a_passing_limits_check(self):
        data = policy().data
        data = {**data, "admission": {**data["admission"], "stage": "after"}}
        s = service(data=data)
        r = s.admit("hermes", {"bot": "rift", "task": "t", "kind": "sandbox"})
        self.assertIn("limits check", r["reason"])
        s.limits_record("root", {"ok": False, "details": ["memory.max is max"]})
        self.assertIn("limits check", s.admit("hermes", {"bot": "rift", "task": "t2", "kind": "sandbox"})["reason"])
        s2 = service(data=data)
        s2.limits_record("root", {"ok": True})
        self.assertEqual(s2.admit("hermes", {"bot": "rift", "task": "t", "kind": "sandbox"})["state"], "admitted")

    def test_a_silent_turn_gives_its_slot_back(self):
        clock = Clock()
        s = service(clock=clock)
        a = s.admit("hermes", {"bot": "rift", "task": "t1"})
        b = s.admit("hermes", {"bot": "wisp", "task": "t2"})
        clock.advance(minutes=5)
        s.touch("hermes", {"bot": "rift", "ticket": a["ticket"]})
        clock.advance(minutes=8)
        self.assertEqual(s.admit("hermes", {"bot": "wisp", "ticket": b["ticket"]})["state"], "waiting")
        clock.advance(minutes=3)
        self.assertEqual(s.admit("hermes", {"bot": "wisp", "ticket": b["ticket"]})["state"], "admitted")

    def test_a_ticket_is_its_bots(self):
        s = service()
        a = s.admit("hermes", {"bot": "rift", "task": "t1"})
        with self.assertRaises(Refused):
            s.admit("hermes", {"bot": "wisp", "ticket": a["ticket"]})
        with self.assertRaises(Refused):
            s.touch("hermes", {"bot": "wisp", "ticket": a["ticket"]})

    def test_only_bramble_sets_risk(self):
        s = service()
        with self.assertRaises(Refused):
            s.set_task_risk("hermes", {"bot": "rift", "task": "t", "risk": "low"})
        s.set_task_risk("hermes", {"bot": "bramble", "task": "t", "risk": "low"})
        a = s.admit("hermes", {"bot": "rift", "task": "t"})
        self.assertEqual(s.active("rift")["risk"], "low")
        s.finish("hermes", {"bot": "rift", "ticket": a["ticket"]})
        s.admit("hermes", {"bot": "rift", "task": "unclassed"})
        self.assertEqual(s.active("rift")["risk"], "high")

    def test_not_a_bot(self):
        with self.assertRaises(Refused):
            service().admit("hermes", {"bot": "ops", "task": "t"})


class OwedWakes(unittest.TestCase):
    """1 Nov, 10:02 PM on: each of Bramble's wakes the queue turned away was
    dropped, so Ops #205 and #206 sat green and unswept. A turned-away wake is
    owed here instead, and the owed-wake check asks when it can run."""

    def burst(self, s, n=20):
        for i in range(n):
            t = s.admit("hermes", {"bot": "rift", "task": f"t{i}"})["ticket"]
            s.finish("hermes", {"bot": "rift", "ticket": t})

    def test_nothing_is_owed_at_first(self):
        self.assertEqual(service().wake_due("hermes", {"bot": "bramble"}), {"owed": False, "due": False})

    def test_an_owed_wake_waits_out_the_hours_cap_and_is_due_after_it(self):
        clock = Clock()
        s = service(clock=clock)
        self.burst(s)
        with self.assertRaises(Refused):
            s.admit("hermes", {"bot": "bramble", "task": "wake", "source": "webhook"})
        s.owe_wake("hermes", {"bot": "bramble", "reason": "20 tickets were made in the last hour"})
        s.owe_wake("hermes", {"bot": "bramble", "reason": "again"})
        due = s.wake_due("hermes", {"bot": "bramble"})
        self.assertTrue(due["owed"])
        self.assertFalse(due["due"])
        self.assertIn("20 an hour", due["why_not"])
        self.assertEqual((due["refused"], due["reason"]), (2, "again"))
        clock.advance(hours=1, seconds=1)
        due = s.wake_due("hermes", {"bot": "bramble"})
        self.assertTrue(due["due"], due)
        self.assertEqual(due["why_not"], "")

    def test_asking_spends_no_ticket(self):
        s = service()
        s.owe_wake("hermes", {"bot": "bramble", "reason": "r"})
        for _ in range(30):
            s.wake_due("hermes", {"bot": "bramble"})
        self.assertEqual(s.db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 0)

    def test_a_running_turn_or_one_waiting_first_keeps_it_not_due(self):
        s = service()
        s.owe_wake("hermes", {"bot": "bramble", "reason": "r"})
        running = s.admit("hermes", {"bot": "rift", "task": "a"})["ticket"]
        self.assertIn("at most 1 at once", s.wake_due("hermes", {"bot": "bramble"})["why_not"])
        s.admit("hermes", {"bot": "wisp", "task": "b"})
        s.finish("hermes", {"bot": "rift", "ticket": running})
        self.assertIn("waiting first", s.wake_due("hermes", {"bot": "bramble"})["why_not"])

    def test_a_drained_host_keeps_it_not_due(self):
        low = lambda: HostState(1.2, 0, 0, 0, 0.1, 80, 80)
        s = service(host=low)
        s.owe_wake("hermes", {"bot": "bramble", "reason": "r"})
        self.assertFalse(s.wake_due("hermes", {"bot": "bramble"})["due"])

    def test_a_sweep_pays_it_once(self):
        s = service()
        s.owe_wake("hermes", {"bot": "bramble", "reason": "r"})
        paid = s.wake_paid("hermes", {"bot": "bramble"})
        self.assertEqual((paid["paid"], paid["refused"]), (True, 1))
        self.assertEqual(s.wake_paid("hermes", {"bot": "bramble"}), {"paid": False})
        self.assertFalse(s.wake_due("hermes", {"bot": "bramble"})["owed"])

    def test_it_outlives_a_restart_and_a_reload(self):
        db = Path(tempfile.mkdtemp()) / "policy.db"
        s = service(db=db)
        s.owe_wake("hermes", {"bot": "bramble", "reason": "r"})
        s.release_turns("root", {"why": "tw-reload-bots"})
        again = service(db=db)
        self.assertTrue(again.wake_due("hermes", {"bot": "bramble"})["owed"])

    def test_only_the_router_owes_a_wake_and_only_the_gateway_asks(self):
        s = service()
        for op in (s.owe_wake, s.wake_due, s.wake_paid):
            with self.assertRaises(Refused):
                op("hermes", {"bot": "rift"})
        for name in ("owe_wake", "wake_due", "wake_paid"):
            self.assertEqual(s.ops()[name][1], {s.policy.data["brokers"]["users"]["gateway"]}, name)

    def test_the_hours_cap_is_not_raised(self):
        self.assertEqual(policy().data["admission"]["queue_new_per_hour"], 20)


class OneSweepAtATime(unittest.TestCase):
    """2 Nov: two webhook turns of Bramble's, at 3:57:55 and 3:58:00 PM, each
    did the whole sweep and both revised #5458's card. One sweep runs at a
    time; a wake that comes while one runs is owed, and runs after it."""

    def sweep(self, s, task="wake"):
        return s.admit("hermes", {"bot": "bramble", "task": task, "source": "webhook", "sweep": True})

    def after(self):
        import copy
        data = copy.deepcopy(policy().data)
        data["admission"]["stage"] = "after"
        return data

    def test_a_second_sweep_is_turned_away_while_one_runs_and_starts_once_it_ends(self):
        s = service(data=self.after())
        first = self.sweep(s)
        self.assertEqual(first["state"], "admitted")
        with self.assertRaises(Refused) as e:
            self.sweep(s, "wake2")
        self.assertIn("one sweep runs at a time", str(e.exception))
        self.assertEqual(s.db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 1, "no ticket spent on it")
        s.finish("hermes", {"bot": "bramble", "ticket": first["ticket"]})
        self.assertEqual(self.sweep(s, "wake3")["state"], "admitted")

    def test_bramble_s_other_turns_and_other_bots_run_beside_a_sweep(self):
        s = service(data=self.after())
        self.sweep(s)
        self.assertEqual(s.admit("hermes", {"bot": "bramble", "task": "room", "source": "discord"})["state"], "admitted")
        self.assertEqual(s.admit("hermes", {"bot": "rift", "task": "t"})["state"], "admitted")

    def test_a_waiting_sweep_is_turned_away_once_another_sweep_got_in(self):
        s = service()
        rift = s.admit("hermes", {"bot": "rift", "task": "t"})["ticket"]
        waiting = self.sweep(s)
        self.assertEqual(waiting["state"], "waiting")
        s.finish("hermes", {"bot": "rift", "ticket": rift})
        s.db.execute("UPDATE tickets SET state='admitted', admitted_at=?, seen_at=? WHERE id=?",
                     ("2026-11-02T12:00:00+00:00", "2026-11-02T12:00:00+00:00", waiting["ticket"]))
        s.db.execute("INSERT INTO tickets(id, bot, kind, risk, task, source, state, created_at, sweep) "
                     "VALUES('w2','bramble','turn','high','wake2','webhook','waiting','2026-11-02T12:00:00+00:00',1)")
        with self.assertRaises(Refused):
            s.admit("hermes", {"bot": "bramble", "ticket": "w2"})
        self.assertEqual(s.db.execute("SELECT state FROM tickets WHERE id='w2'").fetchone()[0], "done",
                         "it holds no place in the line")

    def test_only_the_router_sweeps(self):
        with self.assertRaises(Refused):
            service().admit("hermes", {"bot": "rift", "task": "t", "sweep": True})

    def test_an_owed_wake_is_not_due_while_a_sweep_runs(self):
        s = service(data=self.after())
        first = self.sweep(s)
        s.owe_wake("hermes", {"bot": "bramble", "reason": "r"})
        due = s.wake_due("hermes", {"bot": "bramble"})
        self.assertFalse(due["due"])
        self.assertIn("one sweep runs at a time", due["why_not"])
        s.finish("hermes", {"bot": "bramble", "ticket": first["ticket"]})
        self.assertTrue(s.wake_due("hermes", {"bot": "bramble"})["due"])

    def test_a_sweep_pays_what_was_owed_when_it_started_and_not_what_came_during_it(self):
        clock = Clock()
        s = service(clock=clock, data=self.after())
        s.owe_wake("hermes", {"bot": "bramble", "reason": "before"})
        clock.advance(seconds=1)
        first = self.sweep(s)
        clock.advance(seconds=5)
        s.owe_wake("hermes", {"bot": "bramble", "reason": "during"})
        s.finish("hermes", {"bot": "bramble", "ticket": first["ticket"]})
        out = s.wake_paid("hermes", {"bot": "bramble", "ticket": first["ticket"]})
        self.assertFalse(out["paid"])
        self.assertTrue(s.wake_due("hermes", {"bot": "bramble"})["due"])
        clock.advance(seconds=5)
        second = self.sweep(s, "owed")
        s.finish("hermes", {"bot": "bramble", "ticket": second["ticket"]})
        self.assertTrue(s.wake_paid("hermes", {"bot": "bramble", "ticket": second["ticket"]})["paid"])
        self.assertFalse(s.wake_due("hermes", {"bot": "bramble"})["owed"])

    def test_a_store_made_before_the_sweep_column_takes_it(self):
        import sqlite3
        db = Path(tempfile.mkdtemp()) / "policy.db"
        old = sqlite3.connect(str(db))
        old.execute("CREATE TABLE tickets (id TEXT PRIMARY KEY, bot TEXT NOT NULL, kind TEXT NOT NULL, "
                    "risk TEXT NOT NULL, task TEXT NOT NULL, source TEXT NOT NULL, state TEXT NOT NULL, "
                    "created_at TEXT NOT NULL, admitted_at TEXT, seen_at TEXT, finished_at TEXT, reason TEXT)")
        old.execute("INSERT INTO tickets VALUES('t0','rift','turn','high','t','chat','done','2026-11-01T00:00:00+00:00',"
                    "NULL,NULL,NULL,NULL)")
        old.commit()
        old.close()
        s = service(db=db, data=self.after())
        self.assertEqual(self.sweep(s)["state"], "admitted")
        self.assertEqual(s.db.execute("SELECT sweep FROM tickets WHERE id='t0'").fetchone()[0], 0)



class Operations(unittest.TestCase):
    def begin(self, s, **kw):
        args = dict(bot="bramble", task="t1", action="merge", repo=APP, target="pr/42", commit=HEAD)
        args.update(kw)
        return s.op_begin("tw-git", args)

    def test_a_confirmed_write_is_never_done_again(self):
        s = service()
        op = self.begin(s)
        s.op_finish("tw-git", {"key": op["key"], "outcome": "confirmed", "receipt": {"sha": "m"}})
        with self.assertRaises(Refused) as e:
            self.begin(s)
        self.assertIn("already done", str(e.exception))

    def test_20_a_crash_before_the_receipt_means_reconcile(self):
        db = Path(tempfile.mkdtemp()) / "policy.db"
        s = service(db=db)
        op = self.begin(s)
        # tw-git (or tw-policy) stops here: the write may have happened.
        s.db.close()
        s2 = service(db=db)
        self.assertEqual(s2.op_get(op["key"])["state"], "unknown")
        again = self.begin(s2)
        self.assertEqual(again["state"], "reconcile")
        s2.op_reconcile("tw-git", {"key": op["key"], "found": True, "receipt": {"read_back": "merged"}})
        with self.assertRaises(Refused):
            self.begin(s2)

    def test_reconciled_not_found_may_try_again(self):
        s = service()
        op = self.begin(s)
        s.op_finish("tw-git", {"key": op["key"], "outcome": "unknown"})
        self.assertEqual(self.begin(s)["state"], "reconcile")
        s.op_reconcile("tw-git", {"key": op["key"], "found": False})
        again = self.begin(s)
        self.assertEqual(again["state"], "go")
        self.assertEqual(again["attempt"], 2)

    def test_one_write_at_a_time(self):
        s = service()
        self.begin(s)
        with self.assertRaises(Refused) as e:
            self.begin(s, target="pr/43")
        self.assertIn("in flight", str(e.exception))

    def test_a_new_head_is_a_new_operation(self):
        s = service()
        op = self.begin(s)
        s.op_finish("tw-git", {"key": op["key"], "outcome": "failed"})
        self.assertNotEqual(self.begin(s, commit=OTHER)["key"], op["key"])


class Decisions(unittest.TestCase):
    def record(self, s, **kw):
        d = new_decision(id=kw.get("id", "d1"), kind=kw.get("kind", "main"), repo=APP, commit=kw.get("commit", HEAD),
                         revision=1, answered_by=kw.get("by", "EvilPatrick06"), at=s.clock())
        return s.decision_record("root", {"decision": d.__dict__})

    def test_only_patrick_answers(self):
        with self.assertRaises(Refused):
            self.record(service(), by="thornswood-bramble[bot]")

    def test_used_once(self):
        s = service()
        self.record(s)
        self.assertTrue(s.check_decision("d1", kind="main", repo=APP, commit=HEAD).allowed)
        s.decision_use("tw-git", {"id": "d1"})
        v = s.check_decision("d1", kind="main", repo=APP, commit=HEAD)
        self.assertFalse(v.allowed)
        self.assertIn("used once", v.reason)

    def test_another_commit_or_expired(self):
        clock = Clock()
        s = service(clock=clock)
        self.record(s)
        self.assertFalse(s.check_decision("d1", kind="main", repo=APP, commit=OTHER).allowed)
        clock.advance(hours=73)
        self.assertFalse(s.check_decision("d1", kind="main", repo=APP, commit=HEAD).allowed)

    def test_21_a_restore_kills_main_records(self):
        s = service()
        self.record(s)
        self.record(s, id="d2", kind="cutover")
        self.assertEqual(s.mark_restored("root", {})["killed"], 2)
        self.assertIn("restored", s.check_decision("d1", kind="main", repo=APP, commit=HEAD).reason)
        self.assertIn("restored", s.check_decision("d2", kind="cutover", repo=APP, commit=HEAD).reason)


class Routes(unittest.TestCase):
    def test_a_subscription_limit_never_closes_credits(self):
        s = service()
        s.route_limit("hermes", {"route": "max", "window": "weekly", "reset_at": "2026-11-05T00:00:00+00:00"})
        self.assertFalse(s.route_state("max")["open"])
        self.assertTrue(s.route_state("credits")["open"])

    def test_a_credit_refusal_never_closes_route_1(self):
        s = service()
        st = s.credit_refused("tw-proxy", {})
        self.assertFalse(st["open"])
        self.assertTrue(s.route_state("max")["open"])
        self.assertEqual(st["closed"]["credit"][:10], "2026-11-21")

    def test_route_opens_only_when_every_window_reset(self):
        clock = Clock()
        s = service(clock=clock)
        s.route_limit("hermes", {"route": "max", "window": "session", "reset_at": "2026-11-02T13:00:00+00:00"})
        s.route_limit("hermes", {"route": "max", "window": "weekly", "reset_at": "2026-11-03T00:00:00+00:00"})
        clock.advance(hours=2)
        self.assertFalse(s.route_state("max")["open"])
        clock.advance(hours=11)
        self.assertTrue(s.route_state("max")["open"])

    def test_backoff_doubles_without_a_reset_time(self):
        clock = Clock()
        s = service(clock=clock)
        a = s.route_limit("hermes", {"route": "chatgpt"})["closed"]["default"]
        b = s.route_limit("hermes", {"route": "chatgpt"})["closed"]["default"]
        self.assertEqual(datetime.fromisoformat(a), clock.t + timedelta(seconds=60))
        self.assertEqual(datetime.fromisoformat(b), clock.t + timedelta(seconds=120))
        s.route_ok("hermes", {"route": "chatgpt"})
        c = s.route_limit("hermes", {"route": "chatgpt"})["closed"]["default"]
        self.assertEqual(datetime.fromisoformat(c), clock.t + timedelta(seconds=60))

    def test_credits_stop_with_ten_dollars_left_and_reset_on_the_21st(self):
        clock = Clock()
        s = service(clock=clock)
        s.credit_spend("tw-proxy", {"usd": 191})
        self.assertFalse(s.route_state("credits")["open"])
        clock.t = datetime(2026, 11, 21, 0, 1, tzinfo=timezone.utc)
        self.assertTrue(s.route_state("credits")["open"])

    def test_route_admit_needs_a_ticket_and_the_rules(self):
        s = service()
        with self.assertRaises(Refused):
            s.route_admit("tw-proxy", {"bot": "bramble", "route": "credits"})
        s.set_task_risk("hermes", {"bot": "bramble", "task": "triage", "risk": "low"})
        s.admit("hermes", {"bot": "bramble", "task": "triage"})
        self.assertEqual(s.route_admit("tw-proxy", {"bot": "bramble", "route": "credits"})["risk"], "low")

    def test_route_2_never_sees_a_worker_or_high_risk(self):
        s = service()
        s.admit("hermes", {"bot": "rift", "task": "code"})
        with self.assertRaises(Refused):
            s.route_admit("tw-proxy", {"bot": "rift", "route": "credits"})
        s2 = service()
        s2.admit("hermes", {"bot": "bramble", "task": "big change"})
        with self.assertRaises(Refused) as e:
            s2.route_admit("tw-proxy", {"bot": "bramble", "route": "credits"})
        self.assertIn("low-risk", str(e.exception))

    def test_a_route_that_is_not_ready_is_refused_even_to_an_admitted_turn(self):
        data = pilot_data()
        data["admission_proxy"]["ready"] = {"credits": False, "qwen": True}
        s = service(data=data)
        s.set_task_risk("hermes", {"bot": "bramble", "task": "triage", "risk": "low"})
        s.admit("hermes", {"bot": "bramble", "task": "triage"})
        with self.assertRaises(Refused) as e:
            s.route_admit("tw-proxy", {"bot": "bramble", "route": "credits"})
        self.assertIn("not ready", str(e.exception))
        self.assertTrue(s.route_admit("tw-proxy", {"bot": "bramble", "route": "qwen"})["admitted"])

    def test_a_closed_route_is_a_wait_with_its_opening_time(self):
        s = service()
        s.set_task_risk("hermes", {"bot": "bramble", "task": "triage", "risk": "low"})
        s.admit("hermes", {"bot": "bramble", "task": "triage"})
        s.credit_refused("tw-proxy", {})
        out = s.route_admit("tw-proxy", {"bot": "bramble", "route": "credits"})
        self.assertFalse(out["admitted"])
        self.assertEqual(out["until"][:10], "2026-11-21")
        s.credit_spend("tw-proxy", {"usd": 195})
        self.assertFalse(s.route_admit("tw-proxy", {"bot": "bramble", "route": "credits"})["admitted"])


class Leases(unittest.TestCase):
    def test_one_holder_and_a_new_generation_each_time(self):
        clock = Clock()
        s = service(clock=clock)
        a = s.lease_acquire("tw-proxy", {"name": "qwen", "holder": "t1", "ttl_seconds": 60})
        with self.assertRaises(Refused):
            s.lease_acquire("tw-proxy", {"name": "qwen", "holder": "t2"})
        self.assertFalse(s.lease_release("tw-proxy", {"name": "qwen", "holder": "t1", "generation": a["generation"] + 5})["released"])
        self.assertTrue(s.lease_release("tw-proxy", {"name": "qwen", "holder": "t1", "generation": a["generation"]})["released"])
        b = s.lease_acquire("tw-proxy", {"name": "qwen", "holder": "t2"})
        self.assertEqual(b["generation"], a["generation"] + 1)


class Ledger(unittest.TestCase):
    def test_an_issue_counts_once(self):
        s = service()
        for _ in range(3):
            s.ledger_event("tw-git", {"repo": APP, "issue": 3150, "event": "eligible"})
        s.ledger_event("tw-git", {"repo": APP, "issue": 3162, "event": "eligible"})
        self.assertEqual(len(s.ledger_pending()), 2)

    def test_a_verified_release_uses_its_set_and_a_failed_one_does_not(self):
        s = service()
        for n in (1, 2, 3):
            s.ledger_event("tw-git", {"repo": APP, "issue": n, "event": "eligible"})
        s.ledger_event("tw-git", {"repo": APP, "issue": 1, "event": "candidate_failed", "release": "c1"})
        self.assertEqual(len(s.ledger_pending()), 3)
        for n in (1, 2, 3):
            s.ledger_event("tw-git", {"repo": APP, "issue": n, "event": "Dev_verified", "release": "c2"})
        self.assertEqual(s.ledger_pending(), [])


class Callers(unittest.TestCase):
    def test_each_op_names_its_callers(self):
        ops = service().ops()
        self.assertEqual(ops["decision_record"][1], {"root"})
        self.assertEqual(ops["op_begin"][1], {"tw-git"})
        self.assertNotIn("hermes", ops["decision_use"][1])
        self.assertNotIn("hermes", ops["route_admit"][1])
        self.assertNotIn("hermes", ops["limits_record"][1])


if __name__ == "__main__":
    unittest.main()
