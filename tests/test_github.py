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
        fork = "ThornswoodRP/skymp"
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


class OpsIsBrambles(unittest.TestCase):
    """Ops is private on GitHub Free, which keeps no rules there, so the
    workers' App is not installed on it and no worker writes in it: only
    Bramble's App can change Ops's main, and only by merging."""

    OPS = "ThornswoodRP/Thornswood-Ops"

    def setUp(self):
        self.r = rules()

    def test_a_worker_neither_pushes_nor_opens_a_pull_request_in_ops(self):
        for bot in ("rift", "sprocket", "echo", "magpie", "wisp"):
            with self.subTest(bot=bot):
                v = self.r.push(bot, self.OPS, f"refs/heads/{bot}/12-x")
                self.assertFalse(v.allowed)
                self.assertIn("Bramble's alone", v.reason)
                self.assertFalse(self.r.open_pr(bot, self.OPS, f"{bot}/12-x", "main").allowed)

    def test_a_worker_neither_labels_nor_edits_in_ops(self):
        self.assertFalse(self.r.add_label("rift", issue(repo=self.OPS), "claimed: Rift").allowed)
        self.assertFalse(self.r.remove_label("rift", issue(repo=self.OPS, labels=["claimed: Rift"]), "claimed: Rift").allowed)
        self.assertFalse(self.r.edit_pr("rift", pr(repo=self.OPS, head_ref="rift/12-x", author_bot="rift")).allowed)

    def test_bramble_still_works_in_ops(self):
        self.assertTrue(self.r.push("bramble", self.OPS, "refs/heads/bramble/12-x").allowed)
        self.assertTrue(self.r.open_pr("bramble", self.OPS, "bramble/12-x", "main").allowed)
        self.assertTrue(self.r.add_label("bramble", issue(repo=self.OPS), "claimed: Bramble").allowed)

    def test_the_workers_app_is_on_the_two_public_repos_only(self):
        p = policy()
        self.assertEqual(sorted(p.data["github_apps"]["workers"]["repos"]), [APP, "ThornswoodRP/skymp"])
        self.assertEqual(p.reader_for(self.OPS), "bramble")
        self.assertEqual(p.reader_for(APP), "workers")


class PullRequests(unittest.TestCase):
    def setUp(self):
        self.r = rules()

    def test_worker_opens_pr_from_own_branch_to_main(self):
        self.assertTrue(self.r.open_pr("sprocket", APP, "sprocket/3561-path-fence", "main").allowed)

    def test_fork_prs_go_to_engine_changes(self):
        fork = "ThornswoodRP/skymp"
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


OPS = "ThornswoodRP/Thornswood-Ops"
FORK = "ThornswoodRP/skymp"
# The status checks main's ruleset requires on Thornswood (read 2 Nov 2026),
# and the fork branch's.
APP_RULES = {"Build check result", "Test tasks in the pull request", "Release note"}
FORK_RULES = {"build-pr", "save-directory-linux"}


class Merges(unittest.TestCase):
    """Who may merge, at which head, with which method: the same in every repo."""

    def setUp(self):
        self.p = policy()
        self.s = store(self.p)
        self.r = rules(self.p, self.s)

    def merge(self, bot="bramble", p=None, sha=HEAD, method="squash", checks=None, branch_checks=APP_RULES):
        return self.r.merge(bot, p or pr(), sha, method, green() if checks is None else checks,
                            branch_checks=branch_checks)

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

    def test_wrong_method(self):
        self.assertFalse(self.merge(method="merge").allowed)

    def test_draft_and_merged_refused(self):
        self.assertFalse(self.merge(p=pr(draft=True)).allowed)
        self.assertFalse(self.merge(p=pr(merged=True)).allowed)

    def test_wrong_base_refused(self):
        self.assertFalse(self.merge(p=pr(base_ref="release")).allowed)

    def test_hands_off_pr_refused(self):
        self.assertFalse(self.merge(p=pr(author_login="Jhinswife", author_bot=None)).allowed)
        self.assertFalse(self.merge(p=pr(head_ref="codex/2618-x", author_bot=None)).allowed)

    def test_bramble_never_merges_in_thornswood_hermes(self):
        self.assertFalse(self.merge(p=pr(repo="ThornswoodRP/Thornswood-Hermes")).allowed)


