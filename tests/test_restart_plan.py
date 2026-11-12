"""What an install starts again (thornswood_brokers.restart_plan), and the
install scripts' options that keep the rest running.

12 Nov 2026: every install held new turns for up to 20 minutes, stopped the
connector with every Bot Chat turn it ran, and restarted all four brokers,
whatever the release changed.
"""

import sys as _sys
import unittest as _unittest

if not _sys.platform.startswith("linux"):
    # The brokers are Linux code (Unix sockets with SO_PEERCRED, pwd): they run
    # only on the Oracle VM, and these tests run in the VM half of
    # scripts/test-at-head.sh.
    raise _unittest.SkipTest("the brokers run only on the Oracle VM; their tests run there")

import shutil
import socket
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from helpers import REPO

from thornswood_brokers import restart_plan, wire
from thornswood_guard import brokers as guard_brokers


def release(change: dict[str, str] | None = None, remove: str = "") -> tuple[Path, Path]:
    d = Path(tempfile.mkdtemp())
    shutil.copytree(REPO / "src", d / "src", ignore=shutil.ignore_patterns("__pycache__", "app"))
    shutil.copytree(REPO / "deploy", d / "deploy")
    for rel, text in (change or {}).items():
        (d / rel).write_text((d / rel).read_text() + text)
    if remove:
        (d / remove).unlink()
    return d, d / "src"


class Loaded(unittest.TestCase):
    def setUp(self):
        self.before = release()

    def changed(self, **kw):
        return restart_plan.changed(self.before, release(**kw))

    def test_the_same_code_starts_nothing_again(self):
        self.assertEqual(self.changed(), {"brokers": False, "connector": False})

    def test_a_module_neither_loads_starts_neither(self):
        for rel in ("src/thornswood_decisions/api.py", "src/thornswood_guard/owed_wake.py", "src/thornswood_brokers/follow_main.py"):
            self.assertEqual(self.changed(change={rel: "\n# x\n"}), {"brokers": False, "connector": False}, rel)

    def test_a_module_a_broker_imports_inside_a_function_starts_them(self):
        # serve.py imports policy_service only for the policy broker, in main().
        self.assertEqual(self.changed(change={"src/thornswood_brokers/policy_service.py": "\n# x\n"}),
                         {"brokers": True, "connector": False})

    def test_the_policy_both_load_starts_both(self):
        self.assertEqual(self.changed(change={"src/thornswood_guard/policy.json": " "}),
                         {"brokers": True, "connector": True})

    def test_a_helper_the_connector_runs_by_path_starts_it(self):
        self.assertEqual(self.changed(change={"src/thornswood_decisions/room_send.py": "\n# x\n"}),
                         {"brokers": False, "connector": True})

    def test_a_launcher_or_unit_starts_its_process(self):
        self.assertTrue(self.changed(change={"deploy/systemd/tw-git.service": "\n"})["brokers"])
        self.assertTrue(self.changed(change={"deploy/decisions/tw-decide-connector.service": "\n"})["connector"])
        self.assertFalse(self.changed(change={"deploy/decisions/tw-decide-live.service": "\n"})["connector"])

    def test_a_release_without_the_code_starts_everything(self):
        self.assertEqual(restart_plan.changed(None, self.before), {"brokers": True, "connector": True})
        self.assertEqual(self.changed(remove="src/thornswood_decisions/connector.py")["connector"], True)
        empty = Path(tempfile.mkdtemp())
        self.assertEqual(restart_plan.changed((empty, empty), self.before), {"brokers": True, "connector": True})


class InstallOptions(unittest.TestCase):
    def test_keep_brokers_leaves_them_running_and_checks_they_are_up(self):
        text = (REPO / "deploy" / "install.sh").read_text()
        self.assertIn("--keep-brokers) keep_brokers=", text)
        keep = text.split('if [[ -n "$keep_brokers" ]]; then', 1)[1].split("\nfi\n", 1)[0]
        ours, theirs = keep.split("\nelse\n")
        self.assertNotIn("systemctl restart", ours)
        self.assertIn("systemctl is-active --quiet", ours)
        self.assertIn("systemctl restart tw-policy.service tw-git.service tw-files.service tw-proxy.service", theirs)

    def test_keep_connector_leaves_it_out_of_the_stop_and_never_restarts_it(self):
        text = (REPO / "deploy" / "decisions" / "install.sh").read_text()
        self.assertIn("--keep-connector) keep_connector=1", text)
        self.assertIn('CORE_WORKERS="${CORE_WORKERS/tw-decide-connector.service /}"', text)
        self.assertLess(text.index("CORE_WORKERS/tw-decide-connector.service"), text.index("systemctl stop $CORE_WORKERS"))
        self.assertIn('if [[ -n "$keep_connector" ]] && systemctl is-active --quiet tw-decide-connector.service; then',
                      text)


class AResetIsNoAnswer(unittest.TestCase):
    """12 Nov 2026, 12:40 AM: tw-policy restarting for an install reset the
    connector's call mid-answer, and the reset ended the connector."""

    def resetting(self) -> Path:
        """A broker that takes the request and then goes, its socket reset
        as the call reads the answer."""
        path = Path(tempfile.mkdtemp()) / "b.sock"
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(path))
        srv.listen(1)
        self.addCleanup(srv.close)
        return path

    def call(self, fn, path):
        real = socket.socket

        class Reset(real):
            def recv(self, n):
                raise ConnectionResetError(104, "Connection reset by peer")
        with mock.patch("socket.socket", Reset):
            return fn(str(path), "admit", {}, timeout=5)

    def test_the_brokers_call_says_the_outcome_is_unknown(self):
        with self.assertRaises(wire.Refused) as got:
            self.call(wire.call, self.resetting())
        self.assertIn("closed without answering (Connection reset by peer); the outcome is unknown", str(got.exception))

    def test_the_guards_call_says_the_outcome_is_unknown(self):
        with self.assertRaises(guard_brokers.BrokerRefused) as got:
            self.call(guard_brokers.call, self.resetting())
        self.assertIn("closed without answering (Connection reset by peer); the outcome is unknown", str(got.exception))


if __name__ == "__main__":
    unittest.main()
