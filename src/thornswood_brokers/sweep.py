"""tw-sweep: every hour, as root from tw-sweep.timer, the Bots' finished
trees go without waiting for a Bot turn.

tw-git fetches each repo's mirror and writes every Bot's branch list
(branches), then tw-files removes each tree that was sent and whose branch
GitHub no longer has (sweep), as a checkout does. Until 26 Nov 2026 both
happened only inside a Bot's turn, and the disk floor that the trees bring
on stops every turn, so once the floor was reached nothing freed the space.
"""

from __future__ import annotations

import sys

from thornswood_guard.policy import Policy

from . import wire


def sweep(policy: Policy, call=wire.call, say=print) -> int:
    socks = policy.data["brokers"]["sockets"]
    try:
        lists = call(socks["git"]["path"], "branches", {}, timeout=600)
    except (wire.Refused, OSError) as e:
        say(f"FAIL tw-git did not list the Bots' branches ({e}); no tree is removed without a fresh list")
        return 1
    for repo, why in sorted((lists.get("failed") or {}).items()):
        say(f"FAIL {repo}: the mirror was not fetched ({why}); its lists stay as they were")
    try:
        done = call(socks["files"]["path"], "sweep", {}, timeout=600)
    except (wire.Refused, OSError) as e:
        say(f"FAIL tw-files did not sweep ({e})")
        return 1
    removed = done.get("removed") or {}
    for bot, trees in sorted(removed.items()):
        say(f"removed {len(trees)} finished tree(s) of {bot}: {', '.join(trees)}")
    if not removed:
        say("no finished tree to remove")
    return 1 if lists.get("failed") else 0


def main(argv: list[str]) -> int:
    return sweep(Policy.load())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
