#!/usr/bin/env bash
# Send the commit Patrick merged to the Oracle VM and apply it there.
#
# Runs on One, from a clone of Thornswood-Hermes, by Hermes on One after
# Patrick merges:
#
#   scripts/send-to-oracle.sh <full commit of main> [--check]
#
# The VM holds no GitHub credential (Patrick's decision 4), so the VM never
# pulls: One fetches main, checks the commit is main's tip, bundles it, copies
# it over Tailscale (ssh thornswood-bots), and the VM's hermes user unpacks it
# into its own release folder and runs scripts/apply.py --expect <commit>.
set -euo pipefail

commit="${1:?usage: send-to-oracle.sh <full commit of main> [--check]}"
check="${2:-}"
host="thornswood-bots"
here="$(cd "$(dirname "$0")/.." && pwd)"

if [[ ! "$commit" =~ ^[0-9a-f]{40}$ ]]; then
  echo "STOP: give the full 40-character commit, not '$commit'."; exit 1
fi
if [[ -n "$check" && "$check" != "--check" ]]; then
  echo "STOP: the only option is --check."; exit 1
fi

git -C "$here" fetch --quiet origin main
tip="$(git -C "$here" rev-parse origin/main)"
if [[ "$tip" != "$commit" ]]; then
  echo "STOP: main is at $tip, not $commit. Only main's tip, as Patrick merged it, goes to the VM."; exit 1
fi

scratch="$(mktemp -d)"
remote_bundle=""
cleanup() {
  rm -rf "$scratch"
  if [[ -n "$remote_bundle" ]]; then ssh "$host" "rm -f $remote_bundle"; fi
}
trap cleanup EXIT
bundle="$scratch/thornswood-hermes-$commit.bundle"
git -C "$here" bundle create --quiet "$bundle" "refs/remotes/origin/main"
git -C "$here" bundle verify --quiet "$bundle"

remote_bundle="/tmp/thornswood-hermes-$commit.bundle"
scp -q "$bundle" "$host:$remote_bundle"
ssh "$host" "chmod 0644 '$remote_bundle' && sudo -n -u hermes bash -s -- '$commit' '$remote_bundle' '$check'" <<'REMOTE'
set -euo pipefail
commit="$1"; bundle="$2"; check="$3"
releases="/home/hermes/thornswood-hermes/releases"
mkdir -p "$releases"
dest="$releases/$commit"
# A clone of a bundle takes only branches, and the bundle holds main as One's
# origin/main, so fetch that ref into a fresh repo instead.
if [[ ! -d "$dest/.git" ]]; then
  git init --quiet "$dest"
fi
git -C "$dest" fetch --quiet "$bundle" "+refs/remotes/origin/main:refs/remotes/origin/main"
git -C "$dest" -c advice.detachedHead=false checkout --quiet --detach "$commit"
cd "$dest"
python3 scripts/apply.py --expect "$commit" $check
if [[ -z "$check" ]]; then
  ln -sfn "$dest" /home/hermes/thornswood-hermes/current
fi
REMOTE
