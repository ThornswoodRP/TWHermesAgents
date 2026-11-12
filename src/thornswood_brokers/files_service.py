"""tw-files: the only way a Bot reads, writes or runs anything in a work tree
(08 section 3).

Runs as tw-files. Each Bot's tree is /srv/tw/work/<bot>/<repo>/<task>/,
group tw-work-<bot>, setgid directories, mode 2770: tw-files and that Bot's
execution user (tw-<bot>, Tier B checks) are the only members, so no other
Bot's checks can read it. The Bot is the profile the gateway plugin names on
the call, and the socket checks the caller is the gateway's user.

Paths are walked one component at a time with O_NOFOLLOW from the tree's own
directory descriptor, so "..", absolute paths, a symlink anywhere on the way,
a hard link, a device, a FIFO or a socket never leads out of the tree. No path
may name .git: the repository's own files change only through the git ops
here, which run with hooks, filters, fsmonitor, pagers and file:// off, and
refuse a tree whose .git/config holds anything outside the short list in the
policy (a check runs as tw-<bot> and could have planted one).

Work comes in and goes out as git bundles: tw-git writes the base into
spool/in/<bot>, tw-files writes the Bot's branch into spool/out/<bot>, and
tw-git pushes it after its own checks. With the base, tw-git writes which of
the Bot's branches GitHub has now; a checkout removes the Bot's trees in that
repo whose branch went out and is gone from GitHub since. Neither broker
reads the other's state.
"""

from __future__ import annotations

import errno
import fnmatch
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import threading
from pathlib import Path
from typing import Callable

from thornswood_guard.policy import Policy

from .wire import Refused, call

TASK = re.compile(r"[a-z0-9][a-z0-9-]{0,60}")
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
OUT_CAP = 200_000
# No setgid bit in the mode: tw-files runs with RestrictSUIDSGID=yes, whose
# filter refuses a mkdir that asks for one with EPERM, before it even looks
# whether the folder is there (31 Oct, Rift's first checkout). The install
# makes each Bot's folder tw-files:tw-work-<bot> 2770, and Linux gives every
# folder made under a setgid folder the same group and the setgid bit, so the
# trees keep the Bot's group without asking.
DIR_MODE = 0o770
FILE_MODE = 0o660

# tw-checks.socket: the root helper a check starts through (run_check.py).
CHECK_SOCKET = "/run/tw-checks/checks.sock"


