"""Which group disk caps on the VM are not the Bots' own (deploy/install.sh, step 7b).

The only group caps Thornswood sets are each Bot's work group's (disk_quota_gb in the
policy). A cap is kept by gid, not by name, so a cap left on a deleted group passes to
whichever group groupadd gives that gid next. Until 3 Nov the canary deleted its
tw-work-canary group with its 32 MB cap still on, the next system group made was
tw-decide-voice, and that cap refused the Whisper model's download and stopped every
install. The install takes every cap that is not a Bot's off, and prints each one.

Usage: repquota -g -n <mount> | python3 -I disk_caps.py <gid of each Bot's work group>...
It prints the gid of every other group that has a block or file limit, one a line.
"""

from __future__ import annotations

import sys


def capped(report: str) -> dict[int, tuple[int, int, int, int]]:
    """The groups in `repquota -g -n` output that have a limit, as gid -> (block soft, block
    hard, file soft, file hard). A grace column shows only while a group is over its soft
    limit, so the file numbers are the ones after the block hard limit that are numbers."""
    out = {}
    for line in report.splitlines():
        parts = line.split()
        if not parts or not parts[0].startswith("#") or not parts[0][1:].isdigit() or len(parts) < 5:
            continue
        blocks = parts[2:5]
        files = [p for p in parts[5:] if p.isdigit()]
        if not all(p.isdigit() for p in blocks) or len(files) < 3:
            raise ValueError(f"repquota's line for group {parts[0]} is not in the shape read here: {line.strip()}")
        limits = (int(blocks[1]), int(blocks[2]), int(files[1]), int(files[2]))
        if any(limits):
            out[int(parts[0][1:])] = limits
    return out


def leftover(report: str, keep: set[int]) -> list[int]:
    """The gids with a cap that is not one of the Bots' work groups' caps."""
    return sorted(g for g in capped(report) if g not in keep)


def main(argv: list[str]) -> int:
    try:
        keep = {int(a) for a in argv}
    except ValueError:
        print(f"the gids to keep must be numbers, not {argv}", file=sys.stderr)
        return 2
    report = sys.stdin.read()
    if "Report for group quotas" not in report:
        print("this is not repquota -g -n output", file=sys.stderr)
        return 2
    try:
        gids = leftover(report, keep)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    for g in gids:
        print(g)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
