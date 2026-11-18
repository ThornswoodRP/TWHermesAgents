"""An unidentified old brief cannot permanently idle a claimed worker."""
import copy
import shutil
import tempfile
import unittest
from pathlib import Path

from test_owed_wake import BUSY, Idle, NOON, POLICY
from test_worker_wake_progress import receipt
from thornswood_guard import owed_wake
from thornswood_guard.brokers import BrokerRefused


class IdleAmbiguousClaims(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.path = self.home.joinpath(*owed_wake.IDLE_FILE)
        owed_wake._save(self.path, {"morning": "2026-11-09", "alert_checked": NOON})
        self.b = Idle(claimed=dict(BUSY), open_={'"area: Website"': 3, '"area: GitHub & CI"': 2})
        self.b.turns = {w: {"running": w not in ("echo", "magpie"), "waiting": False}
                        for w in POLICY["bots"]}
        self.rows = {f"cx_old_{w}": dict(receipt(f"old_{w}", NOON - 86400, "uncertain"), target=w)
                     for w in ("echo", "magpie")}
        self.b.scan_rows = list(self.rows.values())
        self.b.receipt_rows = dict(self.rows)

    def look(self, broker=None):
        observed = {"workers": set(), "no_work": set()}
        lines = owed_wake._look("bramble", broker or self.b, NOON, POLICY,
                               {w: p["claim_label"] for w, p in POLICY["bots"].items() if p.get("role") == "worker"},
                               POLICY["repos"]["app"], set(), observed, self.rows)
        return "\n".join(lines), observed

    def test_idle_claimed_echo_and_magpie_receive_only_new_checked_candidates(self):
        before = copy.deepcopy(self.b.claimed)
        out = "\n".join(owed_wake.check(self.home, "bramble", self.b, now=NOON, policy=POLICY))
        for worker in ("echo", "magpie"):
            self.assertIn(f"Checked next issue for {worker}:", out)
            self.assertIn(worker, owed_wake._load(self.path)["pending_workers"])
        self.assertIn(f"Checked next issue for echo: {POLICY['repos']['app']}#9000.", out)
        self.assertIn(f"Checked next issue for magpie: {POLICY['repos']['app']}#9003.", out)
        self.assertNotIn("otherwise tell it to carry on", out)
        self.assertIn("prior claims", out)
        self.assertEqual(self.b.claimed, before, "no old claim is released or replayed")
        self.assertFalse(any(op in ("assign_task", "message_agent", "label") for _, op, _ in self.b.asked))

    def test_active_queued_waiting_and_unknown_workers_cannot_take_a_second_issue(self):
        for state, queued in (({"running": True, "waiting": False}, 0),
                              ({"running": False, "waiting": True}, 0),
                              ({"running": False, "waiting": False}, 1),
                              ({"running": False}, 0), ({}, 0), ({"running": 0, "waiting": False}, 0)):
            with self.subTest(state=state, queued=queued):
                self.b.turns["echo"] = state
                self.b.queued = {"echo": queued} if queued else {}
                out, observed = self.look()
                self.assertNotIn("Checked next issue for echo:", out)
                self.assertNotIn("echo", observed["workers"])
                self.assertIn("Checked next issue for magpie:", out)

    def test_malformed_runtime_or_queue_shapes_do_not_prove_idle_capacity(self):
        for kind, op in (("policy", "bot_turns"), ("decide", "queued_for")):
            for shape in (None, {}, {"bots": None}, {"bots": []}):
                def broker(broker_kind, broker_op, args):
                    return shape if (broker_kind, broker_op) == (kind, op) else self.b(broker_kind, broker_op, args)
                with self.subTest(kind=kind, shape=shape):
                    out, observed = self.look(broker)
                    self.assertNotIn("Checked next issue", out)
                    self.assertEqual(observed["workers"], set())
        def bad_counter(kind, op, args):
            return {"bots": {"echo": False}} if (kind, op) == ("decide", "queued_for") else self.b(kind, op, args)
        out, _ = self.look(bad_counter)
        self.assertNotIn("Checked next issue for echo:", out)
        self.assertIn("Checked next issue for magpie:", out)

    def test_exact_held_task_stays_excluded_while_a_different_issue_is_selected(self):
        row = dict(receipt("exact_held", NOON - 10, "cancelled",
                           {"repo": POLICY["repos"]["app"], "number": 9000}), target="echo")
        self.rows[row["id"]] = row
        out, observed = self.look()
        self.assertIn(f"Checked next issue for echo: {POLICY['repos']['app']}#9001.", out)
        self.assertNotIn(f"Checked next issue for echo: {POLICY['repos']['app']}#9000.", out)
        self.assertIn("echo", observed["workers"])

    def test_unavailable_runtime_metadata_cannot_be_treated_as_verified_idle(self):
        for unavailable in ("policy", "decide"):
            def broker(kind, op, args):
                if kind == unavailable and op in ("bot_turns", "queued_for"):
                    raise BrokerRefused("runtime metadata unavailable")
                return self.b(kind, op, args)
            with self.subTest(unavailable=unavailable):
                out, observed = self.look(broker)
                self.assertNotIn("Checked next issue for echo:", out)
                self.assertNotIn("Checked next issue for magpie:", out)
                self.assertEqual(observed["workers"], set())

    def test_admission_refusal_and_midnight_still_block_new_assignment_hints(self):
        self.b.room = False
        out = "\n".join(owed_wake.check(self.home, "bramble", self.b, now=NOON, policy=POLICY))
        self.assertNotIn("Checked next issue", out)
        self.b.room = True
        out = "\n".join(owed_wake.check(self.home, "bramble", self.b, now=NOON + 12 * 3600, policy=POLICY))
        self.assertNotIn("Checked next issue", out)
