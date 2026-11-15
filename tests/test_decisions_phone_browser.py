"""The phone app in a real browser engine: headless Chromium through Playwright,
with Chrome's virtual authenticator standing in for the phone's passkey.

It runs where node, Playwright with its Chromium, and the passkey library are
all installed, and skips elsewhere, saying which is missing. It is not test T10:
that is Patrick's own phone, on the ts.net origin, with Tailscale on.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import unittest
from pathlib import Path

from test_decisions_phone import HAVE_PASSKEY_LIB

HERE = Path(__file__).resolve().parent


def node_env() -> dict | None:
    node = shutil.which("node")
    if not node:
        return None
    env = dict(os.environ)
    npm = shutil.which("npm")
    if npm:
        root = subprocess.run([npm, "root", "-g"], capture_output=True, text=True).stdout.strip()
        if root:
            env["NODE_PATH"] = root
    ok = subprocess.run([node, "-e", "require.resolve('playwright')"], env=env, capture_output=True)
    return env if ok.returncode == 0 else None


ENV = node_env()

# A whole run takes well under this; one that does not is stopped and says how far it got (#123).
LIMIT = 180


@unittest.skipUnless(HAVE_PASSKEY_LIB, "py_webauthn is not installed here")
@unittest.skipUnless(ENV, "node with Playwright is not installed here")
class PhoneInChromium(unittest.TestCase):
    def test_the_phone_app(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = subprocess.Popen([sys.executable, str(HERE / "decisions_phone_server.py"), str(port)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            info = json.loads(server.stdout.readline())
            try:
                out = subprocess.run([shutil.which("node"), str(HERE / "decisions_phone_check.js"), str(port),
                                      info["code"], info["card"], info["high"], info["third"], info["board_next"],
                                      info["voice_wav"], info["voice_words"], info["voice_reply"]], env=ENV, capture_output=True, text=True, timeout=LIMIT)
            except subprocess.TimeoutExpired as late:
                # A run stopped at its limit said nothing before, so its failing step was never seen (#123).
                said = "".join(x.decode(errors="replace") if isinstance(x, bytes) else x for x in (late.stdout, late.stderr) if x)
                self.fail(f"the check was stopped after {LIMIT} seconds; what it said until then:\n" + said)
        finally:
            server.terminate()
            server.wait(timeout=10)
            server.stdout.close()
            server.stderr.close()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("ALL PASS", out.stdout)


if __name__ == "__main__":
    unittest.main()
