#!/usr/bin/env bash
# Install the brokers, units, slices and backups on the Oracle VM from the
# commit Patrick merged (Phase 2).
#
# Runs on One, from a clone of TWHermesAgents, after Patrick merges:
#
#   scripts/install-on-oracle.sh <full commit of main> [--move-gateway] [--quotas]
#
# Like send-to-oracle.sh, the VM never pulls: One checks the commit is main's
# tip, bundles it and copies it over Tailscale. On the VM, root unpacks it into
# /opt/thornswood-hermes/releases/<commit> (root-owned, so nothing the hermes
# user can write is ever run as a broker or as root) and runs
# deploy/install.sh there. sudo asks for the password on the VM if it needs
# one; ssh -t gives it a terminal to ask on.
#
# Then send-to-oracle.sh with the same commit applies the profiles as hermes.
set -euo pipefail

commit="${1:?usage: install-on-oracle.sh <full commit of main> [--move-gateway] [--quotas]}"
shift
move="$*"
host="thornswood-bots"
here="$(cd "$(dirname "$0")/.." && pwd)"

if [[ ! "$commit" =~ ^[0-9a-f]{40}$ ]]; then
  echo "STOP: give the full 40-character commit, not '$commit'."; exit 1
fi
for arg in "$@"; do
  [[ "$arg" == --move-gateway || "$arg" == --quotas ]] || { echo "STOP: the options are --move-gateway and --quotas."; exit 1; }
done

git -C "$here" fetch --quiet origin main
tip="$(git -C "$here" rev-parse origin/main)"
if [[ "$tip" != "$commit" ]]; then
  echo "STOP: main is at $tip, not $commit. Only main's tip, as Patrick merged it, is installed."; exit 1
fi

scratch="$(mktemp -d)"
remote=""
cleanup() {
  rm -rf "$scratch"
  if [[ -n "$remote" ]]; then ssh "$host" "rm -rf '$remote'"; fi
}
trap cleanup EXIT

bundle="$scratch/twh-install-$commit.bundle"
git -C "$here" bundle create --quiet "$bundle" "refs/remotes/origin/main"
git -C "$here" bundle verify --quiet "$bundle"
cat > "$scratch/twh-install-$commit.sh" <<'REMOTE'
#!/usr/bin/env bash
# Runs as root on the VM. Unpacks the bundle into a root-owned release folder
# and runs that folder's own deploy/install.sh.
set -euo pipefail
commit="$1"; bundle="$2"; move="${3:-}"
[[ $EUID -eq 0 ]] || { echo "STOP: this half runs as root (sudo)."; exit 1; }
releases=/opt/thornswood-hermes/releases
install -d -m 0755 -o root -g root /opt/thornswood-hermes "$releases"
dest="$releases/$commit"
if [[ -e "$dest" && "$(stat -c %U "$dest")" != root ]]; then
  echo "STOP: $dest exists and is not root's; look at it before anything runs from it."; exit 1
fi
if [[ ! -d "$dest/.git" ]]; then
  git init --quiet "$dest"
fi
git -C "$dest" fetch --quiet "$bundle" "+refs/remotes/origin/main:refs/remotes/origin/main"
git -C "$dest" -c advice.detachedHead=false checkout --quiet --force --detach "$commit"
git -C "$dest" clean -qfdx
chown -R root:root "$dest"
chmod -R go-w "$dest"
if [[ -f "$(dirname "$bundle")/backup-sftp.pub" ]]; then
  install -d -m 0755 /etc/thornswood
  install -m 0644 -o root -g root "$(dirname "$bundle")/backup-sftp.pub" /etc/thornswood/backup-sftp.pub
fi
exec bash "$dest/deploy/install.sh" "$commit" $move
REMOTE

# A fresh private folder on the VM: a fixed name under /tmp could be made
# first by another user (the hermes user included), and root runs what is in it.
remote="$(ssh "$host" "mktemp -d /tmp/twh-install.XXXXXXXX")"
[[ "$remote" =~ ^/tmp/twh-install\.[A-Za-z0-9]{8}$ ]] || { echo "STOP: the VM did not make a private folder ($remote)."; remote=""; exit 1; }
# One's key for pulling the backups (Pull-HermesBackup.ps1 -MakeKey), when
# One has one: its public half goes to the VM with the install.
pub="${TWH_BACKUP_PUB:-$HOME/.ssh/tw-backup-pull.pub}"
send=("$bundle" "$scratch/twh-install-$commit.sh")
if [[ -f "$pub" ]]; then
  grep -qE '^ssh-ed25519 [A-Za-z0-9+/=]+ one-pulls-hermes-backups$' "$pub" || { echo "STOP: $pub is not One's backup pull key."; exit 1; }
  cp "$pub" "$scratch/backup-sftp.pub"; send+=("$scratch/backup-sftp.pub")
else
  echo "No backup pull key on One yet ($pub); Pull-HermesBackup.ps1 -MakeKey makes it, then run this again."
fi
scp -q "${send[@]}" "$host:$remote/"
ssh -t "$host" "sudo bash '$remote/twh-install-$commit.sh' '$commit' '$remote/twh-install-$commit.bundle' '$move'"
