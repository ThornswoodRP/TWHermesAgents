"""The limits check at every boot (08 section 2, attack test 16).

tw-limits-check.service runs this as root, once, ordered before the brokers
and the gateway. It reads memory.max, memory.high, cpu.max and pids.max of
each Thornswood slice from the cgroup tree, starts one throwaway check of each
tier and reads which slice it really landed in, and checks the swapfile,
/srv/tw's group quotas, each Bot's own Tier A disk and the free-space
floors. It records the outcome in
tw-policy. The gateway starts either way so Patrick can be told; tw-policy
admits no sandbox job until a check passes. A failed check is never fixed by
raising or removing a limit.

Every reader takes a root so the tests can hand it a planted /sys and /proc.
"""

from __future__ import annotations

import json
import logging
import os
import pwd
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

from thornswood_guard.policy import Policy

from . import host as hostmod

logger = logging.getLogger(__name__)

UNITS = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}


def to_bytes(v: str) -> int:
    m = re.fullmatch(r"(\d+)([KMGT]?)", str(v))
    if not m:
        raise ValueError(f"{v} is not a size")
    return int(m.group(1)) * UNITS.get(m.group(2), 1)


def quota_us(v: str, period: int = 100000) -> int:
    return int(float(str(v).rstrip("%")) / 100 * period)


def _read(p: Path) -> str | None:
    try:
        return p.read_text().strip()
    except OSError:
        return None


def slice_path(name: str) -> str:
    """systemd's cgroup path for a slice: tw-hermes.slice -> tw.slice/tw-hermes.slice."""
    parts = name[:-len(".slice")].split("-")
    out = []
    for i in range(1, len(parts) + 1):
        out.append("-".join(parts[:i]) + ".slice")
    if name.startswith("user-"):
        return "user.slice/" + name
    return "/".join(out)


def slice_problems(cg: Path, name: str, want: dict) -> list[str]:
    d = cg / slice_path(name)
    if not d.is_dir():
        return [f"{name} has no cgroup: nothing started it (tw-limits-check.service starts the Thornswood slices)"]
    out = []
    checks = [("memory.max", "MemoryMax", to_bytes), ("memory.high", "MemoryHigh", to_bytes),
              ("pids.max", "TasksMax", int)]
    for file, key, conv in checks:
        if key not in want:
            continue
        got = _read(d / file)
        if got is None:
            out.append(f"{name}: {file} cannot be read; the {file.split('.')[0]} controller is not delegated there")
        elif got == "max":
            out.append(f"{name}: {file} is max; {key}={want[key]} is not applied")
        elif int(got) != conv(want[key]):
            out.append(f"{name}: {file} is {got}, not {key}={want[key]}")
    if "CPUQuota" in want:
        got = _read(d / "cpu.max")
        if got is None:
            out.append(f"{name}: cpu.max cannot be read; the cpu controller is not delegated there")
        else:
            q, _, period = got.partition(" ")
            if q == "max":
                out.append(f"{name}: cpu.max is max; CPUQuota={want['CPUQuota']} is not applied")
            elif int(q) != quota_us(want["CPUQuota"], int(period or 100000)):
                out.append(f"{name}: cpu.max is {got}, not CPUQuota={want['CPUQuota']}")
    return out


def swap_problems(root: Path, want_gb: float = 4.0) -> list[str]:
    lines = (_read(root / "proc" / "swaps") or "").splitlines()[1:]
    total_kb = sum(int(l.split()[2]) for l in lines if len(l.split()) >= 3)
    if total_kb < want_gb * 1024 * 1024 * 0.95:
        return [f"swap is {total_kb / 1024 / 1024:.1f} GB; the host needs its {want_gb:.0f} GB swapfile"]
    return []


def quota_problems(run: Callable[[list[str]], subprocess.CompletedProcess], srv: str,
                   caps_gb: dict[str, float] | None = None) -> list[str]:
    """Group quotas are on for the filesystem holding srv, and each Bot's
    work group has its hard limit (caps_gb: group name to GB)."""
    try:
        mnt = run(["findmnt", "-n", "-o", "TARGET", "--target", srv])
        mount = (mnt.stdout or "").strip() or srv
        r = run(["quotaon", "-p", "-g", mount])
    except FileNotFoundError:
        return ["the quota tools are not installed, so the Bots' disk caps are not on"]
    text = (r.stdout or "") + (r.stderr or "")
    if "is on" not in text:
        return [f"group quotas are not on for {mount}, so the Bots' disk caps are not enforced"]
    out = []
    for group, gb in (caps_gb or {}).items():
        q = run(["quota", "-g", "-w", "-p", group])
        want = int(gb * 1024 * 1024)  # 1 KB blocks
        limits = [int(f[3]) for f in (l.split() for l in (q.stdout or "").splitlines())
                  if len(f) >= 4 and f[0].startswith("/") and f[3].isdigit()]
        if want not in limits:
            out.append(f"{group} has no {gb:g} GB hard limit on {mount}")
    return out


def disk_caps(policy: Policy) -> dict[str, float]:
    files = policy.data["brokers"]["files"]
    caps = files["disk_quota_gb"]
    return {files["work_group_prefix"] + b: caps.get(b, caps["default"]) for b in policy.bots}


