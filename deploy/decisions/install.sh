#!/usr/bin/env bash
# Install the decision core on the Oracle VM, as root, from the root-owned
# copy of the commit Patrick merged:
#
#   sudo /opt/thornswood-hermes/releases/<commit>/deploy/decisions/install.sh <commit> [--room <id>] [--origin <url>]
#
# scripts/install-on-oracle.sh puts that copy there and points
# /opt/thornswood-hermes/current at it; run that first, at the same commit.
#
# --room is the Workbench room id check-hermes-contract.py printed. --origin is
# the phone app's exact address once Tailscale Serve gives the VM one, like
# https://thornswood-bots.<tailnet>.ts.net. Both are kept in
# /etc/thornswood/tw-decide.env (no secrets there) and stay until given again.
#
# Idempotent. It makes the tw-decide user and /srv/tw/decide (0700, tw-decide's
# only); builds the core's venv from deploy/decisions/requirements.txt (exact
# hashes, wheels only) and checks a whole passkey ceremony with it under the
# unit's own limits; puts the launchers and units in place; makes the store with
# every flag off the first time; makes the Work and Room feed tokens once, into
# the hermes user's ~/.config/thornswood (0600); starts the core on
# 127.0.0.1:9135 and the feeds once a minute; and checks the core answers on
# loopback only. It changes no flag and makes no pairing code.
set -euo pipefail
umask 022

commit="${1:?usage: install.sh <commit> [--room <id>] [--origin <url>]}"
shift
room=""; origin=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --room) room="${2:-}"; shift 2 ;;
    --origin) origin="${2:-}"; shift 2 ;;
    *) echo "STOP: the options are --room <id> and --origin <url>, not $1."; exit 1 ;;
  esac
