"""A connector learns of new work the moment it is there, in the core's memory only.

Patrick, 9 Nov: "it doesnt feel live it feels very delayed and I want it all to be live". A connector
claimed every five seconds, so a message, a Stop, an answer to Hermes's question or a page the phone asked
for waited up to five seconds before its connector even saw it. Now each thing a connector is handed bumps
its gateway's number here ("bots" for the Bots' connector, "me" and "one" for Patrick's own two), and the
connector holds a wait open on that number (`POST /connector/wait`, `POST /own/<gateway>/wait`), answered
the moment it changes. A write to the store bumps it once the write is in (Store.wake), so a connector that
wakes always finds what woke it.

Only numbers, never what the work is. A core that starts again starts from 0, and a connector holding an
older number is answered at once: one round more, never one less. A connector that cannot wait (an older
core, the network) claims every five seconds as before.
"""

from __future__ import annotations

import threading
import time

from .store import CoreError

MAX_WAIT = 25.0
BOTS_GW = "bots"  # the Bots' connector, as chat.PAGES_GW names it


class _Wakes:
    def __init__(self):
        self.cond = threading.Condition()
        self.n: dict[tuple[str, str], int] = {}

    def bump(self, ws: str, gw: str) -> None:
        with self.cond:
            self.n[(ws, gw)] = self.n.get((ws, gw), 0) + 1
            self.cond.notify_all()

    def wait(self, ws: str, gw: str, after, seconds) -> dict:
        """The gateway's number once it differs from `after`, waiting at most `seconds` of real time; at once
        when `after` is not given (a connector's first ask)."""
        try:
            seconds = min(max(float(seconds or 0), 0.0), MAX_WAIT)
            after = None if after is None else int(after)
        except (TypeError, ValueError):
            raise CoreError(400, "bad_wait", f"wait is 0 to {MAX_WAIT:g} seconds and after a number") from None
        end = time.monotonic() + seconds
        with self.cond:
            while True:
                n = self.n.get((ws, gw), 0)
                left = end - time.monotonic()
                if after is None or n != after or left <= 0:
                    return {"n": n}
                self.cond.wait(left)


WAKES = _Wakes()
