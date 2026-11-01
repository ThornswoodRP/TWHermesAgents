#!/usr/bin/env bash
# Put the thornswood-decisions plugin in Patrick's own Hermes on the VM (the
# patrick-hermes user), as root:
#
#   sudo /opt/thornswood-hermes/releases/<commit>/deploy/decisions/install-plugin.sh <commit>
#
# It links that user's plugins/thornswood-decisions to the root-owned release
# (/opt/thornswood-hermes/current/plugins/thornswood-decisions), the way the
# Hermes docs link a plugin from a checkout, so the code that carries Desktop's
# session to the core is never a copy anyone but root can change, and each
# install moves it with the release. It never touches the Bots' Hermes (the
# hermes user) and never edits a Hermes setting: Patrick turns the plugin on
# himself with `hermes plugins enable thornswood-decisions` as patrick-hermes.
set -euo pipefail
umask 022

commit="${1:?usage: install-plugin.sh <commit>}"
owner=patrick-hermes
opt=/opt/thornswood-hermes
here="$(cd "$(dirname "$0")/../.." && pwd -P)"
stop() { echo "STOP: $*"; exit 1; }
ok() { echo "PASS $*"; }

[[ $EUID -eq 0 ]] || stop "run as root, with sudo."
[[ "$commit" =~ ^[0-9a-f]{40}$ ]] || stop "give the full 40-character commit."
[[ "$here" == "$opt/releases/$commit" ]] || stop "run the copy in $opt/releases/$commit, not $here."
[[ "$(stat -c %U "$here")" == root ]] || stop "$here is not root's."
[[ "$(git -C "$here" rev-parse HEAD)" == "$commit" ]] || stop "$here is not at $commit."
[[ "$(readlink -f "$opt/current")" == "$here" ]] || stop "$opt/current is not this commit; the VM puts main in place by itself (tw-follow-main), so wait for it to reach $commit, or see /var/lib/thornswood/follow-main.json for why it has not."
target="$opt/current/plugins/thornswood-decisions"
[[ -f "$target/dashboard/plugin_api.py" ]] || stop "$target has no plugin in it."

id "$owner" >/dev/null 2>&1 || stop "the $owner user does not exist; Patrick's Hermes runs as it."
[[ "$owner" != hermes ]] || stop "the plugin never goes in the Bots' Hermes."
home="$(getent passwd "$owner" | cut -d: -f6)"
hh="$home/.hermes"
[[ -d "$hh" && ! -L "$hh" ]] || stop "$hh is not a folder; is Patrick's Hermes set up as $owner?"
[[ "$(stat -c %U "$hh")" == "$owner" ]] || stop "$hh is not $owner's."
if [[ ! -e "$hh/plugins" ]]; then
  runuser -u "$owner" -- mkdir -m 0755 "$hh/plugins"
fi
[[ -d "$hh/plugins" && ! -L "$hh/plugins" ]] || stop "$hh/plugins is not a plain folder."

link="$hh/plugins/thornswood-decisions"
if [[ -e "$link" && ! -L "$link" ]]; then
  stop "$link is a real folder, not the link this makes; move it aside first."
fi
# The link is made as the owner, in the owner's folder: root writes nothing there.
runuser -u "$owner" -- ln -sfnT "$target" "$link"
ok "$link -> $target"
echo "Turn it on as $owner: hermes plugins enable thornswood-decisions"
