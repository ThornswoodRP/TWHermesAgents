"""A stand-in for the hermes command in apply.py's tests: only
`hermes -p <bot> cron list --all|create|edit`, printed as Hermes 0.22.5
prints them (hermes_cli/cron.py cron_list). It keeps the jobs in the JSON
file named by FAKE_HERMES_STATE, logs each call beside it, and fails every
call when the state file holds {"broken": true}."""

import json
import os
import sys
import uuid
from pathlib import Path

state_file = Path(os.environ["FAKE_HERMES_STATE"])
state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
args = sys.argv[1:]
with open(state_file.with_suffix(".log"), "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\n")
if state.get("broken"):
    print("Error: the cron store is not writable", file=sys.stderr)
    sys.exit(1)
if args[:1] != ["-p"] or args[2:3] != ["cron"]:
    sys.exit(2)
profile, verb, rest = args[1], args[3], args[4:]
jobs = state.setdefault("jobs", {}).setdefault(profile, [])


def options(items):
    out, i = {}, 0
    while i < len(items):
        if items[i].startswith("--"):
            out.setdefault(items[i][2:], []).append(items[i + 1])
            i += 2
        else:
            out.setdefault("_", []).append(items[i])
            i += 1
    return out


if verb == "list":
    if not jobs:
        print(f"No scheduled jobs in profile '{profile}'.")
    for job in jobs:
        print(f"  {job['id']} [{job.get('state', 'active')}]")
        print(f"    {'Name:':<11}{job['name']}")
        print(f"    {'Schedule:':<11}{job['schedule']}")
        print(f"    {'Script:':<11}{job.get('script', '')}")
        print()
    sys.exit(0)
o = options(rest)
if verb == "create":
    schedule, prompt = o["_"]
    jobs.append({"id": uuid.uuid4().hex[:12], "name": o["name"][0], "schedule": schedule, "prompt": prompt,
                 "skills": o.get("skill", []), "script": o["script"][0], "deliver": o["deliver"][0]})
elif verb == "edit":
    job = next(j for j in jobs if j["id"] == o["_"][0])
    job.update({"name": o["name"][0], "schedule": o["schedule"][0], "prompt": o["prompt"][0],
                "skills": o.get("skill", []), "script": o["script"][0], "deliver": o["deliver"][0]})
else:
    sys.exit(2)
state_file.write_text(json.dumps(state), encoding="utf-8")
