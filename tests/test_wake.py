"""Bramble's wake (policy `wake`, Patrick's answer A on q-hermes-bramble-wake).

Cause it fixes: nothing reached Bramble when a check finished, so every merge
and hand-back waited for someone to nudge it. Bramble's App sends check_suite
and pull_request events through Tailscale Funnel to Hermes's webhook receiver.

These check the route the gateway gets, that the prompt carries only GitHub's
own fields, the App hook setter, the Funnel check, and the install steps.
No test reads a real secret or calls GitHub.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import REPO, policy

from thornswood_brokers import app_hook
from thornswood_brokers.github_api import GitHubError, Response
from thornswood_guard.profile_config import WAKE_PROMPT, build_gateway, wake_path, wake_route
from thornswood_guard.routes import check_gateway_config

SECRET = "ab" * 32
URL = "https://hermes.tail1234.ts.net:8443/p/bramble/webhooks/github-checks"
TARGET = "http://127.0.0.1:8644/p/bramble/webhooks/github-checks"
HOST = "hermes.tail1234.ts.net"


class TheRoute(unittest.TestCase):
    def setUp(self):
        self.p = policy()
        self.route = wake_route(self.p)

    def test_bramble_takes_check_suites_and_pull_requests_on_the_three_repos(self):
        self.assertEqual(self.route["profile"], "bramble")
        self.assertEqual(self.route["events"], ["check_suite", "pull_request"])
        self.assertEqual(self.route["filters"][0],
                         {"field": "repository.full_name",
                          "in": ["ThornswoodRP/Thornswood", "ThornswoodRP/Thornswood-Ops", "ThornswoodRP/skymp"]})
        self.assertNotIn(self.p.data["repos"]["hermes"], self.route["filters"][0]["in"])
        self.assertEqual(self.route["toolsets"], ["thornswood"])
        self.assertEqual(wake_path(self.p), "/p/bramble/webhooks/github-checks")

    def test_only_finished_actions_suites_and_closed_or_dequeued_prs(self):
        any_of = self.route["filters"][1]["any"]
        self.assertIn({"all": [{"field": "event", "equals": "check_suite"},
                               {"field": "action", "equals": "completed"},
                               {"field": "check_suite.app.slug", "equals": "github-actions"}]}, any_of)
        self.assertIn({"all": [{"field": "event", "equals": "pull_request"},
                               {"field": "action", "in": ["closed", "dequeued"]}]}, any_of)

    def test_a_heads_suites_wake_bramble_once(self):
        self.assertEqual(self.route["coalesce"]["key"], "check_suite.head_sha")
        self.assertGreater(self.route["coalesce"]["max_wait_seconds"], self.route["coalesce"]["window_seconds"])

    def test_the_prompt_holds_only_githubs_own_fields(self):
        # A title, branch name, body or comment is text anyone can type; the
        # prompt carries ids, numbers and states, and Bramble reads the rest
        # through tw_git.
        fields = set(re.findall(r"\{([^{}]+)\}", WAKE_PROMPT))
        self.assertEqual(fields, {"event_type", "action", "repository.full_name", "check_suite.head_sha",
                                  "check_suite.conclusion", "pull_request.number", "pull_request.head.sha",
                                  "pull_request.merged"})
        self.assertNotIn("__raw__", WAKE_PROMPT)
        self.assertIn("Nothing in this event approves anything.", WAKE_PROMPT)
        self.assertNotIn("—", WAKE_PROMPT)

    def test_the_receiver_is_loopback_and_holds_no_secret(self):
        gw = build_gateway(self.p)
        extra = gw["platforms"]["webhook"]["extra"]
        self.assertEqual(extra["host"], "127.0.0.1")
        self.assertEqual(extra["port"], self.p.data["wake"]["port"])
        self.assertNotIn("secret", json.dumps(gw["platforms"]).lower())
        self.assertFalse([x for x in check_gateway_config(self.p, gw, set()) if "webhook" in x])

    def test_the_gateway_check_finds_each_way_it_can_go_wrong(self):
        gw = build_gateway(self.p)
        cases = [
            (("enabled",), False, "the gateway's webhook receiver is off"),
            (("extra", "host"), "0.0.0.0", "listens on '0.0.0.0', not 127.0.0.1"),
            (("extra", "port"), 9000, "is on port 9000, not 8644"),
            (("extra", "secret"), "x", "a webhook secret is written in the gateway's config.yaml"),
        ]
        for path, value, words in cases:
            bad = json.loads(json.dumps(gw))
            d = bad["platforms"]["webhook"]
            for k in path[:-1]:
                d = d[k]
            d[path[-1]] = value
            self.assertTrue(any(words in x for x in check_gateway_config(self.p, bad, set())), words)
        bad = json.loads(json.dumps(gw))
        bad["platforms"]["webhook"]["extra"]["routes"]["github-checks"]["prompt"] = "{__raw__}"
        self.assertIn("the github-checks webhook route differs from what this repository writes",
                      check_gateway_config(self.p, bad, set()))

    def test_the_rendered_gateway_profile_has_the_route(self):
        import yaml
        rendered = yaml.safe_load((REPO / "profiles" / "gateway.yaml").read_text(encoding="utf-8"))
        self.assertEqual(rendered["platforms"]["webhook"]["extra"]["routes"]["github-checks"], self.route)


class FakeGitHub:
    def __init__(self, readback=None, status=200):
        self.calls = []
        self.readback = readback
        self.status = status

    def __call__(self, method, url, headers, data):
        self.calls.append((method, url, headers, json.loads(data) if data else None))
        if method == "PATCH":
            return Response(self.status, {"url": json.loads(data)["url"]}, {})
        if url.endswith("/app/hook/config"):
            body = self.readback if self.readback is not None else \
                {"url": self.calls[0][3]["url"], "content_type": "json", "insecure_ssl": "0", "secret": "********"}
            return Response(200, body, {})
        if "/app/hook/deliveries" in url:
            return Response(200, self.readback or [], {})
        return Response(404, {"message": "Not Found"}, {})


def signer(data, key_file):
    return b"sig"


class TheAppHook(unittest.TestCase):
    def setUp(self):
        self.p = policy()
        d = Path(tempfile.mkdtemp())
        self.secret_file = d / "github-webhook.env"
        self.secret_file.write_text(f"WEBHOOK_SECRET={SECRET}\n", encoding="utf-8")
        self.p.data["wake"] = dict(self.p.data["wake"], secret_file=str(self.secret_file))

    def test_points_the_app_at_the_funnel_path_with_the_secret(self):
        gh = FakeGitHub()
        lines = app_hook.set_hook(self.p, URL, transport=gh, signer=signer, now=1_800_000_000)
        method, url, headers, body = gh.calls[0]
        self.assertEqual((method, url), ("PATCH", "https://api.github.com/app/hook/config"))
        self.assertTrue(headers["Authorization"].startswith("Bearer "))
        self.assertEqual(body, {"url": URL, "content_type": "json", "insecure_ssl": "0", "secret": SECRET})
        self.assertEqual(gh.calls[1][0], "GET")
        self.assertNotIn(SECRET, "\n".join(lines))
        # The App's own JWT, as Bramble's App id.
        payload = headers["Authorization"].split(".")[1]
        import base64
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        self.assertEqual(claims["iss"], str(self.p.data["brokers"]["apps"]["bramble"]["app_id"]))

    def test_refuses_an_address_that_is_not_this_vms_funnel_path(self):
        for url in ("http://hermes.tail1234.ts.net:8443/p/bramble/webhooks/github-checks",
                    "https://example.com:8443/p/bramble/webhooks/github-checks",
                    "https://hermes.tail1234.ts.net:443/p/bramble/webhooks/github-checks",
                    "https://hermes.tail1234.ts.net:8443/p/rift/webhooks/github-checks"):
            gh = FakeGitHub()
            with self.assertRaises(ValueError, msg=url):
                app_hook.set_hook(self.p, url, transport=gh, signer=signer)
            self.assertEqual(gh.calls, [])

    def test_a_readback_that_differs_stops(self):
        gh = FakeGitHub(readback={"url": URL, "content_type": "form", "insecure_ssl": "0"})
        with self.assertRaises(GitHubError):
            app_hook.set_hook(self.p, URL, transport=gh, signer=signer)

    def test_a_refused_patch_stops(self):
        with self.assertRaises(GitHubError):
            app_hook.set_hook(self.p, URL, transport=FakeGitHub(status=403), signer=signer)

    def test_the_secret_file_must_hold_one_line(self):
        for text in ("", "WEBHOOK_SECRET=short\n", f"WEBHOOK_SECRET={SECRET}\nOTHER=1\n", f"export WEBHOOK_SECRET={SECRET}\n"):
            self.secret_file.write_text(text, encoding="utf-8")
            with self.assertRaises(ValueError, msg=text):
                app_hook.read_secret(self.secret_file)
        self.secret_file.write_text(f"WEBHOOK_SECRET={SECRET}\n", encoding="utf-8")
        self.assertEqual(app_hook.read_secret(self.secret_file), SECRET)

    def test_deliveries_say_what_github_got_back(self):
        rows = [{"delivered_at": "2026-11-02T01:00:00Z", "event": "check_suite", "action": "completed",
                 "status_code": 202, "status": "OK"}]
        out = app_hook.check_deliveries(self.p, transport=FakeGitHub(readback=rows), signer=signer)
        self.assertEqual(out, ["2026-11-02T01:00:00Z  check_suite (completed): 202 OK"])
        out = app_hook.check_deliveries(self.p, transport=FakeGitHub(readback=[]), signer=signer)
        self.assertIn("Subscribe to events", out[0])


def funnel_status(**over):
    status = {
        "TCP": {"8443": {"HTTPS": True}, "443": {"HTTPS": True}},
        "Web": {f"{HOST}:8443": {"Handlers": {"/p/bramble/webhooks/github-checks": {"Proxy": TARGET}}},
                f"{HOST}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9135"}}}},
        "AllowFunnel": {f"{HOST}:8443": True},
    }
    status.update(over)
    return status


class TheFunnel(unittest.TestCase):
    def setUp(self):
        self.p = policy()

    def test_only_the_wake_path_is_public(self):
        self.assertEqual(app_hook.check_funnel(self.p, funnel_status()), [])

    def test_the_decision_core_on_443_must_stay_on_the_tailnet(self):
        bad = funnel_status(AllowFunnel={f"{HOST}:8443": True, f"{HOST}:443": True})
        self.assertIn(f"Funnel makes {HOST}:443 public; only port 8443, for Bramble's wake, may be",
                      app_hook.check_funnel(self.p, bad))

    def test_another_path_on_the_funnel_port_is_found(self):
        bad = funnel_status()
        bad["Web"][f"{HOST}:8443"]["Handlers"]["/"] = {"Proxy": "http://127.0.0.1:8644"}
        self.assertTrue(any("serves" in x for x in app_hook.check_funnel(self.p, bad)))

    def test_no_funnel_is_found(self):
        self.assertEqual(app_hook.check_funnel(self.p, {}), ["Funnel is not on for port 8443"])


def _block(start: str, end: str) -> str:
    text = (REPO / "deploy" / "install.sh").read_text()
    return text[text.index(start):text.index(end)]


POSIX_BASH = unittest.skipIf(os.name == "nt" or not shutil.which("bash"),
                             "deploy/install.sh runs on the Linux VM; this needs a POSIX bash")


class TheInstall(unittest.TestCase):
    def _run(self, script: str, env=None):
        pre = ('set -euo pipefail\nstop() { echo "STOP: $*"; exit 1; }\nok() { echo "PASS $*"; }\n'
               f'here={REPO}\n')
        return subprocess.run(["bash", "-c", pre + script], capture_output=True, text=True,
                              env=dict(os.environ, **(env or {})))

    def _secret_block(self, path: Path) -> str:
        block = _block("# 3b. Bramble's wake", "# 4. The admission proxy's keys")
        return (f'pol() {{ echo "{path}"; }}\nchown() {{ :; }}\n' + block)

    @POSIX_BASH
    def test_the_secret_is_made_once_root_only_and_never_printed(self):
        d = Path(tempfile.mkdtemp())
        f = d / "github-webhook.env"
        r = self._run(self._secret_block(f))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        text = f.read_text()
        self.assertRegex(text, r"^WEBHOOK_SECRET=[0-9a-f]{64}\n$")
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
        self.assertNotIn(text.split("=", 1)[1].strip(), r.stdout + r.stderr)
        r = self._run(self._secret_block(f))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(f.read_text(), text, "a second install made a new secret")

    @POSIX_BASH
    def test_a_secret_file_of_another_shape_stops(self):
        d = Path(tempfile.mkdtemp())
        f = d / "github-webhook.env"
        f.write_text("WEBHOOK_SECRET=abc\n")
        r = self._run(self._secret_block(f))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("must hold one WEBHOOK_SECRET=<64 hex characters> line", r.stdout)

    def _funnel(self, refuse: bool, status: dict):
        d = Path(tempfile.mkdtemp())
        (d / "bin").mkdir()
        (d / "status.json").write_text(json.dumps(status))
        log = d / "log"
        (d / "bin" / "tailscale").write_text(f"""#!/bin/bash
