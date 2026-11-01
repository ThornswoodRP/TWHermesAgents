"""tw-reload-bots: after an apply, every Bot process starts again with the
applied plugin. 31 Oct: a gateway restart alone left Bramble's room turns,
which run in Hermes Desktop's login-session backends, on the old tool list."""

import subprocess
import unittest
from pathlib import Path

from thornswood_brokers.reload import GATEWAY, reload_bots, sessions

REPO = Path(__file__).resolve().parent.parent

TABLE = """\
     3 1002 hermes        - pts/0 active  no -
     7 1002 hermes        - pts/1 active  no -
     9 1001 patrick-hermes - pts/2 active no -
    12 1000 ubuntu        - pts/3 active  no -
"""


class FakeHost:
    def __init__(self, table=TABLE, gateway_after="active", lingering=0):
        self.rows = {l.split()[0]: l for l in table.splitlines() if l.strip()}
        self.gateway_after = gateway_after
        self.lingering = lingering  # list-sessions calls before ended sessions disappear
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[:2] == ["loginctl", "list-sessions"]:
            if self.lingering:
                self.lingering -= 1
                table = self.rows.values() if not hasattr(self, "_ended") else \
                    list(self.rows.values()) + [self._ended_rows[s] for s in self._ended]
            else:
                table = self.rows.values()
            return subprocess.CompletedProcess(argv, 0, "\n".join(table) + "\n", "")
        if argv[:2] == ["loginctl", "terminate-session"]:
            self._ended = getattr(self, "_ended", []) + [argv[2]]
            self._ended_rows = getattr(self, "_ended_rows", {})
            self._ended_rows[argv[2]] = self.rows.pop(argv[2])
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:2] == ["systemctl", "restart"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:2] == ["systemctl", "is-active"]:
            return subprocess.CompletedProcess(argv, 0 if self.gateway_after == "active" else 3, self.gateway_after + "\n", "")
        raise AssertionError(argv)


class Reload(unittest.TestCase):
    def run_it(self, host):
        said = []
        code = reload_bots(run=host, sleep=lambda s: None, say=said.append)
        return code, said

    def test_only_the_hermes_users_sessions(self):
        self.assertEqual(sessions(FakeHost()), ["3", "7"])

    def test_ends_hermes_sessions_then_restarts_the_gateway(self):
        host = FakeHost()
        code, said = self.run_it(host)
        self.assertEqual(code, 0, said)
        verbs = [c for c in host.calls if c[1] != "list-sessions"]
        self.assertEqual(verbs, [["loginctl", "terminate-session", "3"], ["loginctl", "terminate-session", "7"],
                                 ["systemctl", "restart", GATEWAY], ["systemctl", "is-active", GATEWAY]])
        self.assertIn("ended 2 hermes login sessions", said[0])

    def test_never_terminate_user_and_never_other_users(self):
        host = FakeHost()
        self.run_it(host)
        flat = [" ".join(c) for c in host.calls]
        self.assertFalse([c for c in flat if "terminate-user" in c or "kill-user" in c])
        self.assertFalse([c for c in flat if c.endswith(("terminate-session 9", "terminate-session 12"))])

    def test_waits_for_the_sessions_to_go(self):
        host = FakeHost(lingering=3)
        code, said = self.run_it(host)
        self.assertEqual(code, 0, said)

    def test_stops_when_the_gateway_does_not_come_back(self):
        code, said = self.run_it(FakeHost(gateway_after="failed"))
        self.assertEqual(code, 1)
        self.assertIn("failed after the restart", said[-1])

    def test_no_sessions_still_restarts_the_gateway(self):
        host = FakeHost(table="    12 1000 ubuntu - pts/3 active no -\n")
        code, said = self.run_it(host)
        self.assertEqual(code, 0)
        self.assertIn(["systemctl", "restart", GATEWAY], host.calls)

    def test_the_install_puts_it_in_bin_and_the_docs_name_it(self):
        self.assertTrue((REPO / "deploy" / "bin" / "tw-reload-bots").exists())
        self.assertIn("deploy/bin/*", (REPO / "deploy" / "install.sh").read_text())
        for f in ("README.md", "scripts/apply.py"):
            text = (REPO / f).read_text()
            self.assertIn("sudo /opt/thornswood-hermes/bin/tw-reload-bots", text, f)
            self.assertNotIn("sudo systemctl restart tw-hermes-gateway", text, f)


if __name__ == "__main__":
    unittest.main()
