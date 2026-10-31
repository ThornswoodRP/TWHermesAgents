"""The root helper tw-files starts a check with (08 section 3).

sudoers lets tw-files run /opt/thornswood-hermes/bin/tw-run-check and
nothing else as root. The helper trusts nothing tw-files passes: it checks the
Bot is a worker with an execution user, the tree is a real directory under
that Bot's own work folder (no link on the way), and the command is one of the
policy's harness forms, then execs systemd-run with the line check_argv
builds. It never runs anything itself.

Usage: tw-run-check <bot> <tree> <command...>
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from thornswood_guard.policy import Policy

from .files_service import FilesService, check_argv
from .wire import Refused


def validate(policy: Policy, argv: list[str]) -> list[str]:
    if len(argv) < 3:
        raise Refused("usage: tw-run-check <bot> <tree> <command...>")
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


def main(argv: list[str]) -> None:
    if os.geteuid() != 0:
        raise SystemExit("tw-run-check runs as root through sudo, for tw-files only")
    if os.environ.get("SUDO_USER") not in (None, "tw-files"):
        raise SystemExit("only tw-files starts checks")
    policy = Policy.load()
    try:
        line = validate(policy, argv)
    except Refused as e:
        raise SystemExit(f"refused: {e}") from None
    os.execvp(line[0], line)


if __name__ == "__main__":
    main(sys.argv[1:])
