"""The VM follows main by itself (Patrick's answer A on q-retire-hermes-one,
31 Oct, #2862): no other machine sends it a commit.

Runs as root from tw-follow-main.timer. Each run:

  1. fetches main of TWHermesAgents over ssh with the VM's own read-only
     deploy key (/etc/thornswood/hermes-read-key, root's alone; GitHub lets a
     deploy key read one repository and, without "Allow write access", never
     write), against GitHub's published host keys only;
  2. stops there when main is the commit already installed, or is a commit
     that already failed here (a failure is said once and fixed by a new
     merge, never tried again in the hope it passes);
  3. puts main's commit in a root-owned release folder, as
     install-on-oracle.sh did;
  4. runs the whole test suite at that commit, as the tw-ci user, with no
     network, and installs nothing if one test fails;
  5. runs that release's deploy/install.sh and, once the decision core is
     installed here (its /etc/thornswood/tw-decide.env is there), that
     release's deploy/decisions/install.sh too, so the core's process, venv,
     units and store move with the code under /opt/thornswood-hermes/current
     (1 Nov: without it the feeds and the connector ran the new release
     while the core kept the old one); then apply.py as hermes (its own
     checks first, as send-to-oracle.sh did), reloads every Bot process with
     tw-reload-bots (the hermes user's login sessions end and the gateway
     restarts, since a gateway restart alone left Desktop's turns on the old
     tool list on 31 Oct) and runs the canary;
  6. if any of 5 fails, installs and applies the release that was running
     before, the same way, and says both. When the failed release migrated
     the core's store past what the earlier release knows, the core's units
     stop and the failed release's own `tw-decide downgrade` puts the store
     back first, as tw-decide; when it refuses (the store holds what the
     older schema cannot), nothing is put back and the core starts again on
     the newer release. Units of the core that the earlier release does not
     ship are stopped and disabled before it goes back in.

What it did is in /var/lib/thornswood/follow-main.json and the journal, and
everything each step printed, whether it passed or not, is in
/var/lib/thornswood/follow-main/<commit>.log (root's alone; the newest
LOGS_KEPT stay). 2 Nov, #68: the install passed, the follower kept only its
summary line, and the card asking for three of the install's PASS lines
could not be answered from the VM.

Each result also goes to GitHub (ThornswoodRP/Thornswood#5564: a failed
install went unseen from 4 Nov to 8 Nov, because the file above is root's
alone). The follower hands it to tw-git, the VM's one GitHub writer, as
vm_install (vm_report.py), which writes it on the ops repository's ledger
issue and keeps a board issue open while the VM is not installing main. A
result is held in follow-main.json's report_pending until tw-git has taken
it, and goes first at the next run when tw-git did not answer: the result
is a message that has to arrive, so it waits in an outbox the way a letter
does, and tw-git finds each write before it makes it, so a result sent again
after a lost answer is written once. Only a new result is sent: a commit
that already failed, or a fetch that fails the same way again, sends nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Callable

from thornswood_guard.policy import Policy

from . import wire

Run = Callable[..., subprocess.CompletedProcess]
LOGS_KEPT = 20
CONNECTOR = "tw-decide-connector.service"


def _text(b) -> str:
    return b.decode("utf-8", "replace") if isinstance(b, (bytes, bytearray)) else (b or "")


class Stop(Exception):
    """A step failed: its sentence, and the step's name."""

    def __init__(self, step: str, why: str):
        super().__init__(why)
        self.step = step

    @property
    def reason(self) -> str:
        """The sentence without the output a command printed (everything after
        its first ": "), which is what may leave the VM."""
        return str(self).split(": ", 1)[0]


def packages_in(release: Path) -> Path:
    """Where a release keeps its Python packages, as its own deploy/layout.json says,
    so this copy reads another release's code wherever that release keeps it. A
    release made before the file existed keeps them at its root."""
    try:
        packages = json.loads((release / "deploy" / "layout.json").read_text(encoding="utf-8"))["packages"]
    except FileNotFoundError:
        return release
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise Stop("put back", f"{release.name[:12]}'s deploy/layout.json does not say where its packages are ({exc})")
    rel = PurePosixPath(packages) if isinstance(packages, str) else None
    if rel is None or rel.is_absolute() or ".." in rel.parts:
        raise Stop("put back", f"{release.name[:12]}'s deploy/layout.json names {packages!r}, not a folder inside it")
    return release / rel


