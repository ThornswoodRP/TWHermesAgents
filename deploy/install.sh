#!/usr/bin/env bash
# Phase 2 install on the Oracle VM, as root. Started by
# scripts/install-on-oracle.sh on One, from the root-owned copy of the
# commit Patrick merged:
#
#   /opt/thornswood-hermes/releases/<commit>/deploy/install.sh <commit> [--move-gateway]
#
# Idempotent: run it again after any merge to TWHermesAgents. It makes the
# broker users and folders, puts the units, slices, check helper socket and SFTP jail
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
# With --pids it prints their process ids, one a line, instead of the count.
#   stale_procs [--pids] <proc root> <uid> <gid> [<gid>...]
stale_procs() {
  local pids=0
  [[ "$1" == --pids ]] && { pids=1; shift; }
  local proc="$1" uid="$2"; shift 2
  local host_ns d n=0 groups g
  host_ns="$(readlink "$proc/1/ns/user" 2>/dev/null || true)"
  for d in "$proc"/[0-9]*; do
    [[ "$(awk '/^Uid:/{print $2}' "$d/status" 2>/dev/null)" == "$uid" ]] || continue
    [[ "$(readlink "$d/ns/user" 2>/dev/null || true)" == "$host_ns" ]] || continue
    [[ "$(awk -F'\t' '/^Name:/{print $2}' "$d/status" 2>/dev/null)" == "(sd-pam)" ]] && continue
    groups=" $(awk '/^Groups:/{$1=""; print}' "$d/status" 2>/dev/null) "
    for g in "$@"; do [[ "$groups" == *" $g "* ]] || { n=$((n + 1)); (( pids )) && echo "${d##*/}"; break; }; done
  done
  (( pids )) || echo "$n"
}

pol() { python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); v=d
for k in sys.argv[2].split("."): v=v[k]
print(json.dumps(v) if isinstance(v,(dict,list,bool)) else v)' "$here/thornswood_guard/policy.json" "$1"; }

bots="bramble rift sprocket echo magpie wisp"
workers="rift sprocket echo magpie wisp"
for tool in git openssl age podman systemd-run python3 sshd ssh-keygen; do
  command -v "$tool" >/dev/null || stop "$tool is not installed on the VM."
done
id hermes >/dev/null 2>&1 || stop "the hermes user does not exist."
hermes_uid="$(id -u hermes)"
hermes_home="$(getent passwd hermes | cut -d: -f6)"

# 1. Groups and users. The groups say who may connect; every op still checks
# the caller's user.
for g in tw-policy-clients tw-git-clients tw-files-clients tw-decide-bots tw-decide-git tw-checks tw-spool $(for b in $bots; do echo "tw-work-$b"; done); do
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
usermod -a -G tw-policy-clients,tw-spool,tw-decide-git tw-git
# tw-git alone reads Patrick's answers on outside pull request cards (#132).
[[ "$(getent group tw-decide-git | cut -d: -f4)" == tw-git ]] || stop "the tw-decide-git group's members are '$(getent group tw-decide-git | cut -d: -f4)'; it must be tw-git alone."
usermod -a -G "tw-policy-clients,tw-checks,tw-spool,$(for b in $bots; do printf 'tw-work-%s,' "$b"; done | sed 's/,$//')" tw-files
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

# 3b. Bramble's wake (policy wake): the secret GitHub signs Bramble's App's
# events with. Made here once, root's alone; systemd hands it to the gateway
# (tw-hermes-gateway-webhook.conf) and step 8b gives it to the App's webhook
# setting with the App's own key. It is never printed or copied by hand.
wake_env="$(pol wake.secret_file)"
if [[ ! -s "$wake_env" ]]; then
  (umask 077; printf 'WEBHOOK_SECRET=%s\n' "$(openssl rand -hex 32)" > "$wake_env")
fi
chown root:root "$wake_env"; chmod 0600 "$wake_env"
grep -qxE 'WEBHOOK_SECRET=[0-9a-f]{64}' "$wake_env" && [[ "$(grep -c . "$wake_env")" == 1 ]] \
  || stop "$wake_env must hold one WEBHOOK_SECRET=<64 hex characters> line; move it aside and run this again."
ok "the wake's webhook secret is in $wake_env, root's alone (0600)"

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

# Each Bot's proxy token is made whether or not a route is ready yet: it is no
# real key, and the proxy admits a route only once policy.yaml says it is
# ready, so a PR can turn a route on after the tokens are there.
tokens_file="$(pol admission_proxy.tokens_file)"
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
ok "the VM's read key for TWHermesAgents: $(cat "$read_key.pub")"