def tier_a_disk_problems(run: Callable[[list[str]], subprocess.CompletedProcess], policy: Policy) -> list[str]:
    """Each Bot's Tier A folder is a filesystem of its own (policy.yaml
    tier_a_disk), no bigger than its cap. A folder that is not mounted is a
    folder on the host disk, with no cap at all."""
    root = policy.data["tier_a_disk"]["work_root"]
    out = []
    for bot, gb in tier_a_caps(policy).items():
        where = f"{root}/{bot}"
        r = run(["findmnt", "-n", "-b", "-o", "TARGET,FSTYPE,SIZE", "--mountpoint", where])
        fields = (r.stdout or "").split()
        if r.returncode != 0 or len(fields) != 3 or fields[0] != where:
            out.append(f"{where} is not a filesystem of its own, so {policy.bot_name(bot)}'s container has no disk cap")
            continue
        if not fields[2].isdigit() or int(fields[2]) > gb * 1024 ** 3:
            out.append(f"{where} is {fields[2]} bytes, more than {policy.bot_name(bot)}'s {gb:g} GB")
    return out


def tier_a_caps(policy: Policy) -> dict[str, float]:
    caps = policy.data["brokers"]["files"]["disk_quota_gb"]
    return {b: caps.get(b, caps["default"]) for b in policy.bots}


def tier_problems(run: Callable[[list[str]], subprocess.CompletedProcess], policy: Policy, hermes_uid: int | None,
                  image: str) -> list[str]:
    """Start one throwaway job of each tier and read where it landed."""
    out = []
    r = run(["systemd-run", "--quiet", "--wait", "--pipe", "--collect", "--slice=tw-sandbox.slice",
             "-p", "PrivateNetwork=yes", "-p", "MemoryMax=64M", "cat", "/proc/self/cgroup"])
    if r.returncode != 0 or "/tw.slice/tw-sandbox.slice/" not in (r.stdout or ""):
        out.append(f"a Tier B job did not land in tw-sandbox.slice ({(r.stdout or r.stderr or '').strip()[:200]})")
    if hermes_uid is not None:
        r = run(["runuser", "-u", "hermes", "--", "env", f"XDG_RUNTIME_DIR=/run/user/{hermes_uid}", "podman", "run",
                 "--rm", "--network=none", "--pull=never", "--cgroupns=host", image, "cat", "/proc/self/cgroup"])
        if r.returncode != 0:
            out.append(f"a Tier A container did not start ({(r.stderr or '').strip()[:200]})")
        elif f"/user.slice/user-{hermes_uid}.slice/" not in (r.stdout or ""):
            out.append("a Tier A container did not land under the hermes user's slice")
    return out


def run_check(policy: Policy, *, root: Path = Path("/"), run=None, hermes_uid: int | None = None,
              image: str | None = None, host_state=None) -> list[str]:
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=120))
    cg = root / "sys" / "fs" / "cgroup"
    problems = []
    for name, want in policy.data["slices"].items():
        real = f"user-{hermes_uid}.slice" if name == "hermes-user.slice" and hermes_uid is not None else name
        if name == "hermes-user.slice" and hermes_uid is None:
            problems.append("the hermes user does not exist, so its slice cannot be checked")
            continue
        problems += slice_problems(cg, real, want)
    problems += swap_problems(root)
    srv = policy.data["brokers"]["state"]["work"].rsplit("/", 1)[0]
    problems += quota_problems(run, srv, disk_caps(policy))
    problems += tier_a_disk_problems(run, policy)
    state = host_state or hostmod.read(root, srv=srv)
    rules = policy.data["admission"]
    if state.srv_free_gb < rules["admit"]["srv_free_gb"]:
        problems.append(f"{state.srv_free_gb:.0f} GB free on {srv}; the floor is {rules['admit']['srv_free_gb']} GB")
    if state.boot_free_gb < rules["admit"]["boot_free_gb"]:
        problems.append(f"{state.boot_free_gb:.0f} GB free on the boot disk; the floor is {rules['admit']['boot_free_gb']} GB")
    if image:
        problems += tier_problems(run, policy, hermes_uid, image)
    return problems


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if os.geteuid() != 0:
        print("tw-limits-check runs as root at boot")
        return 2
    policy = Policy.load()
    from thornswood_guard.profile_config import SANDBOX_IMAGE
    try:
        uid = pwd.getpwnam(policy.data["brokers"]["users"]["gateway"]).pw_uid
    except KeyError:
        uid = None
    problems = run_check(policy, hermes_uid=uid, image=SANDBOX_IMAGE)
    ok = not problems
    for p in problems:
        print(f"FAIL {p}")
    if ok:
        print("PASS every slice's limits, the swapfile, the quotas, the Bots' own disks and the floors hold")
    Path("/run/tw").mkdir(exist_ok=True)
    Path("/run/tw/limits.json").write_text(json.dumps({"ok": ok, "problems": problems}) + "\n")
    from . import wire
    try:
        wire.call(policy.data["brokers"]["sockets"]["policy"]["path"], "limits_record", {"ok": ok, "details": problems})
    except wire.Refused as e:
        # tw-policy reads /run/tw/limits.json when it starts after this.
        print(f"tw-policy was not there to record it ({e}); it reads /run/tw/limits.json at start")
    return 0  # the gateway starts either way; tw-policy holds the sandbox back


if __name__ == "__main__":
    sys.exit(main())
