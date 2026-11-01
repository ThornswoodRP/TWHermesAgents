#!/usr/bin/env bash
# The phone test bench for T10 to T13 (07 section 13), as root, from the
# root-owned copy of the installed commit:
#
#   sudo /opt/thornswood-hermes/releases/<commit>/deploy/decisions/phone-test.sh <commit> start
#   sudo /opt/thornswood-hermes/releases/<commit>/deploy/decisions/phone-test.sh <commit> pair
#   sudo /opt/thornswood-hermes/releases/<commit>/deploy/decisions/phone-test.sh <commit> card [high]
#   sudo /opt/thornswood-hermes/releases/<commit>/deploy/decisions/phone-test.sh <commit> stop
#
# Tests use a separate test workspace and test actions, never a real deploy,
# merge or Ship. So the bench is a second core of its own: the test workspace
# phone-test, in its own store (/srv/tw/decide-test), on 127.0.0.1:9136, with a
# push worker that has its own keys and token (/srv/tw/push-test), behind
# Tailscale Serve on port 8443 of the VM's ts.net name. Only in that test
# workspace does it turn mobile_read, decision_write and push_send on. Its cards
# are test cards, and answering one changes nothing anywhere. It never touches
# the real core, the thornswood workspace or its flags, and it never turns on
# chat or connector_effects, so nothing reaches a Bot or the room.
#
# Run install.sh with --origin first: the bench's address is that origin with
# :8443, so its passkeys and push name the same VM.
#
#   start  puts the two units in place, makes the store and the push worker's
#          token the first time, turns the three flags on in phone-test, starts
#          both, checks the core answers on loopback only, and serves it on 8443
#   pair   prints a one-time code for the phone, good for 10 minutes
#   card   opens one test card as Bramble (high: a high-risk one, which needs a
#          passkey sign-in from the last five minutes) and prints its address
#   stop   takes 8443 off Tailscale Serve and stops both; the store stays
set -euo pipefail
umask 022

opt=/opt/thornswood-hermes
state=/srv/tw/decide-test
pushdir=/srv/tw/push-test
db="$state/core.db"
envfile=/etc/thornswood/tw-decide.env
testenv=/etc/thornswood/tw-decide-test.env
ws=phone-test
stop() { echo "STOP: $*"; exit 1; }
ok() { echo "PASS $*"; }

