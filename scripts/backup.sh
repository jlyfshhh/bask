#!/usr/bin/env bash
set -euo pipefail
umask 077

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
data_dir="${BASK_DATA_PATH:-$root/data}"
backup_dir="${BASK_BACKUP_PATH:-$root/backups}"
stamp="$(date +%Y%m%d-%H%M%S)"
dest="$backup_dir/bask-$stamp"

mkdir -p "$dest"

copy_private() {
  local source="$1" target="$2"
  if [[ -r "$source" ]]; then
    install -m 600 "$source" "$target"
  elif command -v sudo >/dev/null 2>&1; then
    # Bask's container runs as root so credentials it creates in the bind mount
    # can be root-owned. Copy through sudo, but hand the backup back to the
    # invoking keeper and never loosen its permissions.
    sudo install -m 600 -o "$(id -u)" -g "$(id -g)" "$source" "$target"
  else
    echo "Cannot read $source. Run this backup as a user with access or install sudo." >&2
    exit 1
  fi
}

if [[ -f "$data_dir/config.json" ]]; then
  copy_private "$data_dir/config.json" "$dest/config.json"
fi

# This is the private filesystem backup, so preserve the status-only Cielo
# integration when configured. Keep the copied secret owner-readable only; the
# public Manage-page export intentionally continues to omit it.
if [[ -f "$data_dir/cielo-secrets.json" ]]; then
  copy_private "$data_dir/cielo-secrets.json" "$dest/cielo-secrets.json"
fi

# Preserve the optional read-only VeSync humidifier connection in private
# filesystem backups. Portable browser exports continue to omit both files.
for secret in vesync-secrets.json vesync-token.json; do
  if [[ -f "$data_dir/$secret" ]]; then
    copy_private "$data_dir/$secret" "$dest/$secret"
  fi
done

if [[ -f "$data_dir/readings.db" ]]; then
  command -v sqlite3 >/dev/null 2>&1 || {
    echo "sqlite3 is required for a consistent live database backup." >&2
    exit 1
  }
  if [[ -r "$data_dir/readings.db" ]]; then
    sqlite3 "$data_dir/readings.db" ".backup '$dest/readings.db'"
  elif command -v sudo >/dev/null 2>&1; then
    sudo sqlite3 "$data_dir/readings.db" ".backup '$dest/readings.db'"
    sudo chown "$(id -u):$(id -g)" "$dest/readings.db"
    chmod 600 "$dest/readings.db"
  else
    echo "Cannot read $data_dir/readings.db. Run this backup as a user with access or install sudo." >&2
    exit 1
  fi
fi

# Every file above is written 0600 because the archive can contain the VeSync
# account password and the Cielo API key. tar obeys the umask, so without this
# the archive itself lands 0644 and undoes that — create it private, then fill it.
umask 077
tar -C "$backup_dir" -czf "$dest.tar.gz" "$(basename "$dest")"
chmod 600 "$dest.tar.gz"
rm -rf "$dest"
echo "$dest.tar.gz"
