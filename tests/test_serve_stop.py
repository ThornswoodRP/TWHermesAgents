"""A broker stops cleanly on SIGTERM, and tw-policy answers one op at a time.

1 Nov: each broker called server.shutdown() from its SIGTERM handler, on the
thread running serve_forever(). shutdown() waits for serve_forever() to
return, so it waited for itself; systemd killed tw-git and tw-policy after 90
seconds each, and every follower install left the Bots without them for
about three minutes (Bramble's reads were reset and its guard check timed
out). And tw-policy answered each caller on its own thread over one SQLite
connection, which misreads rows when two threads run the same statement.
"""

import sys as _sys
import unittest as _unittest

if not _sys.platform.startswith("linux"):
    raise _unittest.SkipTest("the brokers run only on the Oracle VM; their tests run there")

import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from helpers import REPO

from test_policy_service import service

# A broker as tw-serve starts one: wire.Server on a socket, run until SIGTERM.
# "slow" answers after a second, so a stop has a request in hand.
BROKER = r"""
import sys, time, os
sys.path.insert(0, sys.argv[1])
from thornswood_brokers import wire
from thornswood_brokers.serve import run_until_stopped
me = wire.user_name(os.getuid())
def slow(caller, args):
    time.sleep(1.0)
    return "answered"
server = wire.Server(sys.argv[2], {"slow": (slow, {me}), "quick": (lambda c, a: "hi", {me})}, mode=0o600)
print("ready", flush=True)
run_until_stopped(server, "tw-test", drain_seconds=float(sys.argv[3]))
print("stopped", flush=True)
"""


class CleanStop(unittest.TestCase):
    def start(self, drain=5.0):
        sock = Path(tempfile.mkdtemp()) / "b.sock"
        proc = subprocess.Popen([_sys.executable, "-I", "-c", BROKER, str(REPO), str(sock), str(drain)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.assertEqual(proc.stdout.readline().strip(), "ready")
        return proc, sock

    def test_sigterm_stops_it_at_once(self):
        from thornswood_brokers import wire
        proc, sock = self.start()
        self.assertEqual(wire.call(sock, "quick"), "hi")
        began = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=10)
        self.assertLess(time.monotonic() - began, 5, "a stop waits for nothing that is not in hand")
        self.assertEqual(proc.returncode, 0, err)
        self.assertIn("stopped", out)
        self.assertFalse(sock.exists() and _connects(sock), "the socket takes no request after the stop")

    def test_a_request_in_hand_is_answered_before_it_stops(self):
        from thornswood_brokers import wire
        proc, sock = self.start()
        got = {}
        t = threading.Thread(target=lambda: got.setdefault("a", wire.call(sock, "slow")))
        t.start()
        time.sleep(0.3)
        proc.send_signal(signal.SIGTERM)
        t.join(10)
        out, err = proc.communicate(timeout=10)
        self.assertEqual(got.get("a"), "answered")
        self.assertEqual(proc.returncode, 0, err)

    def test_a_request_still_running_after_the_drain_is_named_and_left(self):
        proc, sock = self.start(drain=0.2)
        from thornswood_brokers import wire
        threading.Thread(target=lambda: _quiet(wire.call, sock, "slow"), daemon=True).start()
        time.sleep(0.3)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, err)
        self.assertIn("unanswered", err)

    def test_every_broker_stops_this_way(self):
        text = (REPO / "thornswood_brokers" / "serve.py").read_text(encoding="utf-8")
        self.assertNotIn("server.shutdown(), sys.exit", text)
        self.assertEqual(text.count("  run_until_stopped(server, "), 2, "the socket brokers and tw-proxy")


class PolicyOneAtATime(unittest.TestCase):
    def test_every_op_holds_the_stores_lock(self):
        s = service()
        held = []

        def probe(caller, args):
            held.append(s.lock._is_owned())
            return None
        s.status = probe  # type: ignore[assignment]
        handler, _ = s.ops()["status"]
        handler("root", {})
        self.assertEqual(held, [True])

    def test_reads_from_many_threads_agree(self):
        s = service()
        handler, _ = s.ops()["ledger_pending"]
        bad = []

        def read():
            for _ in range(300):
                try:
                    if handler("root", {}) != []:
                        bad.append("wrong answer")
                except Exception as e:  # noqa: BLE001
                    bad.append(f"{type(e).__name__}: {e}")
        ts = [threading.Thread(target=read) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(bad, [])


def _connects(sock: Path) -> bool:
    import socket
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        try:
            s.connect(str(sock))
            return True
        except OSError:
            return False


def _quiet(fn, *a):
    try:
        fn(*a)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    unittest.main()
