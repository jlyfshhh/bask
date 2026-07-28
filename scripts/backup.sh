#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
data_dir="${BASK_DATA_PATH:-$root/data}"
backup_dir="${BASK_BACKUP_PATH:-$root/backups}"
stamp="$(date +%Y%m%d-%H%M%S)"
dest="$backup_dir/bask-$stamp"

mkdir -p "$dest"

if [[ -f "$data_dir/config.json" ]]; then
  cp "$data_dir/config.json" "$dest/config.json"
fi

if [[ -f "$data_dir/readings.db" ]]; then
  command -v sqlite3 >/dev/null 2>&1 || {
    echo "sqlite3 is required for a consistent live database backup." >&2
    exit 1
  }
  sqlite3 "$data_dir/readings.db" ".backup '$dest/readings.db'"
fi

tar -C "$backup_dir" -czf "$dest.tar.gz" "$(basename "$dest")"
rm -rf "$dest"
echo "$dest.tar.gz"
