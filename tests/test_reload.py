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
    def __init__(self, table=TABLE, gateway_after="active", lingering=0, stuck=(), unkillable=(), reconnect=()):
        self.rows = {l.split()[0]: l for l in table.splitlines() if l.strip()}
        self.gateway_after = gateway_after
        self.lingering = lingering  # list-sessions calls before ended sessions disappear
        self.stuck = set(stuck)  # ended sessions that stay until SIGKILL
        self.unkillable = set(unkillable)  # and ones that stay even then
        self.reconnect = list(reconnect)  # sessions Desktop opens once the first are ended
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[:2] == ["loginctl", "list-sessions"] and (self.stuck or self.unkillable or self.reconnect):
            ended = getattr(self, "_ended", [])
            rows = list(self.rows.values()) + [self._ended_rows[s] for s in ended if s in self.stuck | self.unkillable]
            if ended:
                rows += [f"    {n} 1002 hermes - pts/9 active no -" for n in self.reconnect]
            return subprocess.CompletedProcess(argv, 0, "\n".join(rows) + "\n", "")
        if argv[:2] == ["loginctl", "show-session"]:
            return subprocess.CompletedProcess(argv, 0, f"session-{argv[2]}.scope\n", "")
        if argv[:2] == ["systemctl", "show"]:
            return subprocess.CompletedProcess(argv, 0, f"/user.slice/user-1002.slice/{argv[2]}\n", "")
        if argv[:2] == ["loginctl", "kill-session"]:
            self.stuck.discard(argv[2])
            return subprocess.CompletedProcess(argv, 0, "", "")
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
        if argv[:2] in (["systemctl", "stop"], ["systemctl", "start"]):
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:2] == ["systemctl", "is-active"]:
            return subprocess.CompletedProcess(argv, 0 if self.gateway_after == "active" else 3, self.gateway_after + "\n", "")
        raise AssertionError(argv)


