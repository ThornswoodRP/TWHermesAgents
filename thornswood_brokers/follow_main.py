"""The VM follows main by itself (Patrick's answer A on q-retire-hermes-one,
31 Oct, #2862): no other machine sends it a commit.

Runs as root from tw-follow-main.timer. Each run:

  1. fetches main of Thornswood-Hermes over ssh with the VM's own read-only
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
     before, the same way, and says both.

What it did is in /var/lib/thornswood/follow-main.json and the journal.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

from thornswood_guard.policy import Policy

Run = Callable[..., subprocess.CompletedProcess]


class Stop(Exception):
    """A step failed: its sentence, and the step's name."""

    def __init__(self, step: str, why: str):
        super().__init__(why)
        self.step = step


class Follower:
    def __init__(self, policy: Policy, *, run: Run = subprocess.run, root: Path = Path("/"),
                 say: Callable[[str], None] = print):
        self.p = policy
        self.r = policy.data["follow_main"]
        self.run_ = run
        self.root = root
        self.say = say
        self.opt = self._at(policy.data["brokers"]["install_root"])
        self.mirror = self._at(self.r["mirror"])
        self.state_file = self._at(self.r["state_file"])
        self.hermes_releases = self._at(self.r["hermes_releases"])

    def _at(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def sh(self, step: str, args: list[str], **kw) -> subprocess.CompletedProcess:
        c = self.run_(args, capture_output=True, text=True, **kw)
        if c.returncode != 0:
            tail = ((c.stdout or "") + (c.stderr or "")).strip()[-600:]
            raise Stop(step, f"{' '.join(args[:3])} ... exited {c.returncode}: {tail}")
        return c

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
                raise Stop("fetch", "GitHub refused the VM's read key. Add it to Thornswood-Hermes as a deploy key, "
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
        self.sh("install", ["bash", str(core), commit])

    def apply(self, dest: Path, commit: str) -> None:
        user = self.r["hermes_user"]
        hdest = self.hermes_releases / commit
        script = (
            "set -euo pipefail\n"
            f"dest='{hdest}'\n"
            f"mkdir -p '{self.hermes_releases}'\n"
            "[[ -d \"$dest/.git\" ]] || git init --quiet \"$dest\"\n"
            f"git -c safe.directory='*' -C \"$dest\" fetch --quiet '{dest}' '+{commit}:refs/remotes/origin/main'\n"
            f"git -C \"$dest\" -c advice.detachedHead=false checkout --quiet --force --detach '{commit}'\n"
            "cd \"$dest\"\n"
            f"python3 scripts/apply.py --expect '{commit}' --check\n"
            f"python3 scripts/apply.py --expect '{commit}'\n"
            f"ln -sfn \"$dest\" '{self.hermes_releases.parent / 'current'}'\n"
        )
        self.sh("apply", ["runuser", "-u", user, "--", "bash", "-c", script])

    def restart(self) -> None:
        # The installed release's own tw-reload-bots (thornswood_brokers/reload.py),
        # the step every apply ends with: it ends the hermes user's login
        # sessions, restarts the gateway and says STOP unless it is active.
        self.sh("reload", [str(self.opt / "bin" / "tw-reload-bots")])

    def canary(self) -> None:
        self.sh("canary", [str(self.opt / "bin" / "tw-canary")])

    # -- the run
    def follow(self) -> int:
        try:
            tip = self.fetch()
        except Stop as e:
            self.say(f"STOP {e}")
            self.record(result="fetch failed", why=str(e))
            return 1
        before = self.installed()
        if tip == before:
            return 0
        st = self.state()
        if st.get("failed") == tip:
            # Said once, when it failed; a new merge on main is the fix.
            return 1
        self.say(f"main is at {tip}; installed is {before or 'nothing'}")
        try:
            dest = self.release(tip)
            ran = self.tests(dest, tip)
            self.say(f"PASS {ran} at {tip[:12]} on the VM")
        except Stop as e:
            self.say(f"STOP {tip[:12]} is not installed: {e}")
            self.record(failed=tip, step=e.step, why=str(e), installed=before)
            return 1
        try:
            self.install(dest, tip)
            self.apply(dest, tip)
            self.restart()
            self.canary()
        except Stop as e:
            self.say(f"STOP {tip[:12]} failed at {e.step}: {e}")
            back = self.put_back(before)
            self.record(failed=tip, step=e.step, why=str(e), installed=self.installed(), put_back=back)
            return 1
        self.say(f"PASS {tip[:12]} is installed, applied, every Bot process reloaded and the canary passed")
        self.record(installed=tip, failed=None, step=None, why=None, put_back=None)
        return 0

    def put_back(self, before: str | None) -> str:
        if not before:
            return "nothing was installed before, so nothing was put back"
        dest = self.opt / "releases" / before
        try:
            self.install(dest, before)
            self.apply(dest, before)
            self.restart()
            self.canary()
        except Stop as e:
            self.say(f"FAIL putting back {before[:12]} failed too, at {e.step}: {e}")
            return f"putting back {before} failed at {e.step}: {e}"
        self.say(f"PASS put back {before[:12]}, which runs as it did")
        return f"put back {before}"


def main(argv: list[str]) -> int:
    if os.geteuid() != 0:
        print("tw-follow-main runs as root from tw-follow-main.timer")
        return 2
    return Follower(Policy.load()).follow()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
