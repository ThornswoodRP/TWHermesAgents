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

# How many of a user's processes, in the host's user namespace, lack any of
# the given group ids. A process keeps the groups it started with: one that
# began before usermod added the broker groups cannot open their sockets
# (31 Oct: Rift's first turn in the Workbench was refused, "permission denied
# on its socket", from a hermes session that started at 1:47 PM).
# (sd-pam) is left out: systemd forks it beside every user manager only to
# close the PAM session at logout, and it drops all its supplementary groups
# on purpose, so it never has them and never opens a socket (31 Oct, the
# first install with this check stopped on it).
#   stale_procs <proc root> <uid> <gid> [<gid>...]
stale_procs() {
  local proc="$1" uid="$2"; shift 2
  local host_ns d n=0 groups g
  host_ns="$(readlink "$proc/1/ns/user" 2>/dev/null || true)"
  for d in "$proc"/[0-9]*; do
    [[ "$(awk '/^Uid:/{print $2}' "$d/status" 2>/dev/null)" == "$uid" ]] || continue
    [[ "$(readlink "$d/ns/user" 2>/dev/null || true)" == "$host_ns" ]] || continue
    [[ "$(awk -F'\t' '/^Name:/{print $2}' "$d/status" 2>/dev/null)" == "(sd-pam)" ]] && continue
    groups=" $(awk '/^Groups:/{$1=""; print}' "$d/status" 2>/dev/null) "
    for g in "$@"; do [[ "$groups" == *" $g "* ]] || { n=$((n + 1)); break; }; done
  done
  echo "$n"
}

pol() { python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); v=d
for k in sys.argv[2].split("."): v=v[k]
print(json.dumps(v) if isinstance(v,(dict,list,bool)) else v)' "$here/thornswood_guard/policy.json" "$1"; }

bots="bramble rift sprocket echo magpie wisp"
workers="rift sprocket echo magpie wisp"
for tool in git openssl age podman systemd-run python3 visudo sshd ssh-keygen; do
  command -v "$tool" >/dev/null || stop "$tool is not installed on the VM."
done
id hermes >/dev/null 2>&1 || stop "the hermes user does not exist."
hermes_uid="$(id -u hermes)"
hermes_home="$(getent passwd hermes | cut -d: -f6)"

# 1. Groups and users. The groups say who may connect; every op still checks
# the caller's user.
for g in tw-policy-clients tw-git-clients tw-files-clients tw-decide-bots tw-spool $(for b in $bots; do echo "tw-work-$b"; done); do
  getent group "$g" >/dev/null || groupadd --system "$g"
done
mkuser() {
  local u="$1"
  if ! id "$u" >/dev/null 2>&1; then
    useradd --system --no-create-home --home-dir / --shell /usr/sbin/nologin --user-group "$u"
  fi
}
for u in tw-policy tw-files tw-git tw-proxy tw-backup tw-offsite tw-boxdrop tw-ci; do mkuser "$u"; done
# tw-offsite reads the VM's archives (tw-backup:tw-backup 0440) to copy them to Drive.
usermod -a -G tw-backup tw-offsite
for b in $workers; do mkuser "tw-$b"; usermod -a -G "tw-work-$b" "tw-$b"; done
usermod -a -G tw-policy-clients,tw-git-clients,tw-files-clients,tw-decide-bots hermes
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
# tw-files never makes a Bot's own folder: its group and setgid bit are what
# keep every tree under it the Bot's (31 Oct: a listing showed no wisp folder).
for b in $bots; do
  got="$(stat -c '%U:%G %a' "/srv/tw/work/$b")"
  [[ "$got" == "tw-files:tw-work-$b 2770" ]] || stop "/srv/tw/work/$b is $got, not tw-files:tw-work-$b 2770."
done
install -d -m 0755 -o root -g root /srv/tw/backups
install -d -m 0700 -o root -g root /srv/tw/snapshots
# The box's add-only drop: the jail's root must be root's and not writable by
# anyone else (sshd's ChrootDirectory rule); /in lets tw-boxdrop add and open
# by name, never list. Root's intake moves each finished file to boxbackups.
install -d -m 0755 -o root -g root /srv/tw/boxdrop
install -d -m 0730 -o root -g tw-boxdrop /srv/tw/boxdrop/in
install -d -m 0700 -o root -g root /srv/tw/boxdrop/rejected
install -d -m 0750 -o tw-offsite -g tw-offsite /srv/tw/boxbackups
install -d -m 0700 -o tw-offsite -g tw-offsite "$(dirname "$(pol backups.offsite.rclone_config)")"
# Following main: root's mirror of it, the test runs' folder, and the record.
install -d -m 0700 -o root -g root "$(dirname "$(pol follow_main.mirror)")" "$(dirname "$(pol follow_main.state_file)")"
install -d -m 0755 -o root -g root "$(pol follow_main.ci_dir)"
ok "folders under /srv/tw, with one tw-files:tw-work-<bot> 2770 folder for each of $bots"

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