done
opt=/opt/thornswood-hermes
venv="$opt/decisions-venv"
state=/srv/tw/decide
db="$state/core.db"
envfile=/etc/thornswood/tw-decide.env
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
if [[ -n "$origin" && ! "$origin" =~ ^https://[a-z0-9-]+\.[a-z0-9-]+\.ts\.net$ ]]; then
  stop "the origin is the VM's Tailscale address, like https://thornswood-bots.tail1234.ts.net, with no path."
fi
for tool in python3 systemd-run runuser; do command -v "$tool" >/dev/null || stop "$tool is not installed on the VM."; done
python3 -c 'import ensurepip, venv' 2>/dev/null || stop "python3 cannot make a venv; install it with: sudo apt-get install -y python3-venv"
id hermes >/dev/null 2>&1 || stop "the hermes user does not exist."

# 1. The user. Nobody else is in its group, the Bots' user least of all.
if ! id tw-decide >/dev/null 2>&1; then
  useradd --system --no-create-home --home-dir / --shell /usr/sbin/nologin --user-group tw-decide
fi
members="$(getent group tw-decide | cut -d: -f4)"
[[ -z "$members" ]] || stop "the tw-decide group has members ($members); it must have none."
ok "user tw-decide"

# 2. The store's folder: tw-decide's only. The settings file: root's, no secrets.
install -d -m 0755 -o root -g root /srv/tw /etc/thornswood
install -d -m 0700 -o tw-decide -g tw-decide "$state"
old_room=""; old_origin=""
if [[ -f "$envfile" ]]; then
  old_room="$(sed -n 's/^TW_DECIDE_ROOM=//p' "$envfile")"
  old_origin="$(sed -n 's/^TW_DECIDE_ORIGIN=//p' "$envfile")"
fi
room="${room:-$old_room}"; origin="${origin:-$old_origin}"
printf 'TW_DECIDE_ROOM=%s\nTW_DECIDE_ORIGIN=%s\n' "$room" "$origin" > "$envfile.new"
chmod 0644 "$envfile.new"; mv "$envfile.new" "$envfile"
ok "$state is 0700 tw-decide; room '${room:-not set}', origin '${origin:-not set}'"

# 3. The venv, root's, from the exact hashes. Rebuilt only when the list changes.
want="$(sha256sum "$here/deploy/decisions/requirements.txt" | cut -d' ' -f1)"
if [[ "$(cat "$venv/.requirements.sha256" 2>/dev/null || true)" != "$want" ]]; then
  rm -rf "$venv.new"
  python3 -m venv "$venv.new"
  "$venv.new/bin/pip" install --quiet --disable-pip-version-check --no-input --require-hashes \
    --only-binary :all: --no-deps -r "$here/deploy/decisions/requirements.txt"
  echo "$want" > "$venv.new/.requirements.sha256"
  rm -rf "$venv"; mv "$venv.new" "$venv"
  # The venv's own python link must point at the new folder's interpreter.
  [[ -x "$venv/bin/python3" ]] || stop "the venv has no python3"
fi
chown -R root:root "$venv"; chmod -R go-w "$venv"
ok "venv at $venv, requirements $want"

# 4. The launchers and units, from this copy.
install -d -m 0755 "$opt/bin"
for f in tw-decide tw-decide-feed; do install -m 0755 -o root -g root "$here/deploy/decisions/$f" "$opt/bin/$f"; done
for u in tw-decide.service tw-decide-feed.service tw-decide-feed.timer; do
  install -m 0644 -o root -g root "$here/deploy/decisions/$u" "/etc/systemd/system/$u"
done
systemctl daemon-reload
ok "launchers and units in place"

# 5. A whole passkey ceremony with the installed library, as tw-decide, under the
# core unit's own limits (a scratch store in a private /tmp; the real store is untouched).
check="$(systemd-run --quiet --wait --pipe --collect -p User=tw-decide -p NoNewPrivileges=yes \
  -p MemoryDenyWriteExecute=yes -p ProtectSystem=strict -p ProtectHome=yes -p PrivateTmp=yes \
  -p IPAddressDeny=any -p IPAddressAllow=localhost \
  "$opt/bin/tw-decide" --db /tmp/selfcheck-store.db selfcheck 2>&1)" || stop "the passkey self-check failed: $check"
grep -q '"ok": true' <<<"$check" || stop "the passkey self-check did not pass: $check"
ok "passkey self-check: $(tr -d '\n ' <<<"$check")"

run() { runuser -u tw-decide -- "$opt/bin/tw-decide" --db "$db" "$@"; }

# 6. The store, the first time: the workspace with every flag off, Patrick, the six Bots.
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

# 7. The feed tokens, once each, straight into the hermes user's files (0600).
hh="$(getent passwd hermes | cut -d: -f6)"
for name in work room; do
  tok="$hh/.config/thornswood/feed-$name.token"
  if ! runuser -u hermes -- test -s "$tok"; then
    runuser -u hermes -- sh -c 'umask 077; mkdir -p "$(dirname "$1")"' _ "$tok"
    run feed-token --workspace thornswood --name "$name" | runuser -u hermes -- sh -c 'umask 077; cat > "$1"' _ "$tok"
    ok "feed token for $name made"
  else
    ok "feed token for $name already there"
  fi
done

# 8. Start the core and check it answers on loopback, and only there.
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
  wide="$(ss -Htln 2>/dev/null | awk '{print $4}' | grep ':9135$' | grep -v '^127.0.0.1:9135$' || true)"
  [[ -z "$wide" ]] || stop "something listens on port 9135 beyond loopback: $wide"
  ok "port 9135 is on loopback only"
fi

# 9. The feeds, once now and then once a minute, when the room is known.
if [[ -n "$room" ]]; then
  systemctl enable --quiet --now tw-decide-feed.timer
  if systemctl start tw-decide-feed.service; then ok "feeds sent"; else
    stop "the feeds did not send: journalctl -u tw-decide-feed -n 20"
  fi
else
  echo "The feeds wait for --room."
fi
