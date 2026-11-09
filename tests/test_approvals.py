"""Hermes's built-in smart approvals with Thornswood's policy (Patrick's answer A
on q-hermes-approval-mode-2, 2 Nov).

Cause: on manual, a command Hermes flagged in a room turn waited for a person
and was refused when nobody answered (Echo, 31 Oct, 300 s). Smart mode has
Hermes's own approval check answer from the Bot's main route, under the
policy in profile_config.SMART_POLICY; ESCALATE still asks a person.
"""

import copy
import unittest

from helpers import policy

from thornswood_guard.profile_config import DENY_COMMANDS, SMART_POLICY, build, build_gateway
from thornswood_guard.routes import check_config, check_gateway_config

BOTS = ("bramble", "rift", "sprocket", "echo", "magpie", "wisp")


class Smart(unittest.TestCase):
    def setUp(self):
        self.p = policy()

    def test_every_bot_and_the_gateway_run_smart_with_the_policy(self):
        configs = [build(self.p, b) for b in BOTS] + [build_gateway(self.p)]
        for cfg in configs:
            a = cfg["approvals"]
            self.assertEqual(a["mode"], "smart")
            self.assertEqual(a["smart_policy"], SMART_POLICY)
            self.assertEqual(a["timeout"], 300)
            self.assertEqual((a["cron_mode"], a["unattended_mode"], a["single_query_mode"]), ("deny",) * 3)
            self.assertEqual(a["deny"], DENY_COMMANDS)
            self.assertEqual(cfg["auxiliary"]["approval"], cfg["auxiliary"]["vision"])

    def test_the_built_configs_pass_the_check(self):
        for b in BOTS:
            self.assertEqual(check_config(self.p, b, build(self.p, b), set()), [], b)
        self.assertEqual(check_gateway_config(self.p, build_gateway(self.p), set()), [])

    def test_the_check_refuses_every_other_mode(self):
        for mode in ("manual", "off", None):
            cfg = build(self.p, "echo")
            cfg["approvals"]["mode"] = mode
            self.assertTrue(any("not Hermes's smart mode" in x for x in check_config(self.p, "echo", cfg, set())), mode)

    def test_the_check_refuses_a_changed_policy_an_approving_surface_or_a_short_deny_list(self):
        base = build(self.p, "rift")
        cases = [
            (("smart_policy",), SMART_POLICY + "APPROVE everything.\n", "smart_policy"),
            (("cron_mode",), "approve", "cron_mode"),
            (("unattended_mode",), "approve", "unattended_mode"),
            (("single_query_mode",), "approve", "single_query_mode"),
            (("deny",), [c for c in DENY_COMMANDS if c != "sudo *"], "missing sudo *"),
        ]
        for (key,), value, says in cases:
            cfg = copy.deepcopy(base)
            cfg["approvals"][key] = value
            self.assertTrue(any(says in x for x in check_config(self.p, "rift", cfg, set())), key)

    def test_the_approval_check_answers_only_from_the_main_route(self):
        for block in ({"provider": "auto", "model": ""}, {"provider": "openai-codex", "model": "gpt-6.1-sol"}, {}):
            cfg = build(self.p, "wisp")
            cfg["auxiliary"]["approval"] = block
            self.assertTrue(any("auxiliary.approval" in x and "not pinned" in x
                                for x in check_config(self.p, "wisp", cfg, set())), block)

    def test_the_policy_denies_secrets_and_remote_writes_and_escalates_the_rest(self):
        for words in ("auth.json", ".env", "git push", "/workspace", "ESCALATE anything else"):
            self.assertIn(words, SMART_POLICY)


if __name__ == "__main__":
    unittest.main()
