"""scripts/apply.py against a stand-in for the VM's Hermes home, the rendered
profile files, and the repo itself holding no tokens."""

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

    def run_apply(self, home: Path, *extra, expect=None, image="present", podman=None):
        work = home.parent / "tw-work"
        state = home.parent / "podman.state"
        state.write_text(image, encoding="utf-8")
        fake = f"{Path(sys.executable).as_posix()} {(Path(__file__).with_name('fake_podman.py')).as_posix()}"
        env = dict(os.environ, FAKE_PODMAN_STATE=str(state))
        return subprocess.run([sys.executable, str(self.repo / "scripts" / "apply.py"), "--expect", expect or self.commit,
                               "--hermes-home", str(home), "--work-root", str(work), "--podman", podman or fake, *extra],
                              capture_output=True, text=True, env=env)

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
            self.assertEqual(cfg["plugins"]["enabled"], ["thornswood_guard"])
            self.assertTrue((p / "plugins" / "thornswood_guard" / "policy.yaml").is_file())
            self.assertEqual((p / "SOUL.md").read_text(encoding="utf-8"), f"# SOUL.md\n\nYou're **{bot}**.\n")
            self.assertEqual((p / "memories" / "MEMORY.md").read_text(encoding="utf-8"), "I am here.\n")
            self.assertTrue((home.parent / "tw-work" / bot).is_dir())
            self.assertIn(self.commit, (p / "local" / "thornswood-hermes" / "applied.json").read_text(encoding="utf-8"))

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

    def test_a_missing_skill_stops_everything(self):
        r = self.run_apply(self.home(skip_skill="thornswood-guide"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("thornswood-guide skill is missing", r.stdout)

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
        on_disk = (REPO / "thornswood_guard" / "policy.json").read_text(encoding="utf-8")
        self.assertEqual(on_disk, rendered_policy_json(), "policy.json is stale: run python scripts/render_profiles.py")

    def test_profile_files_match_the_policy(self):
        for bot, text in rendered().items():
            on_disk = (REPO / "profiles" / f"{bot}.yaml").read_text(encoding="utf-8")
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
