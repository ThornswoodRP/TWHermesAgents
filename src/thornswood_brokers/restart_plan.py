"""What an install must start again, read from the code each process loads.

12 Nov 2026: every install stopped the connector (and every Bot turn it was
running) and restarted all four brokers, whatever the release changed, so
tw-follow-main held new turns before each one and waited up to 20 minutes
for the running ones. That night's installs held the Bots for 5, 16 and 21
minutes, and #158, a change to tests and the Bots' route, held them 5
minutes and then restarted nothing a turn runs in.

A process holds the code it imported when it started. So a process needs
starting again only when a file it loads changed: the modules its entry
point imports, followed through every import of our own packages (top level
or inside a function, since each runs at some point), the data files beside
those modules (policy.json, contracts.json and the like), and its launcher
and unit. `loaded` reads that set from a release's own files, and `changed`
says, for each process here, whether it differs between the release that is
installed and the one going in. A release whose files cannot be read (a
release folder without its packages) counts as changed for every process,
so nothing is ever kept on code it does not hold.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

# Our own packages: an import of anything else is not followed.
PACKAGES = ("thornswood_guard", "thornswood_brokers", "thornswood_decisions", "thornswood_live")

# Each process an install may keep running, by what it loads: its entry
# modules (followed) and its files (launcher, unit), relative to the release.
# The brokers are one: each starts from thornswood_brokers.serve, whose own
# imports name all four services.
PROCESSES = {
    "brokers": {"modules": ["thornswood_brokers.serve"],
                "files": ["deploy/bin/tw-serve", "deploy/systemd/tw-policy.service", "deploy/systemd/tw-git.service",
                          "deploy/systemd/tw-files.service", "deploy/systemd/tw-proxy.service",
                          "deploy/systemd/tw-policy.socket", "deploy/systemd/tw-git.socket",
                          "deploy/systemd/tw-files.socket", "deploy/systemd/tw-proxy.socket"]},
    "connector": {"modules": ["thornswood_decisions.connector"],
                  "files": ["deploy/decisions/tw-decide-connector", "deploy/decisions/tw-decide-connector.service"]},
}


def _module_file(src: Path, name: str) -> Path | None:
    p = src.joinpath(*name.split("."))
    if p.with_suffix(".py").is_file():
        return p.with_suffix(".py")
    if (p / "__init__.py").is_file():
        return p / "__init__.py"
    return None


def _imports(tree: ast.AST, name: str, is_package: bool) -> list[str]:
    """The modules of our own packages one file imports, absolute or relative."""
    here = name.split(".") if is_package else name.split(".")[:-1]
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names if a.name.split(".")[0] in PACKAGES]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = here[:len(here) - (node.level - 1)] if node.level > 1 else here
                parts = base + (node.module.split(".") if node.module else [])
            else:
                parts = (node.module or "").split(".")
            if not parts or parts[0] not in PACKAGES:
                continue
            mod = ".".join(parts)
            out.append(mod)
            # `from . import brokers` names a module; `from .policy import Policy` a name in one.
            out += [f"{mod}.{a.name}" for a in node.names if a.name != "*"]
    return out


def _beside(tree: ast.AST, f: Path) -> list[Path]:
    """The files one module names beside itself, `Path(__file__).with_name("x")`:
    a helper it runs by path (the connector's room_send.py) is not imported,
    so only this finds it."""
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "with_name"
                and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            p = f.with_name(node.args[0].value)
            if p.is_file():
                out.append(p)
    return out


def loaded(src: Path, modules: list[str]) -> list[Path] | None:
    """Every file a process starting from `modules` loads from `src`: the
    modules, followed through their imports of our packages, each package's
    __init__, the files a module names beside itself, and the data files at
    the top of each package it uses. None when an entry module is not there."""
    files: set[Path] = set()
    seen: set[str] = set()
    todo = list(modules)
    for m in modules:
        if _module_file(src, m) is None:
            return None
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        f = _module_file(src, name)
        if f is None:
            continue  # a name inside a module (from .policy import Policy), not a module
        files.add(f)
        parts = name.split(".")
        for i in range(1, len(parts)):
            todo.append(".".join(parts[:i]))  # importing a.b runs a/__init__.py first
        tree = ast.parse(f.read_bytes(), filename=str(f))
        todo += _imports(tree, name, f.name == "__init__.py")
        files.update(_beside(tree, f))
    for pkg in {f.relative_to(src).parts[0] for f in files}:
        files.update(p for p in (src / pkg).iterdir() if p.is_file() and p.suffix not in (".py", ".pyc"))
    return sorted(files)


def digest(release: Path, src: Path, process: str) -> str | None:
    """One digest of everything `process` loads from this release; None when
    the release does not hold it."""
    spec = PROCESSES[process]
    files = loaded(src, spec["modules"])
    if files is None:
        return None
    h = hashlib.sha256()
    for f in files:
        h.update(f.relative_to(src).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    for rel in spec["files"]:
        f = release / rel
        h.update(rel.encode() + b"\0" + (f.read_bytes() if f.is_file() else b"<missing>") + b"\0")
    return h.hexdigest()


def changed(before: tuple[Path, Path] | None, after: tuple[Path, Path]) -> dict[str, bool]:
    """For each process, whether it must start again to run `after`: True
    unless both releases hold it and what it loads is the same. Each release
    is (its folder, where its packages are)."""
    out = {}
    for process in PROCESSES:
        new = digest(*after, process)
        old = digest(*before, process) if before else None
        out[process] = new is None or old is None or new != old
    return out
