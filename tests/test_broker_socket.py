"""A broker takes the socket systemd holds for it (#162).

12 Nov 2026: each broker removed and bound its own socket when it started, so
for a second or two every call was refused, and a turn whose ticket check was
refused then was refused for the rest of it. Now systemd's socket unit keeps
the socket open while the broker starts again, and a call made meanwhile
waits for the new broker instead.
"""

import sys as _sys
import unittest as _unittest

if not _sys.platform.startswith("linux"):
    raise _unittest.SkipTest("the brokers run only on the Oracle VM; their tests run there")

import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from thornswood_brokers import serve, wire


def held(path: Path) -> socket.socket:
    """The socket as systemd holds it: bound and listening, outliving any broker."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    s.listen(16)
    return s


def broker(path: Path, sock: socket.socket, answer: str) -> wire.Server:
    me = wire.user_name(os.getuid())
    server = wire.Server(path, {"who": (lambda caller, args: answer, {me})}, fd=os.dup(sock.fileno()))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class TakesSystemdsSocket(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "policy.sock"
        self.sock = held(self.path)
        self.addCleanup(self.sock.close)

    def test_a_call_made_while_the_broker_starts_again_is_answered_by_the_new_one(self):
        old = broker(self.path, self.sock, "old")
        self.assertEqual(wire.call(self.path, "who", timeout=5), "old")
        old.shutdown()
        old.server_close()
        self.assertTrue(self.path.exists(), "the broker's stop leaves systemd's socket where it is")
        got = {}
        waiting = threading.Thread(target=lambda: got.update(answer=wire.call(self.path, "who", timeout=10)))
        waiting.start()
        waiting.join(0.5)
        self.assertTrue(waiting.is_alive(), "the call waits for the broker; it is not refused")
        new = broker(self.path, self.sock, "new")
        self.addCleanup(new.server_close)
        self.addCleanup(new.shutdown)
        waiting.join(10)
        self.assertEqual(got, {"answer": "new"})

    def test_a_socket_on_another_path_is_refused(self):
        other = held(self.path.with_name("git.sock"))
        self.addCleanup(other.close)
        with self.assertRaises(wire.Refused) as e:
            wire.Server(self.path, {}, fd=os.dup(other.fileno()))
        self.assertIn("the socket unit and the policy disagree", str(e.exception))


class ListenFd(unittest.TestCase):
    def test_systemds_socket_for_this_process(self):
        fd = os.dup(0)
        self.addCleanup(os.close, fd)
        env = {"LISTEN_PID": "42", "LISTEN_FDS": "1", "LISTEN_FDNAMES": "tw-policy.socket"}
        start = serve.LISTEN_FDS_START
        try:
            serve.LISTEN_FDS_START = fd
            self.assertEqual(serve.listen_fd(env, pid=42), fd)
        finally:
            serve.LISTEN_FDS_START = start
        self.assertEqual(env, {}, "nothing the broker starts takes them for its own")
        self.assertFalse(os.get_inheritable(fd))

    def test_none_without_one_or_for_another_process(self):
        self.assertIsNone(serve.listen_fd({}, pid=42))
        self.assertIsNone(serve.listen_fd({"LISTEN_PID": "7", "LISTEN_FDS": "1"}, pid=42))
        self.assertIsNone(serve.listen_fd({"LISTEN_PID": "42", "LISTEN_FDS": "0"}, pid=42))

    def test_more_than_one_socket_stops_it(self):
        with self.assertRaises(SystemExit):
            serve.listen_fd({"LISTEN_PID": "42", "LISTEN_FDS": "2"}, pid=42)


if __name__ == "__main__":
    unittest.main()
