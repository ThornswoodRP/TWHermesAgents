"""Bramble's owed-wake check, run by Hermes's cron as the job tw-owed-wake.

Installed into <profile>/scripts/ by scripts/apply.py. The check itself is
thornswood_guard/owed_wake.py, from the guard installed in this profile. It
never fails: Hermes runs the agent on a failed script, so even a guard that
cannot be imported ends in {"wakeAgent": false}.
"""
import sys
from pathlib import Path

try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins"))
    from thornswood_guard.owed_wake import main
except Exception as e:
    print(f"the owed-wake check could not load the guard ({type(e).__name__}: {e})")
    print('{"wakeAgent": false}')
    sys.exit(0)
sys.exit(main())
