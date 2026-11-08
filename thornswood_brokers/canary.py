"""The canary attacks on the live VM (08 section 8, run again at the canary).

Run as root on the VM after an install, from the root-owned copy:

    sudo /opt/thornswood-hermes/bin/tw-canary

Each attack must be stopped by the kernel, systemd, podman or an auth gate,
and leave the gateway and brokers up. It plants canary files only (never a
real secret), prints a PASS or FAIL line per check, removes what it planted,
and exits 1 when any check fails.

  2   a Tier B check reads a canary in the brokers' state, /etc/thornswood and
      another Bot's tree, the gateway's /proc/<pid>/environ, and opens a socket
  3   a Tier B check forks past TasksMax, allocates past MemoryMax, spins past
      CPUQuota; a canary group writes past its disk quota
  5   a Tier A container (Hermes's own flags) mounts, sees the host home or
      has any network interface but lo
  18  every listener of hermes and the brokers is on loopback or Tailscale
      only, and each Tailscale one refuses a request with no sign-in
  one executor: as hermes, a Bramble merge through tw-git's socket is refused
      by the one-executor rule until the cutover (policy.yaml executor)
  connector: where the decision core is installed, its connector's Hermes
      helper (room_send.py, in Hermes's own runtime) answers {"ready": true}
      for Bramble, as hermes (30 Oct to 2 Nov it ran under /bin/sh, failed every
      check, and Patrick's answers waited unseen as "gateway not ready")
"""

from __future__ import annotations

import ipaddress
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from thornswood_guard.policy import Policy

from .files_service import check_argv

Result = tuple[str, bool, str]
Run = Callable[..., subprocess.CompletedProcess]

# Hermes's own container flags (tools/environments/docker.py, v0.23.0) with
# docker_network: false.
HERMES_PODMAN_ARGS = ["--cap-drop", "ALL", "--cap-add", "DAC_OVERRIDE", "--cap-add", "CHOWN", "--cap-add", "FOWNER",
                      "--tmpfs", "/tmp:rw,nosuid,size=512m", "--network=none", "--pids-limit", "2048",
                      "--memory", "2048m", "--cpus", "1"]

PROBE_TIER_B = r'''
import os, socket, sys
for p in sys.argv[1:]:
    try:
        open(p, "rb").read(1)
        print("READ " + p)
    except OSError as e:
        print("DENIED " + p + " " + type(e).__name__)
try:
    socket.create_connection(("1.1.1.1", 443), timeout=3)
    print("NETWORK open")
except OSError as e:
    print("NETWORK closed " + type(e).__name__)
'''

FORK = r'''
import os, signal, sys, time
kids = []
try:
    for _ in range(int(sys.argv[1]) * 2):
        pid = os.fork()
        if pid == 0:
            time.sleep(30)
            os._exit(0)
        kids.append(pid)
except OSError as e:
    print("FORK stopped at %d %s" % (len(kids), type(e).__name__))
else:
    print("FORK not stopped at %d" % len(kids))
for k in kids:
    os.kill(k, signal.SIGKILL)
'''

MEMORY = r'''
import sys
blocks = []
for _ in range(int(sys.argv[1])):
    blocks.append(bytearray(b"\x01") * (64 * 1024 * 1024))  # written, so every page counts
print("ALLOCATED %d MB" % (len(blocks) * 64))
'''

CPU = r'''
import os, resource, time
start = time.monotonic()
kids = []
for _ in range(4):
    pid = os.fork()
    if pid == 0:
        end = time.monotonic() + 4
        while time.monotonic() < end:
            pass
        os._exit(0)
    kids.append(pid)
for k in kids:
    os.waitpid(k, 0)
wall = time.monotonic() - start
cpu = resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime + resource.getrusage(resource.RUSAGE_CHILDREN).ru_stime
print("CPU %.2f WALL %.2f" % (cpu, wall))
'''

# Asks a broker over its socket, as whoever runs it, with the standard
# library only: argv is the socket, the op and the args as JSON. Prints the
# broker's answer as one JSON line.
ASK = r'''
import json, socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(60)
try:
    s.connect(sys.argv[1])
except OSError as e:
    print(json.dumps({"ok": False, "reason": "NOCONNECT " + str(e)}))
    sys.exit(0)
s.sendall((json.dumps({"op": sys.argv[2], "args": json.loads(sys.argv[3])}) + "\n").encode())
buf = b""
while not buf.endswith(b"\n"):
    chunk = s.recv(65536)
    if not chunk:
        break
    buf += chunk
print(buf.decode().strip() or json.dumps({"ok": False, "reason": "NOANSWER"}))
'''


