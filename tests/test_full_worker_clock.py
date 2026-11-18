"""Recent partial progress must not hide an overdue full-worker sweep."""
import shutil
import tempfile
import unittest
from pathlib import Path

from test_owed_wake import BUSY, Idle, NOON, POLICY
from test_worker_wake_progress import receipt, stamp
from thornswood_guard import owed_wake


class FullWorkerClock(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.path = self.home.joinpath(*owed_wake.IDLE_FILE)
        self.state = {"morning": "2026-11-09", "alert_checked": NOON,
                      "idle_checked": NOON - 300, "all_workers_checked": NOON - 4000}
        self.b = Idle(claimed=dict(BUSY), open_={'"area: Website"': 1, '"area: GitHub & CI"': 1})
        self.b.turns = {w: {"running": w not in ("bramble", "echo", "magpie"), "waiting": False}
                        for w in POLICY["bots"]}
        rows = [dict(receipt(f"old_{w}", NOON - 86400, "uncertain"), target=w) for w in ("echo", "magpie")]
        self.b.scan_rows = rows
        self.b.receipt_rows = {r["id"]: r for r in rows}

    def check(self, now=NOON):
        owed_wake._save(self.path, self.state)
        return "\n".join(owed_wake.check(self.home, "bramble", self.b, now=now, policy=POLICY))

    def test_overdue_full_scan_runs_despite_recent_partial_check(self):
        out = self.check()
        self.assertIn("Checked next issue for echo:", out)
        self.assertIn("Checked next issue for magpie:", out)
        self.assertEqual(owed_wake._load(self.path)["all_workers_checked"], NOON)
        self.assertEqual(sum(op == "board" for _, op, _ in self.b.asked), 1)

    def test_busy_pending_worker_does_not_hold_back_other_overdue_fields(self):
        self.state.update(pending_workers={"wisp": NOON - 4000}, said_at=NOON - 300, said=["previous Wisp look"])
        out = self.check()
        self.assertIn("Checked next issue for echo:", out)
        self.assertIn("Checked next issue for magpie:", out)
        self.assertNotIn("Checked next issue for wisp:", out)
        self.assertEqual(owed_wake._load(self.path)["pending_workers"]["wisp"], NOON - 4000)

    def test_fresh_full_snapshot_and_legacy_recent_check_stay_quiet(self):
        for full in (NOON - 300, "legacy"):
            self.b.asked.clear()
            if full == "legacy":
                self.state.pop("all_workers_checked", None)
            else:
                self.state["all_workers_checked"] = full
            with self.subTest(full=full):
                self.assertNotIn("Checked next issue", self.check())
                self.assertFalse(any(op == "board" for _, op, _ in self.b.asked))

    def test_source_backoff_and_work_hours_still_prevent_a_full_scan(self):
        self.state["progress_retry_after"] = NOON + 3600
        self.assertNotIn("Checked next issue", self.check())
        self.assertFalse(any(op == "board" for _, op, _ in self.b.asked))
        self.state.pop("progress_retry_after")
        self.b.asked.clear()
        self.assertNotIn("Checked next issue", self.check(NOON + 12 * 3600))
        self.assertFalse(any(op == "board" for _, op, _ in self.b.asked))

    def test_hand_back_still_checks_before_the_full_scan_is_due(self):
        self.state["all_workers_checked"] = NOON - 60
        self.b.turns["echo"]["last"] = stamp(NOON - 10)
        self.assertIn("Checked next issue for echo:", self.check())