class FilesService:
    def __init__(self, policy: Policy, ask_policy: Callable[[str, dict], dict], *, work: Path, spool: Path,
                 runner: Callable[[list[str], int], subprocess.CompletedProcess] | None = None,
                 check_helper: list[str] | None = None, check_socket: str | Path = CHECK_SOCKET):
        self.policy = policy
        self.ask = ask_policy
        self.work = Path(work)
        self.spool_in = Path(spool) / "in"
        self.spool_out = Path(spool) / "out"
        self.cfg = policy.data["brokers"]["files"]
        self.runner = runner or _run
        # Checks start through tw-checks.socket (run_check.py), never sudo, so
        # tw-files keeps NoNewPrivileges. check_helper is the tests' stand-in.
        self.check_helper = check_helper
        self.check_socket = Path(check_socket)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()

    # Who and where

    def _bot(self, args: dict) -> str:
        bot = args.get("bot")
        if not isinstance(bot, str) or not self.policy.is_bot(bot):
            raise Refused(f"{bot!r} is not one of the six Bots")
        return bot

    def _repo_dir(self, bot: str, repo: str) -> str:
        repos = self.policy.data["repos"]
        if repo in (repos["app"], repos["ops"]):
            return repo.split("/")[1]
        if repo == repos["fork"]:
            if not self.policy.bots[bot].get("fork_branches"):
                raise Refused(f"only Wisp works in the engine fork, not {self.policy.bot_name(bot)}")
            return repo.split("/")[1]
        raise Refused(f"{repo} is not a repo the Bots work in")

    def _task(self, args: dict) -> str:
        task = str(args.get("task") or "")
        if not TASK.fullmatch(task):
            raise Refused("a task's tree is named like <issue>-<what>, lower case; the trees op lists yours")
        return task

    def _tree(self, args: dict, *, must_exist: bool = True) -> tuple[str, str, Path]:
        bot = self._bot(args)
        repo = str(args.get("repo") or "")
        tree = self.work / bot / self._repo_dir(bot, repo) / self._task(args)
        if must_exist:
            try:
                st = os.lstat(tree)
            except FileNotFoundError:
                raise Refused(f"{bot} has no tree for {repo} task {args.get('task')}; check it out first") from None
            if not stat.S_ISDIR(st.st_mode):
                raise Refused("that tree is not a directory")
            if os.path.realpath(tree) != str(tree):
                raise Refused("that tree's path leads through a link")
        return bot, repo, tree

    def _lock(self, bot: str) -> threading.Lock:
        with self._locks_lock:
            return self._locks.setdefault(bot, threading.Lock())

    # Safe paths

    @staticmethod
    def _parts(path: str) -> list[str]:
        if not isinstance(path, str) or not path or "\0" in path or len(path) > 1024:
            raise Refused("name a path inside the tree")
        if path.startswith("/") or path.startswith("\\") or re.match(r"^[A-Za-z]:", path):
            raise Refused(f"{path} is absolute; paths are inside the tree")
        parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
        if not parts:
            raise Refused("name a path inside the tree")
        for p in parts:
            if p == "..":
                raise Refused(f"{path} climbs out of the tree")
            if p.lower() == ".git" or p.lower().startswith(".git/"):
                raise Refused("the repository's own files change only through the git ops")
        return parts

    def _dir_fd(self, tree: Path, parts: list[str], *, create: bool = False) -> int:
        fd = os.open(tree, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for p in parts:
                if create:
                    try:
                        os.mkdir(p, DIR_MODE, dir_fd=fd)
                    except FileExistsError:
                        pass
                try:
                    nfd = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except OSError as e:
                    if e.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise Refused(f"{p} is a link or not a folder; paths never follow links") from None
                    if e.errno == errno.ENOENT:
                        raise Refused(f"{p} does not exist") from None
                    raise
                os.close(fd)
                fd = nfd
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _open_file(self, dir_fd: int, name: str, flags: int) -> int:
        try:
            fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, FILE_MODE, dir_fd=dir_fd)
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise Refused(f"{name} is a link; paths never follow links") from None
            if e.errno == errno.ENOENT:
                raise Refused(f"{name} does not exist") from None
            if e.errno == errno.EISDIR:
                raise Refused(f"{name} is a folder") from None
            raise
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            raise Refused(f"{name} is not a plain file")
        if st.st_nlink > 1:
            os.close(fd)
            raise Refused(f"{name} has another name somewhere (a hard link); it is not read or written")
        return fd

    # File ops

    def read(self, caller: str, args: dict) -> dict:
        bot, _, tree = self._tree(args)
        parts = self._parts(args.get("path"))
        offset = max(0, int(args.get("offset") or 0))
        limit = min(int(args.get("limit") or self.cfg["max_write_bytes"]), self.cfg["max_write_bytes"])
        with self._lock(bot):
            dfd = self._dir_fd(tree, parts[:-1])
            try:
                fd = self._open_file(dfd, parts[-1], os.O_RDONLY)
            finally:
                os.close(dfd)
            try:
                size = os.fstat(fd).st_size
                os.lseek(fd, offset, os.SEEK_SET)
                data = os.read(fd, limit)
            finally:
                os.close(fd)
        return {"text": data.decode("utf-8", "replace"), "size": size, "offset": offset, "read": len(data),
                "more": offset + len(data) < size}

    def write(self, caller: str, args: dict) -> dict:
        bot, _, tree = self._tree(args)
        parts = self._parts(args.get("path"))
        content = args.get("content")
        if not isinstance(content, str):
            raise Refused("write takes the file's text")
        data = content.encode("utf-8")
        if len(data) > self.cfg["max_write_bytes"]:
            raise Refused(f"one write is at most {self.cfg['max_write_bytes']} bytes")
        with self._lock(bot):
            self._write(tree, parts, data)
        return {"path": "/".join(parts), "bytes": len(data)}

    def _write(self, tree: Path, parts: list[str], data: bytes) -> None:
        dfd = self._dir_fd(tree, parts[:-1], create=True)
        try:
            try:
                st = os.lstat(parts[-1], dir_fd=dfd)
                if stat.S_ISDIR(st.st_mode):
                    raise Refused(f"{parts[-1]} is a folder")
                if not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
                    raise Refused(f"{parts[-1]} is not a plain file")
                if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
                    raise Refused(f"{parts[-1]} has another name somewhere (a hard link); it is not written")
            except FileNotFoundError:
                pass
            tmp = f".tw-write-{secrets.token_hex(6)}"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, FILE_MODE, dir_fd=dfd)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, parts[-1], src_dir_fd=dfd, dst_dir_fd=dfd)
        finally:
            os.close(dfd)

    def edit(self, caller: str, args: dict) -> dict:
        """Replace one exact piece of text, found exactly once."""
        bot, _, tree = self._tree(args)
        parts = self._parts(args.get("path"))
        old, new = args.get("old"), args.get("new")
        if not isinstance(old, str) or not isinstance(new, str) or not old:
            raise Refused("edit takes the old text and the new text")
        if len(new.encode()) > self.cfg["max_patch_bytes"]:
            raise Refused(f"one edit is at most {self.cfg['max_patch_bytes']} bytes")
        with self._lock(bot):
            dfd = self._dir_fd(tree, parts[:-1])
            try:
                fd = self._open_file(dfd, parts[-1], os.O_RDONLY)
            finally:
                os.close(dfd)
            try:
                if os.fstat(fd).st_size > self.cfg["max_write_bytes"]:
                    raise Refused(f"{parts[-1]} is larger than a write may be")
                text = os.read(fd, self.cfg["max_write_bytes"] + 1).decode("utf-8")
            finally:
                os.close(fd)
            n = text.count(old)
            if n != 1:
                raise Refused(f"the old text is found {n} times; it must be found exactly once")
            data = text.replace(old, new).encode("utf-8")
            if len(data) > self.cfg["max_write_bytes"]:
                raise Refused(f"the file would be over {self.cfg['max_write_bytes']} bytes")
            self._write(tree, parts, data)
        return {"path": "/".join(parts), "bytes": len(data)}

    def delete(self, caller: str, args: dict) -> dict:
        bot, _, tree = self._tree(args)
        parts = self._parts(args.get("path"))
        with self._lock(bot):
            dfd = self._dir_fd(tree, parts[:-1])
            try:
                st = os.lstat(parts[-1], dir_fd=dfd)
                if stat.S_ISDIR(st.st_mode):
                    raise Refused("delete takes a file, not a folder")
                os.unlink(parts[-1], dir_fd=dfd)
            except FileNotFoundError:
                raise Refused(f"{parts[-1]} does not exist") from None
            finally:
                os.close(dfd)
        return {"deleted": "/".join(parts)}

    def list(self, caller: str, args: dict) -> dict:
        bot, _, tree = self._tree(args)
        path = args.get("path") or "."
        parts = [] if path in (".", "") else self._parts(path)
        with self._lock(bot):
            dfd = self._dir_fd(tree, parts)
            try:
                out = []
                with os.scandir(dfd) as it:
                    for e in it:
                        if e.name == ".git" and not parts:
                            continue
                        st = e.stat(follow_symlinks=False)
                        kind = "dir" if stat.S_ISDIR(st.st_mode) else "file" if stat.S_ISREG(st.st_mode) else \
                            "link" if stat.S_ISLNK(st.st_mode) else "other"
                        out.append({"name": e.name, "type": kind, "size": st.st_size if kind == "file" else None})
                        if len(out) >= 5000:
                            break
            finally:
                os.close(dfd)
        return {"entries": sorted(out, key=lambda x: x["name"])}

    def trees(self, caller: str, args: dict) -> dict:
        """The Bot's own trees, so it can find a task's name before it asks
        for anything in one (31 Oct: Rift was asked for git status and had no
        way to learn its task names). Only this Bot's folder is read, links
        are never followed, and a tree the git ops would stop says why."""
        bot = self._bot(args)
        repos = self.policy.data["repos"]
        names = [repos["app"], repos["ops"]] + ([repos["fork"]] if self.policy.bots[bot].get("fork_branches") else [])
        out = []
        with self._lock(bot):
            try:
                bfd = self._dir_fd(self.work, [bot])
            except Refused:
                return {"trees": []}
            try:
                for repo in names:
                    try:
                        rfd = os.open(self._repo_dir(bot, repo), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=bfd)
                    except OSError:
                        continue
                    try:
                        with os.scandir(rfd) as it:
                            entries = sorted(e.name for e in it
                                             if TASK.fullmatch(e.name) and stat.S_ISDIR(e.stat(follow_symlinks=False).st_mode))
                    finally:
                        os.close(rfd)
                    for task in entries:
                        tree = self.work / bot / self._repo_dir(bot, repo) / task
                        entry = {"repo": repo, "task": task, "branch": None}
                        try:
                            r = self._git(tree, "symbolic-ref", "--short", "-q", "HEAD", check=False)
                            entry["branch"] = r.stdout.strip() or None
                        except Refused as e:
                            entry["stopped"] = str(e)
                        out.append(entry)
                        if len(out) >= 200:
                            return {"trees": out, "cut": True}
            finally:
                os.close(bfd)
        return {"trees": out}

    # Git in the tree

    def _git_ok(self, tree: Path) -> Path:
        gd = tree / ".git"
        try:
            st = os.lstat(gd)
        except FileNotFoundError:
            raise Refused("this tree has no repository") from None
        if not stat.S_ISDIR(st.st_mode):
            raise Refused(".git is not a folder here; this tree is stopped until it is cleaned up")
        for bad in ("commondir", "objects/info/alternates", "objects/info/http-alternates"):
            if os.path.lexists(gd / bad):
                raise Refused(f".git/{bad} points this tree at another repository; it is stopped until cleaned up")
        cfg = gd / "config"
        if os.path.lexists(cfg):
            cst = os.lstat(cfg)
            if not stat.S_ISREG(cst.st_mode):
                raise Refused(".git/config is not a plain file; this tree is stopped until it is cleaned up")
            r = subprocess.run(["git", "config", "--file", str(cfg), "--list", "--name-only"],
                               capture_output=True, text=True, env=_git_env(tree))
            if r.returncode != 0:
                raise Refused(".git/config cannot be read as git config; this tree is stopped")
            allowed = self.cfg["git_config_keys"]
            for key in r.stdout.split():
                if not any(fnmatch.fnmatchcase(key.lower(), a.lower()) for a in allowed):
                    raise Refused(f".git/config holds {key}, which no work tree may hold; this tree is stopped until it is cleaned up")
        return gd

    def _git(self, tree: Path, *args: str, check: bool = True, allow_file: bool = False,
             identity: tuple[str, str] | None = None) -> subprocess.CompletedProcess:
        gd = self._git_ok(tree)
        safe = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "credential.helper=",
                "-c", "submodule.recurse=false", "-c", f"protocol.file.allow={'always' if allow_file else 'never'}",
                "-c", "core.pager=cat", "-c", "core.editor=false", "-c", "core.symlinks=true",
                "-c", "gc.auto=0", "-c", "maintenance.auto=false", "-c", "commit.gpgsign=false",
                "-c", "core.untrackedCache=false", "-c", "transfer.fsckObjects=true"]
        if identity:
            safe += ["-c", f"user.name={identity[0]}", "-c", f"user.email={identity[1]}"]
        r = subprocess.run(["git", f"--git-dir={gd}", f"--work-tree={tree}", *safe, *args],
                           capture_output=True, text=True, env=_git_env(tree), cwd=tree)
        if check and r.returncode != 0:
            raise Refused(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[-400:]}")
        return r

    def checkout(self, caller: str, args: dict) -> dict:
        """A new tree for a task, from the base bundle tw-git put in spool/in."""
        bot, repo, tree = self._tree(args, must_exist=False)
        branch = str(args.get("branch") or "")
        if not self.policy.own_branch(bot, branch):
            raise Refused(f"{branch} is not {self.policy.bot_name(bot)}'s; a Bot branch is <bot>/<issue>-<what>")
        base = self.policy.data["merge"]["base"][repo]
        bundle = self._spool(self.spool_in, bot, str(args.get("bundle") or ""))
        with self._lock(bot):
            if os.path.lexists(tree):
                raise Refused(f"the tree for task {args.get('task')} already exists")
            finished = self._remove_finished(bot, repo)
            # The Bot's own folder is the install's (its group and setgid bit
            # are what keep the tree the Bot's); the broker never makes it.
            if not os.path.lexists(self.work / bot):
                raise Refused(f"{self.work / bot} is missing; the install makes each Bot's folder")
            os.close(self._dir_fd(self.work, [bot]))
            os.close(self._dir_fd(self.work / bot, [tree.parent.name, tree.name], create=True))
            r = subprocess.run(["git", "init", "-q", "-b", base, str(tree)], capture_output=True, text=True, env=_git_env(tree))
            if r.returncode != 0:
                raise Refused(f"git init failed: {r.stderr.strip()[-300:]}")
            self._git(tree, "fetch", "-q", str(bundle), f"+refs/heads/{base}:refs/remotes/origin/{base}", allow_file=True)
            self._git(tree, "checkout", "-q", "-b", branch, f"refs/remotes/origin/{base}")
            head = self._git(tree, "rev-parse", "HEAD").stdout.strip()
        out = {"tree": f"{repo} {tree.name}", "branch": branch, "base": base, "head": head}
        if finished:
            out["removed"] = finished
        return out

    def _remove_finished(self, bot: str, repo: str) -> list[str]:
        """The Bot's trees in repo whose branch went out (tw-files bundled it
        for tw-git) and that GitHub no longer has, by tw-git's list written
        after that bundle: the pull request merged and GitHub deleted the
        branch, or someone deleted it. Until 11 Nov 2026 nothing removed a
        tree, and 72 of them (21 GB) took the VM's disk under the fleet
        queue's floor. Called with the Bot's lock held."""
        listed = self.spool_in / bot / f"branches-{repo.split('/')[1]}.json"
        try:
            st = os.lstat(listed)
            if not stat.S_ISREG(st.st_mode):
                return []
            on_github = set(json.loads(listed.read_text()).get("branches") or [])
        except (OSError, ValueError):
            return []
        folder = self.work / bot / self._repo_dir(bot, repo)
        try:
            names = sorted(e.name for e in os.scandir(folder)
                           if TASK.fullmatch(e.name) and stat.S_ISDIR(e.stat(follow_symlinks=False).st_mode))
        except OSError:
            return []
        removed = []
        for name in names:
            sent = self._sent(bot, name)
            if not sent or max(os.lstat(p).st_mtime for p in sent) >= st.st_mtime:
                continue
            tree = folder / name
            try:
                branch = self._git(tree, "symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip()
            except Refused:
                continue
            if not branch or not self.policy.own_branch(bot, branch) or branch in on_github:
                continue
            try:
                shutil.rmtree(tree)
                for p in sent:
                    os.unlink(p)
            except OSError as e:
                removed.append(f"{name} (not all of it: {e.strerror} on {Path(e.filename or '').name})")
                continue
            removed.append(name)
        return removed

    def _sent(self, bot: str, task: str) -> list[Path]:
        """The bundles tw-files made of this tree for tw-git to push."""
        d = self.spool_out / bot
        pattern = re.compile(re.escape(task) + r"-[0-9a-f]{12}\.bundle")
        try:
            return [d / e.name for e in os.scandir(d) if pattern.fullmatch(e.name) and e.is_file(follow_symlinks=False)]
        except OSError:
            return []

    def status(self, caller: str, args: dict) -> dict:
        bot, _, tree = self._tree(args)
        with self._lock(bot):
            out = self._git(tree, "status", "--porcelain=v1", "--branch", "--untracked-files=all").stdout
        return {"status": out[:OUT_CAP]}

    def diff(self, caller: str, args: dict) -> dict:
        bot, _, tree = self._tree(args)
        with self._lock(bot):
            r = self._git(tree, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "HEAD")
        return {"diff": r.stdout[:OUT_CAP], "cut": len(r.stdout) > OUT_CAP}

    def search(self, caller: str, args: dict) -> dict:
        bot, _, tree = self._tree(args)
        pattern = str(args.get("pattern") or "")
        if not pattern or len(pattern) > 500:
            raise Refused("search takes a pattern of up to 500 characters")
        with self._lock(bot):
            r = self._git(tree, "grep", "-n", "-I", "--no-color", "--untracked", "-e", pattern, "--", check=False)
        if r.returncode not in (0, 1):
            raise Refused(f"search failed: {r.stderr.strip()[-300:]}")
        return {"matches": r.stdout[:OUT_CAP], "cut": len(r.stdout) > OUT_CAP}

    def commit(self, caller: str, args: dict) -> dict:
        bot, repo, tree = self._tree(args)
        message = str(args.get("message") or "").strip()
        if not message or len(message) > 5000:
            raise Refused("a commit has a message of up to 5,000 characters")
        identity = (f"{self.policy.bot_name(bot)} (Thornswood Bot)", self.cfg["commit_email"].format(bot=bot))
        with self._lock(bot):
            branch = self._git(tree, "symbolic-ref", "--short", "HEAD").stdout.strip()
            if not self.policy.own_branch(bot, branch):
                raise Refused(f"the tree is on {branch}, not one of {self.policy.bot_name(bot)}'s branches")
            self._git(tree, "add", "-A")
            names = self._git(tree, "diff", "--cached", "--name-only").stdout.split("\n")
            names = [n for n in names if n]
            if not names:
                raise Refused("nothing to commit")
            if len(names) > self.cfg["max_changed_files"]:
                raise Refused(f"{len(names)} files changed; a task changes at most {self.cfg['max_changed_files']}")
            self._git(tree, "commit", "-q", "--no-verify", "-m", message, identity=identity)
            head = self._git(tree, "rev-parse", "HEAD").stdout.strip()
        return {"commit": head, "files": len(names), "branch": branch}

    def bundle(self, caller: str, args: dict) -> dict:
        """The Bot's branch as a bundle in spool/out, for tw-git to push."""
        bot, repo, tree = self._tree(args)
        base = self.policy.data["merge"]["base"][repo]
        with self._lock(bot):
            branch = self._git(tree, "symbolic-ref", "--short", "HEAD").stdout.strip()
            if not self.policy.own_branch(bot, branch):
                raise Refused(f"the tree is on {branch}, not one of {self.policy.bot_name(bot)}'s branches")
            head = self._git(tree, "rev-parse", "HEAD").stdout.strip()
            out = self.spool_out / bot
            out.mkdir(parents=True, exist_ok=True)
            name = f"{tree.name}-{head[:12]}.bundle"
            self._git(tree, "bundle", "create", "-q", str(out / name), f"refs/heads/{branch}",
                      f"^refs/remotes/origin/{base}")
        return {"bundle": name, "branch": branch, "head": head}

    def remove(self, caller: str, args: dict) -> dict:
        """The tree goes once its pull request merged or closed."""
        bot, repo, tree = self._tree(args)
        with self._lock(bot):
            shutil.rmtree(tree)
            for p in self._sent(bot, tree.name):
                os.unlink(p)
        return {"removed": f"{repo} {tree.name}"}

    def _spool(self, root: Path, bot: str, name: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}\.bundle", name):
            raise Refused("name the bundle by its file name")
        d = (root / bot).resolve()
        p = (d / name)
        if not p.is_file() or p.is_symlink() or p.resolve().parent != d:
            raise Refused(f"{name} is not a bundle in {bot}'s spool")
        return p

    # Checks (Tier B)

    def check_command(self, cmd) -> list[str]:
        if not isinstance(cmd, list) or not all(isinstance(c, str) for c in cmd):
            raise Refused("a check is a list of words")
        for form in self.cfg["check_commands"]:
            if len(form) != len(cmd):
                continue
            ok = True
            for want, got in zip(form, cmd):
                if "{name}" in want:
                    pre, post = want.split("{name}")
                    mid = got[len(pre):len(got) - len(post)] if got.startswith(pre) and got.endswith(post) else ""
                    ok = ok and bool(NAME.fullmatch(mid)) and len(got) == len(pre) + len(mid) + len(post)
                else:
                    ok = ok and want == got
            if ok:
                return cmd
        raise Refused("a check runs one harness in the forms CLAUDE.md names, Check-Words.ps1, or one of the two "
                      "builders (node scripts/dev/Build-Gamemode.js, python3 scripts/mods/Add-ContentRecords.py); "
                      "nothing else")

    def run_check(self, caller: str, args: dict) -> dict:
        bot, repo, tree = self._tree(args)
        if bot not in self.policy.data["brokers"]["exec_users"]:
            raise Refused("Bramble runs no checks; it acts only through the brokers")
        cmd = self.check_command(args.get("command"))
        self._git_ok(tree)
        ticket = self.ask("admit", {"bot": bot, "kind": "sandbox", "task": f"check {repo} {tree.name}"})
        if ticket["state"] != "admitted":
            self.ask("finish", {"bot": bot, "ticket": ticket["ticket"]})
            raise Refused(f"the check waits: {ticket['reason']}")
        try:
            with self._lock(bot):
                r = self._start_check(bot, tree, cmd, self.cfg["check_minutes"] * 60 + 30)
        finally:
            self.ask("finish", {"bot": bot, "ticket": ticket["ticket"]})
        lines = [l for l in (r.stdout or "").splitlines() if re.match(r"^\s*(PASS|FAIL|SKIP)\b", l)]
        return {"exit": r.returncode, "lines": lines[:2000], "tail": (r.stdout or "")[-4000:],
                "stderr": (r.stderr or "")[-2000:]}

    def _start_check(self, bot: str, tree: Path, cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
        if self.check_helper is not None:
            return self.runner([*self.check_helper, bot, str(tree), *cmd], timeout)
        r = call(self.check_socket, "run", {"bot": bot, "tree": str(tree), "command": cmd}, timeout=timeout + 30)
        return subprocess.CompletedProcess(cmd, int(r["exit"]), r.get("stdout") or "", r.get("stderr") or "")

    def ops(self) -> dict:
        gw = self.policy.data["brokers"]["users"]["gateway"]
        names = ("trees", "read", "write", "edit", "delete", "list", "checkout", "status", "diff", "search", "commit",
                 "bundle", "remove", "run_check")
        return {n: (getattr(self, n), {gw}) for n in names}


def check_argv(policy: Policy, bot: str, tree: str, cmd: list[str]) -> list[str]:
    """The systemd-run line the root helper starts a check with (08 section 3).
    The helper calls this itself after checking bot, tree and command again."""
    b = policy.data["brokers"]
    user = b["exec_users"][bot]
    group = b["files"]["work_group_prefix"] + bot
    lim = b["files"]["check_limits"]
    state = b["state"]
    hidden = " ".join([state["policy_db"].rsplit("/", 1)[0], state["git_mirrors"], state["spool"], state["backups"],
                       state["snapshots"], "/etc/thornswood", "/run/tw"])
    props = {
        "MemoryMax": lim["MemoryMax"], "MemorySwapMax": "0", "CPUQuota": lim["CPUQuota"], "TasksMax": str(lim["TasksMax"]),
        "RuntimeMaxSec": str(b["files"]["check_minutes"] * 60),
        "ProtectSystem": "strict", "ProtectHome": "yes", "PrivateTmp": "yes", "PrivateDevices": "yes",
        "NoNewPrivileges": "yes", "PrivateNetwork": "yes", "ProtectProc": "invisible", "ProcSubset": "pid",
        "CapabilityBoundingSet": "", "RestrictSUIDSGID": "yes", "LockPersonality": "yes",
        "ProtectKernelTunables": "yes", "ProtectKernelModules": "yes", "ProtectControlGroups": "yes",
        "RestrictNamespaces": "yes", "UMask": "0007",
        # Only this tree is visible under /srv/tw/work; the other Bots' trees
        # and every broker's state are not there at all.
        "TemporaryFileSystem": f"{state['work']}:ro",
        "BindPaths": tree,
        "ReadWritePaths": tree,
        "InaccessiblePaths": hidden,
    }
    argv = ["systemd-run", "--quiet", "--wait", "--pipe", "--collect", "--service-type=exec",
            f"--uid={user}", f"--gid={group}", "--slice=tw-sandbox.slice", f"--working-directory={tree}",
            "--setenv=HOME=/tmp", "--setenv=PATH=/usr/local/bin:/usr/bin:/bin"]
    for k, v in props.items():
        argv += ["-p", f"{k}={v}"]
    return argv + ["--", *cmd]


def _git_env(tree: Path) -> dict:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tree), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_ATTR_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C.UTF-8"}


def _run(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(argv, 124, (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""),
                                           "the check ran past its time and was stopped")
