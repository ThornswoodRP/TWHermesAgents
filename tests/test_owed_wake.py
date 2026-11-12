"""Bramble's owed-wake check (src/thornswood_guard/owed_wake.py and the script
Hermes's cron runs, deploy/hermes-scripts/tw-owed-wake.py)."""

import json
from datetime import datetime, timezone
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
        due = {"owed": True, "due": False, "why_not": "bramble started 50 turns in the last hour; each Bot starts at most 50 an hour", "since": "t"}
        out = owed_wake.check(self.home(), "bramble", Broker(due))
        self.assertFalse(wakes("\n".join(out)))
        self.assertIn("50 turns", out[0])

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
NOON = AFTER_SIX + 5 * 3600


class Idle:
    """tw-policy with no wake owed, and tw-git's issue search: claimed maps
    a claim label to its open issues, open maps a search's terms to a count
    by the first matching key."""

    def __init__(self, room=True, claimed=None, open_=None, pulls=None):
        self.room, self.claimed, self.open = room, claimed or {}, open_ or {}
        self.pulls = pulls or []
        self.cards = []
        self.turns = {}
        self.queued = {}
        self.asked = []

    def __call__(self, broker, op, args):
        self.asked.append((broker, op, args))
        if op == "wake_due":
            return {"owed": False, "due": False}
        if op == "my_cards":
            return self.cards
        if op == "bot_turns":
            return {"bots": self.turns}
        if op == "queued_for":
            return {"bots": self.queued}
        if op == "turn_room":
            return {"room": self.room, "why_not": "" if self.room else "bramble started 50 turns in the last hour; each Bot starts at most 50 an hour"}
        if op == "read":
            import re
            import urllib.parse
            if "/pulls?" in args["path"]:
                return {"body": self.pulls}
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(args["path"]).query)["q"][0]
            # The search's one positive label: term, any of its labels (GitHub's comma).
            term = re.search(r'(?:^|\s)label:((?:"[^"]+",?)+)', q)
            wanted = re.findall(r'"([^"]+)"', term.group(1)) if term else []
            if wanted and all(w.startswith("claimed: ") for w in wanted):
                items = [{"number": 100 * (1 + list(self.claimed).index(label)) + i, "labels": [{"name": label}]}
                         for label, n in self.claimed.items() if label in wanted for i in range(n)]
                return {"body": {"total_count": len(items), "items": items}}
            if wanted:
                items, total = [], 0
                for key, n in self.open.items():
                    areas = [a for a in re.findall(r'"(area: [^"]+)"', key) if a in wanted]
                    if areas and not key.startswith("-"):
                        total += n
                        items += [{"number": 9000 + len(items), "labels": [{"name": areas[0]}]} for _ in range(n)]
                return {"body": {"total_count": total, "items": items[:100]}}
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

    def look(self, home, b, now):
        """The check at now, and when that run was the alert look, the next one
        five minutes on, which is the free worker look's."""
        out = owed_wake.check(home, "bramble", b, now=now, policy=POLICY)
        if "runs at the next check" in "\n".join(out):
            out = owed_wake.check(home, "bramble", b, now=now + 300, policy=POLICY)
        return out

    def day(self):
        """A home whose morning sweep ran today, and whose alert look ran at noon."""
        home = self.home()
        owed_wake._save(home.joinpath(*owed_wake.IDLE_FILE), {"morning": "2026-11-09", "alert_checked": NOON})
        return home

    def test_the_first_check_after_six_mountain_wakes_once_that_day(self):
        home, b = self.home(), Idle(claimed=BUSY)
        out = owed_wake.check(home, "bramble", b, now=AFTER_SIX, policy=POLICY)
        self.assertTrue(wakes("\n".join(out)))
        self.assertIn("morning sweep", out[-1])
        self.assertIn(("policy", "turn_room", {"bot": "bramble", "sweep": True}), b.asked)
        again = owed_wake.check(home, "bramble", b, now=AFTER_SIX + 300, policy=POLICY)
        self.assertFalse(wakes("\n".join(again)), "the next check is the alert look, and nothing is new")
        self.assertIn("runs at the next check", again[-2], "the free worker look never shares a run with it")
        self.assertFalse([a for a in b.reads()[-3:] if "/pulls?" in a["path"]], "no free worker read in that run")
        again = owed_wake.check(home, "bramble", b, now=AFTER_SIX + 600, policy=POLICY)
        self.assertFalse(wakes("\n".join(again)), "the next check is the free worker one, and all are busy")
        self.assertIn("every worker is on an active issue", again[-2])
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
        out = owed_wake.check(self.day(), "bramble", b, now=NOON, policy=POLICY)
        self.assertTrue(wakes("\n".join(out)))
        self.assertIn("sprocket", out[-1])
        search = [urllib_q(a) for a in b.reads() if "/search/" in a["path"]][-1]
        for term in ('-label:"claimed: Bramble"', '-label:"claimed: Wyrd"', "-author:WiccanDjinn",
                     "-assignee:Jhinswife", "repo:ThornswoodRP/Thornswood "):
            self.assertIn(term, search)

    def test_wisp_is_busy_with_a_claim_in_the_fork(self):
        claimed = dict(BUSY, **{"claimed: Wisp": 0})
        b = Idle(claimed=claimed, open_={'"area: Gamemode"': 5})

        def ask(broker, op, args):
            if op == "read" and "/search/issues" in args["path"] and "ThornswoodRP/skymp" in args["path"].replace("%2F", "/"):
                b.asked.append((broker, op, args))
                return {"body": {"total_count": 1, "items": [{"number": 7, "labels": [{"name": "claimed: Wisp"}]}]}}
            return b(broker, op, args)
        out = owed_wake.check(self.day(), "bramble", ask, now=NOON, policy=POLICY)
        self.assertFalse(wakes("\n".join(out)))
        self.assertIn("every worker is on an active issue", out[-2])

    def test_a_free_worker_with_only_issues_of_the_part_they_name_wakes(self):
        claimed = dict(BUSY, **{"claimed: Echo": 0})
        b = Idle(claimed=claimed, open_={'-label:"area: Release"': 7})
        out = owed_wake.check(self.day(), "bramble", b, now=NOON, policy=POLICY)
        self.assertTrue(wakes("\n".join(out)))
        self.assertIn("picked by the part", out[-1])

    def test_a_free_worker_with_nothing_sleeps_and_is_looked_at_again_an_hour_later(self):
        home = self.day()
        claimed = dict(BUSY, **{"claimed: Rift": 0})
        out = owed_wake.check(home, "bramble", Idle(claimed=claimed), now=NOON, policy=POLICY)
        self.assertFalse(wakes("\n".join(out)))
        self.assertIn("no open unclaimed issue", out[-2])
        b = Idle(claimed=claimed)
        out = owed_wake.check(home, "bramble", b, now=NOON + 600, policy=POLICY)
        self.assertFalse(wakes("\n".join(out)))
        self.assertEqual(b.reads(), [], "no GitHub read until the hour is up")
        b = Idle(claimed=claimed, open_={'"area: Mods","area: Load order"': 1})
        out = self.look(home, b, NOON + owed_wake.IDLE_EVERY)
        self.assertTrue(wakes("\n".join(out)))

    def test_tw_git_down_sleeps(self):
        def ask(broker, op, args):
            if broker == "git":
                raise BrokerRefused("the broker at /run/tw/git.sock is not answering")
            return Idle()(broker, op, args)
        out = owed_wake.check(self.day(), "bramble", ask, now=NOON, policy=POLICY)
        self.assertFalse(wakes("\n".join(out)))
        self.assertIn("tw-git did not answer", out[-2])

    def test_no_free_worker_check_from_11_pm_to_6_am(self):
        # Patrick, 9 Nov 2026: the workers start new work only from 6 AM to 11 PM Mountain.
        for t in (BEFORE_SIX, AFTER_SIX + 16 * 3600):  # 3:30 AM, 11:30 PM
            with self.subTest(t=t):
                b = Idle(claimed=dict(BUSY, **{"claimed: Echo": 0}), open_={'"area: Website"': 4})
                out = owed_wake.check(self.day(), "bramble", b, now=t, policy=POLICY)
                self.assertFalse(wakes("\n".join(out)))
                self.assertIn("no worker starts new work", out[-2])
                paths = [a["path"] for a in b.reads()]
                self.assertFalse([p for p in paths if "/pulls?" in p or "area%3A" in p], paths)

    def test_bramble_is_alert_at_any_hour_for_its_own_work(self):
        # Patrick, 9 Nov 2026, 1:27 AM: "Bramble should still be working and always be working and alert".
        home = self.home()
        items = {"Thornswood-Ops": [{"number": 261, "updated_at": "2026-11-09T08:00:00Z"}]}

        def ask(broker, op, args):
            if op == "read" and "/search/issues" in args["path"]:
                repo = args["repo"].split("/")[-1]
                q = urllib_q(args)
                if repo == "Thornswood-Ops" and '-label:"claimed: Bramble"' in q:
                    return {"body": {"total_count": len(items[repo]), "items": items[repo]}}
                return {"body": {"total_count": 0, "items": []}}
            return Idle()(broker, op, args)
        out = owed_wake.check(home, "bramble", ask, now=BEFORE_SIX, policy=POLICY)
        self.assertTrue(wakes("\n".join(out)), out)
        self.assertIn("Thornswood-Ops#261", out[-1])
        later = owed_wake.check(home, "bramble", ask, now=BEFORE_SIX + owed_wake.ALERT_EVERY, policy=POLICY)
        self.assertFalse(wakes("\n".join(later)), "nothing changed, so no turn")
        self.assertIn("nothing of your own is new", "\n".join(later))
        soon = owed_wake.check(home, "bramble", ask, now=BEFORE_SIX + owed_wake.ALERT_EVERY + 60, policy=POLICY)
        self.assertFalse(wakes("\n".join(soon)))
        items["Thornswood-Ops"] = [{"number": 261, "updated_at": "2026-11-09T09:00:00Z"},
                                   {"number": 262, "updated_at": "2026-11-09T09:05:00Z"}]
        changed = owed_wake.check(home, "bramble", ask, now=BEFORE_SIX + 2 * owed_wake.ALERT_EVERY, policy=POLICY)
        self.assertTrue(wakes("\n".join(changed)))
        self.assertIn("Thornswood-Ops#262", changed[-1])

    def test_a_worker_whose_issues_all_wait_on_a_pull_request_is_free(self):
        # Patrick, 9 Nov 2026: waiting on a check, merge, release, update or him is no reason to sit idle.
        claimed = dict(BUSY, **{"claimed: Echo": 2})
        echo = 100 * (1 + list(claimed).index("claimed: Echo"))
        pulls = [{"head": {"ref": f"echo/{echo}-site"}, "state": "open"},
                 {"head": {"ref": f"echo/{echo + 1}-feed"}, "state": "closed", "merged_at": "2026-11-09T01:00:00Z"}]
        b = Idle(claimed=claimed, open_={'"area: Website","area: Discord","area: Voice"': 2}, pulls=pulls)
        out = owed_wake.check(self.day(), "bramble", b, now=NOON, policy=POLICY)
        self.assertTrue(wakes("\n".join(out)))
        self.assertIn("echo", out[-1])
        pulls[1] = {"head": {"ref": f"echo/{echo + 1}-feed"}, "state": "closed", "merged_at": None}
        out = owed_wake.check(self.day(), "bramble", Idle(claimed=claimed, pulls=pulls), now=NOON, policy=POLICY)
        self.assertFalse(wakes("\n".join(out)), "a closed, unmerged pull request leaves its issue active")

    def test_a_worker_waiting_on_a_decision_is_free(self):
        # Patrick, 9 Nov 2026: a decision is waiting too, so the worker goes on to its next issue.
        claimed = dict(BUSY, **{"claimed: Echo": 1})
        echo = 100 * (1 + list(claimed).index("claimed: Echo"))
        b = Idle(claimed=claimed, open_={'"area: Website","area: Discord","area: Voice"': 2})
        out = owed_wake.check(self.day(), "bramble", b, now=NOON, policy=POLICY)
        self.assertFalse(wakes("\n".join(out)), "its issue is active")
        b.cards = [{"status": "pending", "task": f"ThornswoodRP/Thornswood#{echo}"}]
        out = owed_wake.check(self.day(), "bramble", b, now=NOON, policy=POLICY)
        self.assertTrue(wakes("\n".join(out)), out)
        self.assertIn("echo", out[-1])
        b.cards = [{"status": "answered", "task": f"ThornswoodRP/Thornswood#{echo}"}]
        out = owed_wake.check(self.day(), "bramble", b, now=NOON, policy=POLICY)
        self.assertFalse(wakes("\n".join(out)), "an answered card leaves the issue active")

    def test_a_worker_that_stopped_mid_issue_is_said_at_every_look_until_it_runs(self):
        # 9 Nov 2026: Sprocket's turn was turned away at 9:23 AM on #4329, its
        # claim read as active work, and nothing woke it again. At 12:59 PM
        # the line naming it was printed once and kept as told; the queue
        # turned that turn away, and the 2:00 PM look stayed quiet.
        home = self.day()
        b = Idle(claimed=BUSY)
        an_hour_ago = datetime.fromtimestamp(NOON - owed_wake.IDLE_EVERY - 60, timezone.utc).isoformat()
        just_now = datetime.fromtimestamp(NOON - 120, timezone.utc).isoformat()
        b.turns = {"sprocket": {"running": False, "last": an_hour_ago},
                   "rift": {"running": True, "last": an_hour_ago},
                   "echo": {"running": False, "last": just_now},
                   "wisp": {"running": False, "waiting": True, "last": an_hour_ago},
                   "magpie": {"running": False, "last": None}}
        out = owed_wake.check(home, "bramble", b, now=NOON, policy=POLICY)
        self.assertTrue(wakes("\n".join(out)), out)
        self.assertIn("echo, magpie, sprocket hold an active issue", out[-1],
                      "a turn that ended two minutes ago is as stopped as one that ended an hour ago")
        self.assertIn("items 5 and 6", out[-1])
        self.assertIn("take the claim off and give it its next issue", out[-1])
        for w in ("rift", "wisp"):
            self.assertNotIn(w, out[-1], "running, or in line, is not stopped")
        later = NOON + owed_wake.IDLE_EVERY + 1
        out = self.look(home, b, later)
        later += 300
        self.assertTrue(wakes("\n".join(out)), "a stop that is still there is said again an hour later")
        self.assertIn("echo, magpie, sprocket", out[-1])
        self.assertNotIn("stopped", owed_wake._load(home.joinpath(*owed_wake.IDLE_FILE)),
                         "nothing is kept as told")
        b.turns["sprocket"] = {"running": False, "last": datetime.fromtimestamp(later + owed_wake.IDLE_EVERY, timezone.utc).isoformat()}
        b.queued = {"sprocket": 1}
        b.turns["magpie"] = {"running": True, "last": None}
        b.turns["echo"] = {"running": True, "last": None}
        out = self.look(home, b, later + owed_wake.IDLE_EVERY)
        self.assertFalse(wakes("\n".join(out)), "once each ran a turn or has one queued, nothing is said")

    def test_a_hand_back_runs_the_look_at_the_next_check_not_on_the_hour(self):
        # 11 Nov 2026: Rift handed back #4980 blocked at 4:00 PM and Magpie
        # #5765 at 3:59 PM. The 4:52 PM look counted both busy, quiet just
        # under an hour, and the next look was an hour away: four of the five
        # workers sat with no turn from 4 PM while Wisp alone worked.
        home = self.day()
        # The alert look last ran at 12:15 PM, so none falls in these checks.
        owed_wake._save(home.joinpath(*owed_wake.IDLE_FILE),
                        {"morning": "2026-11-09", "alert_checked": NOON + 900, "idle_checked": NOON})
        b = Idle(claimed=BUSY)
        busy = {w: {"running": True, "last": None} for w in ("rift", "sprocket", "echo", "magpie", "wisp")}
        b.turns = dict(busy)
        out = owed_wake.check(home, "bramble", b, now=NOON + 300, policy=POLICY)
        self.assertFalse(wakes("\n".join(out)), out)
        self.assertEqual(b.reads(), [], "every worker has a turn: no GitHub read")
        handed = datetime.fromtimestamp(NOON + 400, timezone.utc).isoformat()
        b.turns["rift"] = {"running": False, "last": handed}
        b.turns["bramble"] = {"running": True, "last": handed}
        out = owed_wake.check(home, "bramble", b, now=NOON + 600, policy=POLICY)
        self.assertFalse(wakes("\n".join(out)), "Bramble is answering the hand-back; the look waits for that turn")
        self.assertIn("rift ended a turn", "\n".join(out))
        self.assertEqual(b.reads(), [])
        b.turns["bramble"] = {"running": False, "last": handed}
        b.queued = {"bramble": 1}
        out = owed_wake.check(home, "bramble", b, now=NOON + 660, policy=POLICY)
        self.assertFalse(wakes("\n".join(out)), "a hand-back queued for Bramble is still to be answered")
        b.queued = {}
        out = owed_wake.check(home, "bramble", b, now=NOON + 900, policy=POLICY)
        self.assertTrue(wakes("\n".join(out)), out)
        self.assertIn("rift hold an active issue but have no turn running", out[-1])
        self.assertTrue(b.reads(), "the look ran, a quarter hour after the last one")
        n = len(b.reads())
        b.turns["bramble"] = {"running": False, "last": handed,
                              "sweep_done": datetime.fromtimestamp(NOON + 905, timezone.utc).isoformat()}
        out = owed_wake.check(home, "bramble", b, now=NOON + 1200, policy=POLICY)
        self.assertFalse(wakes("\n".join(out)), "the hand-back was looked at; the next look is the hourly one")
        self.assertEqual(len(b.reads()), n)
        b.turns["rift"] = {"running": False, "last": handed}
        b.queued = {"rift": 1}
        b.turns["echo"] = {"running": False, "last": datetime.fromtimestamp(NOON + 1300, timezone.utc).isoformat()}
        b.queued["echo"] = 1
        out = owed_wake.check(home, "bramble", b, now=NOON + 1500, policy=POLICY)
        self.assertFalse(wakes("\n".join(out)), "a worker whose next brief is queued has its turn on the way")
        self.assertEqual(len(b.reads()), n)

    def test_a_look_whose_sweep_never_ran_looks_again_and_its_lines_ride_the_next_wake(self):
        # 11 Nov 2026: the 6:31 PM look named Rift and Magpie stopped and
        # Sprocket, Echo and Wisp free. Its sweep was turned away (another was
        # running), a GitHub sweep paid the wake it owed without its lines, the
        # 6:44 PM sweep carried only an Ops issue, and no worker got work.
        home = self.day()
        owed_wake._save(home.joinpath(*owed_wake.IDLE_FILE),
                        {"morning": "2026-11-09", "alert_checked": NOON + 60})
        claimed = dict(BUSY, **{"claimed: Sprocket": 0})
        b = Idle(claimed=claimed, open_={'"area: Launcher","area: Packaging"': 3})
        out = owed_wake.check(home, "bramble", b, now=NOON, policy=POLICY)
        self.assertTrue(wakes("\n".join(out)), out)
        self.assertIn("in the field of sprocket", out[-1])
        # The sweep it woke was turned away: no ticket. GitHub sweeps ran.
        later = datetime.fromtimestamp(NOON + 200, timezone.utc).isoformat()
        b.turns = {"bramble": {"running": False, "last": later, "sweep_done": None}}
        b.asked.clear()
        due = {"owed": True, "due": True, "since": "t", "refused": 1, "reason": "another sweep is running"}

        def owed(broker, op, args):
            return due if op == "wake_due" else b(broker, op, args)
        out = owed_wake.check(home, "bramble", owed, now=NOON + 240, policy=POLICY)
        self.assertTrue(wakes("\n".join(out)), out)
        self.assertIn("turned away", out[0])
        # The owed wake runs the look too (12 Nov 2026: a wake owed all
        # evening kept every look from running), fresh, so its lines ride it.
        self.assertIn("no sweep carried its lines", "\n".join(out), "the owed sweep carries the look's lines")
        self.assertIn("in the field of sprocket", out[-1])
        self.assertEqual(sum("in the field of sprocket" in l for l in out), 1, "said once, not carried twice")
        # A GitHub sweep paid the owed wake: nothing is owed, and the look runs again.
        out = owed_wake.check(home, "bramble", b, now=NOON + 300, policy=POLICY)
        self.assertTrue(wakes("\n".join(out)), out)
        self.assertIn("no sweep carried its lines", "\n".join(out))
        self.assertIn("in the field of sprocket", out[-1])
        self.assertTrue(b.reads(), "a fresh look")
        # While Bramble runs a turn, which may be that sweep, nothing runs.
        b.asked.clear()
        b.turns["bramble"] = {"running": True, "last": later, "sweep_done": None}
        out = owed_wake.check(home, "bramble", b, now=NOON + 600, policy=POLICY)
        self.assertFalse(wakes("\n".join(out)), out)
        self.assertEqual(b.reads(), [])
        # The sweep ran to its end: its lines are gone, and the next look is the hourly one.
        b.turns["bramble"] = {"running": False, "last": later,
                              "sweep_done": datetime.fromtimestamp(NOON + 305, timezone.utc).isoformat()}
        out = owed_wake.check(home, "bramble", b, now=NOON + 900, policy=POLICY)
        self.assertFalse(wakes("\n".join(out)), out)
        self.assertNotIn("said_at", owed_wake._load(home.joinpath(*owed_wake.IDLE_FILE)))
        out = owed_wake.check(home, "bramble", b, now=NOON + 1200, policy=POLICY)
        self.assertFalse(wakes("\n".join(out)), out)
        self.assertEqual([a for a in b.reads() if "/pulls?" in a["path"]], [])

    def test_a_look_that_finds_nothing_leaves_nothing_to_carry(self):
        home = self.day()
        out = owed_wake.check(home, "bramble", Idle(claimed=BUSY), now=NOON, policy=POLICY)
        self.assertFalse(wakes("\n".join(out)), out)
        self.assertNotIn("said_at", owed_wake._load(home.joinpath(*owed_wake.IDLE_FILE)))

    def test_a_free_worker_after_a_hand_back_gets_its_next_issue_at_the_next_check(self):
        home = self.day()
        owed_wake._save(home.joinpath(*owed_wake.IDLE_FILE),
                        {"morning": "2026-11-09", "alert_checked": NOON, "idle_checked": NOON})
        claimed = dict(BUSY, **{"claimed: Sprocket": 0})
        b = Idle(claimed=claimed, open_={'"area: Launcher","area: Packaging"': 3})
        b.turns = {"sprocket": {"running": False, "last": datetime.fromtimestamp(NOON + 60, timezone.utc).isoformat()}}
        out = owed_wake.check(home, "bramble", b, now=NOON + 300, policy=POLICY)
        self.assertTrue(wakes("\n".join(out)), out)
        self.assertIn("in the field of sprocket", out[-1])
        self.assertIn("not on the hour", "\n".join(out))

    def test_a_stop_and_a_free_worker_are_both_said(self):
        claimed = dict(BUSY, **{"claimed: Wisp": 0})
        b = Idle(claimed=claimed, open_={'"area: Gamemode","area: Engine","area: In-game"': 181})
        b.turns = {"sprocket": {"running": False, "last": None}}
        out = owed_wake.check(self.day(), "bramble", b, now=NOON, policy=POLICY)
        self.assertTrue(wakes("\n".join(out)), out)
        self.assertIn("sprocket hold an active issue", out[-2])
        self.assertIn("in the field of wisp", out[-1])

    def test_every_free_worker_with_work_in_its_field_is_named(self):
        # 9 Nov 2026, 2:00 PM: the line named Rift's field alone, and Wisp got nothing.
        claimed = dict(BUSY, **{"claimed: Rift": 0, "claimed: Wisp": 0})
        b = Idle(claimed=claimed, open_={'"area: Mods","area: Load order"': 2,
                                         '"area: Gamemode","area: Engine","area: In-game"': 181})
        out = owed_wake.check(self.day(), "bramble", b, now=NOON, policy=POLICY)
        self.assertTrue(wakes("\n".join(out)), out)
        self.assertIn("in the field of rift, wisp", out[-1])

    def test_a_thornswood_issue_whose_pull_request_is_in_the_fork_waits(self):
        # 10 Nov 2026: Wisp's #4513 was fixed by fork #148 (merged 5:49 AM), and
        # the check, reading Thornswood's pull requests alone, held Wisp as
        # working it from 9:44 AM while 179 issues were open in its field.
        claimed = dict(BUSY, **{"claimed: Wisp": 1})
        wisp = 100 * (1 + list(claimed).index("claimed: Wisp"))
        b = Idle(claimed=claimed, open_={'"area: Gamemode","area: Engine","area: In-game"': 179})

        def ask(broker, op, args):
            if op == "read" and "/pulls?" in args["path"]:
                b.asked.append((broker, op, args))
                if args["repo"] == POLICY["repos"]["fork"]:
                    return {"body": [{"head": {"ref": f"wisp/{wisp}-created-forms"}, "state": "closed",
                                      "merged_at": "2026-11-10T12:49:58Z", "created_at": "2026-11-10T04:00:00Z"}]}
                return {"body": []}
            if op == "read" and "/search/issues" in args["path"] and args["repo"] == POLICY["repos"]["fork"]:
                b.asked.append((broker, op, args))
                return {"body": {"total_count": 0, "items": []}}
            return b(broker, op, args)
        out = owed_wake.check(self.day(), "bramble", ask, now=NOON, policy=POLICY)
        self.assertTrue(wakes("\n".join(out)), out)
        self.assertIn("in the field of wisp", out[-1])

    def test_a_claim_the_worker_moved_on_from_is_parked_and_the_worker_is_free(self):
        # 10 Nov 2026: Rift's #4511 (last changed 2:26 AM UTC, waiting on game
        # files nobody had read) held Rift as busy while it opened pull
        # requests for #4509 and #4534, so from 10:33 AM Mountain it got nothing new.
        claimed = dict(BUSY, **{"claimed: Rift": 1})
        rift = 100 * (1 + list(claimed).index("claimed: Rift"))
        pulls = [{"head": {"ref": "rift/4534-current-revision"}, "state": "closed",
                  "merged_at": "2026-11-10T17:33:31Z", "created_at": "2026-11-10T17:08:57Z"}]
        changed = {"at": "2026-11-10T02:26:14Z"}
        b = Idle(claimed=claimed, open_={'"area: Mods","area: Load order"': 61}, pulls=pulls)

        def ask(broker, op, args):
            r = b(broker, op, args)
            if op == "read" and "/search/issues" in args["path"]:
                for i in r["body"].get("items") or []:
                    if {"name": "claimed: Rift"} in i.get("labels", []):
                        i["updated_at"] = changed["at"]
            return r
        out = owed_wake.check(self.day(), "bramble", ask, now=NOON, policy=POLICY)
        self.assertTrue(wakes("\n".join(out)), out)
        self.assertIn(f"rift #{rift}", out[-2])
        self.assertIn("take the claim off", out[-2])
        self.assertIn("in the field of rift", out[-1])
        # Claimed after its last pull request: that is the issue it is on now.
        changed["at"] = "2026-11-10T17:54:06Z"
        out = owed_wake.check(self.day(), "bramble", ask, now=NOON, policy=POLICY)
        self.assertFalse(wakes("\n".join(out)), out)
        self.assertIn("every worker is on an active issue", out[-2])

    def test_a_free_worker_look_sends_few_searches(self):
        # 10 Nov 2026: one search per worker's claims and one per free worker's
        # field, after the alert look's three, passed GitHub's secondary rate
        # limit at every hourly look from 11:59 AM (a 403 on the tenth search
        # in seven seconds), so no worker was given an issue from 12:27 PM.
        claimed = {f"claimed: {n}": 0 for n in ("Rift", "Sprocket", "Echo", "Magpie", "Wisp")}
        b = Idle(claimed=claimed, open_={'"area: Mods","area: Load order"': 61,
                                         '"area: Gamemode","area: Engine","area: In-game"': 179,
                                         '"area: Website","area: Discord","area: Voice"': 4})
        out = owed_wake.check(self.day(), "bramble", b, now=NOON, policy=POLICY)
        self.assertTrue(wakes("\n".join(out)), out)
        self.assertIn("in the field of rift, wisp", out[-1])
        self.assertIn("maybe of sprocket, echo, magpie, past the first 100", out[-1],
                      "a field the first page did not reach is still named for Bramble's pick")
        self.assertIn("pick for each free worker", out[-1])
        searches = [a for a in b.reads() if "/search/" in a["path"]]
        self.assertLessEqual(len(searches), 3, [urllib_q(a) for a in searches])

    def test_an_owed_wake_runs_the_free_worker_look_too(self):
        # 12 Nov 2026: a wake was owed nearly all evening (GitHub's wakes
        # turned away while another sweep ran). The check returned before the
        # look, no look ran from 7:32 PM Mountain on, and Rift, Sprocket and
        # Echo sat with no issue from 9:15 PM.
        claimed = dict(BUSY, **{"claimed: Sprocket": 0})
        b = Idle(claimed=claimed, open_={'"area: Launcher","area: Packaging"': 3})
        due = {"owed": True, "due": True, "since": "t", "refused": 7, "reason": "another sweep is running"}

        def owed(broker, op, args):
            return due if op == "wake_due" else b(broker, op, args)
        out = owed_wake.check(self.day(), "bramble", owed, now=NOON, policy=POLICY)
        self.assertTrue(wakes("\n".join(out)), out)
        self.assertIn("turned away", out[0])
        self.assertIn("in the field of sprocket", out[-1])
        # With every worker busy the owed wake still wakes, with its own line only.
        b2 = Idle(claimed=BUSY)

        def owed2(broker, op, args):
            return due if op == "wake_due" else b2(broker, op, args)
        out = owed_wake.check(self.day(), "bramble", owed2, now=NOON, policy=POLICY)
        self.assertTrue(wakes("\n".join(out)), out)
        self.assertEqual(len(out), 1, out)

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
