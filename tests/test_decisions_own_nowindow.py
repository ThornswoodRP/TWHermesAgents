"""One's connector never opens a window on Patrick's screen (2 Nov).

One's task runs the connector with pythonw, which has no console, and Windows gives every console
program such a process starts a console window of its own. Patrick saw windows flash open and shut
over and over: the readiness check starts Hermes's python.exe each minute (each 15 seconds while the
gateway is down). Checks, with Windows stood in for: every child the connector starts (the runtime
question, the readiness check, a reaction, a turn, git for an update, the connector under --supervise)
is started with CREATE_NO_WINDOW, and nothing changes elsewhere.

Nothing runs a real Hermes, git or process: each start is caught before it happens.
"""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import decisions_helpers  # noqa: F401  (puts the repository on the path)
from thornswood_decisions import own_connector

NO_WINDOW = {"creationflags": 0x08000000}


class Done:
    def __init__(self, out: dict):
        self.returncode, self.stdout, self.stderr = 0, (json.dumps(out) + "\n").encode(), b""


class Proc:
    def __init__(self):
        self.stdout = io.StringIO('{"type": "system", "session_id": "20261102_000000_abcdef"}\n'
                                  '{"type": "result", "exit_code": 0}\n')

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


class NoWindow(unittest.TestCase):
    def setUp(self):
        self.starts = []
        self.home = Path(tempfile.mkdtemp())

    def run_(self, out):
        def run(argv, **kw):
            self.starts.append((argv[0], kw.get("creationflags")))
            return Done(out)
        return run

    def popen(self, argv, **kw):
        self.starts.append((argv[0], kw.get("creationflags")))
        return Proc()

    def every_child(self):
        hermes = own_connector.Hermes(["hermes"], ["python.exe", "-I", "helper.py"], self.home, "One")
        with mock.patch.object(own_connector.subprocess, "run", self.run_({"ready": True, "set": True})), \
                mock.patch.object(own_connector.subprocess, "Popen", self.popen):
            hermes.ready()
            hermes.react("20261102_000000_abcdef", 1, "\U0001F44D")
            hermes.turn("20261102_000000_abcdef", "hello")
        with mock.patch.object(own_connector.subprocess, "run", self.run_({})):
            try:
                own_connector.helper_command("hermes.exe")
            except (SystemExit, ValueError, TypeError, KeyError):
                pass  # only how it was started matters here
        own_connector.Updater(self.home, "git.exe", self.home, run=self.run_({}))._git("status")
        own_connector.supervise(["--supervise"], run=lambda argv, **kw: self.popen(argv, **kw) and Exit(),
                                sleep=lambda s: None, rounds=1, contain=False)
        return self.starts

    def test_on_windows_every_child_starts_with_no_window(self):
        with mock.patch.object(own_connector, "WINDOWS", True):
            starts = self.every_child()
        names = [n for n, _ in starts]
        for want in ("python.exe", "hermes", "hermes.exe", "git.exe"):
            self.assertIn(want, names)
        self.assertGreaterEqual(len(starts), 6)
        self.assertEqual({flags for _, flags in starts}, {NO_WINDOW["creationflags"]}, starts)

    def test_elsewhere_nothing_changes(self):
        with mock.patch.object(own_connector, "WINDOWS", False):
            starts = self.every_child()
        self.assertEqual({flags for _, flags in starts}, {None}, starts)

    def test_no_child_is_started_any_other_way(self):
        # Every subprocess call in the connector carries quiet(): a new one without it fails here.
        src = Path(own_connector.__file__).read_text(encoding="utf-8")
        import re
        calls = re.findall(r"(?:subprocess\.(?:run|Popen)|self\.run|\brun)\((?:[^()]|\([^()]*\))*\)", src)
        calls = [c for c in calls if not c.startswith("run(argv") and "def " not in c]
        self.assertTrue(calls)
        for c in calls:
            self.assertIn("**quiet()", c, c)


class Exit:
    def wait(self):
        return 0


if __name__ == "__main__":
    unittest.main()
