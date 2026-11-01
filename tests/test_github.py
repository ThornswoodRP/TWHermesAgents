"""The git broker's rules: pushes, pull requests, merges, claims, runs and
the GitHub settings no Bot touches. Numbers in the test names point at the
attack tests in 08-SECURITY_AND_GUARD_RAILS.md section 8."""

import unittest

from helpers import (APP, HEAD, OTHER, decide, green, issue, past, policy, pr, rules, store,
                     verb_lists)

BOX = "scripts/vps/gate/verbs.json"
NEXUS = "scripts/release/nexus-verbs.json"


class Pushes(unittest.TestCase):
    def setUp(self):
        self.r = rules()

    def test_worker_pushes_own_branch(self):
        self.assertTrue(self.r.push("wisp", APP, "refs/heads/wisp/3150-hold-ingredients", force_with_lease=True).allowed)

    def test_06_worker_cannot_push_main(self):
        self.assertFalse(self.r.push("wisp", APP, "refs/heads/main").allowed)

    def test_06_nobody_pushes_main_not_even_bramble(self):
        self.assertFalse(self.r.push("bramble", APP, "refs/heads/main").allowed)

    def test_06_worker_cannot_push_a_tag(self):
        self.assertFalse(self.r.push("sprocket", APP, "refs/tags/v1.2.11").allowed)

    def test_06_worker_cannot_push_another_bots_branch(self):
        v = self.r.push("rift", APP, "refs/heads/wisp/3150-hold-ingredients")
        self.assertFalse(v.allowed)
        self.assertIn("Wisp", v.reason)

    def test_plain_force_push_refused(self):
        self.assertFalse(self.r.push("wisp", APP, "refs/heads/wisp/3150-x", force=True).allowed)

    def test_delete_push_refused(self):
        self.assertFalse(self.r.push("wisp", APP, "refs/heads/wisp/3150-x", delete=True).allowed)

    def test_branch_name_must_be_bot_issue_what(self):
        self.assertFalse(self.r.push("wisp", APP, "refs/heads/wisp/hold-ingredients").allowed)
        self.assertFalse(self.r.push("wisp", APP, "refs/heads/feature/x").allowed)

    def test_11_worker_app_cannot_push_a_workflow_change(self):
        v = self.r.push("magpie", APP, "refs/heads/magpie/4060-gate", workflow_files_changed=True)
        self.assertFalse(v.allowed)
        self.assertIn("Workflows permission", v.reason)

    def test_11_bramble_pushes_magpies_workflow_change(self):
        self.assertTrue(self.r.push("bramble", APP, "refs/heads/magpie/4060-gate", workflow_files_changed=True).allowed)

    def test_bramble_pushes_to_magpie_only_for_a_workflow_change(self):
        self.assertFalse(self.r.push("bramble", APP, "refs/heads/magpie/4060-gate").allowed)

    def test_bramble_does_not_push_other_workers_branches(self):
        self.assertFalse(self.r.push("bramble", APP, "refs/heads/echo/12-site", workflow_files_changed=True).allowed)

    def test_only_wisp_works_in_the_fork(self):
        fork = "EvilPatrick06/skymp"
        self.assertTrue(self.r.push("wisp", fork, "refs/heads/wisp/77-equip-drop").allowed)
        self.assertFalse(self.r.push("rift", fork, "refs/heads/rift/77-x").allowed)
        self.assertFalse(self.r.push("wisp", fork, "refs/heads/thornswood/engine-changes").allowed)

    def test_no_bot_writes_to_thornswood_hermes(self):
        self.assertFalse(self.r.push("bramble", "ThornswoodRP/Thornswood-Hermes", "refs/heads/bramble/1-x").allowed)

    def test_grok_lane_branches_are_hands_off(self):
        self.assertFalse(self.r.push("bramble", APP, "refs/heads/codex/2618-thing").allowed)
        self.assertFalse(self.r.push("wisp", APP, "refs/heads/anvil/12-x").allowed)

    def test_unknown_bot_refused(self):
        self.assertFalse(self.r.push("ops", APP, "refs/heads/ops/1-x").allowed)


