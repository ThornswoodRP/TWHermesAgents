"""scripts/apply.py against a stand-in for the VM's Hermes home, the rendered
profile files, and the repo itself holding no tokens."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml
from helpers import REPO, policy

from render_check import rendered, rendered_policy_json


def make_home(root: Path, bots, *, skip_skill=None, env_root="", env_bot=None) -> Path:
    home = root / ".hermes"
    for bot in bots:
        p = home / "profiles" / bot
        (p / "skills" / "thornswood" / "thornswood-ops").mkdir(parents=True)
        (p / "skills" / "thornswood" / "thornswood-guide").mkdir(parents=True)
        for skill in ("thornswood-ops", "thornswood-guide"):
            if skill != skip_skill:
                (p / "skills" / "thornswood" / skill / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
        (p / "SOUL.md").write_text(f"# SOUL.md\n\nYou're **{bot}**.\n", encoding="utf-8")
        (p / "config.yaml").write_text(
            "model:\n  default: anthropic/claude-opus-4.6\n  provider: auto\n  base_url: https://openrouter.ai/api/v1\n_config_version: 51\n",
            encoding="utf-8")
        (p / ".env").write_text("# Per-profile secrets for this Hermes profile.\n" + ((env_bot or {}).get(bot, "")), encoding="utf-8")
        (p / "memories").mkdir()
        (p / "memories" / "MEMORY.md").write_text("I am here.\n", encoding="utf-8")
    (home / ".env").write_text(env_root, encoding="utf-8")
    return home


class Apply(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A clean checkout of exactly this tree, committed, as the VM gets it.
        cls.root = Path(tempfile.mkdtemp())
        cls.repo = cls.root / "repo"
        shutil.copytree(REPO, cls.repo, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        for cmd in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "test"]):
            subprocess.run(["git", "-C", str(cls.repo), *cmd], check=True, env=env)
        cls.commit = subprocess.run(["git", "-C", str(cls.repo), "rev-parse", "HEAD"], check=True,
                                    capture_output=True, text=True).stdout.strip()

    def run_apply(self, home: Path, *extra, expect=None, image="present", podman=None, wake_secret=True,
                  hermes=None):
        work = home.parent / "tw-work"
        secret = home.parent / "github-webhook.env"
        if wake_secret:
            secret.write_text("WEBHOOK_SECRET=" + "0" * 64 + "\n", encoding="utf-8")
        else:
            secret.unlink(missing_ok=True)
        state = home.parent / "podman.state"
        state.write_text(image, encoding="utf-8")
        fake = f"{Path(sys.executable).as_posix()} {(Path(__file__).with_name('fake_podman.py')).as_posix()}"
        fake_hermes = f"{Path(sys.executable).as_posix()} {(Path(__file__).with_name('fake_hermes.py')).as_posix()}"
        env = dict(os.environ, FAKE_PODMAN_STATE=str(state), FAKE_HERMES_STATE=str(home.parent / "hermes.json"))
        return subprocess.run([sys.executable, str(self.repo / "scripts" / "apply.py"), "--expect", expect or self.commit,
                               "--hermes-home", str(home), "--work-root", str(work), "--podman", podman or fake,
                               "--hermes", hermes or fake_hermes, "--wake-secret-file", str(secret), *extra],
                              capture_output=True, text=True, env=env)

    def hermes_state(self, home: Path) -> dict:
        f = home.parent / "hermes.json"
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}

    def podman_log(self, home: Path) -> str:
        log = home.parent / "podman.log"
        return log.read_text(encoding="utf-8") if log.exists() else ""

    def test_the_sandbox_image_is_pulled_before_anything_is_written(self):
        home = self.home()
        r = self.run_apply(home, image="missing")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("pull -q docker.io/nousresearch/hermes-sandbox:desktop", self.podman_log(home))
        cfg = yaml.safe_load((home / "profiles" / "rift" / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(cfg["terminal"]["backend"], "docker")
        self.assertEqual(cfg["terminal"]["docker_image"], "docker.io/nousresearch/hermes-sandbox:desktop")
        self.assertFalse(cfg["terminal"]["docker_network"])

    def test_a_failed_pull_changes_nothing(self):
        home = self.home()
        r = self.run_apply(home, image="broken-pull")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Pulling", r.stdout)
        self.assertFalse((home / "profiles" / "rift" / "plugins").exists())

    def test_check_never_pulls(self):
        home = self.home()
        r = self.run_apply(home, "--check", image="missing")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("pull", self.podman_log(home))

    def test_no_podman_stops_everything(self):
        home = self.home()
        r = self.run_apply(home, podman="no-such-podman-here")
        self.assertEqual(r.returncode, 1)
        self.assertIn("podman could not be run", r.stdout)
        self.assertFalse((home / "profiles" / "rift" / "plugins").exists())

    def home(self, **kw):
        return make_home(Path(tempfile.mkdtemp()), list(policy().bots), **kw)

    def test_applies_to_the_six_existing_profiles(self):
        home = self.home(env_root="DISCORD_BOT_TOKEN=canary\nDISCORD_ALLOWED_USERS=1\n")
        r = self.run_apply(home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("self-test passed", r.stdout)
        for bot in policy().bots:
            p = home / "profiles" / bot
            cfg = yaml.safe_load((p / "config.yaml").read_text(encoding="utf-8"))
            self.assertEqual(cfg["model"], {"provider": "anthropic", "default": "claude-opus-5-5"})
            self.assertEqual(cfg["_config_version"], 51)
            self.assertEqual(cfg["plugins"]["enabled"], ["thornswood_guard", "thornswood_live"])
            self.assertTrue((p / "plugins" / "thornswood_guard" / "policy.yaml").is_file())
            # The live Board's observer, whole, and no copy of the guard in its folder.
            self.assertTrue((p / "plugins" / "thornswood_live" / "plugin.yaml").is_file())
            self.assertFalse((p / "plugins" / "thornswood_live" / "policy.yaml").exists())
            self.assertEqual((p / "SOUL.md").read_text(encoding="utf-8"), f"# SOUL.md\n\nYou're **{bot}**.\n")
            self.assertEqual((p / "memories" / "MEMORY.md").read_text(encoding="utf-8"), "I am here.\n")
            self.assertTrue((home.parent / "tw-work" / bot).is_dir())
            self.assertIn(self.commit, (p / "local" / "thornswood-hermes" / "applied.json").read_text(encoding="utf-8"))

    def test_bramble_gets_its_duties_skill_from_the_repo_and_no_other_bot_does(self):
        # 2 Nov: Bramble had merge authority, and nothing told it to merge.
        home = self.home()
        r = self.run_apply(home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        want = (REPO / "hermes" / "skills" / "bramble-duties" / "SKILL.md").read_text(encoding="utf-8")
        got = home / "profiles" / "bramble" / "skills" / "thornswood" / "bramble-duties" / "SKILL.md"
        self.assertEqual(got.read_text(encoding="utf-8"), want)
        for bot in policy().bots:
            if bot != "bramble":
                self.assertFalse((home / "profiles" / bot / "skills" / "thornswood" / "bramble-duties").exists(), bot)
        got.write_text("changed on the VM\n", encoding="utf-8")
        r = self.run_apply(home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(got.read_text(encoding="utf-8"), want, "the next apply puts the repo's copy back")
        self.assertEqual([p.name for p in got.parent.iterdir()], ["SKILL.md"], "no half-written file is left")

    def test_the_last_line_says_whether_the_running_bots_need_a_reload(self):
        # 9 Nov: every install reloaded every Bot; follow_main reads this line.
        home = self.home()
        r = self.run_apply(home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        last = r.stdout.strip().splitlines()[-1]
        self.assertTrue(last.startswith("RELOAD: needed;"), last)
        self.assertIn("files a running Bot holds changed: config.yaml, plugins/thornswood_guard/", last)
        r = self.run_apply(home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.strip().splitlines()[-1],
                         "RELOAD: not needed; nothing a running Bot holds changed (its config and plugins are as they were)")
        self.assertNotIn("tw-reload-bots", r.stdout)
        # A skill is read from disk when it is used, so changing one needs no reload.
        skill = home / "profiles" / "rift" / "skills" / "thornswood" / "thornswood-guide" / "SKILL.md"
        skill.write_text("changed on the VM\n", encoding="utf-8")
        r = self.run_apply(home)
        self.assertTrue(r.stdout.strip().splitlines()[-1].startswith("RELOAD: not needed"), r.stdout)
        # A plugin file is held by every running process.
        (home / "profiles" / "wisp" / "plugins" / "thornswood_guard" / "policy.yaml").write_text("x: 1\n")
        r = self.run_apply(home)
        last = r.stdout.strip().splitlines()[-1]
        self.assertEqual(last, "RELOAD: needed; 1 file a running Bot holds changed: "
                               "profiles/wisp/plugins/thornswood_guard/policy.yaml")

    def test_bramble_gets_its_owed_wake_check_once_and_no_other_bot_does(self):
        # 1 Nov, 10:02 PM on: every wake the fleet queue turned away was
        # dropped. The owed-wake check wakes Bramble once the queue has room.
        from thornswood_guard import owed_wake
        home = self.home()
        r = self.run_apply(home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        script = home / "profiles" / "bramble" / "scripts" / "tw-owed-wake.py"
        self.assertEqual(script.read_bytes(), (REPO / "deploy" / "hermes-scripts" / "tw-owed-wake.py").read_bytes())
        jobs = self.hermes_state(home)["jobs"]
        self.assertEqual(list(jobs), ["bramble"], "only Bramble's profile gets a job")
        (job,) = [j for j in jobs["bramble"] if j["name"] == "tw-owed-wake"]
        self.assertEqual((job["name"], job["schedule"], job["script"], job["skills"], job["deliver"]),
                         ("tw-owed-wake", "every 5m", "tw-owed-wake.py", ["bramble-duties"], "local"))
        self.assertIn(owed_wake.OWED_WAKE_MARK, job["prompt"])
        self.assertIn(f"owed-wake job {job['id']} is active", r.stdout)
        for bot in policy().bots:
            if bot != "bramble":
                self.assertFalse((home / "profiles" / bot / "scripts").exists(), bot)
        # A second apply brings the same job up to date; it never makes another.
        state = self.hermes_state(home)
        state["jobs"]["bramble"][0].update(schedule="every 1m", prompt="changed on the VM")
        (home.parent / "hermes.json").write_text(json.dumps(state), encoding="utf-8")
        r = self.run_apply(home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (again,) = [j for j in self.hermes_state(home)["jobs"]["bramble"] if j["name"] == "tw-owed-wake"]
        self.assertEqual((again["id"], again["schedule"], again["prompt"]), (job["id"], "every 5m", job["prompt"]))

    def test_a_paused_owed_wake_job_stays_paused(self):
        home = self.home()
        self.assertEqual(self.run_apply(home).returncode, 0)
        state = self.hermes_state(home)
        state["jobs"]["bramble"][0]["state"] = "paused"
        (home.parent / "hermes.json").write_text(json.dumps(state), encoding="utf-8")
        r = self.run_apply(home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Patrick paused it, so it stays paused", r.stdout)
        self.assertEqual(self.hermes_state(home)["jobs"]["bramble"][0]["state"], "paused")

    def test_bramble_gets_its_two_weekly_reviews_at_the_guides_times(self):
        # #51: on 9 Nov the VM's cron list held neither review; nothing made them.
        home = self.home()
        r = self.run_apply(home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        jobs = {j["name"]: j for j in self.hermes_state(home)["jobs"]["bramble"]}
        self.assertEqual(sorted(jobs), ["tw-owed-wake", "tw-weekly-board-review", "tw-weekly-project-review"])
        board, project = jobs["tw-weekly-board-review"], jobs["tw-weekly-project-review"]
        # Sundays at 8:37 AM and 9:53 AM Mountain (thornswood-guide section 15).
        self.assertEqual((board["schedule"], board["timezone"]), ("37 8 * * 0", "America/Denver"))
        self.assertEqual((project["schedule"], project["timezone"]), ("53 9 * * 0", "America/Denver"))
        guide = (REPO / "hermes" / "skills" / "thornswood" / "thornswood-guide" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("### Weekly board review: Sundays at 8:37 AM Mountain", guide)
        self.assertIn("### Weekly project review: Sundays at 9:53 AM Mountain", guide)
        for job in (board, project):
            self.assertEqual((job["skills"], job["deliver"], job["script"]), (["thornswood-guide"], "local", ""))
            self.assertIn("section 15", job["prompt"])
            self.assertIn("Report only", job["prompt"])
            self.assertNotIn("[Thornswood owed wake]", job["prompt"], "a review is not a sweep")
            self.assertIn(f"{job['name']} job {job['id']} is active", r.stdout)
        # A second apply edits the same two; a paused one stays paused.
        state = self.hermes_state(home)
        for j in state["jobs"]["bramble"]:
            if j["name"] == "tw-weekly-board-review":
                j.update(state="paused", schedule="0 0 * * *")
        (home.parent / "hermes.json").write_text(json.dumps(state), encoding="utf-8")
        r = self.run_apply(home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        again = {j["name"]: j for j in self.hermes_state(home)["jobs"]["bramble"]}
        self.assertEqual(len(self.hermes_state(home)["jobs"]["bramble"]), 3)
        self.assertEqual((again["tw-weekly-board-review"]["id"], again["tw-weekly-board-review"]["schedule"],
                          again["tw-weekly-board-review"]["state"]), (board["id"], "37 8 * * 0", "paused"))

    def test_finished_one_time_jobs_go_and_every_other_job_stays(self):
        # 9 Nov: Bramble still listed five 1 Nov dry runs of the board review,
        # four [completed] (one of them failed) and one paused.
        home = self.home()
        jobs = {"bramble": [
            {"id": "1297f8b5947d", "name": "Weekly board review DRY RUN (1 Nov)", "schedule": "x",
             "schedule_display": "once at 2026-11-01 15:30", "state": "completed"},
            {"id": "22cc9a1e7490", "name": "Weekly board review DRY RUN rerun 3 (1 Nov)", "schedule": "x",
             "schedule_display": "once at 2026-11-01 22:40", "state": "paused"},
            {"id": "f0f0f0f0f0f0", "name": "Patrick's hourly check", "schedule": "every 1h"},
            {"id": "e1e1e1e1e1e1", "name": "Patrick's paused nightly", "schedule": "0 2 * * *", "state": "paused"},
            {"id": "d2d2d2d2d2d2", "name": "Next year", "schedule": "x", "schedule_display": "once at 2027-01-01 09:00"}],
            "rift": [{"id": "c3c3c3c3c3c3", "name": "old one", "schedule": "x", "schedule_display": "once in 30m",
                      "state": "completed"}]}
        (home.parent / "hermes.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
        r = self.run_apply(home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        left = {b: sorted(j["name"] for j in js) for b, js in self.hermes_state(home)["jobs"].items()}
        self.assertEqual(left["bramble"], ["Next year", "Patrick's hourly check", "Patrick's paused nightly",
                                           "tw-owed-wake", "tw-weekly-board-review", "tw-weekly-project-review"])
        self.assertEqual(left["rift"], [])
        for gone in ("1297f8b5947d", "22cc9a1e7490", "c3c3c3c3c3c3"):
            self.assertIn(f"removed the finished one-time job {gone}", r.stdout)

    def test_two_owed_wake_jobs_are_named_not_guessed_between(self):
        home = self.home()
        two = [{"id": i, "name": "tw-owed-wake", "schedule": "every 5m", "script": "tw-owed-wake.py"} for i in ("a1", "b2")]
        (home.parent / "hermes.json").write_text(json.dumps({"jobs": {"bramble": two}}), encoding="utf-8")
        r = self.run_apply(home)
        self.assertEqual(r.returncode, 1)
        self.assertIn("2 cron jobs are named tw-owed-wake (a1, b2)", r.stdout)

    def test_a_hermes_that_does_not_answer_stops_everything(self):
        home = self.home()
        (home.parent / "hermes.json").write_text(json.dumps({"broken": True}), encoding="utf-8")
        r = self.run_apply(home)
        self.assertEqual(r.returncode, 1)
        self.assertIn("bramble: hermes -p bramble cron list exited 1", r.stdout)
        self.assertIn("Nothing was changed", r.stdout)
        self.assertFalse((home / "profiles" / "rift" / "plugins").exists())

    def test_check_reads_the_jobs_and_writes_none(self):
        home = self.home()
        r = self.run_apply(home, "--check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.hermes_state(home), {})
        log = (home.parent / "hermes.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(line) for line in log], [["-p", "bramble", "cron", "list", "--all"]])

    def test_the_duties_skill_says_what_bramble_does(self):
        text = (REPO / "hermes" / "skills" / "bramble-duties" / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nname: bramble-duties\n"))
        for must in ("ThornswoodRP/Thornswood-Ops", "ThornswoodRP/skymp", "dequeued", "gh-readonly-queue/main/pr-",
                     "There is no timer and no retry", "ops/claude-queue/", "ops/bramble-list.md", "ops/status.md",
                     "Never merge ThornswoodRP/TWHermesAgents", "tw_decide", "start_run"):
            self.assertIn(must, text)
        self.assertIn("whoever did it", text)
        # 9 Nov: two box-step cards stayed up after Patrick ran both steps by hand.
        for must in ("tw_decide my_cards", "close_card", "`cancelled`", "Box step output:"):
            self.assertIn(must, text)
        self.assertNotIn("\u2014", text)

    def test_the_gateways_own_profile_is_set_and_guarded(self):
        # 31 Oct: the gateway runs as the hermes user's default profile, which
        # had no route and no helpers of its own, so its vision helper walked
        # to the Nous Portal. apply.py now sets it under the same rules.
        home = self.home(env_root="DISCORD_BOT_TOKEN=canary\n")
        (home / "config.yaml").write_text(
            "model:\n  provider: auto\n  default: ''\nterminal:\n  backend: local\n_config_version: 51\n", encoding="utf-8")
        r = self.run_apply(home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("the gateway's own profile: config keys set:", r.stdout)
        cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(cfg["model"], {"provider": "anthropic", "default": "claude-opus-5-5"})
        self.assertEqual(cfg["fallback_providers"], [])
        self.assertEqual(cfg["auxiliary"]["vision"], {"provider": "anthropic", "model": "claude-opus-5-5"})
        self.assertEqual(cfg["plugins"]["enabled"], ["thornswood_guard"])
        self.assertEqual(cfg["terminal"], {"backend": "local"})
        self.assertEqual(cfg["_config_version"], 51)
        self.assertTrue((home / "plugins" / "thornswood_guard" / "policy.json").is_file())
        self.assertIn(self.commit, (home / "local" / "thornswood-hermes" / "applied.json").read_text(encoding="utf-8"))
        self.assertEqual((home / ".env").read_text(encoding="utf-8"), "DISCORD_BOT_TOKEN=canary\n")

    def test_the_gateway_takes_bramble_wake_route_with_no_secret_in_config(self):
        home = self.home()
        (home / "config.yaml").write_text(
            "platforms:\n  discord:\n    enabled: true\n_config_version: 51\n", encoding="utf-8")
        r = self.run_apply(home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(cfg["platforms"]["discord"], {"enabled": True})
        hook = cfg["platforms"]["webhook"]
        self.assertTrue(hook["enabled"])
        self.assertEqual(hook["extra"]["host"], "127.0.0.1")
        self.assertEqual(list(hook["extra"]["routes"]), ["github-checks"])
        self.assertEqual(hook["extra"]["routes"]["github-checks"]["profile"], "bramble")
        self.assertNotRegex((home / "config.yaml").read_text(encoding="utf-8"), r"(?m)^\s*secret\s*:")

    def test_without_the_wake_secret_nothing_is_applied(self):
        home = self.home()
        r = self.run_apply(home, wake_secret=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("github-webhook.env is not there; deploy/install.sh makes it", r.stdout)
        self.assertFalse((home / "plugins").exists())

    def test_a_secret_written_into_the_gateway_config_stops_everything(self):
        home = self.home()
        (home / "config.yaml").write_text(
            "platforms:\n  webhook:\n    extra:\n      secret: hunter2\n", encoding="utf-8")
        r = self.run_apply(home)
        self.assertEqual(r.returncode, 1)
        self.assertIn("a webhook secret is written in the gateway's config.yaml", r.stdout)
        self.assertNotIn("hunter2", r.stdout)

    def test_a_second_webhook_route_stops_everything(self):
        home = self.home()
        (home / "config.yaml").write_text(
            "platforms:\n  webhook:\n    extra:\n      routes:\n        anything:\n          prompt: '{__raw__}'\n",
            encoding="utf-8")
        r = self.run_apply(home)
        self.assertEqual(r.returncode, 1)
        self.assertIn("the gateway's webhook routes are ['anything', 'github-checks'], not only github-checks", r.stdout)

    def test_a_paid_helper_in_the_gateways_own_profile_stops_everything(self):
        home = self.home()
        (home / "config.yaml").write_text("auxiliary:\n  compression:\n    provider: openrouter\n", encoding="utf-8")
        before = (home / "profiles" / "rift" / "config.yaml").read_text(encoding="utf-8")
        r = self.run_apply(home)
        self.assertEqual(r.returncode, 1)
        self.assertIn("the gateway's own profile: auxiliary.compression.provider", r.stdout)
        self.assertEqual(before, (home / "profiles" / "rift" / "config.yaml").read_text(encoding="utf-8"))
        self.assertFalse((home / "plugins").exists())

    def test_applying_twice_changes_nothing_more(self):
        home = self.home()
        self.assertEqual(self.run_apply(home).returncode, 0)
        before = (home / "profiles" / "wisp" / "config.yaml").read_text(encoding="utf-8")
        r = self.run_apply(home)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("already current", r.stdout)
        self.assertEqual(before, (home / "profiles" / "wisp" / "config.yaml").read_text(encoding="utf-8"))

    def test_check_writes_nothing(self):
        home = self.home()
        before = (home / "profiles" / "rift" / "config.yaml").read_text(encoding="utf-8")
        r = self.run_apply(home, "--check")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(before, (home / "profiles" / "rift" / "config.yaml").read_text(encoding="utf-8"))
        self.assertFalse((home / "profiles" / "rift" / "plugins").exists())

    def test_never_creates_a_profile(self):
        home = make_home(Path(tempfile.mkdtemp()), ["bramble", "rift", "sprocket", "echo", "magpie"])
        r = self.run_apply(home)
        self.assertEqual(r.returncode, 1)
        self.assertIn("never creates one", r.stdout)
        self.assertFalse((home / "profiles" / "wisp").exists())
        self.assertFalse((home / "profiles" / "rift" / "plugins").exists())

    def test_the_skills_are_installed_from_the_commit(self):
        # 1 Nov: the two skills lived only in each profile, so their stale
        # lines (the old owner's repo names, "private now, going public")
        # could be fixed by no PR. apply.py now writes the commit's copy.
        home = self.home(skip_skill="thornswood-guide")
        r = self.run_apply(home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for bot in policy().bots:
            for skill in ("thornswood-ops", "thornswood-guide"):
                got = (home / "profiles" / bot / "skills" / "thornswood" / skill / "SKILL.md").read_bytes()
                self.assertEqual(got, (REPO / "hermes" / "skills" / "thornswood" / skill / "SKILL.md").read_bytes(), (bot, skill))
        again = self.run_apply(home)
        self.assertIn("skills written: none, already current", again.stdout)

    def test_check_writes_no_skill(self):
        home = self.home(skip_skill="thornswood-guide")
        r = self.run_apply(home, "--check")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse((home / "profiles" / "rift" / "skills" / "thornswood" / "thornswood-guide" / "SKILL.md").exists())

    def test_the_skills_name_themselves_and_name_no_old_home(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("apply_mod", REPO / "scripts" / "apply.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.check_skills(), [])
        for skill in ("thornswood-ops", "thornswood-guide"):
            raw = (REPO / "hermes" / "skills" / "thornswood" / skill / "SKILL.md").read_bytes()
            self.assertNotIn(b"\r", raw, f"{skill}: LF line endings, as .gitattributes keeps them")
            text = raw.decode("utf-8")
            self.assertNotIn("EvilPatrick06/", text, skill)
            self.assertNotIn("going public", text, skill)
            self.assertNotIn("Desk to merge", text, skill)

    def test_a_forbidden_key_stops_everything(self):
        home = self.home(env_root="NEXUS_API_KEY=canary\n")
        r = self.run_apply(home)
        self.assertEqual(r.returncode, 1)
        self.assertIn("NEXUS_API_KEY", r.stdout)
        self.assertNotIn("canary", r.stdout)
        self.assertFalse((home / "profiles" / "bramble" / "plugins").exists())

    def test_the_discord_token_in_a_profile_stops_everything(self):
        r = self.run_apply(self.home(env_bot={"echo": "DISCORD_BOT_TOKEN=canary\n"}))
        self.assertEqual(r.returncode, 1)
        self.assertIn("only in the gateway's own .env", r.stdout)

    def test_signing_material_on_the_box_stops_everything(self):
        home = self.home()
        (home / "profiles" / "magpie" / "workspace").mkdir()
        (home / "profiles" / "magpie" / "workspace" / "signing.json").write_text("{}", encoding="utf-8")
        r = self.run_apply(home)
        self.assertEqual(r.returncode, 1)
        self.assertIn("signing.json", r.stdout)

    def test_a_github_login_in_the_bots_user_stops_everything(self):
        home = self.home()
        (home.parent / ".config" / "gh").mkdir(parents=True)
        (home.parent / ".config" / "gh" / "hosts.yml").write_text("github.com: {}", encoding="utf-8")
        r = self.run_apply(home)
        self.assertEqual(r.returncode, 1)
        self.assertIn("hosts.yml", r.stdout)

    def test_only_the_merged_commit_is_applied(self):
        r = self.run_apply(self.home(), expect="0" * 40)
        self.assertEqual(r.returncode, 1)
        self.assertIn("STOP", r.stdout)

    def test_a_changed_checkout_is_not_applied(self):
        extra = self.repo / "scratch.txt"
        extra.write_text("x", encoding="utf-8")
        try:
            r = self.run_apply(self.home())
            self.assertEqual(r.returncode, 1)
            self.assertIn("changes that are not in the merged commit", r.stdout)
        finally:
            extra.unlink()


class Rendered(unittest.TestCase):
    def test_policy_json_matches_policy_yaml(self):
        on_disk = (REPO / "src" / "thornswood_guard" / "policy.json").read_text(encoding="utf-8")
        self.assertEqual(on_disk, rendered_policy_json(), "policy.json is stale: run python scripts/render_profiles.py")

    def test_profile_files_match_the_policy(self):
        for bot, text in rendered().items():
            on_disk = (REPO / "hermes" / "profiles" / f"{bot}.yaml").read_text(encoding="utf-8")
            self.assertEqual(on_disk, text, f"profiles/{bot}.yaml is stale: run python scripts/render_profiles.py")


class NoTokensInTheRepo(unittest.TestCase):
    PATTERNS = [
        re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
        re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
        re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
        re.compile(r"sk-[A-Za-z0-9]{32,}"),
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        re.compile(r"[MN][A-Za-z\d]{23,25}\.[\w-]{6}\.[\w-]{27,}"),
        re.compile(r"(?m)^\s*[A-Z][A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD)\s*=\s*\S{8,}"),
    ]

    def test_no_token_in_any_tracked_file(self):
        for path in REPO.rglob("*"):
            if ".git" in path.parts or "__pycache__" in path.parts or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern in self.PATTERNS:
                self.assertIsNone(pattern.search(text), f"{path.relative_to(REPO)} holds something shaped like a token")


if __name__ == "__main__":
    unittest.main()
