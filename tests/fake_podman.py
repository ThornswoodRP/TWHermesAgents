"""A stand-in for podman in apply.py's tests. It keeps a one-line state file
named by FAKE_PODMAN_STATE: "present", "missing" or "broken-pull"."""

import os
import sys
from pathlib import Path

state_file = Path(os.environ["FAKE_PODMAN_STATE"])
state = state_file.read_text(encoding="utf-8").strip()
args = sys.argv[1:]

with open(state_file.with_suffix(".log"), "a", encoding="utf-8") as log:
    log.write(" ".join(args) + "\n")

if args[:2] == ["image", "exists"]:
    sys.exit(0 if state == "present" else 1)
if args[:1] == ["pull"]:
    if state == "broken-pull":
        print("Error: initializing source: connection refused", file=sys.stderr)
        sys.exit(125)
    state_file.write_text("present", encoding="utf-8")
    sys.exit(0)
sys.exit(2)
