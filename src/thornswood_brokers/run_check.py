"""The root helper tw-files starts a check with (08 section 3).

tw-checks.socket starts one tw-checks@ instance of this, as root, for each
connection; tw-files never uses sudo, so it keeps NoNewPrivileges. The helper
trusts nothing tw-files passes: the caller must be tw-files by its socket's
peer credentials, the Bot a worker with an execution user, the tree a real
directory under that Bot's own work folder (no link on the way), and the
command one of the policy's harness forms. Then it runs systemd-run with the
line check_argv builds, which starts the check in its own unit as the Bot's
execution user, and answers with the check's exit code and output.

The request is one line of JSON, as every broker takes it:
{"op": "run", "args": {"bot": ..., "tree": ..., "command": [...]}}.

Usage (from the unit only): tw-run-check --socket
"""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path

from thornswood_guard.policy import Policy

from .files_service import FilesService, check_argv
from .wire import MAX_LINE, Refused, peer_uid, user_name


def validate(policy: Policy, argv: list[str]) -> list[str]:
    if len(argv) < 3:
        raise Refused("a check is <bot> <tree> <command...>")
    bot, tree, cmd = argv[0], argv[1], argv[2:]
    if bot not in policy.data["brokers"]["exec_users"]:
        raise Refused(f"{bot} has no execution user; only the five workers run checks")
    root = Path(policy.data["brokers"]["state"]["work"]) / bot
    t = Path(tree)
    if not t.is_absolute() or os.path.realpath(t) != str(t) or root not in t.parents:
        raise Refused(f"{tree} is not a tree under {root}")
    if len(t.relative_to(root).parts) != 2:
        raise Refused(f"{tree} is not a task tree (<repo>/<task>) under {root}")
    st = os.lstat(t)
    if not stat.S_ISDIR(st.st_mode):
        raise Refused(f"{tree} is not a folder")
    FilesService.check_command(_Checker(policy), cmd)
    return check_argv(policy, bot, str(t), cmd)


class _Checker:
    """Just enough of a FilesService for check_command."""

    def __init__(self, policy: Policy):
        self.cfg = policy.data["brokers"]["files"]


def answer(policy: Policy, caller: str, line: bytes, run=subprocess.run) -> dict:
    """One request from `caller` to one answer. Never raises."""
    want = policy.data["brokers"]["users"]["files"]
    if caller != want:
        return {"ok": False, "reason": f"{caller} may not start a check; only {want} does"}
    try:
        req = json.loads(line)
        if not isinstance(req, dict) or req.get("op") != "run" or not isinstance(req.get("args"), dict):
            raise ValueError("a request is {op: run, args: {bot, tree, command}}")
        a = req["args"]
        cmd = a.get("command")
        if not isinstance(cmd, list) or not all(isinstance(c, str) for c in cmd):
            raise ValueError("command is a list of words")
        argv = validate(policy, [str(a.get("bot") or ""), str(a.get("tree") or ""), *cmd])
    except Refused as e:
        return {"ok": False, "reason": str(e)}
    except ValueError as e:
        return {"ok": False, "reason": f"not a request: {e}"}
    timeout = policy.data["brokers"]["files"]["check_minutes"] * 60 + 30
    try:
        r = run(argv, capture_output=True, text=True, timeout=timeout)
        return {"ok": True, "result": {"exit": r.returncode, "stdout": r.stdout or "", "stderr": r.stderr or ""}}
    except subprocess.TimeoutExpired:
        return {"ok": True, "result": {"exit": 124, "stdout": "", "stderr": "the check ran past its time and was stopped"}}


def serve_one(sock: socket.socket, policy: Policy) -> None:
    caller = user_name(peer_uid(sock))
    f = sock.makefile("rb")
    line = f.readline(MAX_LINE + 1)
    out = {"ok": False, "reason": "the request is larger than 4 MB"} if len(line) > MAX_LINE else answer(policy, caller, line)
    sock.sendall((json.dumps(out) + "\n").encode("utf-8"))


def main(argv: list[str]) -> None:
    if argv != ["--socket"]:
        raise SystemExit("tw-run-check runs only from tw-checks@.service, with --socket")
    if os.geteuid() != 0:
        raise SystemExit("tw-run-check runs as root, from tw-checks@.service")
    sock = socket.socket(fileno=0)
    if sock.family != socket.AF_UNIX:
        raise SystemExit("tw-run-check answers a Unix socket only")
    serve_one(sock, Policy.load())


if __name__ == "__main__":
    main(sys.argv[1:])