class AutoMerges(unittest.TestCase):
    """Patrick, 2 Nov 2026: "He shouldn't have to be merging to begin with".
    In Thornswood and the fork GitHub's auto-merge merges: Bramble turns it on
    for a Bot's pull request at its exact head, GitHub merges once the checks
    the branch's rules require pass, and nothing waits for a yes."""

    def setUp(self):
        self.p = policy()
        self.r = rules(self.p, store(self.p))

    def merge(self, p=None, checks=None, branch_checks=APP_RULES, method="squash"):
        return self.r.merge("bramble", p or pr(), HEAD, method, [] if checks is None else checks,
                            branch_checks=branch_checks)

    def test_the_shipped_policy_has_auto_merge_in_thornswood_and_the_fork_only(self):
        self.assertTrue(self.p.auto_merge(APP))
        self.assertTrue(self.p.auto_merge(FORK))
        self.assertFalse(self.p.auto_merge(OPS))

    def test_a_bots_pr_goes_on_auto_merge_before_its_checks_finish(self):
        for checks in ([], green(status="in_progress", conclusion=None), green()):
            v = self.merge(checks=checks)
            self.assertTrue(v.allowed, v.reason)
            self.assertIn("auto-merge on", v.reason)
            self.assertIn("Build check result", v.reason)

    def test_a_threads_or_a_persons_pr_is_its_owners(self):
        for p in (pr(head_ref="claude/5381-avalonia", author_bot=None, author_login="EvilPatrick06"),
                  pr(head_ref="claude/9-x", author_bot=None, author_login="WiccanDjinn-not-hands-off")):
            v = self.merge(p=p)
            self.assertFalse(v.allowed)
            self.assertIn("its owner puts it on auto-merge", v.reason)

    def test_a_robots_pr_goes_on_auto_merge_like_a_bots(self):
        for login, ref in (("dependabot[bot]", "dependabot/github_actions/actions-abc"),
                           ("thornswood-pin-robot[bot]", "engine-pin/next")):
            p = pr(head_ref=ref, author_bot=None, author_login=login, files=[".github/workflows/build-pr.yml"])
            v = self.merge(p=p)
            self.assertTrue(v.allowed, f"{login}: {v.reason}")

    def test_rules_that_do_not_require_the_check_refuse(self):
        for branch_checks in (None, set(), {"Release note"}):
            v = self.merge(branch_checks=branch_checks)
            self.assertFalse(v.allowed)
            self.assertIn("Build check result", v.reason)

    def test_a_red_or_skipped_check_is_never_put_on_auto_merge(self):
        for bad in ("failure", "skipped", "cancelled"):
            v = self.merge(checks=green(name="Launcher builds", conclusion=bad))
            self.assertFalse(v.allowed, bad)
            self.assertIn(bad, v.reason)

    def test_red_on_an_old_head_does_not_hold_the_new_one(self):
        self.assertTrue(self.merge(checks=green(sha=OTHER, conclusion="failure")).allowed)

    def test_11_a_workflow_gate_or_verb_script_change_needs_no_yes(self):
        # Patrick, 2 Nov 2026: "I shouldn't need to approve".
        for f in (".github/workflows/box-run.yml", "scripts/vps/gate/GateRules.ps1",
                  "scripts/release/nexus-verbs.json", "scripts/deploy/Deploy-DevGamemode.ps1"):
            p = pr(head_ref="magpie/4060-gate", author_bot="magpie", files=[f])
            v = self.merge(p=p)
            self.assertTrue(v.allowed, f"{f}: {v.reason}")

    def test_the_fork_goes_on_auto_merge_with_a_merge_commit(self):
        fork_pr = pr(repo=FORK, base_ref="thornswood/engine-changes", head_ref="wisp/77-equip-drop", author_bot="wisp")
        self.assertTrue(self.merge(p=fork_pr, method="merge", branch_checks=FORK_RULES).allowed)
        self.assertFalse(self.merge(p=fork_pr, method="squash", branch_checks=FORK_RULES).allowed)
        self.assertFalse(self.merge(p=fork_pr, method="merge", branch_checks={"save-directory-linux"}).allowed)


