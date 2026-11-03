"""tw-runner-egress: what the guard rail tests' runner may reach (3 Nov 2026).

GitHub's included Actions minutes for a private repository ran out on 2 Nov,
and the spending limit stays at $0 (Patrick: "I am not fucking paying for more
usage"). Patrick's A on q-hermes-ci-free-runners: the Linux check runs on this
VM, as its own user, tw-runner, and reaches only GitHub and the package feeds. It
is the same guard the box's runners have (Thornswood's
scripts/deploy/Install-Runner.ps1, NETWORK, #2161), made with nftables here.

The table matches only packets tw-runner sends (meta skuid). They may go to the
VM's own resolver (127.0.0.53, port 53) and, on port 443, to the web, api, git
and actions lists of https://api.github.com/meta and to the addresses the
allowed host names resolve to now. Every other packet tw-runner sends is
rejected: the tailnet, the cloud's metadata service, the brokers' loopback
ports and the rest of the internet.

What that does not do, as on the box: it has no idea of host names (a rule
is addresses, and the actions list is broad Azure space shared with others),
and the lists change, so tw-runner-egress.timer fetches them again daily. A
range GitHub adds is blocked until then, which fails closed. DNS questions
go out through systemd-resolved, not as tw-runner.

When the lists cannot be fetched, the table that is there stays as it was.
When there is none yet, one with empty lists goes on, so tw-runner reaches
nothing until a fetch works. tw-runner.service needs this unit, so the
runner never runs without the table.
"""

from __future__ import annotations

import ipaddress
import json
import pwd
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
PIN = HERE / "deploy" / "runner" / "runner.json"
TABLE = "tw_runner"
META_URL = "https://api.github.com/meta"
RESOLVER = "127.0.0.53"


def pin() -> dict:
    return json.loads(PIN.read_text(encoding="utf-8"))


def lists_from_meta(meta: dict, keys: list[str]) -> tuple[list[str], list[str]]:
    """The IPv4 and IPv6 networks of the named lists, each once, in order."""
    v4: list[str] = []
    v6: list[str] = []
    for k in keys:
        nets = meta.get(k)
        if not isinstance(nets, list) or not nets:
            raise ValueError(f"api.github.com/meta has no {k} list")
        for n in nets:
            net = ipaddress.ip_network(str(n), strict=False)
            (v4 if net.version == 4 else v6).append(str(net))
    return _once(v4), _once(v6)


def resolve(hosts: list[str], lookup=socket.getaddrinfo) -> tuple[list[str], list[str]]:
    v4: list[str] = []
    v6: list[str] = []
    for h in hosts:
        for info in lookup(h, 443, proto=socket.IPPROTO_TCP):
            addr = ipaddress.ip_address(info[4][0])
            (v4 if addr.version == 4 else v6).append(f"{addr}/{addr.max_prefixlen}")
    return _once(v4), _once(v6)


def _once(nets: list[str]) -> list[str]:
    seen, out = set(), []
    for n in nets:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def ruleset(uid: int, v4: list[str], v6: list[str]) -> str:
    """The whole table, replacing the one there in one nft transaction."""
    def elements(nets):
        return f"elements = {{ {', '.join(nets)} }}" if nets else ""
    return "\n".join([
        f"table inet {TABLE}",
        f"delete table inet {TABLE}",
        f"table inet {TABLE} {{",
        "  set github4 { type ipv4_addr; flags interval; auto-merge; " + elements(v4) + " }",
        "  set github6 { type ipv6_addr; flags interval; auto-merge; " + elements(v6) + " }",
        "  chain out {",
        "    type filter hook output priority filter; policy accept;",
        f"    meta skuid != {uid} accept",
        f"    ip daddr {RESOLVER} udp dport 53 accept",
        f"    ip daddr {RESOLVER} tcp dport 53 accept",
        "    ip daddr @github4 tcp dport 443 accept",
        "    ip6 daddr @github6 tcp dport 443 accept",
        "    counter reject",
        "  }",
        "}",
        "",
    ])


def fetch_meta(url: str = META_URL) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "thornswood-hermes-tw-runner-egress"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def table_present(run=subprocess.run) -> bool:
    return run(["nft", "list", "table", "inet", TABLE], capture_output=True).returncode == 0


def apply(text: str, run=subprocess.run) -> None:
    r = run(["nft", "-f", "-"], input=text, text=True, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"nft refused the table: {r.stderr.strip()}")


def main(argv: list[str]) -> int:
    p = pin()
    uid = pwd.getpwnam(p["user"]).pw_uid
    try:
        v4, v6 = lists_from_meta(fetch_meta(), p["egress"]["github_lists"])
        h4, h6 = resolve(p["egress"]["allow_hosts"])
    except Exception as e:  # noqa: BLE001 - any failure keeps what is there
        if table_present():
            print(f"the lists could not be read ({type(e).__name__}: {e}); the table stays as it was")
            return 0
        apply(ruleset(uid, [], []))
        print(f"the lists could not be read ({type(e).__name__}: {e}); {p['user']} reaches nothing until they can")
        return 1
    apply(ruleset(uid, v4 + h4, v6 + h6))
    print(f"{p['user']} reaches {len(v4)} GitHub IPv4 and {len(v6)} IPv6 networks, and "
          f"{len(h4) + len(h6)} addresses of {', '.join(p['egress']['allow_hosts'])}, on port 443")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
