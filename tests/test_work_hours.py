"""Patrick's 17 November shift: workers 7 AM to midnight Mountain; Bramble always."""

import unittest

from helpers import policy
from thornswood_guard.hours import says, work_hours
from thornswood_guard.owed_wake import MORNING_HOUR


class WorkHours(unittest.TestCase):
    def test_installed_policy_and_default_match_the_requested_shift(self):
        self.assertEqual(work_hours(policy().data), (7, 24))
        self.assertEqual(work_hours(None), (7, 24))

    def test_morning_sweep_waits_until_audits_end_at_seven(self):
        self.assertEqual(MORNING_HOUR, 7)

    def test_midnight_boundary_is_named_without_ambiguity(self):
        self.assertEqual(says((7, 24)), "7 AM to midnight")

    def test_pausing_audits_still_allows_workers_at_any_hour(self):
        self.assertEqual(work_hours({"work": {"hours": [7, 24], "lanes_audit_overnight": False}}), (0, 24))