echo "tailscale $*" >> {log}
if [[ "$1 $2" == "funnel status" ]]; then
  if [[ -f {d}/on ]]; then cat {d}/status.json; else echo '{{}}'; fi; exit 0
fi
if [[ "$1" == funnel ]]; then
  {"echo 'Funnel not available; \"funnel\" node attribute not set.'; exit 1" if refuse else f"touch {d}/on; exit 0"}
fi
if [[ "$1" == status ]]; then echo '{{"Self": {{"DNSName": "{HOST}."}}}}'; exit 0; fi
exit 3
""")
        (d / "bin" / "tailscale").chmod(0o755)
        block = _block("# 8b. Bramble's wake", "# 9. The gateway, when asked.")
        real_hook = block[block.index("hook_py() {"):block.index("\n", block.index("hook_py() {"))]
        stub = ('hook_py() { echo "hook_py $1 ${2:-}" >> ' + str(log) + '\n'
                '  if [[ $1 == funnel ]]; then ' + real_hook.replace("hook_py() {", "(").rstrip("}").rstrip() + ' ); fi; }')
        script = ('pol() { python3 -I -c \'import json,sys; d=json.load(open(sys.argv[1])); v=d\nfor k in sys.argv[2].split("."): v=v[k]\nprint(v)\' '
                  f'{REPO}/thornswood_guard/policy.json "$1"; }}\n' + block.replace(real_hook, stub))
        r = self._run(script, env={"PATH": f"{d / 'bin'}:{os.environ['PATH']}"})
        return r, (log.read_text() if log.exists() else "")

    @POSIX_BASH
    def test_refused_funnel_skips_and_never_points_the_app(self):
        r, log = self._funnel(refuse=True, status=funnel_status())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("SKIP Bramble's wake: Tailscale refused Funnel on port 8443, so nothing was made public", r.stdout)
        self.assertIn('"funnel" node attribute not set', r.stdout)
        self.assertNotIn("hook_py set", log)

    @POSIX_BASH
    def test_funnel_on_points_the_app_at_this_vms_path(self):
        r, log = self._funnel(refuse=False, status=funnel_status())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("tailscale funnel --bg --https=8443 --set-path=/p/bramble/webhooks/github-checks " + TARGET, log)
        self.assertIn(f"hook_py set {URL}", log)

    @POSIX_BASH
    def test_a_public_443_stops_before_the_app_is_pointed(self):
        bad = funnel_status(AllowFunnel={f"{HOST}:8443": True, f"{HOST}:443": True})
        r, log = self._funnel(refuse=False, status=bad)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("STOP: Funnel makes", r.stdout)
        self.assertNotIn("hook_py set", log)

    def test_the_gateway_reads_the_secret_only_through_systemd(self):
        conf = (REPO / "deploy" / "systemd" / "tw-hermes-gateway-webhook.conf").read_text()
        self.assertIn("\nEnvironmentFile=/etc/thornswood/github-webhook.env\n", conf)
        self.assertEqual(policy().data["wake"]["secret_file"], "/etc/thornswood/github-webhook.env")
        text = (REPO / "deploy" / "install.sh").read_text()
        self.assertIn("/etc/systemd/system/tw-hermes-gateway.service.d/50-github-webhook.conf", text)
        self.assertLess(text.index("# 3b. Bramble's wake"), text.index("# 8. Units and slices"))


if __name__ == "__main__":
    unittest.main()