# 5b. The box's add-only drop (#2862). Its public key is in this repository
# (backups.offsite.box_key_file), so a merge here is how it changes; it works
# only from the box's address.
box_key="$here/$(pol backups.offsite.box_key_file)"
box_addr="$(pol backups.offsite.box_address)"
if [[ -s "$box_key" ]]; then
  grep -qE '^ssh-ed25519 [A-Za-z0-9+/=]+ thornswood-box-drops-backups$' "$box_key" || stop "$box_key is not the box's drop key."
  printf 'from="%s",restrict %s\n' "$box_addr" "$(cat "$box_key")" > /etc/ssh/authorized_keys/tw-boxdrop.new
  chmod 0644 /etc/ssh/authorized_keys/tw-boxdrop.new
  mv /etc/ssh/authorized_keys/tw-boxdrop.new /etc/ssh/authorized_keys/tw-boxdrop
fi
install -m 0644 "$here/deploy/sshd/51-tw-boxdrop.conf" /etc/ssh/sshd_config.d/51-tw-boxdrop.conf
sshd -t || stop "sshd rejects the tw-boxdrop jail; nothing was reloaded."
systemctl reload ssh 2>/dev/null || systemctl reload sshd
[[ -s /etc/ssh/authorized_keys/tw-boxdrop ]] && ok "the box can add its backups to /srv/tw/boxdrop/in from $box_addr, and do nothing else" || \
  echo "SKIP the box's drop: its key is not in $(pol backups.offsite.box_key_file) yet"

# 5c. Drive, for both kinds of backup (tw-offsite.service). The sign-in is
# made once, by Patrick, with rclone's own headless flow; README, Off-site
# copies, has the two commands. Its file is tw-offsite's alone.
command -v rclone >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y -q rclone >/dev/null
command -v rclone >/dev/null || stop "rclone could not be installed."
rconf="$(pol backups.offsite.rclone_config)"
if [[ -f "$rconf" ]]; then
  chown tw-offsite:tw-offsite "$rconf"; chmod 0600 "$rconf"
  ok "tw-offsite holds the Drive sign-in ($rconf, 0600)"
else
  echo "SKIP Drive: no sign-in at $rconf yet (README, Off-site copies)"
fi

# 5d. The VM's own read-only key for this repository (tw-follow-main.service).
# Made here once, root's alone. Patrick adds its public half to the repo as a
# deploy key with write access off; until then each run says so.
read_key="$(pol follow_main.read_key)"
if [[ ! -f "$read_key" ]]; then
  ssh-keygen -q -t ed25519 -N '' -C thornswood-bots-reads-hermes -f "$read_key"
fi
chown root:root "$read_key" "$read_key.pub"; chmod 0400 "$read_key"; chmod 0644 "$read_key.pub"
install -m 0644 -o root -g root "$here/deploy/ssh/github_known_hosts" "$(pol follow_main.known_hosts)"
ok "the VM's read key for Thornswood-Hermes: $(cat "$read_key.pub")"

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
# quotaon -p exits non-zero when quotas are on, so under pipefail a pipe into
# grep read "on" as "off" (31 Oct, run 2). Its words are the answer, not its
# exit status.
quotas_on() { local out; out="$(quotaon -p -g "$srv_mnt" 2>&1 || true)"; [[ "$out" == *" is on"* ]]; }
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