class PullRequests(unittest.TestCase):
    def setUp(self):
        self.r = rules()

    def test_worker_opens_pr_from_own_branch_to_main(self):
        self.assertTrue(self.r.open_pr("sprocket", APP, "sprocket/3561-path-fence", "main").allowed)

    def test_fork_prs_go_to_engine_changes(self):
        fork = "EvilPatrick06/skymp"
        self.assertTrue(self.r.open_pr("wisp", fork, "wisp/77-equip-drop", "thornswood/engine-changes").allowed)
        self.assertFalse(self.r.open_pr("wisp", fork, "wisp/77-equip-drop", "main").allowed)

    def test_worker_cannot_open_pr_from_another_branch(self):
        self.assertFalse(self.r.open_pr("echo", APP, "sprocket/3561-path-fence", "main").allowed)

    def test_bramble_opens_the_pr_for_magpies_workflow_change(self):
        self.assertTrue(self.r.open_pr("bramble", APP, "magpie/4060-gate", "main").allowed)

    def test_edit_own_pr_only(self):
        self.assertTrue(self.r.edit_pr("sprocket", pr()).allowed)
        self.assertFalse(self.r.edit_pr("echo", pr()).allowed)

    def test_hands_off_people(self):
        self.assertFalse(self.r.edit_pr("bramble", pr(author_login="WiccanDjinn", author_bot=None)).allowed)
        self.assertFalse(self.r.edit_pr("bramble", pr(author_login="Jhinswife", author_bot=None)).allowed)


