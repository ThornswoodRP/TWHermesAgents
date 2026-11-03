"""The guard rail tests' own runner on the VM (deploy/runner, 3 Nov 2026).

GitHub's included minutes for a private repository ran out on 2 Nov with the
spending limit at $0, and Patrick will not pay (A on q-hermes-ci-free-runners):
the Linux check runs on the VM as tw-runner, which reaches only GitHub and the
package feeds, sees none of the Bots' or brokers' files, and holds no
capability.
"""

from __future__ import annotations

import configparser
import ipaddress
import json
import re
import socket
import unittest

from helpers import REPO

from thornswood_brokers import runner_egress as eg

PIN = json.loads((REPO / "deploy" / "runner" / "runner.json").read_text(encoding="utf-8"))
UNIT = REPO / "deploy" / "systemd" / "tw-runner.service"
INSTALL = (REPO / "deploy" / "install.sh").read_text(encoding="utf-8")

META = {"web": ["140.82.112.0/20", "2a0a:a440::/29"], "api": ["192.30.252.0/22", "140.82.112.0/20"],
        "git": ["140.82.112.0/20"], "actions": ["4.148.0.0/16", "2a01:111:f403::/48"], "hooks": ["9.9.9.0/24"]}


def unit(path=UNIT) -> dict:
    out: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#") and not line.startswith("["):
            k, v = line.split("=", 1)
            out.setdefault(k, []).append(v)
    return out