class Follower:
    def __init__(self, policy: Policy, *, run: Run = subprocess.run, root: Path = Path("/"),
                 say: Callable[[str], None] = print, report: Callable[[dict], object] | None = None):
        self.p = policy
        # tw-git's vm_install, or a test's stand-in.
        self.report_ = report or (lambda args: wire.call(policy.data["brokers"]["sockets"]["git"]["path"],
                                                         "vm_install", args))
        self.r = policy.data["follow_main"]
        self.run_ = run
        self.root = root
        self.say = say
        self.opt = self._at(policy.data["brokers"]["install_root"])
        self.mirror = self._at(self.r["mirror"])
        self.state_file = self._at(self.r["state_file"])
        self.hermes_releases = self._at(self.r["hermes_releases"])
        self.logs = self.state_file.parent / "follow-main"
        self.log: Path | None = None

    def _at(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def sh(self, step: str, args: list[str], **kw) -> subprocess.CompletedProcess:
        if isinstance(kw.get("input"), bytes):
            c = self.run_(args, capture_output=True, **kw)
            c.stdout, c.stderr = (_text(c.stdout), _text(c.stderr))
        else:
            c = self.run_(args, capture_output=True, text=True, **kw)
        self.keep(step, args, c)
        if c.returncode != 0:
            tail = ((c.stdout or "") + (c.stderr or "")).strip()[-600:]
            raise Stop(step, f"{' '.join(args[:3])} ... exited {c.returncode}: {tail}")
        return c

    # -- the run's whole output
    def start_log(self, commit: str) -> None:
        """A log of this run's steps, at <state dir>/follow-main/<commit>.log;
        the newest LOGS_KEPT stay."""
        self.logs.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.logs, 0o700)
        self.log = self.logs / f"{commit}.log"
        fd = os.open(self.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(f"=== {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} follow-main run for {commit}\n")
        old = sorted(self.logs.glob("*.log"), key=lambda q: q.stat().st_mtime, reverse=True)[LOGS_KEPT:]
        for q in old:
            q.unlink(missing_ok=True)

    def keep(self, step: str, args: list[str], c: subprocess.CompletedProcess) -> None:
        if self.log is None:
            return
        out, err = _text(c.stdout), _text(c.stderr)
        with open(self.log, "a", encoding="utf-8") as f:
            f.write(f"--- {step}: {' '.join(str(a) for a in args[:4])} (exit {c.returncode})\n")
            if out:
                f.write(out if out.endswith("\n") else out + "\n")
            if err:
                f.write(err if err.endswith("\n") else err + "\n")

    # -- state
    def state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {}

    def record(self, **fields) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        data = {**self.state(), **fields, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, self.state_file)

    # -- the result, on GitHub
    def report(self, result: str, *, commit: str | None, step: str | None = None, reason: str | None = None,
               put_back: str | None = None, before: str | None = None) -> None:
        """Hands one new result to tw-git, or holds it for the next run."""
        self.record(report_pending={
            "result": result, "commit": commit, "step": step, "reason": reason, "put_back": put_back,
            "installed": self.installed(), "before": before,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        self.send_report()

    def send_report(self) -> None:
        pending = self.state().get("report_pending")
        if not pending:
            return
        try:
            self.report_(pending)
        except (wire.Refused, OSError, ValueError) as e:
            self.say(f"REPORT NOT SENT: tw-git did not take the {pending['result']} result of "
                     f"{(pending.get('commit') or 'main')[:12]} ({e}); it is held and goes first at the next run")
            return
        self.record(report_pending=None, reported=pending)
        self.say(f"reported: {pending['result']} for {(pending.get('commit') or 'main')[:12]} is on GitHub")

    def installed(self) -> str | None:
        cur = self.opt / "current"
        if not cur.is_symlink():
            return None
        name = Path(os.readlink(cur)).name
        return name if len(name) == 40 else None

    # -- steps
    def git(self, *args: str) -> list[str]:
        return ["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=always",
                "-c", "safe.directory=*", "-c", "transfer.fsckObjects=true", *args]

    def fetch(self) -> str:
        if not self.mirror.exists():
            self.mirror.parent.mkdir(parents=True, exist_ok=True)
            self.sh("fetch", self.git("init", "-q", "--bare", str(self.mirror)))
        ssh = (f"ssh -i {self._at(self.r['read_key'])} -o IdentitiesOnly=yes -o BatchMode=yes "
               f"-o StrictHostKeyChecking=yes -o UserKnownHostsFile={self._at(self.r['known_hosts'])}")
        try:
            self.sh("fetch", self.git("-C", str(self.mirror), "fetch", "-q", self.r["url"], "+refs/heads/main:refs/heads/main"),
                    env={**os.environ, "GIT_SSH_COMMAND": ssh, "GIT_TERMINAL_PROMPT": "0"})
        except Stop as e:
            if "Permission denied" in str(e) or "publickey" in str(e):
                pub = self._at(self.r["read_key"] + ".pub")
                raise Stop("fetch", "GitHub refused the VM's read key. Add it to TWHermesAgents as a deploy key, "
                                    "with Allow write access left off: " + (pub.read_text().strip() if pub.exists() else str(pub)))
            raise
        return self.sh("fetch", self.git("-C", str(self.mirror), "rev-parse", "refs/heads/main")).stdout.strip()

    def release(self, commit: str) -> Path:
        dest = self.opt / "releases" / commit
        if dest.exists() and dest.stat().st_uid != 0 and self.root == Path("/"):
            raise Stop("release", f"{dest} exists and is not root's; look at it before anything runs from it")
        if not (dest / ".git").exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            self.sh("release", self.git("init", "-q", str(dest)))
        self.sh("release", self.git("-C", str(dest), "fetch", "-q", str(self.mirror), f"+{commit}:refs/remotes/origin/main"))
        self.sh("release", self.git("-C", str(dest), "-c", "advice.detachedHead=false", "checkout", "-q", "--force", "--detach", commit))
        self.sh("release", self.git("-C", str(dest), "clean", "-qfdx"))
        self.sh("release", ["chown", "-R", "root:root", str(dest)])
        self.sh("release", ["chmod", "-R", "go-w", str(dest)])
        return dest

    def tests(self, dest: Path, commit: str) -> str:
        # Not under /tmp or /var/tmp: PrivateTmp gives the run its own of both.
        base = self._at(self.r["ci_dir"])
        base.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="run-", dir=str(base)))
        try:
            tree = work / "tree"
            self.sh("tests", self.git("clone", "-q", "--no-hardlinks", str(dest), str(tree)))
            self.sh("tests", ["chown", "-R", f"{self.r['ci_user']}:{self.r['ci_user']}", str(work)])
            c = self.run_(["systemd-run", "--quiet", "--wait", "--pipe", "--collect",
                           "-p", f"User={self.r['ci_user']}", "-p", "PrivateNetwork=yes", "-p", "PrivateTmp=yes",
                           "-p", "NoNewPrivileges=yes", "-p", f"WorkingDirectory={tree}",
                           "-p", f"Environment=HOME={work} PYTHONDONTWRITEBYTECODE=1",
                           "-p", f"RuntimeMaxSec={self.r['tests_minutes'] * 60}",
                           "python3", "-m", "unittest", "discover", "-s", "tests"],
                          capture_output=True, text=True)
            self.keep("tests", ["python3", "-m", "unittest", "discover"], c)
            out = (c.stdout or "") + (c.stderr or "")
            ran = [line for line in out.splitlines() if line.startswith("Ran ")]
            if c.returncode != 0:
                raise Stop("tests", f"the suite fails at {commit[:12]} on the VM ({ran[-1] if ran else 'no count'}): {out.strip()[-800:]}")
            return ran[-1] if ran else "Ran ? tests"
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def install(self, dest: Path, commit: str) -> None:
        self.sh("install", ["bash", str(dest / "deploy" / "install.sh"), commit])
        # deploy/install.sh has just pointed current at this release, which the
        # decision core's install checks before it does anything.
        if not self._at(self.r["decide_env"]).exists():
            return  # the decision core is not installed on this VM
        core = dest / "deploy" / "decisions" / "install.sh"
        if not core.exists():
            raise Stop("install", f"the decision core is installed here ({self.r['decide_env']}), "
                                  f"but {commit[:12]} has no deploy/decisions/install.sh to keep it at this release")
        # A release whose core install can leave the connector stopped gets it stopped until every Bot
        # runs this release (start_connector, after the reload and the canary): a message handed to a Bot
        # still on the release before would meet its old tools. 2 Nov 2026: Patrick's answers that waited
        # on tw_git's open_issue are put back in the queue by a migration, and the core's install runs
        # that migration before apply.py and tw-reload-bots give the Bots the op.
        self.connector_later = "--connector-later" in core.read_text(encoding="utf-8", errors="replace")
        self.sh("install", ["bash", str(core), commit] + (["--connector-later"] if self.connector_later else []))

    def start_connector(self) -> None:
        """The connector the core's install left stopped, started now every Bot runs this release."""
        if not getattr(self, "connector_later", False):
            return
        self.connector_later = False
        self.sh("connector", ["systemctl", "reset-failed", CONNECTOR])
        self.sh("connector", ["systemctl", "start", CONNECTOR])
        self.sh("connector", ["systemctl", "is-active", "--quiet", CONNECTOR])

    def apply(self, dest: Path, commit: str) -> None:
        # hermes never runs git against the root-owned release: git refuses a
        # repository another user owns ("dubious ownership"), and a -c
        # safe.directory does not reach the upload-pack a local fetch starts
        # (1 Nov, f02c3193). Root bundles the commit and hands the bytes over
        # on stdin, and hermes writes them into a file of its own, under its own
        # folder: no folder of root's has to be open to hermes, and root never
        # writes where hermes could plant a link. (1 Nov, 5d92dda8: a bundle
        # left in /srv/tw/follow, which is 0700, could not be read.)
        user = self.r["hermes_user"]
        hdest = self.hermes_releases / commit
        # A bundle carries refs, not bare commits: name this one first, in the
        # release root owns, so it is this commit and nothing else.
        self.sh("apply", self.git("-C", str(dest), "update-ref", "refs/remotes/origin/main", commit))
        made = self.run_(self.git("-C", str(dest), "bundle", "create", "--quiet", "-", "refs/remotes/origin/main"),
                         capture_output=True)
        if made.returncode != 0 or not made.stdout:
            raise Stop("apply", f"git bundle create in {dest} exited {made.returncode}: {_text(made.stderr).strip()[-300:]}")
        bundle = made.stdout if isinstance(made.stdout, bytes) else made.stdout.encode()
        script = (
            "set -euo pipefail\n"
            "umask 077\n"
            f"dest='{hdest}'\n"
            f"mkdir -p '{self.hermes_releases}'\n"
            f"bundle=\"$(mktemp '{self.hermes_releases}/.incoming-XXXXXX.bundle')\"\n"
            "trap 'rm -f \"$bundle\"' EXIT\n"
            "cat > \"$bundle\"\n"
            "[[ -d \"$dest/.git\" ]] || git init --quiet \"$dest\"\n"
            "git -C \"$dest\" fetch --quiet \"$bundle\" '+refs/remotes/origin/main:refs/remotes/origin/main'\n"
            f"git -C \"$dest\" -c advice.detachedHead=false checkout --quiet --force --detach '{commit}'\n"
            "cd \"$dest\"\n"
            f"python3 scripts/apply.py --expect '{commit}' --check\n"
            f"python3 scripts/apply.py --expect '{commit}'\n"
            f"ln -sfn \"$dest\" '{self.hermes_releases.parent / 'current'}'\n"
        )
        self.sh("apply", ["runuser", "-u", user, "--", "bash", "-c", script], input=bundle)

    def restart(self) -> None:
        # The installed release's own tw-reload-bots (src/thornswood_brokers/reload.py),
        # the step every apply ends with: it ends the hermes user's login
        # sessions, restarts the gateway and says STOP unless it is active.
        self.sh("reload", [str(self.opt / "bin" / "tw-reload-bots")])

    def store_schema(self) -> int | None:
        """The schema number the core's store is at, read only; None with no core."""
        if not self._at(self.r["decide_env"]).exists():
            return None
        db = self._at(self.r["decide_db"])
        if not db.exists():
            return None
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        finally:
            con.close()
        return int(row[0]) if row else 0

    def known_schema(self, dest: Path) -> int:
        """How many store migrations a release's own code knows."""
        c = self.sh("put back", ["python3", "-I", "-c",
                                 "import sys; sys.path.insert(0, sys.argv[1]); "
                                 "from thornswood_decisions.schema import MIGRATIONS; print(len(MIGRATIONS))",
                                 str(packages_in(dest))])
        try:
            return int(c.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise Stop("put back", f"could not read how many store migrations {dest.name[:12]} knows: {c.stdout.strip()[-200:]}")

    def core_units(self) -> list[str]:
        """The core's units installed on this VM, by their unit files. The phone
        test's own (tw-decide-test, tw-decide-push-test) keep their own store
        and are left alone."""
        d = self._at("/etc/systemd/system")
        return sorted(p.name for p in d.glob("tw-decide*")
                      if p.suffix in (".service", ".timer") and p.is_file() and "-test." not in p.name)

    def stop_core(self) -> list[str]:
        """Stops every unit of the core and says which were running. A
        connection made while the core runs migrates the store forward again,
        so nothing of it may run while the store is put back."""
        units = self.core_units()
        if not units:
            return []
        c = self.run_(["systemctl", "is-active", *units], capture_output=True, text=True)
        states = (c.stdout or "").split()
        active = [u for u, st in zip(units, states) if st == "active"]
        # Timers first, so none starts a service again while the rest stop.
        self.sh("put back", ["systemctl", "stop", *[u for u in units if u.endswith(".timer")],
                             *[u for u in units if not u.endswith(".timer")]])
        return active

    def downgrade(self, failed: Path, to: int) -> None:
        """The failed release's own code puts the store back to schema `to`
        (thornswood_decisions.cli downgrade): only it knows the way back, and it
        changes nothing and exits 1 when that would lose what the store holds.
        As tw-decide, whose store it is, with the core's own venv."""
        self.sh("put back", ["runuser", "-u", self.r["decide_user"], "--", str(self._at(self.r["decide_python"])), "-I", "-c",
                             "import sys; sys.path.insert(0, sys.argv[1]); "
                             "from thornswood_decisions.cli import main; sys.exit(main(sys.argv[2:]))",
                             str(packages_in(failed)), "--db", str(self._at(self.r["decide_db"])), "downgrade", "--to", str(to)])

    def drop_units_not_in(self, dest: Path) -> list[str]:
        """Stops and disables the core's units that the release going back in
        does not ship (1 Nov: tw-decide-own from #33 would stay running on code
        the older release does not have). Its install puts back the rest."""
        ships = {p.name for p in (dest / "deploy" / "decisions").glob("tw-decide*")}
        extra = [u for u in self.core_units() if u not in ships]
        if extra:
            self.sh("put back", ["systemctl", "disable", "--quiet", "--now", *extra])
        return extra

    def canary(self) -> None:
        self.sh("canary", [str(self.opt / "bin" / "tw-canary")])

    # -- the run
    def follow(self) -> int:
        pause = self._at(self.r["pause_file"])
        if pause.exists():
            # An install starts the timer again, so a pause is this file (1 Nov:
            # a hand install turned the stopped timer back on).
            self.say(f"paused: {self.r['pause_file']} is there, so nothing is fetched or installed")
            return 0
        # A result tw-git did not take last time goes before anything new.
        self.send_report()
        st = self.state()
        try:
            tip = self.fetch()
        except Stop as e:
            self.say(f"STOP {e}")
            self.record(result="fetch failed", why=str(e))
            if not (st.get("result") == "fetch failed" and st.get("why") == str(e)):
                self.report("fetch failed", commit=None, step="fetch", reason=e.reason)
            return 1
        if st.get("result") == "fetch failed":
            self.record(result="fetched", why=None)
            self.report("fetched", commit=tip)
        before = self.installed()
        if tip == before:
            return 0
        st = self.state()
        if st.get("failed") == tip:
            # Said once, when it failed; a new merge on main is the fix.
            return 1
        self.start_log(tip)
        self.say(f"main is at {tip}; installed is {before or 'nothing'}; every step's output goes to {self.log}")
        try:
            dest = self.release(tip)
            ran = self.tests(dest, tip)
            self.say(f"PASS {ran} at {tip[:12]} on the VM")
        except Stop as e:
            self.say(f"STOP {tip[:12]} is not installed: {e}")
            self.record(result="not installed", failed=tip, step=e.step, why=str(e), installed=before, put_back=None,
                        log=str(self.log))
            self.report("not installed", commit=tip, step=e.step, reason=e.reason, before=before)
            return 1
        try:
            self.install(dest, tip)
            self.apply(dest, tip)
            self.restart()
            self.canary()
            self.start_connector()
        except Stop as e:
            self.say(f"STOP {tip[:12]} failed at {e.step}: {e}")
            back = self.put_back(before, dest)
            self.record(result="failed", failed=tip, step=e.step, why=str(e), installed=self.installed(), put_back=back,
                        log=str(self.log))
            self.report("failed", commit=tip, step=e.step, reason=e.reason, put_back=back.split(": ", 1)[0], before=before)
            return 1
        self.say(f"PASS {tip[:12]} is installed, applied, every Bot process reloaded and the canary passed")
        self.record(result="installed", installed=tip, failed=None, step=None, why=None, put_back=None,
                    log=str(self.log))
        self.report("installed", commit=tip, before=before)
        return 0

    def put_back(self, before: str | None, failed: Path) -> str:
        if not before:
            return "nothing was installed before, so nothing was put back"
        dest = self.opt / "releases" / before
        try:
            have = self.store_schema()
            if have is not None:
                knows = self.known_schema(dest)
                if have > knows:
                    # The older code refuses a newer store (1 Nov: 5d92dda8's
                    # schema 6, and 507dfdbf's put-back stopped at the core's
                    # install, half done), so the store goes back first, by the
                    # failed release's own downgrade, or nothing is put back.
                    refused = self.store_back(failed, have, knows)
                    if refused:
                        msg = (f"not put back: the core's store is at schema {have}, {before[:12]} knows only {knows}, "
                               f"and {failed.name[:12]}'s downgrade refused: {refused}. The newer release stays installed; "
                               f"fix it forward with a merge")
                        self.say(f"FAIL {msg}")
                        return msg
                dropped = self.drop_units_not_in(dest)
                if dropped:
                    self.say(f"stopped and disabled {', '.join(dropped)}, which {before[:12]} does not ship")
            self.install(dest, before)
            self.apply(dest, before)
            self.restart()
            self.canary()
            self.start_connector()
        except Stop as e:
            self.say(f"FAIL putting back {before[:12]} failed too, at {e.step}: {e}")
            return f"putting back {before} failed at {e.step}: {e}"
        self.say(f"PASS put back {before[:12]}, which runs as it did")
        return f"put back {before}"

    def store_back(self, failed: Path, have: int, knows: int) -> str | None:
        """Stops the core, has the failed release put its store back to schema
        `knows`, and says why not when it refuses (the core then starts again as
        it ran, on the newer release)."""
        active = self.stop_core()
        try:
            self.downgrade(failed, knows)
        except Stop as e:
            if active:
                try:
                    self.sh("put back", ["systemctl", "start", *active])
                except Stop as again:
                    return f"{e}; and starting {', '.join(active)} again failed: {again}"
            return str(e)
        self.say(f"PASS the core's store is back at schema {knows} from {have}, by {failed.name[:12]}'s downgrade")
        return None


def main(argv: list[str]) -> int:
    if os.geteuid() != 0:
        print("tw-follow-main runs as root from tw-follow-main.timer")
        return 2
    return Follower(Policy.load()).follow()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