class Merges(unittest.TestCase):
    def setUp(self):
        self.p = policy()
        self.s = store(self.p)
        self.r = rules(self.p, self.s)
        self.lists = list(verb_lists().values())

    def merge(self, bot="bramble", p=None, sha=HEAD, method="squash", checks=None, **kw):
        return self.r.merge(bot, p or pr(), sha, method, green() if checks is None else checks,
                            base_verb_lists=self.lists, **kw)

    def test_bramble_merges_green_exact_head(self):
        self.assertTrue(self.merge().allowed)

    def test_06_worker_cannot_merge_its_own_pr(self):
        v = self.merge(bot="sprocket")
        self.assertFalse(v.allowed)
        self.assertIn("only Bramble merges", v.reason)

    def test_every_worker_is_refused(self):
        for bot in ("rift", "sprocket", "echo", "magpie", "wisp"):
            self.assertFalse(self.merge(bot=bot).allowed, bot)

    def test_head_moved(self):
        v = self.merge(sha=OTHER)
        self.assertFalse(v.allowed)
        self.assertIn("head moved", v.reason)

    def test_short_sha_refused(self):
        self.assertFalse(self.merge(sha=HEAD[:12]).allowed)

    def test_skipped_is_not_success(self):
        v = self.merge(checks=green(conclusion="skipped"))
        self.assertFalse(v.allowed)
        self.assertIn("skipped", v.reason)

    def test_red_is_never_merged(self):
        self.assertFalse(self.merge(checks=green(conclusion="failure")).allowed)

    def test_still_running_is_not_green(self):
        self.assertFalse(self.merge(checks=green(status="in_progress", conclusion=None)).allowed)

    def test_green_on_an_old_head_is_not_green(self):
        self.assertFalse(self.merge(checks=green(sha=OTHER)).allowed)

    def test_both_required_checks_are_green(self):
        v = self.merge()
        self.assertTrue(v.allowed, v.reason)
        self.assertIn("Launcher builds", v.reason)
        self.assertIn("Build check result", v.reason)

    def test_the_build_alone_is_not_enough(self):
        v = self.merge(checks=green(name="Launcher builds"))
        self.assertFalse(v.allowed)
        self.assertIn("Build check result", v.reason)

    def test_the_result_alone_is_not_enough(self):
        v = self.merge(checks=green(name="Build check result"))
        self.assertFalse(v.allowed)
        self.assertIn("Launcher builds", v.reason)

    def test_one_of_the_two_red_or_skipped_refuses(self):
        for bad in ("failure", "skipped", "cancelled"):
            checks = green(name="Launcher builds") + green(name="Build check result", conclusion=bad)
            self.assertFalse(self.merge(checks=checks).allowed, bad)
            checks = green(name="Launcher builds", conclusion=bad) + green(name="Build check result")
            self.assertFalse(self.merge(checks=checks).allowed, bad)

    def test_the_retired_vps_fallback_job_does_not_count(self):
        # Gone from PR builds since #5119; it was the old required check.
        self.assertFalse(self.merge(checks=green(name="Launcher builds (VPS fallback)")).allowed)
        checks = green(name="Launcher builds (VPS fallback)") + green(name="Launcher builds")
        self.assertFalse(self.merge(checks=checks).allowed)

    def test_the_latest_run_counts(self):
        checks = green(conclusion="success") + green(conclusion="failure")
        self.assertFalse(self.merge(checks=checks).allowed)

    def test_wrong_method(self):
        self.assertFalse(self.merge(method="merge").allowed)

    def test_fork_merges_with_merge(self):
        fork_pr = pr(repo="EvilPatrick06/skymp", base_ref="thornswood/engine-changes",
                     head_ref="wisp/77-equip-drop", author_bot="wisp")
        checks = green(name="build-pr")
        self.assertTrue(self.merge(p=fork_pr, method="merge", checks=checks).allowed)
        self.assertFalse(self.merge(p=fork_pr, method="squash", checks=checks).allowed)

    def test_draft_and_merged_refused(self):
        self.assertFalse(self.merge(p=pr(draft=True)).allowed)
        self.assertFalse(self.merge(p=pr(merged=True)).allowed)

    def test_wrong_base_refused(self):
        self.assertFalse(self.merge(p=pr(base_ref="release")).allowed)

    def test_hands_off_pr_refused(self):
        self.assertFalse(self.merge(p=pr(author_login="Jhinswife", author_bot=None)).allowed)
        self.assertFalse(self.merge(p=pr(head_ref="codex/2618-x", author_bot=None)).allowed)

    def gated(self, files):
        return pr(head_ref="magpie/4060-gate", author_bot="magpie", files=files)

    def test_11_workflow_change_waits_for_patrick(self):
        v = self.merge(p=self.gated([".github/workflows/box-run.yml"]))
        self.assertFalse(v.allowed)
        self.assertIn("live box", v.reason)

    def test_gate_and_verb_list_changes_wait_for_patrick(self):
        for f in ("scripts/vps/gate/GateRules.ps1", "scripts/release/nexus-verbs.json"):
            self.assertFalse(self.merge(p=self.gated([f])).allowed, f)

    def test_a_script_a_verb_runs_waits_for_patrick(self):
        v = self.merge(p=self.gated(["scripts/deploy/Deploy-DevGamemode.ps1"]))
        self.assertFalse(v.allowed)

    def test_patricks_yes_at_that_head_allows_it(self):
        decide(self.s, kind="box-change", commit=HEAD, pr_number=42)
        self.assertTrue(self.merge(p=self.gated([".github/workflows/box-run.yml"]), decision_id="d1").allowed)

    def test_a_yes_for_another_head_does_not(self):
        decide(self.s, kind="box-change", commit=OTHER, pr_number=42)
        v = self.merge(p=self.gated([".github/workflows/box-run.yml"]), decision_id="d1")
        self.assertFalse(v.allowed)
        self.assertIn("new head needs a new yes", v.reason)

    def test_a_yes_for_another_pr_does_not(self):
        decide(self.s, kind="box-change", commit=HEAD, pr_number=7)
        self.assertFalse(self.merge(p=self.gated([".github/workflows/x.yml"]), decision_id="d1").allowed)

    def test_13_a_message_saying_patrick_approved_is_not_a_record(self):
        v = self.merge(p=self.gated([".github/workflows/x.yml"]), decision_id="Patrick approved it in the room")
        self.assertFalse(v.allowed)
        self.assertIn("not a record", v.reason)

    def test_a_yes_still_needs_green(self):
        decide(self.s, kind="box-change", commit=HEAD, pr_number=42)
        v = self.merge(p=self.gated([".github/workflows/x.yml"]), decision_id="d1",
                       checks=green(conclusion="failure"))
        self.assertFalse(v.allowed)

    def test_bramble_never_merges_in_thornswood_hermes(self):
        self.assertFalse(self.merge(p=pr(repo="ThornswoodRP/Thornswood-Hermes")).allowed)