class Pin(unittest.TestCase):
    def test_a_release_pinned_by_hash_for_both_arches(self):
        self.assertRegex(PIN["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(set(PIN["sha256"]), {"x64", "arm64"})
        for h in PIN["sha256"].values():
            self.assertRegex(h, r"^[0-9a-f]{64}$")

    def test_this_repository_only_with_our_own_label(self):
        self.assertEqual(PIN["repository"], "https://github.com/ThornswoodRP/Thornswood-Hermes")
        self.assertEqual((PIN["user"], PIN["home"], PIN["labels"]), ("tw-runner", "/srv/tw-runner", ["tw-hermes-vm"]))

    def test_not_the_follower_s_test_user(self):
        """tw-ci runs main's suite with no network before an install; the
        runner is another user, so its table never touches those runs."""
        policy = (REPO / "thornswood_guard" / "policy.yaml").read_text(encoding="utf-8")
        self.assertIn("ci_user: tw-ci", policy)
        self.assertNotEqual(PIN["user"], "tw-ci")


class Egress(unittest.TestCase):
    def test_the_four_lists_and_nothing_else(self):
        v4, v6 = eg.lists_from_meta(META, PIN["egress"]["github_lists"])
        self.assertEqual(v4, ["140.82.112.0/20", "192.30.252.0/22", "4.148.0.0/16"])
        self.assertEqual(v6, ["2a0a:a440::/29", "2a01:111:f403::/48"])
        self.assertNotIn("9.9.9.0/24", v4, "hooks is GitHub calling in, not a place to reach")

    def test_a_missing_list_is_an_error_not_an_empty_allow(self):
        with self.assertRaises(ValueError):
            eg.lists_from_meta({"web": ["140.82.112.0/20"]}, ["web", "actions"])

    def test_hosts_resolve_to_single_addresses(self):
        def lookup(host, port, proto=0):
            return {"pypi.org": [(socket.AF_INET, 1, 6, "", ("151.101.0.223", 443)),
                                 (socket.AF_INET6, 1, 6, "", ("2a04:4e42::223", 443, 0, 0))],
                    "files.pythonhosted.org": [(socket.AF_INET, 1, 6, "", ("151.101.0.223", 443))]}[host]
        v4, v6 = eg.resolve(["pypi.org", "files.pythonhosted.org"], lookup=lookup)
        self.assertEqual((v4, v6), (["151.101.0.223/32"], ["2a04:4e42::223/128"]))

    def test_the_table_matches_only_the_runner_and_rejects_the_rest(self):
        text = eg.ruleset(997, ["140.82.112.0/20"], ["2a0a:a440::/29"])
        self.assertTrue(text.startswith("table inet tw_runner\ndelete table inet tw_runner\n"),
                        "replaced in one transaction, made first when it is not there")
        lines = [ln.strip() for ln in text.splitlines()]
        chain = lines[lines.index("chain out {") + 1:]
        self.assertEqual(chain[0], "type filter hook output priority filter; policy accept;")
        self.assertEqual(chain[1], "meta skuid != 997 accept", "every other user's traffic is untouched")
        self.assertEqual(chain[2:6], ["ip daddr 127.0.0.53 udp dport 53 accept", "ip daddr 127.0.0.53 tcp dport 53 accept",
                                      "ip daddr @github4 tcp dport 443 accept", "ip6 daddr @github6 tcp dport 443 accept"])
        self.assertEqual(chain[6], "counter reject", "anything else the runner sends")
        self.assertIn("elements = { 140.82.112.0/20 }", text)

    def test_with_no_lists_it_reaches_nothing(self):
        text = eg.ruleset(997, [], [])
        self.assertNotIn("elements", text)
        self.assertIn("counter reject", text)

    def test_nothing_private_is_in_the_allowed_lists(self):
        v4, v6 = eg.lists_from_meta(META, PIN["egress"]["github_lists"])
        for n in v4 + v6:
            net = ipaddress.ip_network(n)
            self.assertFalse(net.is_private or net.is_loopback or net.is_link_local, n)

    def test_a_failed_read_keeps_the_table_or_puts_an_empty_one_on(self):
        applied = []

        class Done:
            def __init__(self, rc):
                self.returncode, self.stderr = rc, ""

        def run_with(present):
            def run(cmd, **kw):
                if cmd[:2] == ["nft", "list"]:
                    return Done(0 if present else 1)
                applied.append(kw.get("input"))
                return Done(0)
            return run

        def failing_meta():
            raise OSError("no route")

        orig = (eg.fetch_meta, eg.table_present, eg.apply, eg.account)
        try:
            eg.fetch_meta = failing_meta
            eg.account = lambda name: type("P", (), {"pw_uid": 997})()
            eg.table_present = lambda: True
            eg.apply = lambda text: applied.append(text)
            self.assertEqual(eg.main([]), 0)
            self.assertEqual(applied, [], "the table there stays as it was")
            eg.table_present = lambda: False
            self.assertEqual(eg.main([]), 1)
            self.assertEqual(len(applied), 1)
            self.assertNotIn("elements", applied[0])
        finally:
            eg.fetch_meta, eg.table_present, eg.apply, eg.account = orig


class Unit(unittest.TestCase):
    def setUp(self):
        self.u = unit()

    def test_its_own_user_slice_and_folder(self):
        self.assertEqual(self.u["User"], ["tw-runner"])
        self.assertEqual(self.u["Group"], ["tw-runner"])
        self.assertEqual(self.u["Slice"], ["tw-runner.slice"])
        self.assertEqual(self.u["ReadWritePaths"], ["/srv/tw-runner"])
        self.assertEqual(self.u["ExecStart"], ["/srv/tw-runner/runner/run.sh"])

    def test_never_without_its_network_table(self):
        self.assertIn("tw-runner-egress.service", self.u["Requires"][0])
        self.assertIn("tw-runner-egress.service", self.u["After"][0])

    def test_no_privilege_and_the_rest_of_the_machine_out_of_sight(self):
        for k, v in {"NoNewPrivileges": "yes", "CapabilityBoundingSet": "", "AmbientCapabilities": "",
                     "ProtectSystem": "strict", "ProtectHome": "yes", "PrivateTmp": "yes", "PrivateDevices": "yes",
                     "RestrictSUIDSGID": "yes", "RestrictNamespaces": "yes", "ProtectProc": "invisible"}.items():
            self.assertEqual(self.u[k], [v], k)
        hidden = self.u["InaccessiblePaths"][0].split()
        for p in ("-/srv/tw", "-/etc/thornswood", "-/var/lib/thornswood", "-/run/tw"):
            self.assertIn(p, hidden)

    def test_the_slice_comes_from_the_policy(self):
        from thornswood_guard.policy import Policy
        caps = Policy.load().data["slices"]["tw-runner.slice"]
        text = (REPO / "deploy" / "systemd" / "tw-runner.slice").read_text(encoding="utf-8")
        for k, v in caps.items():
            self.assertIn(f"{k}={v}", text)


class Install(unittest.TestCase):
    def test_the_runner_is_checked_against_its_pin_before_it_is_unpacked(self):
        i_sum, i_tar = INSTALL.index('got="$(sha256sum'), INSTALL.index("tar -xzf")
        self.assertLess(i_sum, i_tar)
        self.assertIn('[[ "$got" == "$want" ]] || { rm -rf "$tmp"; stop', INSTALL)
        self.assertIn("runuser -u tw-runner -- tar", INSTALL)

    def test_its_user_is_in_no_group_but_its_own(self):
        self.assertIn("--home-dir /srv/tw-runner --shell /usr/sbin/nologin --user-group tw-runner", INSTALL)
        self.assertIn('[[ "$(id -nG tw-runner)" == tw-runner ]] || stop', INSTALL)
        self.assertNotRegex(INSTALL, r"usermod[^\n]*tw-runner")

    def test_the_install_never_restarts_the_runner_or_its_table(self):
        """A restart of the table's unit would stop the runner (Requires) and
        the check it is running."""
        code = "\n".join(ln for ln in INSTALL.splitlines() if not ln.lstrip().startswith("#"))
        self.assertNotRegex(code, r"restart[^\n]*tw-runner")
        self.assertIn("systemctl enable --quiet --now tw-runner.service", INSTALL)
        self.assertIn('nft list table inet tw_runner >/dev/null 2>&1 || stop', INSTALL)

    def test_the_runner_starts_only_once_registered(self):
        i_if = INSTALL.index('if [[ -f "$runner_home/runner/.runner" ]]; then')
        self.assertLess(i_if, INSTALL.index("systemctl enable --quiet --now tw-runner.service"))


class Register(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from thornswood_brokers import runner_register as rr
        self.rr = rr
        home = Path(tempfile.mkdtemp())
        (home / "runner").mkdir()
        (home / "runner" / "config.sh").write_text("")
        self.pin = dict(PIN, home=str(home))
        self.runner = home / "runner"
        self.calls = []

    def run_fake(self, egress_on=True, registers=True):
        class Done:
            def __init__(self, rc):
                self.returncode = rc

        def run(cmd, **kw):
            self.calls.append((cmd, kw))
            if cmd[:3] == ["systemctl", "is-active", "--quiet"]:
                return Done(0 if (egress_on or cmd[3] != "tw-runner-egress.service") else 3)
            if cmd[0] == "./config.sh" and registers:
                (self.runner / ".runner").write_text("{}")
            return Done(0)
        return run

    def test_the_token_is_in_the_environment_and_on_no_command_line(self):
        orig = self.rr.account
        self.rr.account = lambda n: type("P", (), {"pw_uid": 997, "pw_gid": 997})()
        try:
            out = self.rr.register(self.pin, "AAAATOKEN", run=self.run_fake(), sleep=lambda s: None)
        finally:
            self.rr.account = orig
        self.assertIn("PASS", out[0])
        config = next(kw for cmd, kw in self.calls if cmd[0] == "./config.sh")
        cmd = next(cmd for cmd, kw in self.calls if cmd[0] == "./config.sh")
        for cmd_, _ in self.calls:
            self.assertNotIn("AAAATOKEN", " ".join(cmd_))
        self.assertNotIn("--token", cmd)
        self.assertEqual(config["env"]["ACTIONS_RUNNER_INPUT_TOKEN"], "AAAATOKEN")
        self.assertEqual(set(config["env"]), {"HOME", "PATH", "LANG", "ACTIONS_RUNNER_INPUT_TOKEN"})
        self.assertIsNotNone(config["preexec_fn"], "the config runs as tw-runner, not root")
        self.assertIn("--no-default-labels", cmd)
        self.assertEqual(cmd[cmd.index("--labels") + 1], "tw-hermes-vm")
        self.assertNotIn("--disableupdate", cmd)

    def test_nothing_is_registered_without_the_network_table(self):
        with self.assertRaises(SystemExit) as e:
            self.rr.register(self.pin, "AAAATOKEN", run=self.run_fake(egress_on=False), sleep=lambda s: None)
        self.assertIn("network is not limited", str(e.exception))
        self.assertFalse(any(cmd[0] == "./config.sh" for cmd, _ in self.calls))

    def test_no_token_registers_nothing(self):
        with self.assertRaises(SystemExit):
            self.rr.register(self.pin, "", run=self.run_fake(), sleep=lambda s: None)
        self.assertFalse(any(cmd[0] == "./config.sh" for cmd, _ in self.calls))

    def test_a_config_that_did_not_register_leaves_the_runner_stopped(self):
        orig = self.rr.account
        self.rr.account = lambda n: type("P", (), {"pw_uid": 997, "pw_gid": 997})()
        try:
            with self.assertRaises(SystemExit):
                self.rr.register(self.pin, "AAAATOKEN", run=self.run_fake(registers=False), sleep=lambda s: None)
        finally:
            self.rr.account = orig
        self.assertNotIn(["systemctl", "enable", "--quiet", "--now", "tw-runner.service"], [c for c, _ in self.calls])

    def test_every_new_file_is_placed(self):
        for name in ("tw-runner.service", "tw-runner.slice", "tw-runner-egress.service", "tw-runner-egress.timer",
                     "tw-runner-egress-refresh.service"):
            self.assertTrue((REPO / "deploy" / "systemd" / name).is_file(), name)
        timer = configparser.ConfigParser()
        timer.read(REPO / "deploy" / "systemd" / "tw-runner-egress.timer")
        self.assertEqual(timer["Timer"]["Unit"], "tw-runner-egress-refresh.service")
        self.assertTrue(re.search(r"^#!/usr/bin/python3 -I$", (REPO / "deploy" / "bin" / "tw-runner-egress")
                                  .read_text(encoding="utf-8"), re.M))


if __name__ == "__main__":
    unittest.main()
