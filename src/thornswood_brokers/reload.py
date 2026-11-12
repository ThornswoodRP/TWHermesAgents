"""Reload the Bots after an apply, as root: /opt/thornswood-hermes/bin/tw-reload-bots.

A Hermes process loads the guard plugin and the profile's config when it
starts and keeps them. Two kinds of process run the Bots:

- tw-hermes-gateway, the system unit in tw-hermes.slice (the Discord side);
- the hermes user's login sessions: Hermes Desktop's backends over ssh
  (session-*.scope under user-<uid>.slice), which run the room's turns.

31 Oct: after the #20 apply and a gateway restart, Bramble's room turn still
had the old tool list, because Desktop's backends had started before the
apply. So both go: the hermes user's login sessions are ended, one by one,
and the gateway is restarted (stopped first, started last). Never terminate-user: that also stops
user@<uid>.service, which the gateway needs, and took the gateway down once
(31 Oct). Desktop reconnects by itself.

2 Nov, 8:24 PM: the put-back's reload ended Desktop's seven dashboard
sessions, they were still there 30 seconds later, and the reload stopped with
the gateway stopped, so the Bots were down until a new merge installed. Two
things were wrong, and both are fixed here:

- Ending a session is systemd's SIGTERM, then SIGKILL for whatever is still
  in it (logind's scope stop, after 90 seconds). The reload gave up at 30
  seconds while systemd was still ending them. Now a session still there after
  GRACE seconds is ended the same way systemd would end it, with SIGKILL, and
  what was still running in it is said first, so the process that would not
  stop is on record.
- The gateway is the Discord side and holds no Desktop session, so a session
  that will not end never keeps it stopped. The ended turns' tickets go back
  and the gateway starts in every case; the reload still fails, naming the
  session, when one is left.

9 Nov 2026 (#52): the gateway was stopped 64 times from 1 to 9 Nov, mostly by
this reload after follow-main installed a merge, and 14 of those stops cut a
running turn off when the gateway's drain timed out. A cut turn leaves its
writes for read-back and its sweep owed. So the reload first has tw-policy
hold new turns (they wait in line), waits up to DRAIN_WAIT for the running
ones to end, and only then stops the gateway. A turn still running after
that is named and cut off; tw-policy's ten-minute rule has already ended
any that went silent. The hold ends when the gateway is back, or by itself
at tw-policy's HOLD_MAX if the reload dies.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

USER = "hermes"
GATEWAY = "tw-hermes-gateway.service"
# Seconds a terminated session gets to end on SIGTERM, then after SIGKILL.
GRACE = 30
AFTER_KILL = 10
# How long running turns get to end before the gateway stops, and how often
# the reload looks. Within follow-main's 45 minutes with the tests and install.
DRAIN_WAIT = 20 * 60
DRAIN_POLL = 10
# Given by a caller that has held new turns and waited for the running ones
# already (follow_main.Follower), which also ends the hold.
HELD_FLAG = "--held"

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


def _policy(op: str, args: dict) -> dict:
    """Ask tw-policy, as root."""
    import json

    from .wire import call
    policy = json.loads(Path(__file__).resolve().parent.parent.joinpath("thornswood_guard", "policy.json").read_text())
    return call(policy["brokers"]["sockets"]["policy"]["path"], op, args, timeout=30)


def _release_turns(why: str) -> dict:
    """Ask tw-policy, as root, to take back every model turn's ticket."""
    return _policy("release_turns", {"why": why})


def drain(ask: Callable[[str, dict], dict], sleep: Callable[[float], None], say, wait: int = DRAIN_WAIT) -> bool:
    """Holds new turns and waits for the running ones to end. True when the
    hold is on (end it with end_hold); False when tw-policy did not answer,
    and the reload goes on as before."""
    try:
        running = ask("hold_turns", {"why": "the Bots are being reloaded after an install",
                                     "seconds": wait + 600})["running"]
    except Exception as e:  # noqa: BLE001  the reload still has to happen
        say(f"FAIL: tw-policy did not hold new turns ({e}); a turn running now may be cut off")
        return False
    waited = 0
    while running and waited < wait:
        if waited == 0:
            say(f"holding new turns; waiting for {running} running turn{'s' if running != 1 else ''} to end "
                f"(at most {wait // 60} minutes)")
        sleep(DRAIN_POLL)
        waited += DRAIN_POLL
        try:
            running = ask("turns_running", {})["running"]
        except Exception as e:  # noqa: BLE001
            say(f"FAIL: tw-policy stopped answering while turns ended ({e}); going on")
            break
    if running:
        say(f"{running} turn{'s' if running != 1 else ''} still running after {waited // 60} minutes; cut off now")
    else:
        say(f"no turn running{f' after {waited} seconds' if waited else ''}; new turns held until the gateway is back")
    return True


def _gone(run: Runner, sleep: Callable[[float], None], ended: list[str], seconds: int) -> list[str]:
    """Waits up to `seconds` for the ended sessions to leave loginctl's table;
    the ones still there. New sessions (Desktop reconnecting) are not waited for."""
    left = [s for s in sessions(run) if s in ended]
    for _ in range(seconds):
        if not left:
            break
        sleep(1)
        left = [s for s in sessions(run) if s in ended]
    return left


def _procs_in(cgroup: str, root: Path = Path("/sys/fs/cgroup"), proc: Path = Path("/proc")) -> list[str]:
    """`<pid> <name>` for each process in a cgroup and the cgroups under it."""
    out = []
    base = root / cgroup.lstrip("/")
    for f in sorted(base.rglob("cgroup.procs")) if base.is_dir() else []:
        for pid in f.read_text().split():
            try:
                name = (proc / pid / "comm").read_text().strip()
            except OSError:
                name = "?"
            out.append(f"{pid} {name}")
    return out


