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


def peer_uid(sock: socket.socket) -> int:
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


def user_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"uid-{uid}"


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        server: Server = self.server  # type: ignore[assignment]
        caller = user_name(peer_uid(self.request))
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
                 mode: int = 0o660, group: str | None = None):
        """ops maps an op name to (handler(caller, args), users allowed to call it)."""
        self.path = Path(path)
        self.ops = ops
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
        s.sendall((json.dumps({"op": op, "args": args or {}}) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_LINE:
                raise Refused("the broker's answer is larger than 4 MB")
    if not buf:
        raise Refused(f"the broker at {path} closed without answering; the outcome is unknown until it is read back")
    answer = json.loads(buf)
    if not answer.get("ok"):
        raise Refused(answer.get("reason") or "refused")
    return answer.get("result")