[[ $EUID -eq 0 ]] || stop "run as root, with sudo."
commit="${1:-}"; action="${2:-}"
[[ "$commit" =~ ^[0-9a-f]{40}$ ]] || stop "give the full 40-character commit, then start, pair, card or stop."
here="$(cd "$(dirname "$0")/../.." && pwd -P)"
[[ "$here" == "$opt/releases/$commit" ]] || stop "run the copy in $opt/releases/$commit, not $here."
[[ "$(stat -c %U "$here")" == root ]] || stop "$here is not root's."
[[ "$(git -C "$here" rev-parse HEAD)" == "$commit" ]] || stop "$here is not at $commit."
[[ -z "$(git -C "$here" status --porcelain)" ]] || stop "$here has changes; only the merged commit runs."
[[ "$(readlink -f "$opt/current")" == "$here" ]] || stop "$opt/current is not this commit; the VM puts main in place by itself (tw-follow-main), so wait for it to reach $commit."
[[ -x "$opt/bin/tw-decide" && -x "$opt/bin/tw-decide-push" ]] || stop "the decision core is not installed; run deploy/decisions/install.sh first."
[[ -f "$envfile" ]] || stop "$envfile is missing; run deploy/decisions/install.sh first."
origin="$(sed -n 's/^TW_DECIDE_ORIGIN=//p' "$envfile")"
[[ "$origin" =~ ^https://[a-z0-9-]+\.[a-z0-9-]+\.ts\.net$ ]] || stop "the core has no ts.net origin yet; run deploy/decisions/install.sh <commit> --origin https://<the VM's ts.net name> first."
test_origin="$origin:8443"
command -v tailscale >/dev/null || stop "tailscale is not installed on the VM."

run() { runuser -u tw-decide -- "$opt/bin/tw-decide" --db "$db" "$@"; }

answers_401() {
  local answer=""
  for _ in $(seq 1 20); do
    answer="$(python3 -I -c 'import urllib.request,urllib.error
try: urllib.request.urlopen("http://127.0.0.1:9136/api/v1/me", timeout=2)
except urllib.error.HTTPError as e: print(e.code)
except OSError: print("")' 2>/dev/null || true)"
    [[ -n "$answer" ]] && break
    sleep 0.5
  done
  [[ "$answer" == 401 ]]
}

case "$action" in
  start)
    for u in tw-decide-test.service tw-decide-push-test.service; do
      install -m 0644 -o root -g root "$here/deploy/decisions/$u" "/etc/systemd/system/$u"
    done
    systemctl daemon-reload
    install -d -m 0700 -o tw-decide -g tw-decide "$state"
    install -d -m 0700 -o tw-push -g tw-push "$pushdir"
    printf 'TW_DECIDE_TEST_ORIGIN=%s\n' "$test_origin" > "$testenv.new"
    chmod 0644 "$testenv.new"; mv "$testenv.new" "$testenv"
    ok "units in place; the bench's address is $test_origin"
    # init refuses (409) if phone-test were ever a real workspace in this store.
    run init --workspace "$ws" --test >/dev/null || stop "the test store would not take the test workspace: $opt/bin/tw-decide --db $db init --workspace $ws --test"
    for flag in mobile_read decision_write push_send; do
      run flag --workspace "$ws" "$flag" on >/dev/null || stop "$flag would not go on in $ws."
    done
    ok "test workspace $ws: mobile_read, decision_write and push_send on; chat and connector_effects off"
    if ! runuser -u tw-push -- test -s "$pushdir/core.token"; then
      run feed-token --workspace "$ws" --name push | runuser -u tw-push -- sh -c 'umask 077; cat > "$1"' _ "$pushdir/core.token"
    fi
    ok "the bench's push worker has its own token in $pushdir"
    systemctl restart tw-decide-test.service
    answers_401 || stop "the bench's core did not answer on 127.0.0.1:9136: journalctl -u tw-decide-test -n 50"
    ok "the bench's core answers on 127.0.0.1:9136 (401 to a call with no session, as it should)"
    if command -v ss >/dev/null; then
      wide="$(ss -Htln 2>/dev/null | awk '{print $4}' | grep ':9136$' | grep -v '^127.0.0.1:9136$' || true)"
      [[ -z "$wide" ]] || stop "something listens on port 9136 beyond loopback: $wide"
      ok "port 9136 is on loopback only"
    fi
    systemctl restart tw-decide-push-test.service
    sleep 3
    systemctl is-active --quiet tw-decide-push-test.service || stop "the bench's push worker did not start: journalctl -u tw-decide-push-test -n 20"
    ok "the bench's push worker runs"
    tailscale serve --bg --https=8443 127.0.0.1:9136 || stop "Tailscale Serve would not take port 8443; is HTTPS on for the tailnet?"
    ok "Tailscale Serve: $test_origin -> 127.0.0.1:9136"
    echo "Open $test_origin/m/ on the phone, then run this script with pair."
    ;;
  pair)
    systemctl is-active --quiet tw-decide-test.service || stop "the bench is not running; run this script with start."
    run pair --workspace "$ws" --kind phone --label "Test phone"
    echo "Type the code into $test_origin/m/ within 10 minutes."
    ;;
  card)
    systemctl is-active --quiet tw-decide-test.service || stop "the bench is not running; run this script with start."
    risk="normal"
    case "${3:-}" in "") ;; high) risk="high" ;; *) stop "card takes nothing, or high." ;; esac
    out="$(run test-card --workspace "$ws" --risk "$risk")" || stop "the test card was not opened."
    id="$(printf '%s' "$out" | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
    [[ "$id" =~ ^[A-Za-z0-9_-]+$ ]] || stop "the core gave no card id."
    ok "test card $id ($risk risk)"
    echo "$test_origin/m/decisions/$id"
    ;;
  stop)
    if tailscale serve status 2>/dev/null | grep -q ':8443'; then
      tailscale serve --bg --https=8443 off || stop "Tailscale Serve would not take 8443 off; tailscale serve status shows it."
    fi
    systemctl stop tw-decide-push-test.service tw-decide-test.service
    ok "the bench is stopped and off Tailscale Serve; its store stays in $state"
    ;;
  *)
    stop "after the commit, say start, pair, card or stop."
    ;;
esac
