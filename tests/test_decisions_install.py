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

    def test_the_connector_runs_as_hermes_on_loopback_and_holds_no_secret(self):
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        cp.optionxform = str
        cp.read_string((DEPLOY / "tw-decide-connector.service").read_text(encoding="utf-8"))
        s = cp["Service"]
        self.assertEqual((s["User"], s["IPAddressDeny"], s["IPAddressAllow"]), ("hermes", "any", "localhost"))
        self.assertEqual(s["ExecStart"], "/opt/thornswood-hermes/bin/tw-decide-connector --hermes ${TW_HERMES_BIN}")
        self.assertEqual((s["NoNewPrivileges"], s["CapabilityBoundingSet"]), ("yes", ""))
        self.assertNotIn("ListenStream", s)
        for key in s:
            self.assertNotRegex(key + s[key], r"(?i)api_server_key|token=|secret")
        lines = (DEPLOY / "tw-decide-connector").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "#!/usr/bin/python3 -I")
        self.assertIn("from thornswood_decisions.connector import main  # noqa: E402", lines)

    def test_the_push_worker_has_its_own_user_and_only_its_folder(self):
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        cp.optionxform = str
        cp.read_string((DEPLOY / "tw-decide-push.service").read_text(encoding="utf-8"))
        s = cp["Service"]
        self.assertEqual((s["User"], s["Group"]), ("tw-push", "tw-push"))
        self.assertEqual(s["ExecStart"], "/opt/thornswood-hermes/bin/tw-decide-push --store /srv/tw/push "
                                         "--subject ${TW_DECIDE_ORIGIN}")
        self.assertEqual(s["ReadWritePaths"], "/srv/tw/push")
        for key, want in (("ProtectSystem", "strict"), ("ProtectHome", "yes"), ("NoNewPrivileges", "yes"),
                          ("PrivateTmp", "yes"), ("MemoryDenyWriteExecute", "yes"), ("CapabilityBoundingSet", ""),
                          ("UMask", "0077")):
            self.assertEqual(s[key], want, key)
        self.assertNotIn("ListenStream", s)
        for key in s:
            self.assertNotRegex(key + s[key], r"(?i)token=|secret|vapid")
        lines = (DEPLOY / "tw-decide-push").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "#!/opt/thornswood-hermes/decisions-venv/bin/python3 -I")
        self.assertIn("from thornswood_decisions.push_worker import main  # noqa: E402", lines)

    def test_the_install_gives_the_push_worker_its_token_and_folder_and_waits_for_the_origin(self):
        src = (DEPLOY / "install.sh").read_text(encoding="utf-8")
        self.assertIn("install -d -m 0700 -o tw-push -g tw-push /srv/tw/push", src)
        self.assertIn("run feed-token --workspace thornswood --name push | runuser -u tw-push", src)
        self.assertIn('grep -q \'"push": "sealed, encrypted and signed"\'', src)
        self.assertIn('if [[ -n "$origin" ]]; then\n  systemctl enable --quiet tw-decide-push.service', src)
        self.assertNotIn("push_send on", src)

    def test_the_connector_logs_each_line_at_once(self):
        import io
        from unittest import mock
        from thornswood_decisions import connector
        self.assertIs(connector.Connector.__init__.__defaults__[0], connector.say)
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch.object(out, "flush") as flush:
            connector.say("PASS x")
        flush.assert_called()

    def test_the_push_worker_logs_each_line_at_once(self):
        import ast
        tree = ast.parse((REPO / "thornswood_decisions" / "push_worker.py").read_text(encoding="utf-8"))
        say = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "say")
        call = say.body[-1].value
        self.assertEqual((call.func.id, [k.arg for k in call.keywords]), ("print", ["flush"]))
        worker = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Worker")
        init = next(n for n in worker.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
        self.assertEqual(init.args.defaults[-1].id, "say")

    def test_the_install_makes_the_connector_token_and_turns_no_effect_on(self):
        src = (DEPLOY / "install.sh").read_text(encoding="utf-8")
        self.assertIn("for name in work room bots connector; do", src)
        self.assertIn('tok="$hh/.config/thornswood/connector.token"', src)
        self.assertIn("runuser -l hermes -c 'command -v hermes'", src)
        self.assertNotIn("connector_effects on", src)

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
    SCRIPTS = ("install.sh", "install-plugin.sh", "phone-test.sh")

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

    def test_the_core_install_turns_on_the_phone_apps_flags_only(self):
        # Patrick's switch, 2 Nov: the phone app's features are on. The Bots' own flags it never changes.
        src = (DEPLOY / "install.sh").read_text(encoding="utf-8")
        self.assertIn('PHONE_FLAGS="mobile_read mobile_chat_write push_send own_chat_write session_manage_write"', src)
        self.assertIn('run flag --workspace thornswood "$f" on --patrick-switched', src)
        self.assertEqual(src.count("run flag"), 1)
        self.assertEqual(src.count("--patrick-switched"), 1)
        for theirs in ("decision_write", "connector_effects"):
            self.assertNotIn(theirs + " on", src)
            self.assertNotIn(theirs, src.split("PHONE_FLAGS=", 1)[1].split("\n", 1)[0])
        self.assertNotIn(" pair ", src)
        self.assertIn("readlink -f \"$opt/current\"", src)

    def test_the_core_install_checks_the_room_seats_the_policys_bots(self):
        src = (DEPLOY / "install.sh").read_text(encoding="utf-8")
        self.assertIn('runuser -u hermes -- python3 -I "$here/scripts/check-hermes-contract.py"', src)
        self.assertIn('--room "$room"', src)
        self.assertIn('|| stop "the Workbench room does not match the policy', src)

    def test_the_core_install_puts_python3_venv_in_itself(self):
        src = (DEPLOY / "install.sh").read_text(encoding="utf-8")
        self.assertIn("if ! python3 -c 'import ensurepip, venv' 2>/dev/null; then\n"
                      "  DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3-venv", src)
        self.assertIn("python3 still cannot make a venv after installing python3-venv", src)

    def test_the_plugin_goes_only_in_patricks_hermes(self):
        src = (DEPLOY / "install-plugin.sh").read_text(encoding="utf-8")
        self.assertIn("owner=patrick-hermes", src)
        self.assertNotIn("/home/hermes", src)
        self.assertIn('runuser -u "$owner" -- ln -sfnT "$target" "$link"', src)
        run = [ln for ln in src.splitlines() if not ln.lstrip().startswith(("#", "echo "))]
        self.assertFalse([ln for ln in run if "plugins enable" in ln or "config.yaml" in ln],
                         "turning the plugin on is Patrick's step, not the script's")


def _service(name: str) -> dict:
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read_string((DEPLOY / name).read_text(encoding="utf-8"))
    return cp


class PhoneTestBench(unittest.TestCase):
    """T10 to T13 run on a second core with its own store and test workspace."""

    # What may differ between each bench unit and the real unit it copies.
    DIFFERENT = {"Description", "After", "EnvironmentFile", "ExecStart", "ReadWritePaths"}

    def assert_copy(self, real: str, bench: str):
        r, b = _service(real), _service(bench)
        self.assertNotIn("Install", b.sections(), f"{bench} must never start at boot")
        for section in ("Unit", "Service"):
            for key in set(r[section]) | set(b[section]):
                if key in self.DIFFERENT:
                    continue
                self.assertEqual(b[section].get(key), r[section].get(key), f"{bench} [{section}] {key}")
        return b["Service"]

    def test_the_bench_core_is_the_real_core_on_its_own_store_port_and_test_workspace(self):
        s = self.assert_copy("tw-decide.service", "tw-decide-test.service")
        self.assertEqual(s["ExecStart"], "/opt/thornswood-hermes/bin/tw-decide --db /srv/tw/decide-test/core.db "
                                         "serve --workspace phone-test --port 9136 --origin ${TW_DECIDE_TEST_ORIGIN}")
        self.assertEqual(s["ReadWritePaths"], "/srv/tw/decide-test")
        self.assertEqual(s["EnvironmentFile"], "/etc/thornswood/tw-decide-test.env")
        self.assertEqual((s["IPAddressDeny"], s["IPAddressAllow"]), ("any", "localhost"))

    def test_the_bench_push_worker_has_its_own_keys_token_and_core(self):
        s = self.assert_copy("tw-decide-push.service", "tw-decide-push-test.service")
        self.assertEqual(s["ExecStart"], "/opt/thornswood-hermes/bin/tw-decide-push --store /srv/tw/push-test "
                                         "--core http://127.0.0.1:9136/api/v1 --subject ${TW_DECIDE_TEST_ORIGIN}")
        self.assertEqual(s["ReadWritePaths"], "/srv/tw/push-test")

    def test_the_bench_touches_only_the_test_workspace(self):
        src = (DEPLOY / "phone-test.sh").read_text(encoding="utf-8")
        code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
        self.assertIn("ws=phone-test", code)
        self.assertIn('db="$state/core.db"', code)
        self.assertIn("state=/srv/tw/decide-test", code)
        self.assertIn('run init --workspace "$ws" --test', code)
        self.assertNotIn("thornswood ", code)
        self.assertNotIn("--workspace thornswood", code)
        self.assertNotIn("/srv/tw/decide/", code)
        self.assertNotIn("--patrick-switched", code)
        self.assertNotIn("tw-decide.service", code)
        self.assertNotIn("tw-decide-push.service", code)
        self.assertNotIn("tw-decide-connector", code)
        self.assertNotRegex(code, r"--https=443\b")
        # The one place a flag changes, and the only three it turns on.
        self.assertEqual(code.count("run flag"), 1)
        self.assertIn("for flag in mobile_read decision_write push_send; do\n"
                      '      run flag --workspace "$ws" "$flag" on', code)

    def test_the_bench_serves_on_8443_and_takes_it_off_again(self):
        src = (DEPLOY / "phone-test.sh").read_text(encoding="utf-8")
        self.assertIn('test_origin="$origin:8443"', src)
        self.assertIn("tailscale serve --bg --https=8443 127.0.0.1:9136", src)
        self.assertIn("tailscale serve --bg --https=8443 off", src)
        self.assertNotIn("|| true\n    systemctl stop", src)

    @unittest.skipIf(os.name == "nt", "file modes are POSIX")
    def test_the_bench_script_is_executable(self):
        self.assertTrue(os.stat(DEPLOY / "phone-test.sh").st_mode & 0o111)


class PluginPackage(unittest.TestCase):
    def test_manifest_names_agree(self):
        manifest = json.loads((PLUGIN / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
        yaml_name = next(line.split(":", 1)[1].strip() for line in
                         (PLUGIN / "plugin.yaml").read_text(encoding="utf-8").splitlines() if line.startswith("name:"))
        self.assertEqual(manifest["name"], "thornswood-decisions")
        self.assertEqual(yaml_name, "thornswood-decisions")

    def test_the_agent_half_registers_only_rule_zero(self):
        # It gives Patrick's Hermes no tools, hooks or commands: only the rule
        # zero prompt section (tests/test_rule_zero.py checks the text).
        import importlib.util
        spec = importlib.util.spec_from_file_location("tw_decisions_agent_half", PLUGIN / "__init__.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        called = []

        class Ctx:
            def register_system_prompt_section(self, sid, text, **kw):
                called.append(sid)

            def __getattr__(self, name):
                raise AssertionError(f"the agent half called ctx.{name}")

        self.assertIsNone(mod.register(Ctx()))
        self.assertEqual(called, ["thornswood.rule-zero"])

if __name__ == "__main__":
    unittest.main()
