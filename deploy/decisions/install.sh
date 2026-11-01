#!/usr/bin/env bash
# Install the decision core on the Oracle VM, as root, from the root-owned
# copy of the commit Patrick merged:
#
#   sudo /opt/thornswood-hermes/releases/<commit>/deploy/decisions/install.sh <commit> [--room <Workbench room id>]
#
# scripts/install-on-oracle.sh puts that copy there and points
# /opt/thornswood-hermes/current at it; run that first, at the same commit.
#
# Idempotent. It makes the tw-decide user and /srv/tw/decide (0700, tw-decide's
# only), puts the launcher and tw-decide.service in place, makes the store with
# every flag off the first time (and binds the Workbench room id given with
# --room), starts the core on 127.0.0.1:9135 and checks it answers there.
# It changes no flag and makes no pairing code. It reads no secret.
set -euo pipefail
umask 022

commit="${1:?usage: install.sh <commit> [--room <Workbench room id>]}"
shift
room=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --room) room="${2:-}"; shift 2 ;;
    *) echo "STOP: the only option is --room <id>, not $1."; exit 1 ;;
  esac
done
opt=/opt/thornswood-hermes
state=/srv/tw/decide
db="$state/core.db"
here="$(cd "$(dirname "$0")/../.." && pwd -P)"
stop() { echo "STOP: $*"; exit 1; }
ok() { echo "PASS $*"; }

[[ $EUID -eq 0 ]] || stop "run as root, with sudo."
[[ "$commit" =~ ^[0-9a-f]{40}$ ]] || stop "give the full 40-character commit."
[[ "$here" == "$opt/releases/$commit" ]] || stop "run the copy in $opt/releases/$commit, not $here."
[[ "$(stat -c %U "$here")" == root ]] || stop "$here is not root's."
[[ "$(git -C "$here" rev-parse HEAD)" == "$commit" ]] || stop "$here is not at $commit."
[[ -z "$(git -C "$here" status --porcelain)" ]] || stop "$here has changes; only the merged commit is installed."
[[ "$(readlink -f "$opt/current")" == "$here" ]] || stop "$opt/current is not this commit; run scripts/install-on-oracle.sh $commit on One first."
if [[ -n "$room" && ! "$room" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
  stop "the room id is the one check-hermes-contract.py printed, like 54b3ad46-3419-4adb-b60d-c79cb347fcee."
fi
command -v python3 >/dev/null || stop "python3 is not installed on the VM."

# 1. The user. Nobody else is in its group, the Bots' user least of all.
if ! id tw-decide >/dev/null 2>&1; then
  useradd --system --no-create-home --home-dir / --shell /usr/sbin/nologin --user-group tw-decide
fi
members="$(getent group tw-decide | cut -d: -f4)"
[[ -z "$members" ]] || stop "the tw-decide group has members ($members); it must have none."
ok "user tw-decide"

# 2. The store's folder: tw-decide's only.
install -d -m 0755 -o root -g root /srv/tw
install -d -m 0700 -o tw-decide -g tw-decide "$state"
ok "$state is 0700 tw-decide"

# 3. The launcher and the unit, from this copy.
install -d -m 0755 "$opt/bin"
install -m 0755 -o root -g root "$here/deploy/decisions/tw-decide" "$opt/bin/tw-decide"
install -m 0644 -o root -g root "$here/deploy/decisions/tw-decide.service" /etc/systemd/system/tw-decide.service
systemctl daemon-reload
ok "launcher and unit in place"

run() { runuser -u tw-decide -- "$opt/bin/tw-decide" --db "$db" "$@"; }

# 4. The store, the first time: the workspace with every flag off, Patrick, the six Bots.
if [[ ! -e "$db" ]]; then
  run init --workspace thornswood >/dev/null
  ok "store made, every flag off"
else
  ok "store already there; nothing in it changed"
fi
if [[ -n "$room" ]]; then
  run bind-room --workspace thornswood --room-id "$room" >/dev/null
  ok "Workbench room $room bound"
fi
run verify >/dev/null || stop "the store's event chain does not hold: $opt/bin/tw-decide --db $db verify"
ok "event chain holds"

# 5. Start it and check it answers on loopback, and only there.
systemctl enable --quiet tw-decide.service
systemctl restart tw-decide.service
answer=""
for _ in $(seq 1 20); do
  answer="$(python3 -I -c 'import urllib.request,urllib.error
try: urllib.request.urlopen("http://127.0.0.1:9135/api/v1/me", timeout=2)
except urllib.error.HTTPError as e: print(e.code)
except OSError: print("")' 2>/dev/null || true)"
  [[ -n "$answer" ]] && break
  sleep 0.5
done
[[ "$answer" == 401 ]] || stop "the core did not answer on 127.0.0.1:9135: journalctl -u tw-decide -n 50"
ok "the core answers on 127.0.0.1:9135 (401 to a call with no session, as it should)"
if command -v ss >/dev/null; then
  wide="$(ss -Htlnp 2>/dev/null | grep ':9135 ' | grep -v '127.0.0.1:9135' || true)"
  [[ -z "$wide" ]] || stop "something listens on port 9135 beyond loopback: $wide"
  ok "port 9135 is on loopback only"
fi
