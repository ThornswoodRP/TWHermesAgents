"""Bramble's owed-wake check (src/thornswood_guard/owed_wake.py and the script
Hermes's cron runs, deploy/hermes-scripts/tw-owed-wake.py)."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import REPO

from thornswood_guard import owed_wake
from thornswood_guard.brokers import OWED_FILE, BrokerRefused


def wakes(output: str) -> bool:
    """Hermes 0.23.0's wake gate (cron/scheduler_prompt.py _parse_wake_gate):
    the agent is skipped only when the last non-empty line is the JSON
    {"wakeAgent": false}."""
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        return True
    try:
        gate = json.loads(lines[-1].strip())
    except ValueError:
        return True
    return not isinstance(gate, dict) or gate.get("wakeAgent", True) is not False


class Broker:
    def __init__(self, due=None, down=False):
        self.due = due or {"owed": False, "due": False}
        self.down = down
        self.asked = []

    def __call__(self, broker, op, args):
        self.asked.append((broker, op, args))
        if self.down:
            raise BrokerRefused("the broker at /run/tw/policy.sock is not answering")
        return self.due if op == "wake_due" else {"owed": True}


class Check(unittest.TestCase):
    def home(self):
        return Path(tempfile.mkdtemp())

    def test_nothing_owed_sleeps(self):
        out = owed_wake.check(self.home(), "bramble", Broker())
        self.assertFalse(wakes("\n".join(out)))

    def test_owed_but_no_room_sleeps_and_says_why(self):
        due = {"owed": True, "due": False, "why_not": "20 tickets were made in the last hour", "since": "t"}
        out = owed_wake.check(self.home(), "bramble", Broker(due))
        self.assertFalse(wakes("\n".join(out)))
        self.assertIn("20 tickets", out[0])

    def test_owed_with_room_wakes(self):
        due = {"owed": True, "due": True, "why_not": "", "since": "t", "refused": 3, "reason": "r"}
        out = owed_wake.check(self.home(), "bramble", Broker(due))
        self.assertTrue(wakes("\n".join(out)))
        self.assertIn("3 wake(s)", out[0])

    def test_tw_policy_down_sleeps_and_keeps_the_outbox(self):
        home = self.home()
        box = home.joinpath(*OWED_FILE)
        box.parent.mkdir(parents=True)
        box.write_text("r\n", encoding="utf-8")
        out = owed_wake.check(home, "bramble", Broker(down=True))
        self.assertFalse(wakes("\n".join(out)))
        self.assertTrue(box.exists())

    def test_the_outbox_is_handed_over_first(self):
        home = self.home()
        box = home.joinpath(*OWED_FILE)
        box.parent.mkdir(parents=True)
        box.write_text("tw-policy was not answering\n", encoding="utf-8")
        b = Broker({"owed": True, "due": True, "since": "t", "refused": 1, "reason": "x"})
        owed_wake.check(home, "bramble", b)
        self.assertEqual([op for _, op, _ in b.asked], ["owe_wake", "wake_due"])
        self.assertEqual(b.asked[0][2], {"bot": "bramble", "reason": "tw-policy was not answering"})
        self.assertFalse(box.exists())

    def test_the_prompt_is_a_whole_sweep_and_carries_the_mark(self):
        self.assertIn(owed_wake.OWED_WAKE_MARK, owed_wake.PROMPT)
        self.assertIn("bramble-duties", owed_wake.PROMPT)
        self.assertIn("approves anything", owed_wake.PROMPT)
        self.assertEqual((owed_wake.SKILL, owed_wake.SCRIPT), ("bramble-duties", "tw-owed-wake.py"))
        self.assertTrue((REPO / "hermes" / "skills" / owed_wake.SKILL / "SKILL.md").is_file())


POLICY = json.loads((REPO / "src" / "thornswood_guard" / "policy.json").read_text(encoding="utf-8"))
# 9 Nov 2026, 7:30 AM and 3:30 AM Mountain (UTC-7).
AFTER_SIX = 1794234600.0
BEFORE_SIX = AFTER_SIX - 4 * 3600


class Idle:
    """tw-policy with no wake owed, and tw-git's issue search: claimed maps
    a claim label to its open issues, open maps a search's terms to a count
    by the first matching key."""

    def __init__(self, room=True, claimed=None, open_=None):
        self.room, self.claimed, self.open = room, claimed or {}, open_ or {}
        self.asked = []

    def __call__(self, broker, op, args):
        self.asked.append((broker, op, args))
        if op == "wake_due":
            return {"owed": False, "due": False}
        if op == "turn_room":
            return {"room": self.room, "why_not": "" if self.room else "20 tickets were made in the last hour"}
        if op == "read":
            import urllib.parse
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(args["path"]).query)["q"][0]
            for label, n in self.claimed.items():
                if q.endswith(f'label:"{label}"'):
                    return {"body": {"total_count": n}}
            for key, n in self.open.items():
                if key in q:
                    return {"body": {"total_count": n}}
            return {"body": {"total_count": 0}}
        raise AssertionError(op)

    def reads(self):
        return [a for _, op, a in self.asked if op == "read"]


BUSY = {f"claimed: {n}": 1 for n in ("Rift", "Sprocket", "Echo", "Magpie", "Wisp")}


class MorningAndFreeWorkers(unittest.TestCase):
    """TWHermesAgents #47: until 9 Nov 2026 a sweep ran only on a GitHub
    event, so a quiet board was never picked from and the lanes' comments
    waited for an unrelated pull request."""

    def home(self):
        return Path(tempfile.mkdtemp())

    def test_the_first_check_after_six_mountain_wakes_once_that_day(self):
        home, b = self.home(), Idle(claimed=BUSY)
        out = owed_wake.check(home, "bramble", b, now=AFTER_SIX, policy=POLICY)
        self.assertTrue(wakes("\n".join(out)))
        self.assertIn("morning sweep", out[-1])
        self.assertIn(("policy", "turn_room", {"bot": "bramble", "sweep": True}), b.asked)
        again = owed_wake.check(home, "bramble", b, now=AFTER_SIX + 300, policy=POLICY)
        self.assertFalse(wakes("\n".join(again)), "the next check is the free worker one, and all are busy")
        self.assertIn("every worker holds", again[-2])
        tomorrow = owed_wake.check(home, "bramble", Idle(claimed=BUSY), now=AFTER_SIX + 86400, policy=POLICY)
        self.assertTrue(wakes("\n".join(tomorrow)))

    def test_no_morning_wake_before_six(self):
        out = owed_wake.check(self.home(), "bramble", Idle(claimed=BUSY), now=BEFORE_SIX, policy=POLICY)
        self.assertFalse(wakes("\n".join(out)))

    def test_no_room_waits_and_keeps_the_morning_owed(self):
        home = self.home()
        out = owed_wake.check(home, "bramble", Idle(room=False), now=AFTER_SIX, policy=POLICY)
        self.assertFalse(wakes("\n".join(out)))
        self.assertIn("no room", out[-2])
        out = owed_wake.check(home, "bramble", Idle(claimed=BUSY), now=AFTER_SIX + 300, policy=POLICY)
        self.assertTrue(wakes("\n".join(out)))

    def test_a_free_worker_with_an_issue_in_its_area_wakes(self):
        claimed = dict(BUSY, **{"claimed: Sprocket": 0})
        b = Idle(claimed=claimed, open_={'"area: Launcher","area: Packaging"': 3})
        out = owed_wake.check(self.home(), "bramble", b, now=BEFORE_SIX, policy=POLICY)
        self.assertTrue(wakes("\n".join(out)))
        self.assertIn("sprocket", out[-1])
        search = [urllib_q(a) for a in b.reads()][-1]
        for term in ('-label:"claimed: Bramble"', '-label:"claimed: Wyrd"', "-author:WiccanDjinn",
                     "-assignee:Jhinswife", "repo:ThornswoodRP/Thornswood "):
            self.assertIn(term, search)

    def test_wisp_is_busy_with_a_claim_in_the_fork(self):
        claimed = dict(BUSY, **{"claimed: Wisp": 0})
        b = Idle(claimed=claimed, open_={'"area: Gamemode"': 5})

        def ask(broker, op, args):
            if op == "read" and "ThornswoodRP/skymp" in args["path"].replace("%2F", "/"):
                b.asked.append((broker, op, args))
                return {"body": {"total_count": 1}}
            return b(broker, op, args)
        out = owed_wake.check(self.home(), "bramble", ask, now=BEFORE_SIX, policy=POLICY)
        self.assertFalse(wakes("\n".join(out)))
        self.assertIn("every worker holds", out[-2])

    def test_a_free_worker_with_only_issues_of_the_part_they_name_wakes(self):
        claimed = dict(BUSY, **{"claimed: Echo": 0})
        b = Idle(claimed=claimed, open_={'-label:"area: Release"': 7})
        out = owed_wake.check(self.home(), "bramble", b, now=BEFORE_SIX, policy=POLICY)
        self.assertTrue(wakes("\n".join(out)))
        self.assertIn("picked by the part", out[-1])

    def test_a_free_worker_with_nothing_sleeps_and_is_looked_at_again_an_hour_later(self):
        home = self.home()
        claimed = dict(BUSY, **{"claimed: Rift": 0})
        out = owed_wake.check(home, "bramble", Idle(claimed=claimed), now=BEFORE_SIX, policy=POLICY)
        self.assertFalse(wakes("\n".join(out)))
        self.assertIn("no open unclaimed issue", out[-2])
        b = Idle(claimed=claimed)
        out = owed_wake.check(home, "bramble", b, now=BEFORE_SIX + 600, policy=POLICY)
        self.assertFalse(wakes("\n".join(out)))
        self.assertEqual(b.reads(), [], "no GitHub read until the hour is up")
        b = Idle(claimed=claimed, open_={'"area: Mods","area: Load order"': 1})
        out = owed_wake.check(home, "bramble", b, now=BEFORE_SIX + owed_wake.IDLE_EVERY, policy=POLICY)
        self.assertTrue(wakes("\n".join(out)))

    def test_tw_git_down_sleeps(self):
        def ask(broker, op, args):
            if broker == "git":
                raise BrokerRefused("the broker at /run/tw/git.sock is not answering")
            return Idle()(broker, op, args)
        out = owed_wake.check(self.home(), "bramble", ask, now=BEFORE_SIX, policy=POLICY)
        self.assertFalse(wakes("\n".join(out)))
        self.assertIn("tw-git did not answer", out[-2])

    def test_an_owed_wake_comes_first(self):
        due = {"owed": True, "due": True, "since": "t", "refused": 1, "reason": "r"}
        out = owed_wake.check(self.home(), "bramble", Broker(due), now=AFTER_SIX, policy=POLICY)
        self.assertTrue(wakes("\n".join(out)))
        self.assertIn("turned away", out[0])


def urllib_q(args):
    import urllib.parse
    return urllib.parse.parse_qs(urllib.parse.urlsplit(args["path"]).query)["q"][0]


class Script(unittest.TestCase):
    """The script as Hermes runs it: from <profile>/scripts/, with the guard
    in <profile>/plugins/. Hermes runs the agent on a failed script, so it
    must never fail."""

    def profile(self, with_guard=True):
        pdir = Path(tempfile.mkdtemp())
        (pdir / "scripts").mkdir()
        shutil.copy(REPO / "deploy" / "hermes-scripts" / "tw-owed-wake.py", pdir / "scripts")
        if with_guard:
            shutil.copytree(REPO / "src" / "thornswood_guard", pdir / "plugins" / "thornswood_guard",
                            ignore=shutil.ignore_patterns("__pycache__"))
            # The brokers' sockets point into this profile, where none
            # listens: on the VM the real tw-policy answers at its own path
            # (3 Nov: "no wake is owed" where "did not answer" was meant).
            pol = pdir / "plugins" / "thornswood_guard" / "policy.json"
            data = json.loads(pol.read_text(encoding="utf-8"))
            for name, sock in data["brokers"]["sockets"].items():
                sock["path"] = str(pdir / "no-broker" / f"{name}.sock")
            pol.write_text(json.dumps(data), encoding="utf-8")
        return pdir

    def run_script(self, pdir):
        return subprocess.run([sys.executable, "-I", str(pdir / "scripts" / "tw-owed-wake.py")],
                              capture_output=True, text=True, timeout=60, cwd=str(pdir))

    @unittest.skipUnless(sys.platform.startswith("linux"), "the brokers' Unix sockets are the VM's")
    def test_with_no_broker_socket_it_sleeps_and_exits_0(self):
        r = self.run_script(self.profile())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(wakes(r.stdout), r.stdout)
        self.assertIn("did not answer", r.stdout)

    def test_with_no_guard_it_sleeps_and_exits_0(self):
        r = self.run_script(self.profile(with_guard=False))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(wakes(r.stdout), r.stdout)
        self.assertIn("could not load the guard", r.stdout)

    def test_a_policy_that_will_not_load_sleeps_and_exits_0(self):
        pdir = self.profile()
        (pdir / "plugins" / "thornswood_guard" / "policy.json").write_text("{", encoding="utf-8")
        r = self.run_script(pdir)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(wakes(r.stdout), r.stdout)
        self.assertIn("could not run", r.stdout)


if __name__ == "__main__":
    unittest.main()