# 7c. Swap: the 4 GB swapfile the host plan names (02 section 8.5), made when
# the box has no swap at all. The limits check fails without it.
if [[ -z "$(swapon --show --noheadings)" ]]; then
  [[ ! -e /swapfile ]] || stop "/swapfile exists but is not in use; look at it before anything replaces it."
  boot_floor="$(pol admission.admit.boot_free_gb)"
  free_gb="$(df --output=avail -BG / | tail -1 | tr -dc 0-9)"
  (( free_gb >= boot_floor + 4 )) || stop "only ${free_gb} GB free on /; a 4 GB swapfile would take it under the ${boot_floor} GB floor."
  fallocate -l 4G /swapfile
  chmod 0600 /swapfile
  mkswap -q /swapfile
  swapon /swapfile
  if ! awk '$1 !~ /^#/ && $1 == "/swapfile"' /etc/fstab | grep -q .; then
    { cat /etc/fstab; printf '/swapfile\tnone\tswap\tsw\t0\t0\n'; } > /etc/fstab.tw-new
    findmnt --verify --tab-file /etc/fstab.tw-new >/dev/null 2>&1 || { rm -f /etc/fstab.tw-new; stop "the new /etc/fstab does not verify; /swapfile is on until the next boot only."; }
    cp -p /etc/fstab "/etc/fstab.before-tw-swap.$(date -u +%Y%m%d%H%M%S)"
    mv /etc/fstab.tw-new /etc/fstab
    systemctl daemon-reload
  fi
  ok "a 4 GB swapfile is on, and in /etc/fstab for every boot"
fi

# 8. Units and slices. Every file in deploy/systemd is placed here or named
# as handled below; one the install does not know stops it (1 Nov: the
# first .path unit, tw-offsite.path, was left out by a list of suffixes, and
# enabling it failed).
unit_dir=/etc/systemd/system
for f in "$here"/deploy/systemd/*; do
  name="$(basename "$f")"
  case "$name" in
    tw-hermes-gateway.service.in|user-hermes.slice.conf) ;;  # placed below, filled in for this VM
    *.service|*.timer|*.slice|*.path|*.socket|*.target)
      sed "s/user@1002\.service/user@$hermes_uid.service/g" "$f" > "$unit_dir/$name" ;;
    *) stop "deploy/systemd/$name is not a kind of unit file the install places; add it to step 8." ;;
  esac
done
# end of the unit copy
install -d "/etc/systemd/system/user-$hermes_uid.slice.d"
install -m 0644 "$here/deploy/systemd/user-hermes.slice.conf" "/etc/systemd/system/user-$hermes_uid.slice.d/50-thornswood.conf"
systemctl daemon-reload
systemctl enable --quiet tw-limits-check.service
systemctl restart tw-limits-check.service
systemctl enable --quiet --now tw-policy.service tw-git.service tw-files.service tw-proxy.service tw-snapshot.timer tw-backup.timer \
  tw-offsite.timer tw-offsite.path tw-follow-main.timer
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

# 9b. The hermes user's own processes take the broker groups only when they
# start. Any still running from before usermod (a Hermes Desktop session over
# ssh, the user's systemd) cannot reach the sockets, so the hermes user's
# sessions are ended and the gateway is started again here, by the install,
# not by hand afterwards. Ending them also stops tw-hermes-gateway, which runs
# as hermes and needs user@UID.service (31 Oct: a hand-run terminate-user left
# it failed).
client_gids=($(for g in tw-policy-clients tw-git-clients tw-files-clients tw-decide-bots; do getent group "$g" | cut -d: -f3; done))
stale="$(stale_procs /proc "$hermes_uid" "${client_gids[@]}")"
if (( stale > 0 )); then
  gateway_was="$(systemctl is-enabled tw-hermes-gateway.service 2>/dev/null || true)"
  echo "$stale hermes processes started before hermes joined the broker groups; ending the hermes user's sessions (Hermes Desktop reconnects)."
  loginctl terminate-user hermes || true
  for _ in $(seq 1 30); do
    pgrep -u "$hermes_uid" >/dev/null || break
    sleep 1
  done
  systemctl start "user@$hermes_uid.service"
  if [[ "$gateway_was" == enabled ]]; then
    systemctl restart tw-hermes-gateway.service
    sleep 5
    systemctl is-active --quiet tw-hermes-gateway.service || stop "tw-hermes-gateway did not come back after the sessions ended: journalctl -u tw-hermes-gateway -n 50"
  fi
  stale="$(stale_procs /proc "$hermes_uid" "${client_gids[@]}")"
  (( stale == 0 )) || stop "$stale hermes processes still lack the broker groups; look at: ps -u hermes -o pid,lstart,cmd"
fi
ok "every hermes process has the broker groups$( [[ "$(systemctl is-enabled tw-hermes-gateway.service 2>/dev/null || true)" == enabled ]] && echo ", and tw-hermes-gateway is $(systemctl is-active tw-hermes-gateway.service || true)")"

# 10. What holds now.
echo "--- limits check"
cat /run/tw/limits.json 2>/dev/null || echo "no result"
for u in tw-policy tw-git tw-files tw-proxy; do
  printf '%-10s %s\n' "$u" "$(systemctl is-active "$u.service" || true)"
done
