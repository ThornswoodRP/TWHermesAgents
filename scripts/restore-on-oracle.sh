#!/usr/bin/env bash
# Restore the Oracle VM's Hermes state from one of the encrypted backups One
# pulled (02 section 7; attack test 21).
#
# Runs on One, from a clone of TWHermesAgents:
#
#   scripts/restore-on-oracle.sh <path to hermes-YYYYMMDD-HHMMSS.tar.gz.age>
#
# One decrypts it with the age key only One holds, copies the plain archive to
# a private folder on the VM, and root there stops the gateway and the
# brokers, puts the files back with tw-backup restore (which refuses an
# archive holding anything it should not, and kills every Main, publish and
# cutover record), starts them again and deletes the plain copy. Sign-ins,
# .env files and keys are never in a backup: after a restore onto a new VM
# they are set again by hand, as the install script's STOP lines say.
set -euo pipefail

archive="${1:?usage: restore-on-oracle.sh <hermes-YYYYMMDD-HHMMSS.tar.gz.age>}"
host="thornswood-bots"
[[ -f "$archive" && "$(basename "$archive")" =~ ^hermes-[0-9]{8}-[0-9]{6}\.tar\.gz\.age$ ]] || {
  echo "STOP: give one of the backups Pull-HermesBackup.ps1 keeps, not '$archive'."; exit 1; }

key_dir="${TWH_AGE_KEY_DIR:-${LOCALAPPDATA:-$HOME/AppData/Local}/hermes/backup-key}"
mapfile -t keys < <(grep -lE '^AGE-SECRET-KEY-1' "$key_dir"/* 2>/dev/null || true)
[[ ${#keys[@]} -eq 1 ]] || { echo "STOP: found ${#keys[@]} age private keys in $key_dir; a restore needs exactly one."; exit 1; }

scratch="$(mktemp -d)"
remote=""
cleanup() {
  rm -rf "$scratch"
  if [[ -n "$remote" ]]; then ssh "$host" "rm -rf '$remote'"; fi
}
trap cleanup EXIT
chmod 700 "$scratch"
age --decrypt -i "${keys[0]}" -o "$scratch/backup.tar.gz" "$archive"
tar -tzf "$scratch/backup.tar.gz" | grep -qx 'MANIFEST.json' || { echo "STOP: $archive decrypts but is not a Hermes backup."; exit 1; }

cat > "$scratch/restore.sh" <<'REMOTE'
#!/usr/bin/env bash
set -euo pipefail
plain="$1"
[[ $EUID -eq 0 ]] || { echo "STOP: this half runs as root (sudo)."; exit 1; }
user_gateway=""
if systemctl --machine=hermes@.host --user is-active --quiet hermes-gateway.service; then user_gateway=1; fi
units="tw-hermes-gateway tw-proxy tw-files tw-git tw-policy"
echo "Stopping the gateway and the brokers."
[[ -n "$user_gateway" ]] && systemctl --machine=hermes@.host --user stop hermes-gateway.service
for u in $units; do systemctl stop "$u.service" 2>/dev/null || true; done
status=0
/opt/thornswood-hermes/bin/tw-backup restore "$plain" || status=$?
echo "Starting them again."
for u in tw-policy tw-git tw-files tw-proxy; do systemctl start "$u.service"; done
if systemctl is-enabled --quiet tw-hermes-gateway.service 2>/dev/null; then systemctl start tw-hermes-gateway.service
elif [[ -n "$user_gateway" ]]; then systemctl --machine=hermes@.host --user start hermes-gateway.service; fi
for u in tw-policy tw-git tw-files tw-proxy tw-hermes-gateway; do
  printf '%-18s %s\n' "$u" "$(systemctl is-active "$u.service" || true)"
done
exit $status
REMOTE

remote="$(ssh "$host" "mktemp -d /tmp/twh-restore.XXXXXXXX")"
[[ "$remote" =~ ^/tmp/twh-restore\.[A-Za-z0-9]{8}$ ]] || { echo "STOP: the VM did not make a private folder ($remote)."; remote=""; exit 1; }
scp -q "$scratch/backup.tar.gz" "$scratch/restore.sh" "$host:$remote/"
ssh -t "$host" "sudo bash '$remote/restore.sh' '$remote/backup.tar.gz'"
