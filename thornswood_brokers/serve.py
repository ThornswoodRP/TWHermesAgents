"""Start one broker: python3 -I -m thornswood_brokers.serve policy|git|files|proxy

Each runs as its own user from its systemd unit (deploy/systemd), from the
root-owned copy under /opt/thornswood-hermes, never from the hermes user's
release folder. The policy is the copy's own policy.json.
"""

from __future__ import annotations

import logging
import os
import pwd
import signal
import sys
import threading
from pathlib import Path

from thornswood_guard.policy import Policy

from . import wire

logger = logging.getLogger("thornswood_brokers")


def _expect_user(policy: Policy, role: str) -> None:
    want = policy.data["brokers"]["users"][role]
    have = pwd.getpwuid(os.getuid()).pw_name
    if have != want:
        raise SystemExit(f"tw-{role} runs as {want}, not {have}; start it from its systemd unit")


def _asker(policy: Policy):
    path = policy.data["brokers"]["sockets"]["policy"]["path"]

    def ask(op: str, args: dict):
        return wire.call(path, op, args)
    return ask


def _serve(policy: Policy, name: str, ops: dict) -> None:
    sock = policy.data["brokers"]["sockets"][name]
    allowed = set(sock["clients"])
    for op, (_, users) in ops.items():
        extra = users - allowed
        if extra:
            raise SystemExit(f"{name}: op {op} names {sorted(extra)}, who may not connect to its socket")
    server = wire.Server(sock["path"], ops, mode=0o660, group=sock.get("group"))
    logger.info("tw-%s listening on %s", name, sock["path"])
    run_until_stopped(server, f"tw-{name}")


# How long a stop waits for the requests in hand to be answered. systemd's
# own stop timeout is 90 seconds; a request still running after this is
# reported and left: tw-git reads back any write whose answer it lost.
DRAIN_SECONDS = 20


def run_until_stopped(server, name: str, *, stop: threading.Event | None = None,
                      drain_seconds: float = DRAIN_SECONDS) -> None:
    """Serve until SIGTERM or SIGINT, then stop cleanly: take no new request,
    let the ones in hand answer, close the socket, and return.

    serve_forever() runs on a thread of its own. shutdown() waits for
    serve_forever() to return, so called from a signal handler on the thread
    that runs serve_forever() it waits for itself forever; until 1 Nov every
    broker did that, and systemd killed each one after 90 seconds, so every
    follower install left the Bots without tw-git and tw-policy for about
    three minutes."""
    stop = stop or threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *a: stop.set())
    serving = threading.Thread(target=server.serve_forever, name=f"{name}-serve", daemon=True)
    serving.start()
    stop.wait()
    logger.info("%s stopping: no new requests", name)
    server.shutdown()
    left = server.drain(drain_seconds) if hasattr(server, "drain") else 0
    if left:
        logger.warning("%s stopped with %d request(s) unanswered after %ss", name, left, drain_seconds)
    server.server_close()
    serving.join(5)
    logger.info("%s stopped", name)


def main(argv: list[str]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if len(argv) != 1 or argv[0] not in ("policy", "git", "files", "proxy"):
        raise SystemExit("usage: python3 -I -m thornswood_brokers.serve policy|git|files|proxy")
    role = argv[0]
    policy = Policy.load()
    _expect_user(policy, role)
    os.umask(0o007)
    state = policy.data["brokers"]["state"]
    if role == "policy":
        from .policy_service import PolicyService
        svc = PolicyService(state["policy_db"], policy)
        # The boot's limits check ran before tw-policy started; its result is
        # in /run/tw (a tmpfs, so never a past boot's).
        import json
        boot = Path("/run/tw/limits.json")
        if boot.exists():
            got = json.loads(boot.read_text())
            svc.limits_record("root", {"ok": bool(got.get("ok")), "details": got.get("problems") or []})
        else:
            svc.limits_record("root", {"ok": False, "details": ["no limits check ran this boot"]})
        _serve(policy, "policy", svc.ops())
    elif role == "git":
        from .git_service import GitService
        from .github_api import GitHub
        gh = GitHub(policy.data["brokers"]["apps"])
        for app in policy.data["brokers"]["apps"].values():
            if not os.access(app["key_file"], os.R_OK):
                raise SystemExit(f"tw-git cannot read {app['key_file']}; the install script says where the App keys go")
        svc = GitService(policy, gh, _asker(policy), mirrors=Path(state["git_mirrors"]), spool=Path(state["spool"]))
        _serve(policy, "git", svc.ops())
    elif role == "files":
        from .files_service import FilesService
        svc = FilesService(policy, _asker(policy), work=Path(state["work"]), spool=Path(state["spool"]))
        _serve(policy, "files", svc.ops())
    else:
        from .proxy import Proxy, Server, check_environment, load_env, load_tokens
        problems = check_environment(os.environ)
        if problems:
            raise SystemExit("; ".join(problems))
        cfg = policy.data["admission_proxy"]
        env = load_env(cfg["env_file"])
        tokens = load_tokens(cfg["tokens_file"], policy) if Path(cfg["tokens_file"]).exists() else {}
        if not tokens:
            logger.warning("no proxy tokens yet; every request is refused until admission_proxy.ready and the install make them")
        proxy = Proxy(policy, _asker(policy), env, tokens)
        server = Server(cfg["listen"], proxy)
        logger.info("tw-proxy listening on %s", cfg["listen"])
        run_until_stopped(server, "tw-proxy")


if __name__ == "__main__":
    main(sys.argv[1:])
