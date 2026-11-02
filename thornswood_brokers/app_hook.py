"""Bramble's wake: the App's webhook and the Funnel path it reaches (policy `wake`).

Patrick's answer A on q-hermes-bramble-wake (1 Nov): Bramble's App sends its
check_suite and pull_request events through Tailscale Funnel to Hermes's own
webhook receiver, which starts a Bramble turn. Cause it fixes: nothing reached
Bramble when a check finished, so every merge and hand-back waited for
someone to nudge it.

deploy/install.sh runs, as root:

  set <url>   points the App's webhook at <url>, signed with the secret in
              wake.secret_file, through the App's own key. The secret goes
              from root's file to GitHub and nowhere else: it is never
              printed, logged or copied by hand.
  funnel <status.json>
              checks `tailscale funnel status --json`: the wake path, and
              only it, is public on wake.funnel_port, and no other port is
              public (443 carries the decision core, tailnet only).
  check       the App's last deliveries and the answer each got, so a wake
              that does not arrive is measured, not guessed at.

Which events the App sends is kept on the App's settings page; GitHub's API
cannot set it, so that one step is Patrick's.

Standard library only, like every broker.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from thornswood_guard.policy import Policy
from thornswood_guard.profile_config import wake_path

from .github_api import API, GitHubError, app_jwt, urllib_transport

SECRET_LINE = re.compile(r"^WEBHOOK_SECRET=([0-9a-f]{64})$")
EVENTS = ("check_suite", "pull_request")


def wake_target(policy: Policy) -> str:
    return f"http://127.0.0.1:{policy.data['wake']['port']}{wake_path(policy)}"


def read_secret(path: str | Path) -> str:
    lines = [ln.strip() for ln in Path(path).read_text(encoding="utf-8").splitlines() if ln.strip()]
    found = [m.group(1) for m in map(SECRET_LINE.match, lines) if m]
    if len(lines) != 1 or len(found) != 1:
        raise ValueError(f"{path} must hold exactly one WEBHOOK_SECRET=<64 hex characters> line")
    return found[0]


def _app_call(policy: Policy, method: str, path: str, body=None, *, transport=urllib_transport, signer=None, now=None):
    app = policy.data["wake"]["app"]
    spec = policy.data["brokers"]["apps"][app]
    jwt = app_jwt(spec["app_id"], spec["key_file"], now=now, signer=signer)
    headers = {"Authorization": f"Bearer {jwt}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    return transport(method, f"{API}{path}", headers, data)


def set_hook(policy: Policy, url: str, *, transport=urllib_transport, signer=None, now=None) -> list[str]:
    """Point the App's webhook at url with the secret; the lines to print."""
    w = policy.data["wake"]
    if not re.fullmatch(r"https://[a-z0-9.-]+\.ts\.net:\d+" + re.escape(wake_path(policy)), url):
        raise ValueError(f"{url} is not this VM's Funnel address for {wake_path(policy)}")
    if not url.split("/p/", 1)[0].endswith(f":{w['funnel_port']}"):
        raise ValueError(f"{url} is not on the Funnel port {w['funnel_port']}")
    secret = read_secret(w["secret_file"])
    body = {"url": url, "content_type": "json", "insecure_ssl": "0", "secret": secret}
    r = _app_call(policy, "PATCH", "/app/hook/config", body, transport=transport, signer=signer, now=now)
    if r.status != 200:
        raise GitHubError(r.status, _message(r.body))
    r = _app_call(policy, "GET", "/app/hook/config", transport=transport, signer=signer, now=now)
    got = r.body or {}
    if r.status != 200 or got.get("url") != url or got.get("content_type") != "json" or str(got.get("insecure_ssl")) != "0":
        raise GitHubError(r.status, f"the App's webhook reads back as url {got.get('url')!r}, "
                                    f"content type {got.get('content_type')!r}, insecure_ssl {got.get('insecure_ssl')!r}")
    return [f"PASS {w['app']}'s App sends its webhook to {url} as JSON over checked TLS, signed with the secret in {w['secret_file']}"]


def check_funnel(policy: Policy, status: dict) -> list[str]:
    """Problems with `tailscale funnel status --json`; empty when the wake path
    alone is public, on the Funnel port, and goes to the gateway's receiver."""
    w = policy.data["wake"]
    port = str(w["funnel_port"])
    problems = []
    public = sorted(hp for hp, on in (status.get("AllowFunnel") or {}).items() if on)
    others = [hp for hp in public if not hp.endswith(f":{port}")]
    if others:
        problems.append(f"Funnel makes {', '.join(others)} public; only port {port}, for Bramble's wake, may be")
    mine = [hp for hp in public if hp.endswith(f":{port}")]
    if not mine:
        problems.append(f"Funnel is not on for port {port}")
    for hp in mine:
        handlers = ((status.get("Web") or {}).get(hp) or {}).get("Handlers") or {}
        want = {wake_path(policy): wake_target(policy)}
        got = {path: (h or {}).get("Proxy") for path, h in handlers.items()}
        if got != want:
            problems.append(f"{hp} serves {got}, not only {want}")
    return problems


def check_deliveries(policy: Policy, *, transport=urllib_transport, signer=None, now=None) -> list[str]:
    r = _app_call(policy, "GET", "/app/hook/deliveries?per_page=10", transport=transport, signer=signer, now=now)
    if r.status != 200:
        raise GitHubError(r.status, _message(r.body))
    rows = r.body or []
    if not rows:
        return [f"no deliveries yet: tick {' and '.join(EVENTS)} under the App's 'Subscribe to events', with Active on"]
    out = []
    for d in rows:
        what = d.get("event") + (f" ({d.get('action')})" if d.get("action") else "")
        out.append(f"{d.get('delivered_at')}  {what}: {d.get('status_code')} {d.get('status')}")
    return out


def _message(body) -> str:
    return body.get("message", "") if isinstance(body, dict) else str(body)[:200]


def main(argv: list[str]) -> int:
    policy = Policy.load()
    try:
        if argv[:1] == ["set"] and len(argv) == 2:
            lines = set_hook(policy, argv[1])
        elif argv[:1] == ["funnel"] and len(argv) == 2:
            problems = check_funnel(policy, json.loads(Path(argv[1]).read_text(encoding="utf-8") or "{}"))
            for p in problems:
                print(f"STOP: {p}")
            if problems:
                return 1
            lines = [f"PASS only {wake_path(policy)} is public, on port {policy.data['wake']['funnel_port']}"]
        elif argv == ["check"]:
            lines = check_deliveries(policy)
        else:
            print("usage: app_hook set <url> | funnel <status.json> | check")
            return 2
    except (GitHubError, ValueError, OSError) as e:
        print(f"STOP: {e}")
        return 1
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
