"""An install's or a reload's own restart never counts toward a unit's start limit.

The brokers, the gateway and the decision core's workers allow 3 starts in 15
minutes (StartLimitBurst), so a crash loop stops. On 2 Nov at 10:06 PM four
installs inside 15 minutes hit that limit: tw-policy was refused, tw-git,
tw-files and tw-proxy never started, and the put-back's own restart was refused
too. A deliberate restart clears the count first (systemctl reset-failed); a
unit that then fails by itself still meets the limit.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UNIT_DIRS = (REPO / "deploy" / "systemd", REPO / "deploy" / "decisions")
SCRIPTS = (REPO / "deploy" / "install.sh", REPO / "deploy" / "decisions" / "install.sh")


def limited_units() -> set[str]:
    """Every unit file here that sets a start limit, by the name systemd knows it."""
    names = set()
    for d in UNIT_DIRS:
        for f in d.iterdir():
            if f.suffix not in (".service", ".in") or not f.is_file():
                continue
            if re.search(r"^StartLimitBurst=", f.read_text(encoding="utf-8"), re.M):
                names.add(f.name.removesuffix(".in"))
    return names


class StartLimit(unittest.TestCase):
    def test_the_limit_is_still_there(self):
        # The fix is not to drop the crash-loop limit.
        for unit in ("tw-policy.service", "tw-git.service", "tw-files.service", "tw-proxy.service",
                     "tw-hermes-gateway.service"):
            self.assertIn(unit, limited_units())

    def test_every_restart_of_a_limited_unit_clears_its_count_first(self):
        units = limited_units()
        for script in SCRIPTS:
            lines = script.read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines):
                m = re.match(r"\s*systemctl (?:restart|start) (.+)$", line)
                if not m:
                    continue
                named = [u for u in m.group(1).split() if u in units]
                if not named:
                    continue
                before = lines[i - 1] if i else ""
                for unit in named:
                    self.assertRegex(before, rf"^\s*systemctl reset-failed .*\b{re.escape(unit)}\b",
                                     f"{script.name}:{i + 1} restarts {unit} without systemctl reset-failed "
                                     f"on the line before it")

    def test_the_reload_clears_the_gateways_count_before_it_starts_it(self):
        src = (REPO / "thornswood_brokers" / "reload.py").read_text(encoding="utf-8")
        self.assertLess(src.index('run(["systemctl", "reset-failed", GATEWAY])'),
                        src.index('run(["systemctl", "start", GATEWAY])'))


if __name__ == "__main__":
    unittest.main()
