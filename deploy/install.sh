#!/usr/bin/env bash
# Phase 2 install on the Oracle VM, as root. Started by
# scripts/install-on-oracle.sh on One, from the root-owned copy of the
# commit Patrick merged:
#
#   /opt/thornswood-hermes/releases/<commit>/deploy/install.sh <commit> [--move-gateway]
#
# Idempotent: run it again after any merge to Thornswood-Hermes. It makes the
# broker users and folders, puts the units, slices, sudoers line and SFTP jail
# in place, points /opt/thornswood-hermes/current at this commit, and starts
# the brokers. It never reads a secret's value: keys are moved by name and
# checked by owner and mode. Anything it cannot do safely is a STOP line that
# says what is missing, and nothing after it runs.
#
# --move-gateway moves the Hermes gateway from the hermes user's own unit to
# tw-hermes-gateway.service in tw-hermes.slice, starting it the same way.
# --quotas turns group quotas on for the filesystem holding /srv/tw (ext4
# only; it adds the journaled quota options to that line of /etc/fstab after
# checking the new file, remounts, and builds the quota file), so each Bot's
# disk cap can be set. Without it, the caps are set only where quotas are
# already on, and the limits check reports them missing.
set -euo pipefail
umask 022

commit="${1:?usage: install.sh <commit> [--move-gateway] [--quotas]}"
shift
move_gateway=""; quotas=""
for arg in "$@"; do
  case "$arg" in
    --move-gateway) move_gateway="$arg" ;;
    --quotas) quotas="$arg" ;;
    "") ;;
    *) echo "STOP: the options are --move-gateway and --quotas, not $arg."; exit 1 ;;
  esac
done
opt=/opt/thornswood-hermes
here="$(cd "$(dirname "$0")/.." && pwd -P)"
stop() { echo "STOP: $*"; exit 1; }
ok() { echo "PASS $*"; }

[[ $EUID -eq 0 ]] || stop "run as root (scripts/install-on-oracle.sh does this)."
[[ "$commit" =~ ^[0-9a-f]{40}$ ]] || stop "give the full 40-character commit."
[[ "$here" == "$opt/releases/$commit" ]] || stop "run the copy in $opt/releases/$commit, not $here."
[[ "$(stat -c %U "$here")" == root ]] || stop "$here is not root's."
[[ "$(git -C "$here" rev-parse HEAD)" == "$commit" ]] || stop "$here is not at $commit."
[[ -z "$(git -C "$here" status --porcelain)" ]] || stop "$here has changes; only the merged commit is installed."

pol() { python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); v=d
for k in sys.argv[2].split("."): v=v[k]
print(json.dumps(v) if isinstance(v,(dict,list,bool)) else v)' "$here/thornswood_guard/policy.json" "$1"; }

bots="bramble rift sprocket echo magpie wisp"
workers="rift sprocket echo magpie wisp"
for tool in git openssl age podman systemd-run python3 visudo sshd; do
  command -v "$tool" >/dev/null || stop "$tool is not installed on the VM."
done
id hermes >/dev/null 2>&1 || stop "the hermes user does not exist."
hermes_uid="$(id -u hermes)"
hermes_home="$(getent passwd hermes | cut -d: -f6)"

# 1. Groups and users. The groups say who may connect; every op still checks
# the caller's user.
for g in tw-policy-clients tw-git-clients tw-files-clients tw-spool $(for b in $bots; do echo "tw-work-$b"; done); do
  getent group "$g" >/dev/null || groupadd --system "$g"
done
mkuser() {
  local u="$1"
  if ! id "$u" >/dev/null 2>&1; then
    useradd --system --no-create-home --home-dir / --shell /usr/sbin/nologin --user-group "$u"
  fi
}
for u in tw-policy tw-files tw-git tw-proxy tw-backup; do mkuser "$u"; done
for b in $workers; do mkuser "tw-$b"; usermod -a -G "tw-work-$b" "tw-$b"; done
usermod -a -G tw-policy-clients,tw-git-clients,tw-files-clients hermes
usermod -a -G tw-policy-clients,tw-spool tw-git
usermod -a -G "tw-policy-clients,tw-spool,$(for b in $bots; do printf 'tw-work-%s,' "$b"; done | sed 's/,$//')" tw-files
usermod -a -G tw-policy-clients tw-proxy
for u in tw-policy tw-files tw-git tw-proxy; do
  id -nG hermes | tr ' ' '\n' | grep -qx "$u" && stop "hermes is in the $u group; it must not be."
