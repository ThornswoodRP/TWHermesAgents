"""The decision core's install on the VM: its unit, launcher and scripts.

Nothing here installs anything. The checks read the files, run `bash -n` where
bash is there, and run each script as a non-root user where the test is not
root, to see it refuse before it writes.
"""

from __future__ import annotations

import configparser
import json
import os
import shutil
import subprocess
import unittest

from helpers import REPO

DEPLOY = REPO / "deploy" / "decisions"
PLUGIN = REPO / "plugins" / "thornswood-decisions"
COMMIT = "a" * 40


def unit() -> configparser.ConfigParser:
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read_string((DEPLOY / "tw-decide.service").read_text(encoding="utf-8"))
    return cp


class Unit(unittest.TestCase):
    def test_runs_as_its_own_user_on_loopback_only(self):
        s = unit()["Service"]
        self.assertEqual((s["User"], s["Group"]), ("tw-decide", "tw-decide"))
        self.assertEqual(s["IPAddressDeny"], "any")
        self.assertEqual(s["IPAddressAllow"], "localhost")
        self.assertIn("serve --workspace thornswood --port 9135", s["ExecStart"])
        self.assertTrue(s["ExecStart"].startswith("/opt/thornswood-hermes/bin/tw-decide "))
        self.assertTrue(s["ExecStart"].endswith("--origin ${TW_DECIDE_ORIGIN}"), "the origin comes from the settings file")
        self.assertEqual(s["EnvironmentFile"], "/etc/thornswood/tw-decide.env")

    def test_writes_only_its_own_folder(self):
        s = unit()["Service"]
        self.assertEqual(s["ReadWritePaths"], "/srv/tw/decide")
        for key, want in (("ProtectSystem", "strict"), ("ProtectHome", "yes"), ("NoNewPrivileges", "yes"),
                          ("PrivateTmp", "yes"), ("CapabilityBoundingSet", ""), ("UMask", "0077")):
            self.assertEqual(s[key], want, key)
        self.assertIn("MemoryMax", s)

    def test_the_launcher_runs_only_the_root_owned_copy(self):
        for name, shebang, module in (
                ("tw-decide", "#!/opt/thornswood-hermes/decisions-venv/bin/python3 -I", "cli"),
                ("tw-decide-feed", "#!/usr/bin/python3 -I", "feed_reader")):
            lines = (DEPLOY / name).read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], shebang, name)
            self.assertIn('sys.path.insert(0, "/opt/thornswood-hermes/current")', lines)
            self.assertIn(f"from thornswood_decisions.{module} import main  # noqa: E402", lines)

    def test_the_feed_runs_as_hermes_on_loopback_only(self):
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        cp.optionxform = str
        cp.read_string((DEPLOY / "tw-decide-feed.service").read_text(encoding="utf-8"))
        s = cp["Service"]
        self.assertEqual((s["User"], s["IPAddressDeny"], s["IPAddressAllow"]), ("hermes", "any", "localhost"))
        self.assertEqual(s["ExecStart"], "/opt/thornswood-hermes/bin/tw-decide-feed --room ${TW_DECIDE_ROOM}")

    def test_the_venv_installs_only_hashed_wheels(self):
        src = (DEPLOY / "install.sh").read_text(encoding="utf-8")
        self.assertIn("--require-hashes", src)
        self.assertIn("--only-binary :all:", src)
        self.assertIn("--no-deps", src)
        reqs = (DEPLOY / "requirements.txt").read_text(encoding="utf-8")
        pins = [ln for ln in reqs.splitlines() if ln and not ln.startswith(("#", " "))]
        self.assertTrue(pins and all("==" in ln for ln in pins), pins)
        self.assertEqual(reqs.count("==") , len(pins))
        self.assertTrue(all(ln.rstrip().endswith("\\") for ln in pins))

    def test_the_selfcheck_runs_under_the_units_limits(self):
        src = (DEPLOY / "install.sh").read_text(encoding="utf-8")
        for prop in ("User=tw-decide", "MemoryDenyWriteExecute=yes", "ProtectSystem=strict", "IPAddressDeny=any"):
            self.assertIn(f"-p {prop}", src)


class Scripts(unittest.TestCase):
    SCRIPTS = ("install.sh", "install-plugin.sh")

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed here")
    def test_they_parse(self):
        for name in self.SCRIPTS:
            out = subprocess.run([shutil.which("bash"), "-n", str(DEPLOY / name)], capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, f"{name}: {out.stderr}")

    @unittest.skipIf(os.name == "nt" or not shutil.which("bash"), "needs a POSIX bash")
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "the refusal is for a user who is not root")
    def test_they_refuse_without_root(self):
        for name in self.SCRIPTS:
            out = subprocess.run([shutil.which("bash"), str(DEPLOY / name), COMMIT], capture_output=True, text=True)
            self.assertEqual(out.returncode, 1, name)
            self.assertIn("STOP: run as root", out.stdout, name)

    def test_the_core_install_turns_nothing_on(self):
        src = (DEPLOY / "install.sh").read_text(encoding="utf-8")
        self.assertNotRegex(src, r"\bflag\b[^\n]*\bon\b")
        self.assertNotIn(" pair ", src)
        self.assertNotIn("--patrick-switched", src)
        self.assertIn("readlink -f \"$opt/current\"", src)

    def test_the_plugin_goes_only_in_patricks_hermes(self):
        src = (DEPLOY / "install-plugin.sh").read_text(encoding="utf-8")
        self.assertIn("owner=patrick-hermes", src)
        self.assertNotIn("/home/hermes", src)
        self.assertIn('runuser -u "$owner" -- ln -sfnT "$target" "$link"', src)
        run = [ln for ln in src.splitlines() if not ln.lstrip().startswith(("#", "echo "))]
        self.assertFalse([ln for ln in run if "plugins enable" in ln or "config.yaml" in ln],
                         "turning the plugin on is Patrick's step, not the script's")


class PluginPackage(unittest.TestCase):
    def test_manifest_names_agree(self):
        manifest = json.loads((PLUGIN / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
        yaml_name = next(line.split(":", 1)[1].strip() for line in
                         (PLUGIN / "plugin.yaml").read_text(encoding="utf-8").splitlines() if line.startswith("name:"))
        self.assertEqual(manifest["name"], "thornswood-decisions")
        self.assertEqual(yaml_name, "thornswood-decisions")

    def test_the_agent_half_registers_nothing(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("tw_decisions_agent_half", PLUGIN / "__init__.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        class Ctx:
            def __getattr__(self, name):
                raise AssertionError(f"the agent half called ctx.{name}")

        self.assertIsNone(mod.register(Ctx()))


if __name__ == "__main__":
    unittest.main()
