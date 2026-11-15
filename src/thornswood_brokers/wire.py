"""One request, one answer, over a Unix socket, with the caller checked by user.

A request is one line of JSON: {"op": "<name>", "args": {...}}. The answer is
one line of JSON: {"ok": true, "result": ...} or {"ok": false, "reason": "..."}.
The server reads the caller's user from the socket's peer credentials
(SO_PEERCRED), never from the request, and refuses any op that user may not
ask for. A refusal always says why.
"""

from __future__ import annotations

import json
import logging
import os
import pwd
import socket
import socketserver
import struct
import threading
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

MAX_LINE = 4 * 1024 * 1024


class Refused(Exception):
    """A request the broker will not do. The message is the reason."""


def peer_creds(sock: socket.socket) -> tuple[int, int]:
    """(pid, uid) of the process at the other end of the socket."""
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, _gid = struct.unpack("3i", creds)
    return pid, uid


def peer_uid(sock: socket.socket) -> int:
    return peer_creds(sock)[1]


# The calling process of the request this thread is answering, from the
# socket like its user. tw-policy keeps it on a turn's ticket, so a reload
# knows which turns run in a process it stops (12 Nov 2026).
_peer = threading.local()


def peer_pid() -> int | None:
    """The pid of the process whose request this thread is answering; None
    outside a request."""
    return getattr(_peer, "pid", None)


def user_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"uid-{uid}"


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        server: Server = self.server  # type: ignore[assignment]
        pid, uid = peer_creds(self.request)
        caller = user_name(uid)
        _peer.pid = pid
        try:
            self._handle(server, caller)
        finally:
            _peer.pid = None

    def _handle(self, server: "Server", caller: str):
        line = self.rfile.readline(MAX_LINE + 1)
        if not line:
            return
        if len(line) > MAX_LINE:
            self._answer({"ok": False, "reason": "the request is larger than 4 MB"})
            return
        try:
            req = json.loads(line)
            if not isinstance(req, dict) or not isinstance(req.get("op"), str):
                raise ValueError("a request is an object with an op")
            args = req.get("args") or {}
            if not isinstance(args, dict):
                raise ValueError("args is an object")
        except ValueError as e:
            self._answer({"ok": False, "reason": f"not a request: {e}"})
            return
        op = req["op"]
        try:
            result = server.dispatch(caller, op, args)
            self._answer({"ok": True, "result": result})
        except Refused as e:
            logger.info("refused %s for %s: %s", op, caller, e)
            self._answer({"ok": False, "reason": str(e)})
        except Exception as e:  # a broker bug is a refusal, never a half-done write
            logger.exception("%s for %s failed", op, caller)
            self._answer({"ok": False, "reason": f"{op} failed inside the broker: {type(e).__name__}: {e}"})

    def _answer(self, obj):
        self.wfile.write((json.dumps(obj) + "\n").encode("utf-8"))


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str | Path, ops: dict[str, tuple[Callable, set[str]]], *,
                 mode: int = 0o660, group: str | None = None, fd: int | None = None):
        """ops maps an op name to (handler(caller, args), users allowed to call it).

        fd: the listening socket systemd holds for this path (its .socket
        unit, #162). It is taken as it is; systemd made the path, its mode and
        its group, and keeps the socket open while this process starts again,
        so a call made meanwhile waits instead of being refused."""
        self.path = Path(path)
        self.ops = ops
        if fd is not None:
            super().__init__(str(self.path), Handler, bind_and_activate=False)
            self.socket.close()
            self.socket = socket.socket(fileno=fd)
            if self.socket.family != socket.AF_UNIX or self.socket.getsockname() != str(self.path):
                got = self.socket.getsockname()
                self.socket.detach()
                raise Refused(f"the socket systemd passed listens on {got!r}, not {self.path}; "
                              "the socket unit and the policy disagree")
        else:
            if self.path.exists() or self.path.is_symlink():
                self.path.unlink()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            super().__init__(str(self.path), Handler)
            os.chmod(self.path, mode)
            if group:
                import grp
                os.chown(self.path, -1, grp.getgrnam(group).gr_gid)
        self._lock = threading.Lock()
        self._busy = 0
        self._idle = threading.Condition()

    def process_request_thread(self, request, client_address):
        with self._idle:
            self._busy += 1
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._idle:
                self._busy -= 1
                self._idle.notify_all()

    def drain(self, timeout: float) -> int:
        """Wait up to `timeout` seconds for the requests in hand to be answered.
        Returns how many were still running."""
        with self._idle:
            self._idle.wait_for(lambda: self._busy == 0, timeout)
            return self._busy

    def dispatch(self, caller: str, op: str, args: dict):
        entry = self.ops.get(op)
        if entry is None:
            raise Refused(f"{op} is not something this broker does")
        handler, allowed = entry
        if caller not in allowed:
            raise Refused(f"{caller} may not ask for {op}")
        return handler(caller, args)


class Servers:
    """Two or more Servers run as one by serve.run_until_stopped: the first
    serves on the caller's thread, the others each on a thread of their own,
    and a stop stops them all (tw-decide-bots and its tw-git socket, #132)."""

    def __init__(self, *servers: Server):
        self.servers = servers
        self._threads: list[threading.Thread] = []

    def serve_forever(self) -> None:
        for extra in self.servers[1:]:
            t = threading.Thread(target=extra.serve_forever, name=f"serve-{extra.path.name}", daemon=True)
            t.start()
            self._threads.append(t)
        self.servers[0].serve_forever()

    def shutdown(self) -> None:
        for s in self.servers:
            s.shutdown()

    def drain(self, timeout: float) -> int:
        return sum(s.drain(timeout) for s in self.servers)

    def server_close(self) -> None:
        for s in self.servers:
            s.server_close()
        for t in self._threads:
            t.join(5)


def call(path: str | Path, op: str, args: dict | None = None, *, timeout: float = 120.0):
    """Ask a broker. Returns the result, or raises Refused with its reason."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect(str(path))
        except OSError as e:
            raise Refused(f"the broker at {path} is not answering ({e.strerror or e}); nothing was done") from e
        buf = b""
        try:
            s.sendall((json.dumps({"op": op, "args": args or {}}) + "\n").encode("utf-8"))
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_LINE:
                    raise Refused("the broker's answer is larger than 4 MB")
        except OSError as e:
            # 12 Nov 2026: tw-policy restarting for an install reset the
            # connector's call mid-answer, and the reset, not a refusal, ended
            # the connector. A call dropped after it was sent has not been
            # answered, the same as a broker that closes without a word.
            raise Refused(f"the broker at {path} closed without answering ({e.strerror or e}); the outcome is "
                          "unknown until it is read back") from e
    if not buf:
        raise Refused(f"the broker at {path} closed without answering; the outcome is unknown until it is read back")
    answer = json.loads(buf)
    if not answer.get("ok"):
        raise Refused(answer.get("reason") or "refused")
    return answer.get("result")