# 6. The code: this commit, root-owned, as current. Each launcher in $opt/bin
# is a link to its file in current, and finds its code from where it really is,
# so the one rename of current moves every launcher and the code they run
# together: no launcher of one release ever runs another release's code. The
# Hermes plugins Patrick's own Hermes loads are linked at $opt/plugins, so his
# plugin link names no folder inside a release.
link_in() { ln -sfnT "$1" "$2.new" && mv -T "$2.new" "$2"; }
install -d -m 0755 "$opt/bin"
for f in "$here"/deploy/bin/*; do link_in "$opt/current/deploy/bin/${f##*/}" "$opt/bin/${f##*/}"; done
link_in "$here" "$opt/current"
link_in "$here/plugins" "$opt/plugins"
ok "$opt/current is $commit"

# 7. Checks start through tw-checks.socket (step 8), never sudo: the sudoers
# line an earlier install made for tw-files goes, and the tw-checks group
# holds tw-files alone.
rm -f /etc/sudoers.d/tw-files /etc/sudoers.d/tw-files.new
members="$(getent group tw-checks | cut -d: -f4)"
[[ "$members" == tw-files ]] || stop "the tw-checks group holds '$members', not tw-files alone."

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
  # Those are the only group caps there are. A cap is kept by gid, so one left on a deleted group
  # passes to the next group given that gid: the canary deleted tw-work-canary with its 32 MB cap
  # on, tw-decide-voice got the gid, and the Whisper model's download was refused (2 Nov). So every
  # other cap is taken off here, and each one is named.
  keep=""
  for b in $bots; do keep+=" $(getent group "tw-work-$b" | cut -d: -f3)"; done
  report="$(repquota -g -n "$srv_mnt")" || stop "repquota could not read the group quotas on $srv_mnt."
  # shellcheck disable=SC2086  # the gids are words on purpose
  left="$(printf '%s\n' "$report" | python3 -I "$here/thornswood_brokers/disk_caps.py" $keep)" \
    || stop "the group quotas on $srv_mnt could not be read; what it printed is above."
  for gid in $left; do
    name="$(getent group "$gid" | cut -d: -f1 || true)"
    setquota -g "$gid" 0 0 0 0 "$srv_mnt" || stop "setquota could not take the cap off gid $gid on $srv_mnt."
    ok "took the cap off gid $gid (${name:-no group has it now}): only the Bots' work groups are capped"
  done
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
    tw-hermes-gateway.service.in|user-hermes.slice.conf|tw-hermes-gateway-webhook.conf) ;;  # placed below
    *.service|*.timer|*.slice|*.path|*.socket|*.target)
      sed "s/user@1002\.service/user@$hermes_uid.service/g" "$f" > "$unit_dir/$name" ;;
    *) stop "deploy/systemd/$name is not a kind of unit file the install places; add it to step 8." ;;
  esac
done
# end of the unit copy
install -d "/etc/systemd/system/user-$hermes_uid.slice.d"
install -m 0644 "$here/deploy/systemd/user-hermes.slice.conf" "/etc/systemd/system/user-$hermes_uid.slice.d/50-thornswood.conf"
install -d /etc/systemd/system/tw-hermes-gateway.service.d
install -m 0644 "$here/deploy/systemd/tw-hermes-gateway-webhook.conf" /etc/systemd/system/tw-hermes-gateway.service.d/50-github-webhook.conf
systemctl daemon-reload
systemctl enable --quiet tw-limits-check.service
systemctl restart tw-limits-check.service
systemctl enable --quiet --now tw-checks.socket tw-policy.service tw-git.service tw-files.service tw-proxy.service tw-snapshot.timer tw-backup.timer \
  tw-offsite.timer tw-offsite.path tw-follow-main.timer
# Each broker allows 3 starts in 15 minutes (StartLimitBurst), so a crash loop
# stops. An install's restart is not a crash: four installs inside 15 minutes
# (2 Nov, 10:06 PM) hit the limit, the brokers stayed down and the put-back's
# own restart was refused too. reset-failed clears the count for this restart
# only; a broker that then fails by itself still meets the limit.
systemctl reset-failed tw-policy.service tw-git.service tw-files.service tw-proxy.service
systemctl restart tw-policy.service tw-git.service tw-files.service tw-proxy.service
ok "units in place and started"

