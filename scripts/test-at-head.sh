#!/usr/bin/env bash
# Run the guard rail tests on One and on the Oracle VM at one exact commit,
# and record the result on GitHub as the commit status that Patrick's merge
# rule reads. This repo's private Actions minutes are held at $0, so GitHub's
# own runners never start; a commit status costs nothing.
#
# Runs on One, from a clone of Thornswood-Hermes, by Hermes on One (or the
# Desk) after every push to a PR:
#
#   scripts/test-at-head.sh <full commit>
#
# It sets the status "Guard rail tests (One and VM)" on that commit: success
# only when both suites pass, failure otherwise, with the counts. It never
# re-runs to get a pass.
set -euo pipefail

commit="${1:?usage: test-at-head.sh <full commit>}"
host="thornswood-bots"
repo="ThornswoodRP/Thornswood-Hermes"
context="Guard rail tests (One and VM)"
here="$(cd "$(dirname "$0")/.." && pwd)"

if [[ ! "$commit" =~ ^[0-9a-f]{40}$ ]]; then
  echo "STOP: give the full 40-character commit, not '$commit'."; exit 1
fi

# The Python on One that runs the suite. One has more than one: Hermes's own
# runtime (3.14 on 31 Oct) is first on PATH there and has none of the suite's
# libraries, so a bare `python` failed the suite on One for a reason that had
# nothing to do with the commit. Name the right one in TWH_ONE_PYTHON. The run
# stops before any status is set unless that Python holds every library CI
# installs by hash for this commit (tests/requirements-ci.txt and
# deploy/decisions/requirements.txt), at the pinned version: on 8 Nov a Python
# with PyYAML alone failed at the decision core's tzdata.
one_py="${TWH_ONE_PYTHON:-python}"
git -C "$here" fetch --quiet origin
git -C "$here" cat-file -e "$commit^{commit}"
pins="$(for f in tests/requirements-ci.txt deploy/decisions/requirements.txt; do git -C "$here" show "$commit:$f" 2>/dev/null || true; done)"
if ! one_py_is="$("$one_py" -I -c '
import sys, re
from importlib import metadata
from importlib.metadata import PackageNotFoundError
missing = []
for line in sys.stdin.read().splitlines():
    m = re.match(r"([A-Za-z0-9_.-]+)==([^ ;\\]+)\s*(?:;\s*python_version\s*<\s*\"([0-9.]+)\")?", line)
    if not m:
        continue
    name, want, below = m.groups()
    if below and sys.version_info[:2] >= tuple(int(x) for x in below.split(".")):
        continue
    try:
        have = metadata.version(name)
    except PackageNotFoundError:
        have = "none"
    if have != want:
        missing.append(f"{name}=={want} (has {have})")
if missing:
    sys.exit("it lacks " + ", ".join(missing))
print(sys.executable, sys.version.split()[0])
' <<<"$pins" 2>&1)"; then
  echo "STOP: '$one_py' can't run the suite on One: ${one_py_is##*$'\n'}"
  echo "Install the pinned libraries into it (README, Running the tests) or name another in TWH_ONE_PYTHON. No status was set."
  exit 1
fi
echo "One runs the suite with $one_py_is."

scratch="$(mktemp -d)"
# The VM half runs in a folder of its own, made for this run, never one named
# by the commit: on 8 Nov two runs of the same commit shared
# /tmp/twh-test-<commit>, and the first to finish deleted the other's tree
# under its running suite (536 errors, brokers that never said ready). Runs
# on the VM also take turns on one lock, so two never share its ports and
# sockets. The folder name is set once the VM has made it.
remote_dir=""
remote_bundle=""
sent=""
cleanup() {
  if [[ -d "$scratch/tree" ]]; then git -C "$here" worktree remove --force "$scratch/tree"; fi
  rm -rf "$scratch"
  if [[ -n "$sent" ]]; then
    ssh "$host" "sudo -n -u hermes rm -rf $remote_dir; rm -f $remote_bundle"
  fi
}
trap cleanup EXIT

status() {
  gh api -X POST "repos/$repo/statuses/$commit" -f state="$1" -f context="$context" -f description="$2" >/dev/null
  echo "$context: $1 ($2)"
}

# One
git -C "$here" worktree add --quiet --detach "$scratch/tree" "$commit"
one_out="$(cd "$scratch/tree" && "$one_py" -m unittest discover -s tests 2>&1)" && one_ok=1 || one_ok=0
one_ran="$(grep -oE '^Ran [0-9]+ tests' <<<"$one_out" | tail -1)"

# The VM
# A bundle carries refs, not bare commits: name the commit for the moment.
ref="refs/twh-test/$commit"
git -C "$here" update-ref "$ref" "$commit"
git -C "$here" bundle create --quiet "$scratch/t.bundle" "$ref"
git -C "$here" update-ref -d "$ref"
remote_dir="$(ssh "$host" "sudo -n -u hermes mktemp -d /tmp/twh-test-XXXXXXXX")"
if [[ ! "$remote_dir" =~ ^/tmp/twh-test-[A-Za-z0-9]{8}$ ]]; then
  echo "STOP: the VM did not make a run folder (it said '$remote_dir'). No status was set."; exit 1
fi
remote_bundle="$remote_dir.bundle"
sent=1
scp -q "$scratch/t.bundle" "$host:$remote_bundle"
vm_out="$(ssh "$host" "chmod 0644 $remote_bundle && sudo -n -u hermes flock -w 3600 /tmp/twh-test.lock bash -c 'git init --quiet $remote_dir && git -C $remote_dir fetch --quiet $remote_bundle +$ref:$ref && git -C $remote_dir -c advice.detachedHead=false checkout --quiet --detach $commit && cd $remote_dir && python3 -m unittest discover -s tests 2>&1'")" && vm_ok=1 || vm_ok=0
vm_ran="$(grep -oE '^Ran [0-9]+ tests' <<<"$vm_out" | tail -1)"

echo "One: $([[ $one_ok == 1 ]] && echo pass || echo FAIL), $one_ran. VM: $([[ $vm_ok == 1 ]] && echo pass || echo FAIL), $vm_ran."
if [[ $one_ok == 1 && $vm_ok == 1 ]]; then
  status success "${one_ran#Ran } passed on One and ${vm_ran#Ran } on the VM"
else
  [[ $one_ok == 1 ]] || { echo "--- One ---"; tail -40 <<<"$one_out"; }
  [[ $vm_ok == 1 ]] || { echo "--- VM ---"; tail -40 <<<"$vm_out"; }
  status failure "One: $([[ $one_ok == 1 ]] && echo pass || echo FAIL). VM: $([[ $vm_ok == 1 ]] && echo pass || echo FAIL)"
  exit 1
fi
