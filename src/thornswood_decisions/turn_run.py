"""One Bot Chat turn, run apart from the connector so that it outlives it (TWHermesAgents #163).

    python3 -I turn_run.py <folder> -- <the turn's command>

The connector (connector.py, Hermes.start_turn) starts this in a session of its own, with the turn's stream
file as its stdout. It runs the turn's command with that same stdout, so Hermes's stream-json goes straight
into the file, never through a pipe to the connector, and once the command ends it writes how it ended into
`<folder>/exit`: the exit code, or "not-started" when the command could not start (nothing was sent).

Before 15 Nov 2026 each turn was a child of the connector writing into a pipe the connector read, so an
install that started the connector again for a change to its own code ended every turn it ran, and the
answer each was working on never reached the core. Now the connector's unit stops only the connector itself
(KillMode=process): this process and the turn go on in the same unit, with its sandbox, and the connector
that starts next follows the stream file and the exit file to the turn's end (connector.Connector.adopt).

Standard library only, and nothing of this repository is imported: it is run by path, with the Python the
connector runs on.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

EXIT = "exit"
NOT_STARTED = "not-started"


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] != "--":
        print("usage: turn_run.py <folder> -- <command>", file=sys.stderr)
        return 2
    folder, command = Path(argv[0]), argv[2:]
    try:
        # stdout is the turn's stream file, inherited as it is; stderr goes nowhere, as before.
        code = str(subprocess.call(command, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    except OSError:
        code = NOT_STARTED
    # Whole or not at all: the connector reads the turn as ended once this file is there.
    part = folder / f"{EXIT}.part"
    part.write_text(code, encoding="utf-8")
    os.replace(part, folder / EXIT)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