done
ok "users and groups"

# 2. Folders.
install -d -m 0755 -o root -g root /srv/tw /srv/tw/work /srv/tw/spool /etc/thornswood
install -d -m 0700 -o tw-policy -g tw-policy /srv/tw/policy
install -d -m 0700 -o tw-git -g tw-git /srv/tw/git
install -d -m 2750 -o tw-git -g tw-spool /srv/tw/spool/in
install -d -m 2750 -o tw-files -g tw-spool /srv/tw/spool/out
for b in $bots; do install -d -m 2770 -o tw-files -g "tw-work-$b" "/srv/tw/work/$b"; done
install -d -m 0755 -o root -g root /srv/tw/backups
install -d -m 0700 -o root -g root /srv/tw/snapshots
ok "folders under /srv/tw"

# 3. The Apps' keys: tw-git's alone.
install -d -m 0750 -o root -g tw-git /etc/thornswood/apps
for app in bramble workers; do
  key="$(pol "brokers.apps.$app.key_file")"
  if [[ ! -f "$key" ]]; then
    echo "The $app App's key is not at $key. Files named *.pem that tw-git owns:"
    find /etc /home /var/lib /opt /root -xdev -maxdepth 5 -name '*.pem' -user tw-git -printf '  %p\n' 2>/dev/null || true
    stop "move each App's key to the path the policy names (brokers.apps), then run this again."
  fi
  chown tw-git:tw-git "$key"; chmod 0400 "$key"
done
ok "the two Apps' keys are tw-git's alone (0400)"

# 4. The admission proxy's keys: moved by name out of the hermes user's reach.
env_file="$(pol admission_proxy.env_file)"
qwen_env="$(pol secrets.qwen_env_file)"
if [[ ! -f "$env_file" ]]; then
  install -m 0440 -o root -g tw-proxy /dev/null "$env_file"
fi
if [[ -f "$qwen_env" ]]; then
  grep -E '^(export[[:space:]]+)?QWEN_(BASE_URL|API_KEY)=' "$qwen_env" >> "$env_file"
  shred -u "$qwen_env" 2>/dev/null || rm -f "$qwen_env"
  ok "moved the Qwen address and token from $qwen_env into $env_file"
