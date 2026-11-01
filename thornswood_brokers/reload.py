"""Reload the Bots after an apply, as root: /opt/thornswood-hermes/bin/tw-reload-bots.

A Hermes process loads the guard plugin and the profile's config when it
starts and keeps them. Two kinds of process run the Bots:

- tw-hermes-gateway, the system unit in tw-hermes.slice (the Discord side);
- the hermes user's login sessions: Hermes Desktop's backends over ssh
  (session-*.scope under user-<uid>.slice), which run the room's turns.

31 Oct: after the #20 apply and a gateway restart, Bramble's room turn still
had the old tool list, because Desktop's backends had started before the
apply. So both go: the hermes user's login sessions are ended, one by one,
and the gateway is restarted. Never terminate-user: that also stops
user@<uid>.service, which the gateway needs, and took the gateway down once
(31 Oct). Desktop reconnects by itself.
"""

from __future__ import annotations

import subprocess
import sys
import time
from typing import Callable

USER = "hermes"
GATEWAY = "tw-hermes-gateway.service"

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True)


def sessions(run: Runner, user: str = USER) -> list[str]:
    """The ids of the user's login sessions, from loginctl's own table."""
    r = run(["loginctl", "list-sessions", "--no-legend", "--no-pager"])
    if r.returncode != 0:
        raise RuntimeError(f"loginctl list-sessions failed: {r.stderr.strip()}")
    out = []
    for line in r.stdout.splitlines():
        cols = line.split()
        if len(cols) >= 3 and cols[2] == user:
            out.append(cols[0])
    return out


def reload_bots(run: Runner = _run, sleep: Callable[[float], None] = time.sleep, say=print) -> int:
    ended = sessions(run)
    for sid in ended:
        r = run(["loginctl", "terminate-session", sid])
        if r.returncode != 0:
            say(f"STOP: loginctl terminate-session {sid} failed: {r.stderr.strip()}")
            return 1
    for _ in range(30):
        left = [s for s in sessions(run) if s in ended]
        if not left:
            break
        sleep(1)
    else:
        say(f"STOP: the hermes sessions {' '.join(left)} did not end in 30 seconds; look at: loginctl list-sessions")
        return 1
    say(f"ended {len(ended)} hermes login session{'s' if len(ended) != 1 else ''} (Hermes Desktop's backends; Desktop reconnects)")

    r = run(["systemctl", "restart", GATEWAY])
    if r.returncode != 0:
        say(f"STOP: systemctl restart {GATEWAY} failed: {r.stderr.strip()}")
        return 1
    sleep(5)
    r = run(["systemctl", "is-active", GATEWAY])
    if r.stdout.strip() != "active":
        say(f"STOP: {GATEWAY} is {r.stdout.strip() or 'not active'} after the restart: journalctl -u {GATEWAY} -n 50")
        return 1
    say(f"{GATEWAY} restarted and active; every Bot process now has the applied plugin and config")
    return 0


def main(argv: list[str] | None = None) -> int:
    import os
    if os.geteuid() != 0:
        print("STOP: run it as root: sudo /opt/thornswood-hermes/bin/tw-reload-bots")
        return 1
    return reload_bots()


if __name__ == "__main__":
    sys.exit(main())