def executor_problem(executor: str, answer: str) -> str | None:
    """Reads tw-git's answer to a Bramble merge that can never go through
    (the head SHA is forty zeros)."""
    try:
        got = json.loads(answer.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return f"tw-git gave no answer the canary can read: {answer.strip()[-200:]!r}"
    if got.get("ok"):
        return "tw-git did the merge it was asked for"
    reason = str(got.get("reason") or "")
    if reason.startswith(("NOCONNECT", "NOANSWER")):
        return f"tw-git did not answer: {reason}"
    if executor != "bramble" and "until the cutover the Bots read and plan only" not in reason:
        return f"refused, but not by the one-executor rule: {reason[:200]}"
    return None


CONTAINER = r'''
mount -t tmpfs canary /mnt 2>/dev/null && echo "MOUNTED tmpfs"
mount -t proc proc /mnt 2>/dev/null && echo "MOUNTED proc"
test -e /home/hermes/.hermes && echo "HOSTHOME seen"
for i in /sys/class/net/*; do echo "IFACE ${i##*/}"; done
echo DONE
'''


def connector_problem(code: int, out: str, err: str) -> str | None:
    """None when the connector's readiness check printed {"ready": true} and exited 0."""
    lines = out.strip().splitlines()
    try:
        got = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError:
        got = None
    if code == 0 and isinstance(got, dict) and got.get("ready") is True:
        return None
    said = (lines[-1] if lines else "") or " ".join(err.split())[-300:] or "nothing"
    return f"exit {code}: {said[-300:]}"


def gateway_waiting(out: str) -> bool:
    """The check ran and the gateway is not ready yet ({"ready": false, ...} from the helper itself)."""
    lines = out.strip().splitlines()
    try:
        got = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError:
        return False
    return isinstance(got, dict) and got.get("ready") is False and not got.get("helper_failed")


def parse_probe(out: str, paths: list[str]) -> list[str]:
    """What a Tier B probe got that it never should."""
    bad = [l for l in out.splitlines() if l.startswith("READ ")]
    if "NETWORK closed" not in out:
        bad.append("the check had a network" if "NETWORK open" in out else "the probe did not finish")
    seen = {l.split()[1] for l in out.splitlines() if l.startswith(("READ ", "DENIED "))}
    bad += [f"no answer for {p}" for p in paths if p not in seen]
    return bad


def parse_fork(out: str, tasks_max: int) -> str | None:
    m = re.search(r"FORK stopped at (\d+)", out)
    if not m:
        return f"forking was not stopped ({out.strip()[:120]})"
    if int(m.group(1)) >= tasks_max:
        return f"{m.group(1)} forks got through; TasksMax is {tasks_max}"
    return None


def parse_cpu(out: str, quota_pct: float) -> str | None:
    m = re.search(r"CPU ([\d.]+) WALL ([\d.]+)", out)
    if not m:
        return f"the CPU spin did not report ({out.strip()[:120]})"
    cpu, wall = float(m.group(1)), float(m.group(2))
    if cpu > wall * quota_pct / 100 * 1.15:
        return f"four spinning processes used {cpu:.1f}s of CPU in {wall:.1f}s; CPUQuota={quota_pct:g}% allows {wall * quota_pct / 100:.1f}s"
    return None


def parse_container(out: str) -> list[str]:
    bad = []
    if "DONE" not in out:
        return [f"the container did not run the probe ({out.strip()[:200]})"]
    for line in out.splitlines():
        if line.startswith("MOUNTED "):
            bad.append(f"the container mounted {line.split()[1]}")
        if line.startswith("HOSTHOME"):
            bad.append("the container sees /home/hermes/.hermes")
        if line.startswith("IFACE ") and line.split()[1] != "lo":
            bad.append(f"the container has a network interface {line.split()[1]}")
    return bad


def parse_listeners(ss: str) -> list[tuple[str, int, int]]:
    """(address, port, pid) for each line of `ss -Hltnp`."""
    out = []
    for line in ss.splitlines():
        f = line.split()
        if len(f) < 5:
            continue
        addr, _, port = f[3].rpartition(":")
        addr = addr.strip("[]").split("%")[0]
        for pid in re.findall(r"pid=(\d+)", line):
            out.append((addr, int(port), int(pid)))
    return out


def listener_problem(addr: str, tailscale: set[str]) -> str | None:
    if addr in ("*", "0.0.0.0", "::"):
        return "listens on every address, the public one included"
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return f"listens on {addr}, which is not an address this check knows"
    if ip.is_loopback or addr in tailscale:
        return None
    return f"listens on {addr}, which is neither loopback nor Tailscale"


def page_problem(status: int, body: str) -> str | None:
    if status == 200 and re.search(r'__HERMES_SESSION_TOKEN__\s*=\s*"[^"]+', body):
        return "hands its session token to anyone who asks"
    if "__HERMES_AUTH_REQUIRED__=false" in body:
        return "says no sign-in is required"
    return None


class Canary:
    def __init__(self, policy: Policy, run: Run | None = None):
        self.policy = policy
        self.run = run or (lambda argv, **kw: subprocess.run(argv, capture_output=True, text=True, **kw))
        self.results: list[Result] = []
        self.cleanup: list[Callable[[], None]] = []
        self.sleep: Callable[[float], None] = __import__("time").sleep

    def check(self, name: str, problems: list[str] | str | None) -> None:
        if isinstance(problems, str):
            problems = [problems]
        problems = [p for p in (problems or []) if p]
        self.results.append((name, not problems, "; ".join(problems)))

    # Tier B (attacks 2 and 3)

    def _tree(self) -> str:
        state = self.policy.data["brokers"]["state"]
        group = self.policy.data["brokers"]["files"]["work_group_prefix"] + "rift"
        tree = Path(state["work"]) / "rift" / "tw-canary" / f"t-{os.getpid()}"
        tree.mkdir(parents=True, exist_ok=True)
        shutil.chown(tree.parent, "tw-files", group)
        shutil.chown(tree, "tw-files", group)
        tree.chmod(0o2770)
        self.cleanup.append(lambda: shutil.rmtree(tree.parent, ignore_errors=True))
        return str(tree)

    def _job(self, tree: str, script: str, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
        argv = check_argv(self.policy, "rift", tree, ["python3", "-I", "-c", script, *args])
        return self.run(argv, timeout=timeout)

    def _plant(self, path: Path) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("canary, not a secret\n")
        path.chmod(0o644)
        self.cleanup.append(lambda: path.unlink(missing_ok=True))
        return str(path)

    def tier_b(self, gateway_pid: int | None) -> None:
        state = self.policy.data["brokers"]["state"]
        tree = self._tree()
        paths = [self._plant(Path(state["policy_db"]).parent / "canary.txt"),
                 self._plant(Path("/etc/thornswood/canary.txt")),
                 self._plant(Path(state["work"]) / "wisp" / "canary.txt"),
                 self._plant(Path(state["spool"]) / "in" / "canary.txt")]
        if gateway_pid:
            paths.append(f"/proc/{gateway_pid}/environ")
        r = self._job(tree, PROBE_TIER_B, *paths)
        self.check("2: a check reads no broker state, other tree or gateway environment, and has no network",
                   parse_probe(r.stdout, paths))

        lim = self.policy.data["brokers"]["files"]["check_limits"]
        r = self._job(tree, FORK, str(lim["TasksMax"]))
        self.check(f"3: forking stops under TasksMax={lim['TasksMax']}", parse_fork(r.stdout, int(lim["TasksMax"])))
        from .limits_check import to_bytes
        blocks = to_bytes(lim["MemoryMax"]) // (64 * 1024 * 1024) + 16
        r = self._job(tree, MEMORY, str(blocks))
        self.check(f"3: allocating past MemoryMax={lim['MemoryMax']} is killed",
                   None if r.returncode != 0 and "ALLOCATED" not in r.stdout
                   else f"the check allocated {blocks * 64} MB and was not stopped")
        r = self._job(tree, CPU)
        self.check(f"3: spinning holds to CPUQuota={lim['CPUQuota']}", parse_cpu(r.stdout, float(lim["CPUQuota"].rstrip("%"))))

    def disk_quota(self) -> None:
        state = self.policy.data["brokers"]["state"]
        mount = self.run(["findmnt", "-n", "-o", "TARGET", "--target", state["work"]]).stdout.strip()
        if "is on" not in (self.run(["quotaon", "-p", "-g", mount]).stdout or ""):
            self.check("3: writing past the disk quota is refused", f"group quotas are not on for {mount}")
            return
        group = "tw-work-canary"
        self.run(["groupadd", "--system", group])
        self.cleanup.append(lambda: self.run(["groupdel", group]))
        d = Path(state["work"]) / "canary-quota"
        d.mkdir(exist_ok=True)
        shutil.chown(d, "tw-rift", group)
        d.chmod(0o2770)
        self.cleanup.append(lambda: shutil.rmtree(d, ignore_errors=True))
        self.run(["setquota", "-g", group, "0", str(32 * 1024), "0", "0", mount])
        r = self.run(["setpriv", "--reuid=tw-rift", f"--regid={group}", "--clear-groups", "dd", "if=/dev/zero",
                      f"of={d}/fill", "bs=1M", "count=64"])
        self.check("3: writing past the disk quota is refused",
                   None if r.returncode != 0 and "quota" in r.stderr.lower()
                   else f"a 64 MB write under a 32 MB quota was not refused ({r.stderr.strip()[-160:]})")

    # Tier A (attack 5)

    def tier_a(self, image: str) -> None:
        uid = pwd.getpwnam("hermes").pw_uid
        argv = ["runuser", "-u", "hermes", "--", "env", f"XDG_RUNTIME_DIR=/run/user/{uid}", "podman", "run", "--rm",
                "--pull=never", *HERMES_PODMAN_ARGS, image, "sh", "-c", CONTAINER]
        r = self.run(argv, timeout=180, cwd="/")
        self.check("5: a container cannot mount, see the host or reach a network",
                   parse_container(r.stdout + r.stderr))

    # Network (attack 18)

    def listeners(self) -> None:
        tailscale = set(self.run(["tailscale", "ip"]).stdout.split())
        mine = {pwd.getpwnam(u).pw_uid for u in ["hermes", *self.policy.data["brokers"]["users"].values()]
                if _user_exists(u)}
        problems, probes = [], []
        for addr, port, pid in parse_listeners(self.run(["ss", "-Hltnp"]).stdout):
            try:
                owner = os.stat(f"/proc/{pid}").st_uid
            except FileNotFoundError:
                continue
            if owner not in mine:
                continue
            p = listener_problem(addr, tailscale)
            if p:
                problems.append(f"{pwd.getpwuid(owner).pw_name} on port {port} {p}")
            elif addr in tailscale:
                probes.append((addr, port))
        self.check("18: hermes and the brokers listen on loopback and Tailscale only", problems)
        problems = []
        for addr, port in sorted(set(probes)):
            for path in ("/", "/api/status", "/api/sessions", "/api/config", "/api/env", "/v1/models"):
                status, body = _get(f"http://{addr}:{port}{path}")
                p = page_problem(status, body) if path == "/" else (
                    f"answers {path} with 200 and no sign-in" if status == 200 else None)
                if p:
                    problems.append(f"{addr}:{port} {p}")
        self.check(f"18: each Tailscale listener ({len(set(probes))}) refuses a request with no sign-in", problems)
        listen = self.policy.data["admission_proxy"]["listen"]
        status, _ = _get(f"http://{listen}/credits/v1/messages", method="POST", body=b"{}")
        self.check("18: the admission proxy refuses a request with no Bot token",
                   None if status in (401, 403, 0) else f"the proxy answered {status}")

    def one_executor(self) -> None:
        """Before the cutover, a Bramble merge through tw-git's own socket, as
        the hermes user the Bots run as, is refused by the one-executor rule.
        After it, still refused, by the head check: nothing is ever merged."""
        executor = self.policy.data["executor"]
        sock = self.policy.data["brokers"]["sockets"]["git"]["path"]
        args = {"bot": "bramble", "repo": self.policy.data["repos"]["app"], "task": "tw-canary",
                "number": 1, "sha": "0" * 40}
        r = self.run(["runuser", "-u", self.policy.data["brokers"]["users"]["gateway"], "--",
                      "python3", "-I", "-c", ASK, sock, "merge", json.dumps(args)], timeout=90)
        self.check(f"one executor ({executor}): a Bramble merge through tw-git is refused"
                   + (" until the cutover" if executor != "bramble" else ""),
                   executor_problem(executor, r.stdout or r.stderr or ""))

    def connector_helper(self, env_file: Path = Path("/etc/thornswood/tw-decide.env"),
                         launcher: str = "/opt/thornswood-hermes/bin/tw-decide-connector", bot: str = "bramble") -> None:
        """The decision connector's own readiness check for one Bot, run as the hermes user the way its unit
        runs it (the same hermes command and runtime directory). It must answer {"ready": true}: anything
        else means Patrick's messages and answers cannot reach the Bots."""
        if not env_file.exists():
            return  # the decision core is not installed on this VM
        conf = dict(line.partition("=")[::2] for line in env_file.read_text().splitlines() if "=" in line)
        hermes_bin = conf.get("TW_HERMES_BIN", "").strip()
        user = self.policy.data["brokers"]["users"]["gateway"]
        name = f"connector: its Hermes helper answers ready for {bot}, as {user}"
        if not hermes_bin:
            self.check(name, f"{env_file} names no TW_HERMES_BIN")
            return
        pw = pwd.getpwnam(user)
        argv = ["runuser", "-u", user, "--", "env", f"HOME={pw.pw_dir}", f"XDG_RUNTIME_DIR=/run/user/{pw.pw_uid}",
                launcher, "--hermes", hermes_bin, "--check-ready", bot]
        # A gateway reloaded a moment ago may still be starting: that answer is asked again for up to a
        # minute. A helper that failed is not the gateway, and fails at once.
        for attempt in range(7):
            r = self.run(argv, timeout=150, cwd=pw.pw_dir)
            problem = connector_problem(r.returncode, r.stdout or "", r.stderr or "")
            if problem is None or not gateway_waiting(r.stdout or "") or attempt == 6:
                break
            self.sleep(10)
        self.check(name, problem)

    def services_up(self) -> None:
        units = ["tw-policy", "tw-git", "tw-files", "tw-proxy"]
        gw = "tw-hermes-gateway" if self.run(["systemctl", "is-enabled", "--quiet", "tw-hermes-gateway.service"]).returncode == 0 else None
        down = [u for u in units + ([gw] if gw else [])
                if self.run(["systemctl", "is-active", "--quiet", f"{u}.service"]).returncode != 0]
        self.check("3: the gateway and the brokers are still up", [f"{u} is down" for u in down])

    def finish(self) -> int:
        for undo in reversed(self.cleanup):
            try:
                undo()
            except Exception as e:  # report, keep cleaning
                print(f"FAIL cleaning up: {e}")
        failed = 0
        for name, ok, detail in self.results:
            print(f"{'PASS' if ok else 'FAIL'} {name}" + ("" if ok else f": {detail}"))
            failed += not ok
        return 1 if failed else 0


def _user_exists(name: str) -> bool:
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


def _get(url: str, method: str = "GET", body: bytes | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(65536).decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except OSError:
        return 0, ""


def gateway_pid(run: Run) -> int | None:
    for unit in ("tw-hermes-gateway.service",):
        out = run(["systemctl", "show", "-p", "MainPID", "--value", unit]).stdout.strip()
        if out.isdigit() and int(out) > 0:
            return int(out)
    out = run(["systemctl", "--machine=hermes@.host", "--user", "show", "-p", "MainPID", "--value",
               "hermes-gateway.service"]).stdout.strip()
    return int(out) if out.isdigit() and int(out) > 0 else None


def main() -> int:
    if os.geteuid() != 0:
        print("tw-canary runs as root on the VM: sudo /opt/thornswood-hermes/bin/tw-canary")
        return 2
    policy = Policy.load()
    from thornswood_guard.profile_config import SANDBOX_IMAGE
    c = Canary(policy)
    try:
        c.tier_b(gateway_pid(c.run))
        c.disk_quota()
        c.tier_a(SANDBOX_IMAGE)
        c.listeners()
        c.one_executor()
        c.connector_helper()
        c.services_up()
    except Exception as e:
        c.check("the canary ran to the end", f"{type(e).__name__}: {e}")
    return c.finish()


if __name__ == "__main__":
    sys.exit(main())