class Claims(unittest.TestCase):
    def setUp(self):
        self.p = policy()
        self.s = store(self.p)
        self.r = rules(self.p, self.s)

    def test_worker_claims_with_its_own_label(self):
        self.assertTrue(self.r.add_label("wisp", issue(), "claimed: Wisp").allowed)

    def test_worker_cannot_put_on_another_bots_label(self):
        self.assertFalse(self.r.add_label("wisp", issue(), "claimed: Rift").allowed)

    def test_bramble_claims_for_any_bot(self):
        self.assertTrue(self.r.add_label("bramble", issue(), "claimed: Rift").allowed)

    def test_only_the_six_labels(self):
        self.assertFalse(self.r.add_label("bramble", issue(), "claimed: Ops").allowed)
        self.assertFalse(self.r.add_label("bramble", issue(), "claimed: Guide").allowed)

    def test_old_labels_never_on_a_new_claim(self):
        for label in ("claimed: Claude", "claimed: ChatGPT", "claimed: Codex"):
            self.assertFalse(self.r.add_label("bramble", issue(), label).allowed, label)

    def test_grok_lane_labels_are_hands_off(self):
        self.assertFalse(self.r.add_label("bramble", issue(), "claimed: Anvil").allowed)

    def test_claim_race_stops_both(self):
        v = self.r.add_label("bramble", issue(labels=["claimed: Wisp"]), "claimed: Rift")
        self.assertFalse(v.allowed)
        self.assertIn("reconcile", v.reason)

    def test_a_lane_claim_is_never_won_by_claiming_over_it(self):
        self.assertFalse(self.r.add_label("bramble", issue(labels=["claimed: Waystone"]), "claimed: Rift").allowed)

    def test_hands_off_people(self):
        self.assertFalse(self.r.add_label("bramble", issue(author_login="WiccanDjinn"), "claimed: Wisp").allowed)
        self.assertFalse(self.r.add_label("bramble", issue(assignees=["Jhinswife"]), "claimed: Wisp").allowed)

    def test_a_worker_drops_only_its_own_claim(self):
        self.assertTrue(self.r.remove_label("wisp", issue(labels=["claimed: Wisp"]), "claimed: Wisp").allowed)
        self.assertFalse(self.r.remove_label("rift", issue(labels=["claimed: Wisp"]), "claimed: Wisp").allowed)

    def test_cutover_swap_needs_patricks_yes(self):
        old = issue(labels=["claimed: Claude"])
        self.assertFalse(self.r.remove_label("bramble", old, "claimed: Claude").allowed)
        self.assertFalse(self.r.remove_label("bramble", old, "claimed: Codex", cutover_commit=HEAD).allowed)
        decide(self.s, id="cut", kind="cutover", commit=HEAD, pr_number=None)
        self.assertTrue(self.r.remove_label("bramble", old, "claimed: Claude", decision_id="cut", cutover_commit=HEAD).allowed)
        self.assertTrue(self.r.remove_label("bramble", old, "claimed: Anvil", decision_id="cut", cutover_commit=HEAD).allowed)
        self.assertFalse(self.r.remove_label("wisp", old, "claimed: Claude", decision_id="cut", cutover_commit=HEAD).allowed)


