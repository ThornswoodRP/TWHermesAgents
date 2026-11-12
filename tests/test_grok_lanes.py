"""The Grok lanes' computer on the VM (Thornswood #5933, Patrick's answer A on
q-5933-lanes-fallback, 12 Nov 2026).

When Grok Bot's cloud usage runs out, the lanes do their read-only audits on
a "local computer". That was One; now it is xAI's Grok Bot for Linux on the
VM, as the grok-lanes account: no shell, no sudo, no group of the brokers or
the Bots, none of their folders, no /home, a virtual screen nobody but its
two units reaches, and a sign-in screen on the Tailscale address alone that
closes by itself. These tests hold the units, the scripts and the install to
that, and to the policy.
"""

import sys as _sys
import unittest as _unittest

if not _sys.platform.startswith("linux"):
    raise _unittest.SkipTest("the brokers run only on the Oracle VM; their tests run there")

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import REPO, policy

UNITS = REPO / "deploy" / "systemd"
LANES = REPO / "deploy" / "grok-lanes"
INSTALL = (REPO / "deploy" / "install.sh").read_text()


def unit(name: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in (UNITS / name).read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            out.setdefault(k, []).append(v)
    return out


def g() -> dict:
    return policy().data["grok_lanes"]


class TheUnits(unittest.TestCase):
    def test_both_run_as_grok_lanes_in_its_slice_with_its_home(self):
        for name in ("tw-grok-lanes.service", "tw-grok-screen.service"):
            u = unit(name)
            self.assertEqual(u["User"], [g()["user"]], name)
            self.assertEqual(u["Group"], [g()["user"]], name)
            self.assertEqual(u["Slice"], ["tw-grok.slice"], name)
            self.assertEqual(u["Environment"], [f"HOME={g()['home']}"], name)
            self.assertEqual(u["ReadWritePaths"], [g()["home"]], name)

    def test_the_app_and_the_screen_are_the_policys(self):
        app = unit("tw-grok-lanes.service")["ExecStart"][0]
        self.assertEqual(app, f'/opt/thornswood-hermes/current/deploy/grok-lanes/run-app {g()["display"]} "{g()["app"]}"')
        screen = unit("tw-grok-screen.service")
        self.assertEqual(screen["ExecStart"],
                         [f'/opt/thornswood-hermes/current/deploy/grok-lanes/screen {g()["display"]} {g()["screen_port"]}'])
        self.assertEqual(screen["RuntimeMaxSec"], [f'{g()["screen_minutes"]}min'])

    def test_neither_can_gain_a_privilege_or_reach_home(self):
        for name in ("tw-grok-lanes.service", "tw-grok-screen.service"):
            u = unit(name)
            for k, v in (("NoNewPrivileges", "yes"), ("ProtectSystem", "strict"), ("ProtectHome", "yes"),
                         ("PrivateTmp", "yes"), ("PrivateDevices", "yes"), ("RestrictSUIDSGID", "yes"),
                         ("CapabilityBoundingSet", ""), ("AmbientCapabilities", "")):
                self.assertEqual(u.get(k), [v], f"{name} {k}")
            self.assertNotIn("AF_PACKET", u["RestrictAddressFamilies"][0])

    def test_every_thornswood_folder_but_its_own_is_hidden(self):
        # Every /srv/tw folder the repository names, now and later, and the
        # places that hold keys: the brokers' App keys, the follower's state,
        # the Drive sign-in.
        named = set()
        for top in ("deploy", "src", "scripts"):
            for f in (REPO / top).rglob("*"):
                if f.is_file() and f.suffix not in (".pyc",):
                    try:
                        text = f.read_text()
                    except UnicodeDecodeError:
                        continue
                    named |= {m.group(0) for m in re.finditer(r"/srv/tw/[A-Za-z0-9_-]+", text)}
        named.discard(g()["home"])
        self.assertIn("/srv/tw/work", named)
        for name in ("tw-grok-lanes.service", "tw-grok-screen.service"):
            hidden = {p.lstrip("-") for p in unit(name)["InaccessiblePaths"][0].split()}
            self.assertEqual(named - hidden, set(), f"{name} can see these")
            for p in ("/etc/thornswood", "/var/lib/thornswood", "/var/lib/tw-offsite", "/root"):
                self.assertIn(p, hidden, name)

    def test_the_screen_shares_the_apps_tmp_and_never_starts_at_boot(self):
        s = unit("tw-grok-screen.service")
        self.assertEqual(s["JoinsNamespaceOf"], ["tw-grok-lanes.service"])
        # Wants, not Requires: the app's own restarts never take the screen down.
        self.assertEqual(s["Wants"], ["tw-grok-lanes.service"])
        self.assertNotIn("Requires", s)
        self.assertNotIn("BindsTo", s)
        self.assertNotIn("WantedBy", s)
        self.assertEqual(s["RestrictNamespaces"], ["yes"])
        self.assertEqual(unit("tw-grok-lanes.service")["WantedBy"], ["multi-user.target"])

    def test_the_app_comes_back_after_it_ends_itself(self):
        # 12 Nov: Patrick's sign-in restarted Grok Bot, it exited 0, and
        # Restart=on-failure left it and its screen stopped.
        u = unit("tw-grok-lanes.service")
        self.assertEqual(u["Restart"], ["always"])
        self.assertEqual(u["RestartSec"], ["5s"])

    def test_the_install_enables_the_app_and_never_the_screen(self):
        flat = INSTALL.replace("\\\n", " ")
        self.assertIn("systemctl enable --quiet tw-grok-lanes.service", flat)
        self.assertNotRegex(flat, r"systemctl enable[^\n]*tw-grok-screen")


class TheScripts(unittest.TestCase):
    def test_the_virtual_screen_listens_on_no_port_and_no_abstract_socket(self):
        text = (LANES / "run-app").read_text()
        self.assertIn("-nolisten tcp -nolisten local", text)
        self.assertIn('-f "$XAUTHORITY"', text)
        self.assertIn('export BROWSER="$here/open-url"', text)

    def test_the_screen_is_a_unix_socket_served_on_tailscale_alone(self):
        text = (LANES / "screen").read_text()
        self.assertIn("-rfbport 0 -unixsock", text)
        self.assertIn("-loop", text)
        self.assertIn('websockify --web "$web" --unix-target "$sock" "$ts:$port"', text)
        self.assertIn('wait -n "$vnc" "$ws"', text)
        self.assertIn(r"^100\.", text)
        self.assertNotIn("0.0.0.0", text)
        self.assertNotIn("-rfbport 59", text)

    def test_the_pinned_key_is_the_one_in_the_repository(self):
        if not shutil.which("gpg"):
            self.skipTest("gpg is not here")
        with tempfile.TemporaryDirectory() as home:
            r = subprocess.run(["gpg", "--homedir", home, "--show-keys", "--with-colons", str(REPO / g()["apt_key"])],
                               capture_output=True, text=True)
        fprs = [l.split(":")[9] for l in r.stdout.splitlines() if l.startswith("fpr:")]
        self.assertEqual(fprs[0], g()["apt_key_fingerprint"])
        self.assertTrue(g()["apt_uri"].startswith("https://"))


class OpenUrl(unittest.TestCase):
    def run_it(self, url):
        home = tempfile.mkdtemp()
        r = subprocess.run(["bash", str(LANES / "open-url"), url], capture_output=True, text=True,
                           env={**os.environ, "HOME": home})
        page = Path(home) / "web" / "sign-in.html"
        return r, (page.read_text() if page.exists() else None), page

    def test_an_https_link_goes_on_the_page(self):
        url = "https://accounts.example.com/loginDeepControl?challenge=abc&uuid=1-2&mode=login"
        r, page, path = self.run_it(url)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('href="https://accounts.example.com/loginDeepControl?challenge=abc&amp;uuid=1-2&amp;mode=login"', page)
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_a_link_cannot_write_markup(self):
        r, page, _ = self.run_it('https://x.example/a?b="><script>alert(1)</script>')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_anything_but_one_https_link_is_refused(self):
        for bad in ("http://x.example/", "javascript:alert(1)", "file:///etc/passwd", "https://x.example/a b", "",
                    "https://user@x.example/"):
            r, page, _ = self.run_it(bad)
            self.assertNotEqual(r.returncode, 0, bad)
            self.assertIsNone(page, bad)


class TheInstall(unittest.TestCase):
    def section(self, start, end):
        a = INSTALL.index(start)
        return INSTALL[a:INSTALL.index(end, a)]

    def test_the_account_has_no_shell_and_no_group_but_its_own(self):
        s = self.section("# 6c.", "# 7.")
        self.assertIn('useradd --system --home-dir "$grok_home" --no-create-home --shell /usr/sbin/nologin --user-group "$grok_user"', s)
        self.assertIn('[[ "$(id -nG "$grok_user")" == "$grok_user" ]] || stop', s)
        self.assertIn('install -d -m 0700 -o "$grok_user" -g "$grok_user" "$grok_home"', s)
        self.assertNotIn("usermod", s)
        self.assertNotIn("sudoers", s)

    def test_a_key_that_is_not_the_pinned_one_stops_and_apt_trouble_does_not(self):
        s = self.section("# 6c.", "# 7.")
        self.assertIn('[[ "$got_fpr" == "$grok_fpr" ]] || stop', s)
        apt = s[s.index("apt-get update"):s.index("useradd")]
        self.assertNotIn("stop ", apt)
        self.assertIn('grok_problem="apt could not', apt)

    def test_the_lanes_account_has_a_disk_cap_that_is_kept(self):
        s = self.section("if quotas_on; then", "SKIP disk caps")
        self.assertIn('setquota -g "$grok_user" 0 "$(( $(pol grok_lanes.disk_quota_gb) * 1024 * 1024 ))" 0 0 "$srv_mnt"', s)
        self.assertIn('keep+=" $(getent group "$grok_user" | cut -d: -f3)"', s)

    def test_the_app_starts_again_only_when_what_it_runs_changed(self):
        s = self.section("# 8a.", "# 8b.")
        self.assertIn('if [[ "$grok_runs" != "$grok_ran" ]] || ! systemctl is-active --quiet tw-grok-lanes.service; then', s)
        self.assertIn('"$here/deploy/grok-lanes/run-app" "$here/deploy/grok-lanes/open-url"', s)
        self.assertIn("pol grok_lanes.screen_open", s)
        self.assertIn("systemctl stop tw-grok-screen.service", s)

    def test_the_status_file_holds_only_its_fields(self):
        s = self.section("# 8a.", "# 8b.")
        m = re.search(r"json\.dump\(\{([^}]*)\}", s)
        self.assertEqual(re.findall(r'"(\w+)":', m.group(1)), ["ready", "screen", "version", "runs", "problem"])


if __name__ == "__main__":
    unittest.main()
