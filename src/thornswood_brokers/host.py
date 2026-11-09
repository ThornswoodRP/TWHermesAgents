"""The host's headroom, read from /proc and the filesystems (02 section 8.5).

Every reader takes a root so the tests can hand it a planted /proc. Nothing
here changes the host.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class HostState:
    mem_available_gb: float
    swap_used_mb: float
    psi_memory_some_avg60: float
    psi_memory_full_avg10: float
    load5: float
    srv_free_gb: float
    boot_free_gb: float


def _meminfo(root: Path) -> dict[str, int]:
    out = {}
    for line in (root / "proc" / "meminfo").read_text().splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            out[name] = int(parts[0])  # kB
    return out


def _psi(root: Path) -> tuple[float, float]:
    some = full = 0.0
    path = root / "proc" / "pressure" / "memory"
    for line in path.read_text().splitlines():
        fields = dict(f.split("=") for f in line.split()[1:] if "=" in f)
        if line.startswith("some"):
            some = float(fields.get("avg60", 0))
        elif line.startswith("full"):
            full = float(fields.get("avg10", 0))
    return some, full


def _free_gb(path: Path) -> float:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 1024 ** 3


def read(root: Path | str = "/", *, srv: str = "/srv/tw", boot: str = "/") -> HostState:
    root = Path(root)
    mem = _meminfo(root)
    some, full = _psi(root)
    load5 = float((root / "proc" / "loadavg").read_text().split()[1])
    swap_used_kb = mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)
    srv_path = root / srv.lstrip("/")
    boot_path = root / boot.lstrip("/")
    return HostState(
        mem_available_gb=mem.get("MemAvailable", 0) / 1024 ** 2,
        swap_used_mb=swap_used_kb / 1024,
        psi_memory_some_avg60=some,
        psi_memory_full_avg10=full,
        load5=load5,
        srv_free_gb=_free_gb(srv_path if srv_path.exists() else boot_path),
        boot_free_gb=_free_gb(boot_path),
    )


def admit_problems(state: HostState, rules: dict, *, sandbox: bool = False) -> list[str]:
    """Why new work may not start now; empty means it may."""
    a = rules["admit"]
    out = []
    need = a["sandbox_mem_available_gb"] if sandbox else a["mem_available_gb"]
    if state.mem_available_gb < need:
        out.append(f"only {state.mem_available_gb:.1f} GB of memory is available; new work needs {need} GB")
    if state.swap_used_mb > a["swap_used_mb"]:
        out.append(f"{state.swap_used_mb:.0f} MB of swap is in use; the limit is {a['swap_used_mb']} MB")
    if state.psi_memory_some_avg60 >= a["psi_memory_some_avg60"]:
        out.append(f"memory pressure is {state.psi_memory_some_avg60:.1f}% over a minute; the limit is {a['psi_memory_some_avg60']}%")
    if state.load5 > a["load5"]:
        out.append(f"the 5-minute load is {state.load5:.2f}; the limit is {a['load5']}")
    if state.srv_free_gb < a["srv_free_gb"]:
        out.append(f"{state.srv_free_gb:.0f} GB free on /srv/tw; the floor is {a['srv_free_gb']} GB")
    if state.boot_free_gb < a["boot_free_gb"]:
        out.append(f"{state.boot_free_gb:.0f} GB free on the boot disk; the floor is {a['boot_free_gb']} GB")
    return out


def drain_problems(state: HostState, rules: dict) -> list[str]:
    """Why running work should finish and nothing new start; empty means none."""
    d = rules["drain"]
    out = []
    if state.mem_available_gb < d["mem_available_gb"]:
        out.append(f"memory available fell to {state.mem_available_gb:.1f} GB")
    if state.swap_used_mb > d["swap_used_gb"] * 1024:
        out.append(f"swap use rose to {state.swap_used_mb / 1024:.1f} GB")
    if state.psi_memory_full_avg10 > d["psi_memory_full_avg10"]:
        out.append(f"full memory pressure rose to {state.psi_memory_full_avg10:.1f}%")
    if min(state.srv_free_gb, state.boot_free_gb) < d["disk_free_gb"]:
        out.append("free disk fell under the floor")
    return out
