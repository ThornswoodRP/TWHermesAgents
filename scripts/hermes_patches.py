#!/usr/bin/env python3
"""Our reviewed fixes for Hermes Agent, on the VM's own Hermes checkout.

Patrick's answer A on q-hermes-vm-patches (26 Nov 2026): until a Hermes
release carries a fix from hermes/patches, the VM's Hermes runs it. Two of
them were why the Bots looked broken that week: on_session_end never fired
for a turn that failed early (0013), so the Board kept such turns as
"Thinking", and a benched Codex fallback was never asked whether its window
had reopened (0014), so turns Claude refused had no ChatGPT.

hermes/patches/series.json names, for each Hermes commit the VM may run,
the patches it takes, in order. apply.py calls this as the hermes user at
every install: the checkout's HEAD stays the pinned commit (so the contract
check and `hermes --version` still read it), and the patches sit in its
index and working tree. The tree is in one of four states:

  pristine  index and working tree are HEAD: the series is applied;
  patched   index and working tree are HEAD plus the series: nothing to do;
  earlier   index and working tree are HEAD plus an earlier series of ours
            (the one the last install recorded, or the start of this one):
            it is put back to HEAD, then the series is applied;
  other     anything else: stop, and change nothing.

After it applies, it records the tree it made in the checkout's git folder
(thornswood-patches.json), so that a series that grows or changes at the next
install is recognised as ours and moved, not mistaken for a hand edit.

A fix leaves the series when a Hermes release has it: the runtime update
takes the patches off first (`--remove`), moves to the new commit, and the
series for that commit no longer names it.

Run by hand, as hermes:

    python3 -I scripts/hermes_patches.py [--check | --remove] [--hermes-dir DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PATCHES = REPO / "hermes" / "patches"
SERIES = PATCHES / "series.json"
HERMES_DIR = "/home/hermes/.hermes/hermes-agent"


RECORD = "thornswood-patches.json"


class Refused(Exception):
    pass


def _git(d: Path, *args: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    full = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", **(env or {})}
    r = subprocess.run(["git", "-C", str(d), "-c", "core.hooksPath=/dev/null", *args],
                       capture_output=True, text=True, env=full)
    if check and r.returncode != 0:
        raise Refused(f"git {' '.join(args[:2])} in {d} exited {r.returncode}: {(r.stderr or r.stdout).strip()[-300:]}")
    return r


def series_for(head: str, series_file: Path = SERIES) -> list[Path]:
    data = json.loads(series_file.read_text(encoding="utf-8"))
    names = data.get("series", {}).get(head)
    if names is None:
        raise Refused(f"Hermes is at {head[:12]}, and {series_file.relative_to(REPO) if series_file.is_relative_to(REPO) else series_file} "
                      "names no patch series for it; the runtime pin and its series change in the same pull request")
    out = []
    for n in names:
        p = (series_file.parent / n).resolve()
        if not p.is_relative_to(series_file.parent.resolve()) or not p.is_file():
            raise Refused(f"the series names {n}, which is not a patch in hermes/patches")
        out.append(p)
    return out


def expected_tree(d: Path, head: str, patches: list[Path]) -> str:
    """The tree of HEAD with every patch applied, made in an index of its own."""
    with tempfile.TemporaryDirectory(prefix="tw-hermes-patches-") as t:
        env = {"GIT_INDEX_FILE": str(Path(t) / "index")}
        _git(d, "read-tree", head, env=env)
        for p in patches:
            r = _git(d, "apply", "--cached", "--whitespace=nowarn", str(p), env=env, check=False)
            if r.returncode != 0:
                raise Refused(f"{p.name} does not apply on Hermes {head[:12]}: {(r.stderr or r.stdout).strip()[-300:]}")
        return _git(d, "write-tree", env=env).stdout.strip()


def _record_path(d: Path) -> Path:
    return Path(_git(d, "rev-parse", "--absolute-git-dir").stdout.strip()) / RECORD


def recorded_tree(d: Path, head: str) -> str | None:
    """The tree the last install made on this HEAD, if it recorded one."""
    try:
        data = json.loads(_record_path(d).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("head") != head or not isinstance(data.get("tree"), str):
        return None
    return data["tree"]


def _write_record(d: Path, head: str, tree: str, patches: list[Path]) -> None:
    path = _record_path(d)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"head": head, "tree": tree, "patches": [p.name for p in patches]}, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def _earlier_trees(d: Path, head: str, patches: list[Path]) -> set[str]:
    """Trees an earlier install of ours may have left: the recorded one, and HEAD plus each start of
    this series (an install from before the record, whose series this one extends)."""
    trees = {recorded_tree(d, head)} - {None}
    for k in range(1, len(patches)):
        try:
            trees.add(expected_tree(d, head, patches[:k]))
        except Refused:
            break
    return trees


def state(d: Path, patches: list[Path]) -> tuple[str, str, str]:
    """(pristine | patched | earlier | other, the HEAD commit, the expected tree)."""
    head = _git(d, "rev-parse", "HEAD").stdout.strip()
    want = expected_tree(d, head, patches)
    index = _git(d, "write-tree", check=False)
    if index.returncode != 0:
        return "other", head, want
    worktree_clean = _git(d, "diff", "--quiet", check=False).returncode == 0
    if not worktree_clean:
        return "other", head, want
    have = index.stdout.strip()
    if have == want:
        return "patched", head, want
    if have == _git(d, "rev-parse", "HEAD^{tree}").stdout.strip():
        return "pristine", head, want
    if have in _earlier_trees(d, head, patches):
        return "earlier", head, want
    return "other", head, want


def _back_to_head(d: Path) -> None:
    """Index and working tree back to HEAD. Called only in state earlier: the index is a tree of
    ours and the working tree matches it, so this drops exactly our patches, files they added too."""
    _git(d, "reset", "-q", "--hard", "HEAD")


def run(d: Path, *, check: bool = False, remove: bool = False, series_file: Path = SERIES,
        say=print) -> tuple[bool, str]:
    """Brings the checkout to its series (or, with remove, back to HEAD).
    Returns (whether the checkout changed, one line saying what holds now).
    Raises Refused, having changed nothing, when it cannot."""
    head = _git(d, "rev-parse", "HEAD").stdout.strip()
    patches = series_for(head, series_file)
    now, head, want = state(d, patches)
    names = ", ".join(p.stem.split("-", 1)[0] for p in patches) or "none"
    if now == "other":
        raise Refused(f"the Hermes checkout {d} has changes that are neither {head[:12]} nor {head[:12]} with "
                      f"our series ({names}); look at `git -C {d} status` before anything is changed")
    if remove:
        if now == "pristine" or (now == "patched" and not patches):
            return False, f"Hermes {head[:12]} runs without our patches"
        if check:
            return False, f"Hermes {head[:12]} runs our patches; --remove would take them off"
        _back_to_head(d)
        _record_path(d).unlink(missing_ok=True)
        return True, f"took our patches off Hermes {head[:12]}"
    if now == "patched":
        return False, f"Hermes {head[:12]} runs our patches {names}, as hermes/patches/series.json says"
    if check:
        if now == "earlier":
            return False, f"Hermes {head[:12]} runs an earlier series of our patches; the install moves it to {names}"
        if not patches:
            return False, f"Hermes {head[:12]} takes no patch of ours"
        return False, f"Hermes {head[:12]} runs none of our patches yet; the install puts on {names}"
    if now == "earlier":
        _back_to_head(d)
    for p in patches:
        _git(d, "apply", "--index", "--whitespace=nowarn", str(p))
    got = _git(d, "write-tree").stdout.strip()
    if got != want:
        raise Refused(f"after the patches the Hermes index is {got[:12]}, not the expected {want[:12]}")
    _write_record(d, head, want, patches)
    if now == "earlier":
        return True, f"moved Hermes {head[:12]} from an earlier series of ours to our patches {names}"
    if not patches:
        return False, f"Hermes {head[:12]} takes no patch of ours"
    return True, f"put our patches {names} on Hermes {head[:12]}"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hermes-dir", default=HERMES_DIR)
    ap.add_argument("--series", type=Path, default=SERIES, help="the series file (tests)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="say what would change; change nothing")
    g.add_argument("--remove", action="store_true", help="take our patches off, before a runtime update")
    a = ap.parse_args(argv)
    try:
        _, line = run(Path(a.hermes_dir), check=a.check, remove=a.remove, series_file=a.series)
    except Refused as e:
        print(f"STOP: {e}. Nothing was changed.")
        return 1
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
