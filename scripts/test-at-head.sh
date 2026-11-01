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
# runtime (3.14 on 31 Oct) is first on PATH there and has no PyYAML, so a bare
# `python` failed the suite on One for a reason that had nothing to do with
# the commit. Name the right one in TWH_ONE_PYTHON; the run stops before any
# status is set if the Python it would use can't load PyYAML.
one_py="${TWH_ONE_PYTHON:-python}"
if ! one_py_is="$("$one_py" -c 'import sys, yaml; print(sys.executable, sys.version.split()[0])' 2>&1)"; then
  echo "STOP: '$one_py' can't run the suite on One: ${one_py_is##*$'\n'}"
  echo "Set TWH_ONE_PYTHON to the One Python that has PyYAML (README, Running the tests). No status was set."
  exit 1
fi
echo "One runs the suite with $one_py_is."

git -C "$here" fetch --quiet origin
git -C "$here" cat-file -e "$commit^{commit}"

scratch="$(mktemp -d)"
remote_dir="/tmp/twh-test-$commit"
remote_bundle="/tmp/twh-test-$commit.bundle"
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
scp -q "$scratch/t.bundle" "$host:$remote_bundle"
sent=1
vm_out="$(ssh "$host" "chmod 0644 $remote_bundle && sudo -n -u hermes bash -c 'git init --quiet $remote_dir && git -C $remote_dir fetch --quiet $remote_bundle +$ref:$ref && git -C $remote_dir -c advice.detachedHead=false checkout --quiet --detach $commit && cd $remote_dir && python3 -m unittest discover -s tests 2>&1'")" && vm_ok=1 || vm_ok=0
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