fi
chown root:tw-proxy "$env_file"; chmod 0440 "$env_file"
names="$(cut -d= -f1 "$env_file" | sed 's/^export[[:space:]]*//' | sort -u | tr '\n' ' ')"
echo "proxy.env names: ${names:-none}"
for f in "$hermes_home/.env" "$hermes_home/.hermes/.env" "$hermes_home"/.hermes/profiles/*/.env; do
  [[ -f "$f" ]] || continue
  if grep -Eq '^(export[[:space:]]+)?(TW_CLAUDE_CREDITS_KEY|QWEN_API_KEY|TW_QWEN_TOKEN|ANTHROPIC_API_KEY)=' "$f"; then
    stop "$f names a route's real key; only proxy.env may. Move it there by hand, then run this again."
  fi
done

tokens_file="$(pol admission_proxy.tokens_file)"
if [[ "$(pol admission_proxy.ready)" == "true" ]]; then
  tmp="$(mktemp)"; echo "{" > "$tmp"; first=1
  for b in $bots; do
    penv="$hermes_home/.hermes/profiles/$b/.env"
    [[ -d "$(dirname "$penv")" ]] || stop "the $b profile does not exist."
    if ! grep -q '^TW_PROXY_TOKEN=' "$penv" 2>/dev/null; then
      runuser -u hermes -- sh -c "umask 077; printf 'TW_PROXY_TOKEN=%s\n' \"\$(openssl rand -hex 32)\" >> '$penv'"
    fi
    digest="$(grep '^TW_PROXY_TOKEN=' "$penv" | tail -1 | cut -d= -f2- | tr -d '\n' | sha256sum | cut -d' ' -f1)"
    [[ $first -eq 1 ]] || echo "," >> "$tmp"; first=0
    printf '  "%s": "%s"' "$b" "$digest" >> "$tmp"
  done
  printf '\n}\n' >> "$tmp"
  install -m 0440 -o root -g tw-proxy "$tmp" "$tokens_file"; rm -f "$tmp"
  ok "each Bot's proxy token is in its own profile; $tokens_file holds their SHA-256 only"
else
  [[ -f "$tokens_file" ]] || { echo "{}" > "$tokens_file"; chown root:tw-proxy "$tokens_file"; chmod 0440 "$tokens_file"; }
  echo "SKIP proxy tokens: admission_proxy.ready is false, so routes 2 and 4 stay out of every chain"
fi

# 5. Backups: Patrick's age public key, and One's key for the SFTP pull.
recipient="$(pol backups.age_recipient_file)"
if [[ ! -s "$recipient" ]]; then
  found="$(grep -rlsE '^age1[0-9a-z]{58}$' "$hermes_home/.config" /etc/thornswood /root 2>/dev/null | head -5 || true)"
  if [[ "$(echo "$found" | grep -c . || true)" -eq 1 ]]; then
    grep -E '^age1[0-9a-z]{58}$' "$found" | head -1 > "$recipient"
    ok "Patrick's age public key copied from $found to $recipient"
  else
    stop "put Patrick's age public key (the age1... line from One's backup key) in $recipient, then run this again."
  fi
fi
chmod 0644 "$recipient"
install -d -m 0755 /etc/ssh/authorized_keys
if [[ -s /etc/thornswood/backup-sftp.pub ]]; then
  install -m 0644 -o root -g root /etc/thornswood/backup-sftp.pub /etc/ssh/authorized_keys/tw-backup
fi
install -m 0644 "$here/deploy/sshd/50-tw-backup.conf" /etc/ssh/sshd_config.d/50-tw-backup.conf
sshd -t || stop "sshd rejects the tw-backup jail; nothing was reloaded."
systemctl reload ssh 2>/dev/null || systemctl reload sshd
[[ -s /etc/ssh/authorized_keys/tw-backup ]] && ok "tw-backup can pull over SFTP" || \
  echo "SKIP the SFTP pull: put One's backup public key in /etc/thornswood/backup-sftp.pub and run this again"

# 6. The code: this commit, root-owned, as current.
install -d -m 0755 "$opt/bin"
for f in "$here"/deploy/bin/*; do install -m 0755 -o root -g root "$f" "$opt/bin/"; done
ln -sfn "$here" "$opt/current.new" && mv -T "$opt/current.new" "$opt/current"
ok "$opt/current is $commit"

# 7. sudoers: tw-files may start the check helper, nothing else.
install -m 0440 -o root -g root "$here/deploy/sudoers/tw-files" /etc/sudoers.d/tw-files.new
visudo -cf /etc/sudoers.d/tw-files.new >/dev/null || { rm -f /etc/sudoers.d/tw-files.new; stop "visudo rejects the tw-files line."; }
mv /etc/sudoers.d/tw-files.new /etc/sudoers.d/tw-files

# 7b. Disk caps: a hard group quota on each Bot's work group.
srv_mnt="$(findmnt -n -o TARGET --target /srv/tw)"
quotas_on() { quotaon -p -g "$srv_mnt" 2>/dev/null | grep -q "is on"; }
if [[ -n "$quotas" ]] && ! quotas_on; then
  fstype="$(findmnt -n -o FSTYPE --target /srv/tw)"
  [[ "$fstype" == ext4 ]] || stop "/srv/tw is on $fstype; --quotas handles ext4 only."
  command -v quotacheck >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y -q quota >/dev/null
  modprobe quota_v2 || stop "the kernel has no quota_v2 module: apt-get install linux-modules-extra-$(uname -r), then run this again."
  if ! awk -v m="$srv_mnt" '$1 !~ /^#/ && $2 == m && $4 ~ /grpjquota=/' /etc/fstab | grep -q .; then
    [[ "$(awk -v m="$srv_mnt" '$1 !~ /^#/ && $2 == m' /etc/fstab | wc -l)" -eq 1 ]] || \
      stop "/etc/fstab does not have exactly one line for $srv_mnt; add grpjquota=aquota.group,jqfmt=vfsv1 to it by hand."
    awk -v m="$srv_mnt" 'BEGIN{OFS="\t"} $1 !~ /^#/ && $2 == m {$4 = $4 ",grpjquota=aquota.group,jqfmt=vfsv1"} {print}' \
      /etc/fstab > /etc/fstab.tw-new
    findmnt --verify --tab-file /etc/fstab.tw-new >/dev/null 2>&1 || { rm -f /etc/fstab.tw-new; stop "the new /etc/fstab does not verify; nothing was changed."; }
    cp -p /etc/fstab "/etc/fstab.before-tw-quotas.$(date -u +%Y%m%d%H%M%S)"
    mv /etc/fstab.tw-new /etc/fstab
    systemctl daemon-reload
  fi
  mount -o remount "$srv_mnt"
  quotacheck -cgm "$srv_mnt"
  quotaon -g "$srv_mnt"
fi
if quotas_on; then
  for b in $bots; do
    gb="$(python3 -I -c 'import json,sys; c=json.load(open(sys.argv[1]))["brokers"]["files"]["disk_quota_gb"]; print(c.get(sys.argv[2], c["default"]))' \
      "$here/thornswood_guard/policy.json" "$b")"
    setquota -g "tw-work-$b" 0 "$((gb * 1024 * 1024))" 0 0 "$srv_mnt"
  done
  ok "each Bot's work group has its disk cap on $srv_mnt"
else
  echo "SKIP disk caps: group quotas are not on for $srv_mnt; run this again with --quotas"
fi

# 8. Units and slices.
for f in "$here"/deploy/systemd/*.service "$here"/deploy/systemd/*.timer "$here"/deploy/systemd/*.slice; do
  sed "s/user@1002\.service/user@$hermes_uid.service/g" "$f" > "/etc/systemd/system/$(basename "$f")"
done
install -d "/etc/systemd/system/user-$hermes_uid.slice.d"
install -m 0644 "$here/deploy/systemd/user-hermes.slice.conf" "/etc/systemd/system/user-$hermes_uid.slice.d/50-thornswood.conf"
systemctl daemon-reload
systemctl enable --quiet tw-limits-check.service
systemctl restart tw-limits-check.service
systemctl enable --quiet --now tw-policy.service tw-git.service tw-files.service tw-proxy.service tw-snapshot.timer tw-backup.timer
systemctl restart tw-policy.service tw-git.service tw-files.service tw-proxy.service
ok "units in place and started"

# 9. The gateway, when asked.
if [[ "$move_gateway" == "--move-gateway" ]]; then
  user_unit="$hermes_home/.config/systemd/user/hermes-gateway.service"
  [[ -f "$user_unit" ]] || stop "the hermes user's hermes-gateway unit is not at $user_unit."
  exec_start="$(grep -E '^ExecStart=' "$user_unit" | tail -1 | cut -d= -f2-)"
  workdir="$(grep -E '^WorkingDirectory=' "$user_unit" | tail -1 | cut -d= -f2- || true)"
  envs="$(grep -E '^Environment(File)?=' "$user_unit" | grep -v -E 'XDG_RUNTIME_DIR|DBUS_SESSION_BUS_ADDRESS' || true)"
  [[ -n "$exec_start" ]] || stop "$user_unit has no ExecStart."
  python3 -I - "$here/deploy/systemd/tw-hermes-gateway.service.in" /etc/systemd/system/tw-hermes-gateway.service \
    "$hermes_uid" "$exec_start" "${workdir:-$hermes_home}" "$envs" <<'PY'
import sys
src, dst, uid, exec_start, workdir, envs = sys.argv[1:]
text = open(src).read().replace("@UID@", uid).replace("@EXECSTART@", exec_start)
text = text.replace("@WORKDIR@", workdir).replace("@ENVIRONMENT@", envs.strip())
open(dst, "w").write(text)
PY
  systemctl daemon-reload
  runuser -u hermes -- env XDG_RUNTIME_DIR="/run/user/$hermes_uid" systemctl --user stop hermes-gateway.service
  runuser -u hermes -- env XDG_RUNTIME_DIR="/run/user/$hermes_uid" systemctl --user disable --quiet hermes-gateway.service
  systemctl enable --quiet --now tw-hermes-gateway.service
  sleep 5
  systemctl is-active --quiet tw-hermes-gateway.service || stop "tw-hermes-gateway did not stay up: journalctl -u tw-hermes-gateway -n 50"
  ok "the gateway runs as tw-hermes-gateway.service in tw-hermes.slice"
fi

# 10. What holds now.
echo "--- limits check"
cat /run/tw/limits.json 2>/dev/null || echo "no result"
for u in tw-policy tw-git tw-files tw-proxy; do
  printf '%-10s %s\n' "$u" "$(systemctl is-active "$u.service" || true)"
done