class OpsMerges(unittest.TestCase):
    """Ops is private on GitHub Free, which has no auto-merge: Bramble merges a
    pull request there itself, only when its check passed on the exact head."""

    def setUp(self):
        self.p = policy()
        self.r = rules(self.p, store(self.p))

    def merge(self, p=None, checks=None):
        return self.r.merge("bramble", p or pr(repo=OPS), HEAD, "squash",
                            green(name="check") if checks is None else checks)

    def test_bramble_merges_green_exact_head(self):
        v = self.merge()
        self.assertTrue(v.allowed, v.reason)
        self.assertIn("green on", v.reason)

    def test_a_threads_ops_pr_merges_too(self):
        self.assertTrue(self.merge(p=pr(repo=OPS, head_ref="claude/x", author_bot=None,
                                        author_login="EvilPatrick06")).allowed)

    def test_a_workflow_change_needs_no_yes(self):
        self.assertTrue(self.merge(p=pr(repo=OPS, files=[".github/workflows/box-run.yml"])).allowed)

    def test_skipped_is_not_success(self):
        v = self.merge(checks=green(name="check", conclusion="skipped"))
        self.assertFalse(v.allowed)
        self.assertIn("skipped", v.reason)

    def test_red_is_never_merged(self):
        self.assertFalse(self.merge(checks=green(name="check", conclusion="failure")).allowed)

    def test_still_running_is_not_green(self):
        self.assertFalse(self.merge(checks=green(name="check", status="in_progress", conclusion=None)).allowed)

    def test_green_on_an_old_head_is_not_green(self):
        self.assertFalse(self.merge(checks=green(name="check", sha=OTHER)).allowed)

    def test_the_latest_run_counts(self):
        checks = green(name="check") + green(name="check", conclusion="failure")
        self.assertFalse(self.merge(checks=checks).allowed)

    def test_no_check_at_all_is_not_green(self):
        self.assertFalse(self.merge(checks=[]).allowed)


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
        # The main gate itself, as it is when Patrick wants his word for each
        # main release; MainWithoutAYes below tests the shipped setting.
        self.p.data["runs"]["main_without_patrick"] = False
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

    def test_a_publish_yes_does_not_approve_main(self):
        decide(self.s, id="b", kind="publish", commit=HEAD, pr_number=None)
        self.assertFalse(self.run_(workflow="release.yml", channel="main", decision_id="b").allowed)

    def test_run_needs_the_full_commit(self):
        self.assertFalse(self.run_(commit="main").allowed)



class TunnelRun(unittest.TestCase):
    """tunnel.yml (Ops, Thornswood #5334) takes no gate line: its mode, wait
    and recipient are each held to the pattern policy.yaml names."""

    def setUp(self):
        self.p = policy()
        self.s = store(self.p)
        self.r = rules(self.p, self.s)
        self.lists = verb_lists()

    def run_(self, inputs, bot="bramble"):
        return self.r.start_run(bot, APP, "tunnel.yml", "main", inputs, self.lists, commit=HEAD)

    def test_it_lives_in_ops(self):
        self.assertEqual(self.p.data["runs"]["workflows"]["tunnel.yml"]["repo"], "ThornswoodRP/Thornswood-Ops")

    def test_each_mode_runs(self):
        for mode in ("install", "switch", "back"):
            v = self.run_({"mode": mode})
            self.assertTrue(v.allowed, (mode, v.reason))
        self.assertTrue(self.run_({"mode": "switch", "wait": "120"}).allowed)
        self.assertTrue(self.run_({"mode": "install", "wait": "", "recipient": ""}).allowed)

    def test_a_mode_it_does_not_have_is_refused(self):
        for mode in ("delete", "switch -Back", "install;whoami", "Install", ""):
            self.assertFalse(self.run_({"mode": mode}).allowed, mode)

    def test_the_mode_is_needed(self):
        v = self.run_({"wait": "120"})
        self.assertFalse(v.allowed)
        self.assertIn("needs mode", v.reason)

    def test_wait_is_up_to_three_digits(self):
        for wait in ("1000", "2m", "-1", "12 -Back"):
            self.assertFalse(self.run_({"mode": "switch", "wait": wait}).allowed, wait)

    def test_no_line_and_no_seal_key_come_in(self):
        for extra in ({"line": "Install-Tunnel -Switch"}, {"seal_key": "<RSAKeyValue/>"}, {"sealed": "abc"}):
            v = self.run_({"mode": "install", **extra})
            self.assertFalse(v.allowed, extra)
            self.assertIn("takes only", v.reason)

    def test_a_recipient_is_a_key_and_nothing_else(self):
        self.assertTrue(self.run_({"mode": "install", "recipient": "<RSAKeyValue><Modulus>ab+/=</Modulus></RSAKeyValue>"}).allowed)
        self.assertFalse(self.run_({"mode": "install", "recipient": "a b"}).allowed)
        self.assertFalse(self.run_({"mode": "install", "recipient": "x\"; rm"}).allowed)

    def test_only_bramble_starts_it(self):
        for bot in ("rift", "sprocket", "echo", "magpie", "wisp"):
            self.assertFalse(self.run_({"mode": "install"}, bot=bot).allowed, bot)

    def test_a_pattern_that_does_not_compile_stops_the_policy(self):
        from thornswood_guard.policy import Policy, PolicyError
        import copy
        data = copy.deepcopy(self.p.data)
        data["runs"]["workflows"]["tunnel.yml"]["inputs"]["mode"] = "install|("
        with self.assertRaises(PolicyError):
            Policy(data)
        data = copy.deepcopy(self.p.data)
        data["runs"]["workflows"]["tunnel.yml"]["required"] = ["line"]
        with self.assertRaises(PolicyError):
            Policy(data)