def still(run: Runner, sid: str, procs_in: Callable[[str], list[str]] = _procs_in) -> list[str]:
    """What is still running in a session, from its scope's cgroup."""
    scope = run(["loginctl", "show-session", sid, "-p", "Scope", "--value"]).stdout.strip()
    if not scope:
        return []
    cg = run(["systemctl", "show", scope, "-p", "ControlGroup", "--value"]).stdout.strip()
    return procs_in(cg) if cg else []


def _end_sessions(run: Runner, sleep: Callable[[float], None], say) -> tuple[list[str], list[str]]:
    """Ends the hermes user's login sessions: SIGTERM, and SIGKILL for any
    still there after GRACE seconds. (ended, left)."""
    ended = sessions(run)
    for sid in ended:
        r = run(["loginctl", "terminate-session", sid])
        if r.returncode != 0:
            say(f"FAIL: loginctl terminate-session {sid} failed: {r.stderr.strip()}")
    left = _gone(run, sleep, ended, GRACE)
    if left:
        for sid in left:
            # Each process by id and name only: a command line can carry a
            # Desktop connection's token, and this goes to the follower's log.
            say(f"session {sid} was still running {GRACE} seconds after SIGTERM: {', '.join(still(run, sid)) or 'no process found'}")
            run(["loginctl", "kill-session", sid, "--signal=SIGKILL"])
        left = _gone(run, sleep, left, AFTER_KILL)
    return ended, left


def reload_bots(run: Runner = _run, sleep: Callable[[float], None] = time.sleep, say=print,
                release: Callable[[str], dict] = _release_turns, ask: Callable[[str, dict], dict] = _policy,
                already_held: bool = False) -> int:
    """already_held: whoever runs the reload has held new turns and waited
    for the running ones already (follow-main's install, HELD_FLAG), and ends
    the hold itself, so the reload neither waits again nor ends it. 12 Nov
    2026: the a98bd644 install held new turns at 7:40 PM Mountain and waited
    its 20 minutes for a running turn of Sprocket's; then this reload held
    them again at 8:01 PM and waited 20 minutes more for the same turn, so
    no Bot started a turn for 40 minutes: Echo's brief, Rift's and Wisp's
    next turns and every Bramble sweep waited in line."""
    if already_held:
        say("new turns are held already, and the running ones had their wait; reloading now")
        return _reload(run, sleep, say, release)
    held = drain(ask, sleep, say)
    try:
        return _reload(run, sleep, say, release)
    finally:
        if held:
            try:
                ask("end_hold", {})
            except Exception as e:  # noqa: BLE001  the hold ends by itself at HOLD_MAX
                say(f"FAIL: tw-policy did not end the hold on new turns ({e}); it ends by itself within 40 minutes")


def _reload(run: Runner, sleep: Callable[[float], None], say, release: Callable[[str], dict]) -> int:
    """Stop the gateway, end the hermes login sessions, give back the tickets
    of the turns that just ended, then start the gateway. Stopped first, so no
    new turn takes a ticket between the release and the start: 1 Nov, 3:42 PM,
    the #47/#48 install reloaded the Bots mid-turn, and Bramble's killed turn
    kept the fleet's one slot for ten minutes. The gateway starts again
    whatever the sessions did (2 Nov)."""
    r = run(["systemctl", "stop", GATEWAY])
    if r.returncode != 0:
        say(f"STOP: systemctl stop {GATEWAY} failed: {r.stderr.strip()}")
        return 1
    ok = True
    ended, left = _end_sessions(run, sleep, say)
    if left:
        ok = False
        say(f"FAIL: the hermes sessions {' '.join(left)} did not end even after SIGKILL; look at: loginctl session-status {left[0]}")
    else:
        say(f"ended {len(ended)} hermes login session{'s' if len(ended) != 1 else ''} (Hermes Desktop's backends; Desktop reconnects)")
    released = None
    try:
        released = release("the Bots were reloaded")["released"]
        say(f"gave back {released} model turn ticket{'s' if released != 1 else ''} the ended turns held")
    except Exception as e:  # noqa: BLE001  the queue's ten-minute rule still frees them
        ok = False
        say(f"FAIL: tw-policy did not take back the ended turns' tickets ({e}); they go back after ten minutes "
            f"of silence, and the next turns wait until then")
    # The gateway allows 3 starts in 15 minutes so a crash loop stops; a reload
    # is not a crash, so its own start does not count toward that (install.sh).
    run(["systemctl", "reset-failed", GATEWAY])
    r = run(["systemctl", "start", GATEWAY])
    if r.returncode != 0:
        say(f"STOP: systemctl start {GATEWAY} failed: {r.stderr.strip()}")
        return 1
    sleep(5)
    r = run(["systemctl", "is-active", GATEWAY])
    if r.stdout.strip() != "active":
        say(f"STOP: {GATEWAY} is {r.stdout.strip() or 'not active'} after the restart: journalctl -u {GATEWAY} -n 50")
        return 1
    say(f"{GATEWAY} restarted and active" + ("; every Bot process now has the applied plugin and config" if not left else
                                             f"; the sessions {' '.join(left)} may still run the old plugin"))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    import os
    argv = sys.argv[1:] if argv is None else argv
    if argv not in ([], [HELD_FLAG]):
        print(f"STOP: tw-reload-bots takes nothing, or {HELD_FLAG} when its caller holds new turns already")
        return 1
    if os.geteuid() != 0:
        print("STOP: run it as root: sudo /opt/thornswood-hermes/bin/tw-reload-bots")
        return 1
    return reload_bots(already_held=argv == [HELD_FLAG])


if __name__ == "__main__":
    sys.exit(main())