class Runs(unittest.TestCase):
    def setUp(self):
        self.p = policy()
        self.s = store(self.p)
        self.r = rules(self.p, self.s)
        self.lists = verb_lists()

    def run_(self, bot="bramble", workflow="box-run.yml", ref="main", line="Get-Logs -Dev -Lines 2000",
             commit=HEAD, **kw):
        return self.r.start_run(bot, APP, workflow, ref, {"line": line}, self.lists, commit=commit, **kw)

    def test_bramble_starts_a_dev_gate_run(self):
        self.assertTrue(self.run_(line='Deploy-DevGamemode -LoadOrder -ShipFollows -Why "a gamemode update"').allowed)

    def test_07_workers_start_no_runs(self):
        for bot in ("rift", "sprocket", "echo", "magpie", "wisp"):
            for wf in ("box-run.yml", "nexus-run.yml", "release.yml"):
                self.assertFalse(self.run_(bot=bot, workflow=wf).allowed, (bot, wf))

    def test_08_workflow_not_on_the_list(self):
        self.assertFalse(self.run_(workflow="harnesses.yml").allowed)
        self.assertFalse(self.run_(workflow="github-admin.yml").allowed)

    def test_08_ref_other_than_main(self):
        self.assertFalse(self.run_(ref="bramble/1-x").allowed)
        self.assertFalse(self.run_(ref="refs/tags/v1").allowed)

    def test_08_verb_not_in_the_list(self):
        v = self.run_(line="Publish-Server -Channel dev")
        self.assertFalse(v.allowed)
        self.assertIn("no verb Publish-Server", v.reason)

    def test_08_parameter_the_verb_does_not_take(self):
        self.assertFalse(self.run_(line="Get-Logs -Everything").allowed)

    def test_08_value_that_breaks_the_pattern(self):
        self.assertFalse(self.run_(line="Get-Logs -Lines lots").allowed)

    def test_08_why_that_breaks_the_text_rule(self):
        self.assertFalse(self.run_(line='Deploy-DevGamemode -Why "$(whoami)"').allowed)
        self.assertFalse(self.run_(line='Deploy-DevGamemode -Why "a; b | c"').allowed)

    def test_08_extra_inputs_refused(self):
        v = self.r.start_run("bramble", APP, "box-run.yml", "main", {"line": "Get-Logs", "ref": "x"},
                             self.lists, commit=HEAD)
        self.assertFalse(v.allowed)

    def test_nexus_key_verb_is_never_run(self):
        v = self.run_(workflow="nexus-run.yml", line="Set-NexusKey")
        self.assertFalse(v.allowed)
        self.assertIn("secret", v.reason)

    def test_publishing_a_revision_needs_patrick(self):
        line = "New-NexusRevision -Mod 1 -FileId 2 -Publish 5"
        self.assertFalse(self.run_(workflow="nexus-run.yml", line=line).allowed)
        decide(self.s, id="pub", kind="publish", commit=HEAD, pr_number=None)
        self.assertTrue(self.run_(workflow="nexus-run.yml", line=line, decision_id="pub").allowed)

    def test_a_revision_without_publish_runs_on_dev(self):
        self.assertTrue(self.run_(workflow="nexus-run.yml", line="New-NexusRevision -Mod 1 -FileId 2").allowed)

    def test_09_main_with_no_record(self):
        v = self.run_(workflow="release.yml", line="", channel="main")
        self.assertFalse(v.allowed)

    def test_09_main_with_a_record_for_another_commit(self):
        decide(self.s, id="m", kind="main", commit=OTHER, pr_number=None)
        self.assertFalse(self.run_(workflow="release.yml", channel="main", decision_id="m").allowed)

    def test_09_main_with_a_stale_record(self):
        decide(self.s, id="m", kind="main", commit=HEAD, pr_number=None, hours=1, at=past(2))
        v = self.run_(workflow="release.yml", channel="main", decision_id="m")
        self.assertFalse(v.allowed)
        self.assertIn("expired", v.reason)

    def test_09_main_record_is_used_once(self):
        decide(self.s, id="m", kind="main", commit=HEAD, pr_number=None)
        self.assertTrue(self.run_(workflow="release.yml", channel="main", decision_id="m").allowed)
        self.s.use("m")
        v = self.run_(workflow="release.yml", channel="main", decision_id="m")
        self.assertFalse(v.allowed)
        self.assertIn("used once", v.reason)

    def test_09_valid_main_still_waits_in_github(self):
        decide(self.s, id="m", kind="main", commit=HEAD, pr_number=None)
        v = self.run_(workflow="release.yml", channel="main", decision_id="m")
        self.assertIn("still waits for Patrick", v.reason)

    def test_a_box_change_yes_does_not_approve_main(self):
        decide(self.s, id="b", kind="box-change", commit=HEAD, pr_number=None)
        self.assertFalse(self.run_(workflow="release.yml", channel="main", decision_id="b").allowed)

    def test_run_needs_the_full_commit(self):
        self.assertFalse(self.run_(commit="main").allowed)


class Settings(unittest.TestCase):
    def setUp(self):
        self.r = rules()

    def test_10_nobody_approves_a_deployment_or_touches_settings(self):
        calls = [
            ("POST", "/repos/EvilPatrick06/Thornswood/actions/runs/123/pending_deployments"),
            ("PUT", "/repos/EvilPatrick06/Thornswood/rulesets/1"),
            ("PUT", "/repos/EvilPatrick06/Thornswood/environments/desk"),
            ("PUT", "/repos/EvilPatrick06/Thornswood/actions/secrets/DESK_KEY_B64"),
            ("PUT", "/repos/EvilPatrick06/Thornswood/collaborators/someone"),
            ("POST", "/repos/EvilPatrick06/Thornswood/hooks"),
            ("PATCH", "/repos/EvilPatrick06/Thornswood"),
            ("PUT", "/repos/EvilPatrick06/Thornswood/branches/main/protection"),
            ("POST", "/repos/EvilPatrick06/Thornswood/keys"),
        ]
        for bot in ("bramble", "magpie"):
            for method, path in calls:
                self.assertFalse(self.r.admin_call(bot, method, path).allowed, (bot, method, path))

    def test_unknown_writes_are_refused(self):
        self.assertFalse(self.r.admin_call("bramble", "POST", "/repos/x/y/something-new").allowed)

    def test_reads_are_fine(self):
        self.assertTrue(self.r.admin_call("rift", "GET", "/repos/x/y/pulls").allowed)


if __name__ == "__main__":
    unittest.main()
