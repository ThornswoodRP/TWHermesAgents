"""The network inside a Tier A container (policy.yaml tier_a_network).

Patrick, 15 Nov 2026: "We need to give them network access so they can do
those tests and so they can do web searches for research and stuff as
needed". Until then every Bot's container ran with --network=none, and the
weekly project review could not run Lighthouse, linkinator or read the live
site's headers ("github.com would not resolve"). These hold what changed and
what did not: each container reaches the public internet and asks public
resolvers; the hermes user's slice denies the VM's own and its network's
private addresses, the tailnet and the cloud metadata service; nothing a
container holds changes (no key, no token, no Hermes home); the Bots have the
web tools and Hermes's own private-address check stays on; and the canary
tries all of it live after each install.
"""

from __future__ import annotations

import importlib.util
import ipaddress
import subprocess
import sys
import unittest

from helpers import REPO, policy

from thornswood_guard.profile_config import build, build_gateway

LINUX = sys.platform.startswith("linux")
BOTS = ("bramble", "rift", "sprocket", "echo", "magpie", "wisp")

# Places a container must never reach, by an address of each.
OURS = ["10.0.0.5", "172.16.4.2", "192.168.1.1", "100.64.0.1", "100.100.100.100", "169.254.169.254",
        "0.0.0.0", "224.0.0.251", "fd7a:115c:a1e0::1", "fe80::1", "ff02::1"]
# Public places it must reach: Cloudflare's and Quad9's resolvers, GitHub.
PUBLIC = ["1.1.1.1", "9.9.9.9", "140.82.112.3", "2606:4700:4700::1111"]


def denied(p) -> list:
    return [ipaddress.ip_network(n) for n in p.data["tier_a_network"]["deny"]]


def covered(nets, addr: str) -> bool:
    a = ipaddress.ip_address(addr)
    return any(a.version == n.version and a in n for n in nets)