class MainWithoutAYes(unittest.TestCase):
    """Patrick, 31 Oct and 1 Nov: main releases go out without his yes for
    each one, for now. What still needs him stays as it was."""

    def setUp(self):
        self.p = policy()
        self.s = store(self.p)
        self.r = rules(self.p, self.s)
        self.lists = verb_lists()

    def run_(self, bot="bramble", workflow="release.yml", line="", channel="main", **kw):
        return self.r.start_run(bot, APP, workflow, "main", {"line": line}, self.lists, commit=HEAD,
                                channel=channel, **kw)

    def test_the_shipped_policy_allows_main_without_a_record(self):
        self.assertIs(self.p.data["runs"]["main_without_patrick"], True)
        v = self.run_()
        self.assertTrue(v.allowed, v.reason)
        self.assertIn("without a yes for each", v.reason)

    def test_only_bramble_and_only_the_listed_runs_still(self):
        self.assertFalse(self.run_(bot="sprocket").allowed)
        self.assertFalse(self.run_(workflow="harnesses.yml").allowed)
        self.assertFalse(self.r.start_run("bramble", APP, "release.yml", "main", {"line": ""}, self.lists,
                                          commit="main", channel="main").allowed)

    def test_publishing_a_revision_still_needs_patrick(self):
        line = "New-NexusRevision -Mod 1 -FileId 2 -Publish 5"
        v = self.run_(workflow="nexus-run.yml", line=line)
        self.assertFalse(v.allowed)
        self.assertIn("needs Patrick's word", v.reason)

    def test_a_secret_verb_is_still_never_run(self):
        self.assertFalse(self.run_(workflow="nexus-run.yml", line="Set-NexusKey").allowed)


class Settings(unittest.TestCase):
    def setUp(self):
        self.r = rules()

    def test_10_nobody_approves_a_deployment_or_touches_settings(self):
        calls = [
            ("POST", "/repos/ThornswoodRP/Thornswood/actions/runs/123/pending_deployments"),
            ("PUT", "/repos/ThornswoodRP/Thornswood/rulesets/1"),
            ("PUT", "/repos/ThornswoodRP/Thornswood/environments/desk"),
            ("PUT", "/repos/ThornswoodRP/Thornswood/actions/secrets/DESK_KEY_B64"),
            ("PUT", "/repos/ThornswoodRP/Thornswood/collaborators/someone"),
            ("POST", "/repos/ThornswoodRP/Thornswood/hooks"),
            ("PATCH", "/repos/ThornswoodRP/Thornswood"),
            ("PUT", "/repos/ThornswoodRP/Thornswood/branches/main/protection"),
            ("POST", "/repos/ThornswoodRP/Thornswood/keys"),
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
