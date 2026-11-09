"""The decision core's install stops every worker before the store migrates (1 Nov).

On 1 Nov at 10:53 PM the install ran this release's migrations while the
release before's connector was still running. Migration 11 had just put an
answer of Patrick's back in the queue; three seconds later that old connector
leased it, met the Bots' gateway mid-reload and failed it the old way, so the
answer was dead again. The first connection to the store migrates it, so
nothing of the older release may run from then until the core answers on the
new schema: the timers, every worker that leases from the core or opens its
store, and the core itself stop first, and each starts again only after the
core's schema check.

These tests read deploy/decisions/install.sh; nothing is installed.
"""

from __future__ import annotations

import re
import unittest

from helpers import REPO

DEPLOY = REPO / "deploy" / "decisions"
SCRIPT = DEPLOY / "install.sh"
CORE = "tw-decide.service"


def lines() -> list[str]:
    return SCRIPT.read_text(encoding="utf-8").splitlines()


def code(line: str) -> str:
    """The line without a trailing comment; a whole-line comment is empty."""
    return "" if line.lstrip().startswith("#") else line


def worker_units() -> set[str]:
    """Every unit of the core's that is not the core itself or the phone test's own."""
    return {p.name for p in DEPLOY.glob("tw-decide*")
            if p.suffix in (".service", ".timer") and p.name != CORE and "-test." not in p.name}


def cli_commands() -> set[str]:
    src = (REPO / "src" / "thornswood_decisions" / "cli.py").read_text(encoding="utf-8")
    return set(re.findall(r'sub\.add_parser\("([a-z-]+)"', src))


def first_store_touch() -> int:
    """The first line that opens the real store (and so migrates it): the `run` helper with a command."""
    pat = re.compile(r"(?:^|[\s|;&(])run (?:%s)\b" % "|".join(sorted(cli_commands())))
    for i, line in enumerate(lines()):
        if pat.search(code(line)):
            return i
    raise AssertionError("install.sh never opens the store with `run`")


def stops() -> dict[str, int]:
    """Each unit the script stops, and the first line that stops it. A $CORE_WORKERS
    reference is read as the units the variable names."""
    src = lines()
    var = {}
    for line in src:
        m = re.match(r'\s*CORE_WORKERS="([^"]*)"', line)
        if m:
            var["CORE_WORKERS"] = m.group(1).split()
    out: dict[str, int] = {}
    for i, line in enumerate(src):
        m = re.match(r"\s*systemctl stop (.+)$", code(line))
        if not m:
            continue
        for word in m.group(1).split():
            names = var.get(word.lstrip("$").strip("{}\"'"), [word]) if word.startswith("$") else [word]
            for n in names:
                out.setdefault(n, i)
    return out


def starts() -> list[tuple[int, str]]:
    """Each line that starts a unit: systemctl start, restart or enable --now."""
    out = []
    for i, line in enumerate(lines()):
        c = code(line)
        m = re.match(r"\s*(?:if\s+)?systemctl (?:start|restart|enable(?: --quiet)? --now) (.+?)(?:;.*)?$", c)
        if m:
            for word in m.group(1).split():
                if word.startswith("tw-decide"):
                    out.append((i, word))
    return out


class InstallOrder(unittest.TestCase):
    def test_every_worker_and_the_core_stop_before_the_store_is_opened(self):
        stopped = stops()
        touch = first_store_touch()
        for unit in sorted(worker_units() | {CORE}):
            self.assertIn(unit, stopped, f"install.sh never stops {unit} before it migrates the store")
            self.assertLess(stopped[unit], touch,
                            f"install.sh stops {unit} on line {stopped[unit] + 1}, after it opens the store "
                            f"on line {touch + 1}")

    def test_the_timers_stop_before_their_services(self):
        stopped = stops()
        for timer in sorted(u for u in worker_units() if u.endswith(".timer")):
            service = timer.removesuffix(".timer") + ".service"
            self.assertLess(stopped[timer], stopped[service], f"{timer} could start {service} again")

    def test_the_timers_that_post_to_the_core_stop_before_it_restarts_and_start_after_it_answers(self):
        # 1 Nov, 10:57 PM: the feed timer posted the new 'work' snapshot, with 'items', to the release
        # before's core 38 seconds before that core restarted, and the old core dropped 'items'.
        stopped = stops()
        core_start = next(i for i, u in starts() if u == CORE)
        check = next(i for i, line in enumerate(lines()) if 'd["store"] == d["code"]' in code(line))
        timers = sorted(u for u in worker_units() if u.endswith(".timer"))
        self.assertIn("tw-decide-feed.timer", timers)
        self.assertIn("tw-decide-peers.timer", timers)
        started = {u: i for i, u in starts()}
        for timer in timers:
            self.assertLess(stopped[timer], core_start, f"{timer} still runs when the core restarts")
            self.assertIn(timer, started, f"install.sh never starts {timer} again")
            self.assertGreater(started[timer], check, f"{timer} starts before the core answers on the new code")

    def test_the_install_checks_each_unit_really_stopped(self):
        self.assertTrue(any('if systemctl is-active --quiet "$u"; then stop ' in code(line) for line in lines()),
                        "install.sh does not check that each unit it stopped really stopped")

    def test_the_core_answers_on_the_new_schema_before_any_worker_starts(self):
        src = lines()
        core_start = next(i for i, u in starts() if u == CORE)
        schema = next(i for i, line in enumerate(src) if re.search(r"\brun schema\b", code(line)))
        check = next(i for i, line in enumerate(src) if 'd["store"] == d["code"]' in code(line))
        self.assertLess(core_start, schema)
        self.assertLess(schema, check)
        workers = [(i, u) for i, u in starts() if u != CORE]
        self.assertTrue(workers)
        for i, unit in workers:
            self.assertGreater(i, check, f"install.sh starts {unit} on line {i + 1}, before the core "
                                         f"answers on the new schema (line {check + 1})")
        # And nothing starts between the stop and the core's own start.
        first_stop = min(stops().values())
        for i, unit in starts():
            self.assertFalse(first_stop < i < core_start, f"{unit} starts on line {i + 1}, while the store migrates")

    def test_every_worker_this_install_runs_is_started_again(self):
        started = {u for _, u in starts()}
        # Each service a timer starts comes back with its timer.
        timed = {u.removesuffix(".timer") + ".service" for u in worker_units() if u.endswith(".timer")}
        for unit in sorted(worker_units() - timed):
            self.assertIn(unit, started, f"install.sh stops {unit} but never starts it again")


if __name__ == "__main__":
    unittest.main()