# 8b. Bramble's wake: Tailscale Funnel publishes the one webhook path, and
# only it, on wake.funnel_port, to the gateway's loopback receiver; then the
# App's webhook is pointed there with the secret from 3b. Two things come
# first and are Patrick's: the App's webhook turned on (Active, the URL and
# the events, on its settings page) and the tailnet's funnel and https node
# attributes for this machine. Until both are there this step says which is
# missing, changes nothing and waits on nothing, and the install goes on.
hook_py() { python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from thornswood_brokers.app_hook import main; sys.exit(main(sys.argv[2:]))' "$here" "$@"; }
wake_path="/p/$(pol wake.app)/webhooks/$(pol wake.route)"
wake_target="http://127.0.0.1:$(pol wake.port)$wake_path"
wake_port="$(pol wake.funnel_port)"
funnel_ok() {
  local f rc
  f="$(mktemp)"
  tailscale funnel status --json > "$f" 2>/dev/null || echo '{}' > "$f"
  hook_py funnel "$f"; rc=$?
  rm -f "$f"
  return $rc
}
# Without the node attributes, `tailscale funnel` does not refuse: it prints
# an approval link and waits for an admin for as long as it takes, which held
# the #65 install on 2 Nov. So it is asked only once this machine holds them,
# with nothing on stdin and a bound, and the rest of the install goes on.
funnel_allowed() {
  local f rc
  f="$(mktemp)"
  tailscale status --json > "$f" 2>/dev/null || echo '{}' > "$f"
  hook_py allowed "$f"; rc=$?
  rm -f "$f"
  return $rc
}
# The App's webhook is turned on by hand (GitHub's API cannot), so it is
# asked first: until it is on, nothing is made public and nothing is set.
hook_rc=0
hook_out="$(hook_py hook)" || hook_rc=$?
if [[ $hook_rc -eq 3 ]]; then
  echo "SKIP Bramble's wake: the App's webhook is off, so Funnel was not asked for and nothing was set:"
  printf '  %s\n' "$hook_out"
  echo "  That is Patrick's step on the App's settings page; then run this again."
  wake_skipped=1
elif [[ $hook_rc -ne 0 ]]; then
  stop "the App's webhook could not be read: $hook_out"
elif ! command -v tailscale >/dev/null; then
  echo "SKIP Bramble's wake: tailscale is not installed on the VM."
  wake_skipped=1
elif ! funnel_ok >/dev/null; then
  if ! allowed_out="$(funnel_allowed)"; then
    echo "SKIP Bramble's wake: Funnel was not asked for, so nothing was made public and nothing waits:"
    printf '  %s\n' "$allowed_out"
    echo "  Those are the tailnet's settings (Patrick's step); then run this again."
    wake_skipped=1
  else
    funnel_rc=0
    funnel_out="$(timeout 60 tailscale funnel --bg --https="$wake_port" --set-path="$wake_path" "$wake_target" </dev/null 2>&1)" || funnel_rc=$?
    if [[ $funnel_rc -eq 124 ]]; then
      echo "SKIP Bramble's wake: tailscale funnel gave no answer in 60 s and was ended. It said:"
    elif [[ $funnel_rc -ne 0 ]]; then
      echo "SKIP Bramble's wake: Tailscale refused Funnel on port $wake_port, so nothing was made public. It said:"
    fi
    if [[ $funnel_rc -ne 0 ]]; then
      printf '  %s\n' "$funnel_out"
      wake_skipped=1
    fi
  fi
fi
if [[ -z "${wake_skipped:-}" ]]; then
  funnel_ok || stop "Funnel is not as the wake needs it (above); tailscale funnel status shows what is public."
  dns="$(tailscale status --json | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
  hook_py set "https://$dns:$wake_port$wake_path" || stop "the App's webhook was not set; Funnel stays on, and the gateway refuses every event without the signature."
fi

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
  # Only the processes without the groups are waited for. Desktop reconnects
  # within seconds, and its new sessions already have the groups, so waiting
  # for no hermes process at all never ends (2 Nov, 8:24 PM: seven dashboard
  # sessions were back before the wait was over). One still running after
  # SIGTERM gets SIGKILL, as systemd would give it, and is named first.
  for _ in $(seq 1 30); do
    (( $(stale_procs /proc "$hermes_uid" "${client_gids[@]}") == 0 )) && break
    sleep 1
  done
  left="$(stale_procs --pids /proc "$hermes_uid" "${client_gids[@]}")"
  if [[ -n "$left" ]]; then
    echo "still running 30 seconds after SIGTERM, without the groups (pid name): $(for p in $left; do printf '%s %s; ' "$p" "$(cat "/proc/$p/comm" 2>/dev/null || echo gone)"; done)"
    kill -KILL $left 2>/dev/null || true
    for _ in $(seq 1 10); do
      (( $(stale_procs /proc "$hermes_uid" "${client_gids[@]}") == 0 )) && break
      sleep 1
    done
  fi
  systemctl start "user@$hermes_uid.service"
  if [[ "$gateway_was" == enabled ]]; then
    systemctl reset-failed tw-hermes-gateway.service
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