class ThePolicy(unittest.TestCase):
    def test_the_deny_list_holds_everything_of_ours_and_nothing_public(self):
        nets = denied(policy())
        for addr in OURS:
            self.assertTrue(covered(nets, addr), addr)
        for addr in PUBLIC:
            self.assertFalse(covered(nets, addr), addr)

    def test_a_containers_own_loopback_stays_open(self):
        # A test's local server and Lighthouse driving Chrome talk over the container's own lo,
        # which is in the slice too.
        nets = denied(policy())
        for addr in ("127.0.0.1", "::1"):
            self.assertFalse(covered(nets, addr), addr)

    def test_the_resolvers_are_public_and_reachable(self):
        p = policy()
        nets = denied(p)
        self.assertTrue(p.data["tier_a_network"]["dns"])
        for server in p.data["tier_a_network"]["dns"]:
            self.assertTrue(ipaddress.ip_address(server).is_global, server)
            self.assertFalse(covered(nets, server), server)

    def test_the_hermes_users_slice_carries_the_deny_list(self):
        p = policy()
        spec = importlib.util.spec_from_file_location("render_units", REPO / "scripts" / "render_units.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        drop_in = mod.files(p)[REPO / "deploy" / "systemd" / "user-hermes.slice.conf"]
        lines = [l for l in drop_in.splitlines() if l.startswith("IPAddress")]
        self.assertEqual(lines, ["IPAddressDeny=" + " ".join(p.data["tier_a_network"]["deny"])])
        # Nothing re-opens what the deny list closes.
        self.assertNotIn("IPAddressAllow", drop_in)


class TheBotsConfig(unittest.TestCase):
    def test_every_container_has_the_network_and_the_public_resolvers(self):
        p = policy()
        dns = p.data["tier_a_network"]["dns"]
        for bot in BOTS:
            t = build(p, bot)["terminal"]
            self.assertTrue(t["docker_network"], bot)
            extra = t["docker_extra_args"]
            # No network mode of our own: podman's default rootless network, as the canary tries it.
            self.assertFalse([a for a in extra if a.startswith(("--network", "--net"))], bot)
            self.assertEqual([extra[i + 1] for i, a in enumerate(extra) if a == "--dns"], dns, bot)

    def test_nothing_a_container_holds_changes(self):
        p = policy()
        for bot in BOTS:
            t = build(p, bot)["terminal"]
            self.assertEqual(t["docker_forward_env"], [], bot)
            self.assertEqual(t["docker_env"], {}, bot)
            self.assertEqual(t["docker_volumes"], [], bot)
            self.assertFalse([a for a in t["docker_extra_args"] if a.startswith(("-v", "--volume", "--mount", "-e", "--env"))], bot)

    def test_the_bots_have_the_web_tools_and_nothing_else_new(self):
        p = policy()
        for bot in BOTS:
            off = build(p, bot)["agent"]["disabled_toolsets"]
            for toolset in ("web", "search"):
                self.assertNotIn(toolset, off, bot)
            for toolset in ("browser", "code_execution", "computer_use", "x_search", "delegation"):
                self.assertIn(toolset, off, bot)

    def test_hermes_refuses_private_addresses_in_every_profile(self):
        p = policy()
        for cfg in [build(p, bot) for bot in BOTS] + [build_gateway(p)]:
            self.assertIs(cfg["security"]["allow_private_urls"], False)

    def test_the_gateways_own_profile_keeps_the_web_tools_off(self):
        off = build_gateway(policy())["agent"]["disabled_toolsets"]
        for toolset in ("web", "search"):
            self.assertIn(toolset, off)


@unittest.skipUnless(LINUX, "the canary runs only on the Oracle VM")
class TheCanary(unittest.TestCase):
    def setUp(self):
        from thornswood_brokers import canary
        self.canary = canary

    def test_it_starts_the_container_as_hermes_does_with_the_network(self):
        flags = self.canary.HERMES_PODMAN_ARGS
        self.assertFalse([a for a in flags if a.startswith(("--network", "--net"))])

    def test_reading_a_container_that_reaches_only_the_internet(self):
        targets = {"10.0.0.5:4000": "the VM's own address 10.0.0.5", "169.254.169.254:80/any": "the metadata service"}
        out = ("RESOLVED github.com 140.82.112.3\nPUBLIC 200\nBLOCKED 10.0.0.5:4000 ConnectionRefusedError\n"
               "BLOCKED 169.254.169.254:80/any ConnectionResetError\nDONE\n")
        self.assertEqual(self.canary.parse_net(out, targets), ([], []))

    def test_reading_a_container_that_reaches_ours_or_not_the_internet(self):
        targets = {"10.0.0.5:4000": "the VM's own address 10.0.0.5", "169.254.169.254:80/any": "the metadata service",
                   "10.0.2.2:4000": "the VM by 10.0.2.2"}
        out = ("UNRESOLVED github.com gaierror\nNOPUBLIC URLError timed out\n"
               "REACHED 10.0.0.5:4000 answered with the token\nREACHED 169.254.169.254:80/any connected\nDONE\n")
        public, reached = self.canary.parse_net(out, targets)
        self.assertEqual(len(public), 2)
        self.assertIn("could not resolve github.com", public[0])
        self.assertIn("could not read https://github.com/", public[1])
        # 10.0.2.2 has no line at all, so it counts as reached.
        self.assertEqual(len(reached), 3)
        self.assertIn("did not run", self.canary.parse_net("Error: image not known", targets)[0][0])

    def test_the_probe_tells_a_canary_listener_from_a_closed_port(self):
        token = "tw-canary-test-token"
        listeners = self.canary.plant_listeners(token, ["127.0.0.1"])
        self.assertEqual(len(listeners), 1)
        try:
            port = listeners[0].getsockname()[1]
            r = subprocess.run([sys.executable, "-I", "-c", self.canary.NET_PROBE, token, "localhost", "http://127.0.0.1:1/",
                                f"127.0.0.1:{port}", "127.0.0.1:1", "127.0.0.1:1/any"],
                               capture_output=True, text=True, timeout=60)
        finally:
            for s in listeners:
                s.close()
        self.assertIn(f"REACHED 127.0.0.1:{port} answered with the token", r.stdout)
        self.assertIn("BLOCKED 127.0.0.1:1 ", r.stdout)
        self.assertIn("BLOCKED 127.0.0.1:1/any ", r.stdout)
        self.assertIn("DONE", r.stdout)

    def test_the_vms_own_addresses_leave_out_loopback(self):
        out = ("1: lo    inet 127.0.0.1/8 scope host lo\n2: ens3    inet 10.0.0.20/24 brd 10.0.0.255 scope global ens3\n"
               "3: tailscale0    inet 100.90.1.2/32 scope global tailscale0\n")
        got = self.canary.vm_addresses(lambda argv, **kw: subprocess.CompletedProcess(argv, 0, out, ""))
        self.assertEqual(got, ["10.0.0.20", "100.90.1.2"])

    def test_the_canary_runs_the_network_attack_with_bramble_s_own_flags(self):
        src = (REPO / "src" / "thornswood_brokers" / "canary.py").read_text(encoding="utf-8")
        self.assertIn('c.tier_a_network(SANDBOX_IMAGE, build(policy, "bramble")["terminal"]["docker_extra_args"])', src)


if __name__ == "__main__":
    unittest.main()