class Reload(unittest.TestCase):
    def run_it(self, host, release=None):
        said = []
        self.released = []

        def gave_back(why):
            self.released.append((why, [c[:2] + c[2:] for c in host.calls]))
            return {"released": 2}
        code = reload_bots(run=host, sleep=lambda s: None, say=said.append, release=release or gave_back)
        return code, said

    def test_only_the_hermes_users_sessions(self):
        self.assertEqual(sessions(FakeHost()), ["3", "7"])

    def test_ends_hermes_sessions_then_restarts_the_gateway(self):
        host = FakeHost()
        code, said = self.run_it(host)
        self.assertEqual(code, 0, said)
        verbs = [c for c in host.calls if c[1] != "list-sessions"]
        self.assertEqual(verbs, [["systemctl", "stop", GATEWAY],
                                 ["loginctl", "terminate-session", "3"], ["loginctl", "terminate-session", "7"],
                                 ["systemctl", "start", GATEWAY], ["systemctl", "is-active", GATEWAY]])
        self.assertIn("ended 2 hermes login sessions", said[0])

    def test_the_ended_turns_tickets_go_back_while_nothing_runs(self):
        # 1 Nov, 3:42 PM: a reload mid-turn left Bramble's ticket holding the
        # fleet's one slot for ten minutes.
        host = FakeHost()
        code, said = self.run_it(host)
        self.assertEqual(code, 0, said)
        [(why, calls_then)] = self.released
        self.assertEqual(why, "the Bots were reloaded")
        verbs_then = [c for c in calls_then if c[1] != "list-sessions"]
        self.assertEqual(verbs_then[0], ["systemctl", "stop", GATEWAY], "the gateway is stopped first")
        self.assertNotIn(["systemctl", "start", GATEWAY], verbs_then, "and started only after the release")
        self.assertIn("gave back 2 model turn tickets", " ".join(said))

    def test_a_release_tw_policy_refuses_is_said_and_the_gateway_still_starts(self):
        host = FakeHost()

        def down(why):
            raise RuntimeError("the broker at /run/tw/policy.sock is not answering")
        code, said = self.run_it(host, release=down)
        self.assertEqual(code, 1)
        self.assertIn(["systemctl", "start", GATEWAY], host.calls)
        self.assertTrue([l for l in said if l.startswith("FAIL: tw-policy did not take back")])

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

    def test_desktops_reconnected_sessions_are_not_waited_for(self):
        # 2 Nov, 8:24 PM: Desktop opened seven dashboard sessions within
        # seconds of the end; only the ended ones are waited for.
        host = FakeHost(reconnect=["20", "21", "22"])
        code, said = self.run_it(host)
        self.assertEqual(code, 0, said)
        self.assertFalse([c for c in host.calls if c[:2] == ["loginctl", "kill-session"]])
        self.assertFalse([c for c in host.calls if c[-1] in ("20", "21", "22") and c[0] == "loginctl"])

    def test_a_session_still_there_after_sigterm_is_named_then_ended_with_sigkill(self):
        host = FakeHost(stuck=["7"])
        said = []
        code = reload_bots(run=host, sleep=lambda s: None, say=said.append, release=lambda why: {"released": 0})
        self.assertEqual(code, 0, said)
        self.assertIn(["loginctl", "kill-session", "7", "--signal=SIGKILL"], host.calls)
        self.assertNotIn(["loginctl", "kill-session", "3", "--signal=SIGKILL"], host.calls)
        named = [l for l in said if l.startswith("session 7 was still running 30 seconds after SIGTERM")]
        self.assertEqual(len(named), 1, said)
        kill_at = host.calls.index(["loginctl", "kill-session", "7", "--signal=SIGKILL"])
        self.assertLess(host.calls.index(["loginctl", "show-session", "7", "-p", "Scope", "--value"]), kill_at)
        self.assertEqual([c for c in host.calls if c[:2] == ["systemctl", "start"]], [["systemctl", "start", GATEWAY]])

    def test_a_session_that_will_not_end_never_keeps_the_gateway_stopped(self):
        # 2 Nov: the put-back's reload stopped with the gateway stopped, and the
        # Bots were down until the next merge installed.
        host = FakeHost(unkillable=["3"])
        code, said = self.run_it(host)
        self.assertEqual(code, 1)
        self.assertIn(["systemctl", "start", GATEWAY], host.calls)
        self.assertEqual(len(self.released), 1, "the ended turns' tickets still go back")
        self.assertTrue([l for l in said if l.startswith("FAIL: the hermes sessions 3 did not end even after SIGKILL")], said)
        self.assertIn("may still run the old plugin", said[-1])

    def test_what_is_left_is_named_by_pid_and_name_never_its_command_line(self):
        import tempfile
        from thornswood_brokers.reload import _procs_in, still
        root, proc = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        scope = root / "user.slice" / "session-7.scope"
        (scope / "inner").mkdir(parents=True)
        (scope / "cgroup.procs").write_text("41\n42\n")
        (scope / "inner" / "cgroup.procs").write_text("43\n")
        for pid, name in (("41", "sshd"), ("42", "node"), ("43", "conmon")):
            (proc / pid).mkdir()
            (proc / pid / "comm").write_text(name + "\n")
            (proc / pid / "cmdline").write_text("hermes\0dashboard\0--token\0SECRET\0")
        got = _procs_in("/user.slice/session-7.scope", root=root, proc=proc)
        self.assertEqual(sorted(got), ["41 sshd", "42 node", "43 conmon"])
        self.assertFalse([g for g in got if "SECRET" in g])
        self.assertEqual(still(FakeHost(), "7", procs_in=lambda cg: [cg]), ["/user.slice/user-1002.slice/session-7.scope"])

    def test_stops_when_the_gateway_does_not_come_back(self):
        code, said = self.run_it(FakeHost(gateway_after="failed"))
        self.assertEqual(code, 1)
        self.assertIn("failed after the restart", said[-1])

    def test_no_sessions_still_restarts_the_gateway(self):
        host = FakeHost(table="    12 1000 ubuntu - pts/3 active no -\n")
        code, said = self.run_it(host)
        self.assertEqual(code, 0)
        self.assertIn(["systemctl", "start", GATEWAY], host.calls)

    def test_the_install_puts_it_in_bin_and_the_docs_name_it(self):
        self.assertTrue((REPO / "deploy" / "bin" / "tw-reload-bots").exists())
        self.assertIn("deploy/bin/*", (REPO / "deploy" / "install.sh").read_text())
        for f in ("README.md", "scripts/apply.py"):
            text = (REPO / f).read_text()
            self.assertIn("sudo /opt/thornswood-hermes/bin/tw-reload-bots", text, f)
            self.assertNotIn("sudo systemctl restart tw-hermes-gateway", text, f)


if __name__ == "__main__":
    unittest.main()
