#!/usr/bin/env bash
# Install the decision core on the Oracle VM, as root, from the root-owned
# copy of the commit Patrick merged:
#
#   sudo /opt/thornswood-hermes/releases/<commit>/deploy/decisions/install.sh <commit> [--room <id>] [--origin <url>]
#
# tw-follow-main puts that copy there and points /opt/thornswood-hermes/current
# at it once main reaches the VM; run this after it, at the same commit.
#
# --room is the Workbench room id check-hermes-contract.py printed. --origin is
# the phone app's exact address once Tailscale Serve gives the VM one, like
# https://hermes.<tailnet>.ts.net. Both are kept in
# /etc/thornswood/tw-decide.env (no secrets there) and stay until given again.
#
# Before it first opens the store (which migrates it) it stops the core and
# every worker of the release before, and starts each worker again only once
# the core answers on this release's schema (step 5b, 1 Nov).
#
# Idempotent. It makes the tw-decide user and /srv/tw/decide (0700, tw-decide's
# only); builds the core's venv from deploy/decisions/requirements.txt (exact
# hashes, wheels only) and checks a whole passkey ceremony with it under the
# unit's own limits; puts the launchers and units in place; makes the store with
# every flag off the first time; makes the Work, Room and Bot Chat feed tokens
# and the connector's token once, into the hermes user's ~/.config/thornswood
# (0600); starts the core on 127.0.0.1:9135, the feeds once a minute, the live
# Board every 2 seconds and the connector (which does nothing while
# connector_effects is off); and checks the
# core answers on loopback only. Step 5: it makes the tw-push user and
# /srv/tw/push (0700, tw-push's only), puts the push worker's core token there,
# and, once --origin is known, starts the push worker, which makes its own VAPID
# and seal keys there the first time and sends nothing while push_send is off.
# Chat with Patrick's own Hermes (1 Nov): when the patrick-hermes user is there,
# it makes my-hermes's connector token once, straight into that user's
# ~/.config/thornswood/own-me.token (0600), and starts tw-decide-own as that
# user, which does nothing and copies nothing while own_chat_write is off.
# It starts tw-decide-peers once a minute, which reads the tailnet's view of
# One (`tailscale status --json`, read only) so the phone says One is off only
# when Tailscale says so.
# Patrick's switch, 2 Nov ("I want all the features I have asked for on"):
# it turns on the phone app's flags, PHONE_FLAGS below, in the thornswood
# workspace; one already on is left as it is. decision_write and
# connector_effects, the Bots' own, it never changes. It makes no pairing code.
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
[[ "$(readlink -f "$opt/current")" == "$here" ]] || stop "$opt/current is not this commit; the VM puts main in place by itself (tw-follow-main), so wait for it to reach $commit, or see /var/lib/thornswood/follow-main.json for why it has not."
if [[ -n "$room" && ! "$room" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
  stop "the room id is the one check-hermes-contract.py printed, like 54b3ad46-3419-4adb-b60d-c79cb347fcee."
fi
if [[ -n "$origin" && ! "$origin" =~ ^https://[a-z0-9-]+\.[a-z0-9-]+\.ts\.net$ ]]; then
  stop "the origin is the VM's Tailscale address, like https://hermes.tail1234.ts.net, with no path."
fi
for tool in python3 systemd-run runuser; do command -v "$tool" >/dev/null || stop "$tool is not installed on the VM."; done
# The venv needs Ubuntu's python3-venv, which the VM's image leaves out. Put it in
# here, as deploy/install.sh does for rclone and quota, so a new VM needs no hand step.
if ! python3 -c 'import ensurepip, venv' 2>/dev/null; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3-venv >/dev/null \
    || stop "apt-get could not install python3-venv: sudo apt-get install -y python3-venv shows why."
  python3 -c 'import ensurepip, venv' 2>/dev/null || stop "python3 still cannot make a venv after installing python3-venv."
  ok "python3-venv installed"
fi
id hermes >/dev/null 2>&1 || stop "the hermes user does not exist."
# The hermes command the Bots' user runs, as its own login shell finds it.
hermes_bin="$(runuser -l hermes -c 'command -v hermes' 2>/dev/null || true)"
[[ "$hermes_bin" == /* && -x "$hermes_bin" ]] || stop "the hermes user's login shell does not find the hermes command."
head -n1 "$hermes_bin" | grep -q '^#!/' || stop "$hermes_bin does not start with #!, so the connector cannot follow it to Hermes's Python."

# 1. The user. Nobody else is in its group, the Bots' user least of all.
if ! id tw-decide >/dev/null 2>&1; then
  useradd --system --no-create-home --home-dir / --shell /usr/sbin/nologin --user-group tw-decide
fi
members="$(getent group tw-decide | cut -d: -f4)"
[[ -z "$members" ]] || stop "the tw-decide group has members ($members); it must have none."
ok "user tw-decide"
# Dictation's socket group (tw-decide-voice.service): the core alone is in it.
getent group tw-decide-voice >/dev/null || groupadd --system tw-decide-voice
id -nG tw-decide | tr ' ' '\n' | grep -qx tw-decide-voice || usermod -aG tw-decide-voice tw-decide
members="$(getent group tw-decide-voice | cut -d: -f4)"
[[ "$members" == tw-decide ]] || stop "the tw-decide-voice group's members are '$members'; it must be tw-decide alone."
ok "group tw-decide-voice: tw-decide alone"
# The push worker's user: the one part that reaches the internet, so a user of its own.
if ! id tw-push >/dev/null 2>&1; then
  useradd --system --no-create-home --home-dir / --shell /usr/sbin/nologin --user-group tw-push
fi
members="$(getent group tw-push | cut -d: -f4)"
[[ -z "$members" ]] || stop "the tw-push group has members ($members); it must have none."
ok "user tw-push"
# The Bots' route (#49): only the Bots' user may open its socket. deploy/install.sh
# makes the group with the brokers' groups and puts hermes in it.
getent group tw-decide-bots >/dev/null || stop "there is no tw-decide-bots group; deploy/install.sh makes it, run that first."
members="$(getent group tw-decide-bots | cut -d: -f4)"
[[ "$members" == hermes ]] || stop "the tw-decide-bots group's members are '$members'; it must be hermes alone."
ok "group tw-decide-bots: hermes alone"

# 2. The store's folder: tw-decide's only. The settings file: root's, no secrets.
install -d -m 0755 -o root -g root /srv/tw /etc/thornswood
install -d -m 0700 -o tw-decide -g tw-decide "$state"
install -d -m 0700 -o tw-push -g tw-push /srv/tw/push
install -d -m 0755 -o root -g root /var/lib/tw-decide-voice  # dictation's Whisper models, root's (step 14)
old_room=""; old_origin=""  # TW_HERMES_BIN is found again each time
if [[ -f "$envfile" ]]; then
  old_room="$(sed -n 's/^TW_DECIDE_ROOM=//p' "$envfile")"
  old_origin="$(sed -n 's/^TW_DECIDE_ORIGIN=//p' "$envfile")"
fi
room="${room:-$old_room}"; origin="${origin:-$old_origin}"
printf 'TW_DECIDE_ROOM=%s\nTW_DECIDE_ORIGIN=%s\nTW_HERMES_BIN=%s\n' "$room" "$origin" "$hermes_bin" > "$envfile.new"
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
for f in tw-decide tw-decide-feed tw-decide-live tw-decide-voice tw-decide-connector tw-decide-push tw-decide-own; do
  install -m 0755 -o root -g root "$here/deploy/decisions/$f" "$opt/bin/$f"
done
for u in tw-decide.service tw-decide-bots.service tw-decide-feed.service tw-decide-feed.timer tw-decide-live.service \
         tw-decide-voice.service \
         tw-decide-connector.service \
         tw-decide-push.service tw-decide-own.service tw-decide-peers.service tw-decide-peers.timer; do
  install -m 0644 -o root -g root "$here/deploy/decisions/$u" "/etc/systemd/system/$u"
done
# The connector finds the gateway's control socket where the gateway's own unit puts it.
install -d -m 0755 /etc/systemd/system/tw-decide-connector.service.d
printf '[Service]\nEnvironment=XDG_RUNTIME_DIR=/run/user/%s\n' "$(id -u hermes)" \
  > /etc/systemd/system/tw-decide-connector.service.d/runtime.conf
chmod 0644 /etc/systemd/system/tw-decide-connector.service.d/runtime.conf
systemctl daemon-reload
ok "launchers and units in place"

# 5. A whole passkey ceremony with the installed library, as tw-decide, under the
# core unit's own limits (a scratch store in a private /tmp; the real store is untouched).
check="$(systemd-run --quiet --wait --pipe --collect -p User=tw-decide -p NoNewPrivileges=yes \
  -p MemoryDenyWriteExecute=yes -p ProtectSystem=strict -p ProtectHome=yes -p PrivateTmp=yes \
  -p IPAddressDeny=any -p IPAddressAllow=localhost \
  "$opt/bin/tw-decide" --db /tmp/selfcheck-store.db selfcheck 2>&1)" || stop "the passkey self-check failed: $check"
grep -q '"ok": true' <<<"$check" || stop "the passkey self-check did not pass: $check"
grep -q '"push": "sealed, encrypted and signed"' <<<"$check" || stop "the push self-check did not pass: $check"
ok "passkey and push self-check: $(tr -d '\n ' <<<"$check")"

run() { runuser -u tw-decide -- "$opt/bin/tw-decide" --db "$db" "$@"; }

# 5b. Nothing of the release before runs while this one opens the store. The
# first `run` below migrates it, and a worker still on the older code would
# lease what a migration put back and handle it the old way: on 1 Nov at
# 10:53 PM the old connector failed again, three seconds after migration 11,
# the answer that migration had just put back in the queue. So the timers
# (first, so none starts its service again), every worker that leases from the
# core or opens its store, and the core itself stop here. Each starts again
# below, on this release, only once the core answers on the new schema (step 8).
CORE_WORKERS="tw-decide-bots.service tw-decide-live.service tw-decide-voice.service tw-decide-connector.service tw-decide-push.service tw-decide-own.service tw-decide-feed.service tw-decide-peers.service"
systemctl stop tw-decide-feed.timer tw-decide-peers.timer
systemctl stop $CORE_WORKERS
systemctl stop tw-decide.service
for u in tw-decide-feed.timer tw-decide-peers.timer $CORE_WORKERS tw-decide.service; do
  if systemctl is-active --quiet "$u"; then stop "$u still runs after systemctl stop: journalctl -u $u -n 20"; fi
done
ok "the core and its workers are stopped while the store migrates"

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

# 6b. The phone app's features, on, by Patrick's switch of 2 Nov: reading and
# chatting with the Bots, alerts, his own Hermes and One, and renaming, hiding
# and deleting his own chats. Each change is in the event chain; a flag already
# on is left as it is, with no event.
PHONE_FLAGS="mobile_read mobile_chat_write push_send own_chat_write session_manage_write"
for f in $PHONE_FLAGS; do
  run flag --workspace thornswood "$f" on --patrick-switched >/dev/null
done
ok "the phone app's features are on: $PHONE_FLAGS"

# 7. The feed tokens and the connector's, once each, straight into the hermes user's files (0600).
hh="$(getent passwd hermes | cut -d: -f6)"
for name in work room bots connector; do
  if [[ "$name" == connector ]]; then tok="$hh/.config/thornswood/connector.token"
  else tok="$hh/.config/thornswood/feed-$name.token"; fi
  if ! runuser -u hermes -- test -s "$tok"; then
    runuser -u hermes -- sh -c 'umask 077; mkdir -p "$(dirname "$1")"' _ "$tok"
    run feed-token --workspace thornswood --name "$name" | runuser -u hermes -- sh -c 'umask 077; cat > "$1"' _ "$tok"
    ok "token for $name made"
  else
    ok "token for $name already there"
  fi
done

# The push worker's token, once, straight into its own folder (0600).
if ! runuser -u tw-push -- test -s /srv/tw/push/core.token; then
  run feed-token --workspace thornswood --name push | runuser -u tw-push -- sh -c 'umask 077; cat > "$1"' _ /srv/tw/push/core.token
  ok "token for the push worker made"
else
  ok "token for the push worker already there"
fi

# 8. Start the core and check it answers on loopback, and only there.
systemctl enable --quiet tw-decide.service
systemctl reset-failed tw-decide.service
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
schema="$(run schema)" || stop "the core's store did not say its schema: $opt/bin/tw-decide --db $db schema"
python3 -I -c 'import json, sys; d = json.load(sys.stdin); sys.exit(0 if d["store"] == d["code"] else 1)' <<<"$schema" \
  || stop "the store is not at the schema this release knows, so no worker starts: $(tr -d '\n ' <<<"$schema")"
ok "the store is at this release's schema: $(tr -d '\n ' <<<"$schema"); the workers start from here"
if command -v ss >/dev/null; then
  wide="$(ss -Htln 2>/dev/null | awk '{print $4}' | grep ':9135$' | grep -v '^127.0.0.1:9135$' || true)"
  [[ -z "$wide" ]] || stop "something listens on port 9135 beyond loopback: $wide"
  ok "port 9135 is on loopback only"
fi

# 8b. The Bots' route (#49), on its socket only: a Bot files cards and reads
# Patrick's answers through it, as itself, with no token.
systemctl enable --quiet tw-decide-bots.service
systemctl reset-failed tw-decide-bots.service
systemctl restart tw-decide-bots.service
for _ in $(seq 1 20); do [[ -S /run/tw/decide-bots/bots.sock ]] && break; sleep 0.5; done
[[ -S /run/tw/decide-bots/bots.sock ]] || stop "the Bots' route did not open its socket: journalctl -u tw-decide-bots -n 50"
[[ "$(stat -c '%a %G' /run/tw/decide-bots/bots.sock)" == "660 tw-decide-bots" ]] \
  || stop "the Bots' route's socket is $(stat -c '%a %G' /run/tw/decide-bots/bots.sock), not 660 tw-decide-bots"
ok "the Bots' route listens on /run/tw/decide-bots/bots.sock (660, tw-decide-bots)"

# 9. The feeds, once now and then once a minute, when the room is known.
if [[ -n "$room" ]]; then
  systemctl enable --quiet --now tw-decide-feed.timer
  if systemctl start tw-decide-feed.service; then ok "feeds sent"; else
    stop "the feeds did not send: journalctl -u tw-decide-feed -n 20"
  fi
else
  echo "The feeds wait for --room."
fi

# 9b. The live Board: what each Bot is doing now, every 2 seconds. Sent once
# here first, so a failure stops the install with its own words.
if runuser -u hermes -- /opt/thornswood-hermes/bin/tw-decide-live --once \
     --hermes-home "$hh/.hermes" --tokens "$hh/.config/thornswood"; then ok "the live Board sent"; else
  stop "the live Board did not send (the line above says what the core answered)"
fi
systemctl enable --quiet tw-decide-live.service
systemctl reset-failed tw-decide-live.service
systemctl restart tw-decide-live.service
sleep 2
systemctl is-active --quiet tw-decide-live.service || stop "the live Board did not start: journalctl -u tw-decide-live -n 20"
ok "the live Board runs"

# 10. The connector. While connector_effects is off it asks the core every half
# minute and does nothing else. Only Patrick changes connector_effects.
systemctl enable --quiet tw-decide-connector.service
systemctl reset-failed tw-decide-connector.service
systemctl restart tw-decide-connector.service
sleep 2
systemctl is-active --quiet tw-decide-connector.service || stop "the connector did not start: journalctl -u tw-decide-connector -n 20"
ok "the connector runs (it waits while connector_effects is off)"

# 11. The push worker, once the origin is known (it names it in every VAPID
# header). While push_send is off it says its public keys to the core and then
# asks every half minute and sends nothing. Only Patrick changes push_send.
if [[ -n "$origin" ]]; then
  systemctl enable --quiet tw-decide-push.service
  systemctl reset-failed tw-decide-push.service
  systemctl restart tw-decide-push.service
  sleep 3
  systemctl is-active --quiet tw-decide-push.service || stop "the push worker did not start: journalctl -u tw-decide-push -n 20"
  ok "the push worker runs (it sends nothing while push_send is off)"
else
  echo "The push worker waits for --origin."
fi

# 12. Patrick's own Hermes on the VM (my-hermes): the connector that carries his
# chat from the phone into it, as patrick-hermes, never as the Bots' user. Its
# token goes straight into that user's own file (0600). While own_chat_write is
# off it asks the core every half minute and copies nothing. Only Patrick
# changes own_chat_write.
own=patrick-hermes
if id "$own" >/dev/null 2>&1; then
  [[ "$own" != hermes ]] || stop "my-hermes's connector never runs as the Bots' user."
  own_bin="$(runuser -l "$own" -c 'command -v hermes' 2>/dev/null || true)"
  [[ "$own_bin" == /* && -x "$own_bin" ]] || stop "the $own user's login shell does not find the hermes command."
  head -n1 "$own_bin" | grep -q '^#!/' || stop "$own_bin does not start with #!, so the connector cannot follow it to Hermes's Python."
  oh="$(getent passwd "$own" | cut -d: -f6)"
  tok="$oh/.config/thornswood/own-me.token"
  # A file whose token the core no longer keeps (a downgrade past schema 7 forgets it) is made again.
  if ! runuser -u "$own" -- test -s "$tok" \
     || ! runuser -u "$own" -- cat "$tok" | run token-known --workspace thornswood --name own-me | grep -q '"known": true'; then
    runuser -u "$own" -- sh -c 'umask 077; mkdir -p "$(dirname "$1")"' _ "$tok"
    run feed-token --workspace thornswood --name own-me | runuser -u "$own" -- sh -c 'umask 077; cat > "$1"' _ "$tok"
    ok "token for my-hermes's connector made"
  else
    ok "token for my-hermes's connector already there"
  fi
  install -d -m 0755 /etc/systemd/system/tw-decide-own.service.d
  printf '[Service]\nEnvironment=XDG_RUNTIME_DIR=/run/user/%s\nEnvironment=TW_OWN_HERMES_BIN=%s\n' \
    "$(id -u "$own")" "$own_bin" > /etc/systemd/system/tw-decide-own.service.d/runtime.conf
  chmod 0644 /etc/systemd/system/tw-decide-own.service.d/runtime.conf
  systemctl daemon-reload
  systemctl enable --quiet tw-decide-own.service
  systemctl reset-failed tw-decide-own.service
  systemctl restart tw-decide-own.service
  sleep 2
  systemctl is-active --quiet tw-decide-own.service || stop "my-hermes's connector did not start: journalctl -u tw-decide-own -n 20"
  ok "my-hermes's connector runs (it waits while own_chat_write is off)"
else
  echo "my-hermes's connector waits for the $own user."
fi

# 13. The tailnet's view of One, once now and then once a minute, as tw-decide,
# from the VM's own Tailscale client, read only (`tailscale status --json`).
command -v tailscale >/dev/null || stop "the tailscale command is not on this VM, and the core is behind Tailscale Serve."
systemctl enable --quiet --now tw-decide-peers.timer
if systemctl start tw-decide-peers.service; then ok "the tailnet's view of One read"; else
  stop "the tailnet's view of One was not read: journalctl -u tw-decide-peers -n 20"
fi

# 14. Dictation, last, so every other part is running whatever it finds: Hermes's own local Whisper on a socket only the core may use.
start_voice() {
  systemctl enable --quiet tw-decide-voice.service
  systemctl reset-failed tw-decide-voice.service
  systemctl restart tw-decide-voice.service
  for _ in $(seq 1 30); do [[ -S /run/tw/decide-voice/voice.sock ]] && break; sleep 0.5; done
  [[ -S /run/tw/decide-voice/voice.sock ]] || stop "dictation did not open its socket: journalctl -u tw-decide-voice -n 50"
  [[ "$(stat -c '%a %G' /run/tw/decide-voice/voice.sock)" == "660 tw-decide-voice" ]] \
    || stop "dictation's socket is $(stat -c '%a %G' /run/tw/decide-voice/voice.sock), not 660 tw-decide-voice"
}
# Asked as the core asks: Voice().check (which Whisper) or Voice().prepare (load its model).
voice_ask() {
  runuser -u tw-decide -g tw-decide -G tw-decide-voice -- /usr/bin/python3 -I -c \
    "import sys; sys.path.insert(0, '/opt/thornswood-hermes/current'); from thornswood_decisions.voice import Voice; print(Voice().$1() or '')"
}
start_voice
ok "dictation listens on /run/tw/decide-voice/voice.sock (660, tw-decide-voice)"
whisper="$(voice_ask check)" || stop "the core could not ask dictation which Whisper it has: journalctl -u tw-decide-voice -n 50"
if [[ -z "$whisper" ]]; then
  # faster-whisper is Hermes's own optional extra, stt-whisper, pinned with its hashes in Hermes's
  # uv.lock. Hermes's package manager installs it with the command Hermes itself names for it
  # (pm/extras.py install_hint, pm/cli.py install --extra, at the pinned commit), run as the owner of
  # Hermes's install, as Hermes asks. Free and local: no new service, nothing outside Hermes's lock.
  # From 3:55 PM on 2 Nov the install only checked for it, so every install stopped here (#97).
  hermes_dir="$(runuser -l hermes -c "$(printf '%q' "$hermes_bin") --version" 2>/dev/null \
    | sed -n 's/^Install directory:[[:space:]]*//p' | head -n1)"
  [[ -n "$hermes_dir" && -d "$hermes_dir" ]] || stop "hermes --version did not name Hermes's install directory, so its Whisper extra cannot be installed as its owner."
  hermes_owner="$(stat -c %U "$hermes_dir")"
  runuser -l "$hermes_owner" -c "$(printf '%q' "$hermes_bin") pm install --extra stt-whisper" \
    || stop "Hermes did not install its stt-whisper extra (hermes pm install --extra stt-whisper, as $hermes_owner); what it printed is above."
  ok "Hermes installed its own local Whisper (its stt-whisper extra) into $hermes_dir, as $hermes_owner"
  start_voice
  whisper="$(voice_ask check)" || stop "the core could not ask dictation which Whisper it has: journalctl -u tw-decide-voice -n 50"
  [[ -n "$whisper" ]] || stop "Hermes installed its stt-whisper extra, but dictation still finds no local Whisper: journalctl -u tw-decide-voice -n 50"
fi
ok "dictation uses Hermes's local Whisper: $whisper"
# The model Hermes's stt.local.model names, fetched here as root, pinned and hash-checked, into the
# cache tw-decide-voice.service reads (HF_HUB_CACHE, offline). Never by the hermes user: its disk quota
# refused the download at 4:47 PM on 2 Nov and stopped every install.
model="$(voice_ask model)" || stop "dictation did not say which Whisper model Hermes uses: journalctl -u tw-decide-voice -n 50"
/usr/bin/python3 -I "$here/thornswood_decisions/whisper_models.py" --model "$model" --cache /var/lib/tw-decide-voice/hub \
  || stop "the Whisper model $model was not fetched; what it printed is above."
# Loaded now, from that cache, so the first recording does not wait on it.
model="$(voice_ask prepare)" || stop "Whisper on the VM did not load its model: journalctl -u tw-decide-voice -n 50"
ok "Whisper's model $model is loaded for dictation"
