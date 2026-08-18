#!/usr/bin/env bash
# Private filesystem-backup regression: consistent SQLite snapshots, private
# archives, path containment, and clean failure without partial success files.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d "$root/.backup-test.XXXXXX")"
cleanup() { rm -rf -- "$work"; }
trap cleanup EXIT

new_fixture() {
  local name="$1" install
  install="$work/$name"
  mkdir -p "$install/data" "$install/backups"
  printf '%s\n' "$install"
}

run_backup() {
  local install="$1"
  BASK_INSTALL_DIR="$install" \
  BASK_DATA_PATH=./data \
  BASK_BACKUP_PATH=./backups \
  BASK_ALLOW_EXTERNAL_PATHS=false \
    bash "$root/scripts/backup.sh"
}

install="$(new_fixture "keeper bask")"
printf 'BASK_DATA_PATH=./data\nBASK_BACKUP_PATH=./backups\n' > "$install/.env"
printf '{"name":"private-test"}\n' > "$install/data/config.json"
printf '{"pending":true}\n' > "$install/data/alert-state.json"
printf '{"token":"private"}\n' > "$install/data/vesync-token.json"
sqlite3 "$install/data/readings.db" \
  'CREATE TABLE readings(id INTEGER PRIMARY KEY, value REAL); INSERT INTO readings(value) VALUES(71.25);'

archive_one="$(run_backup "$install")"
archive_two="$(run_backup "$install")"
[[ "$archive_one" != "$archive_two" ]]
for archive in "$archive_one" "$archive_two"; do
  # GNU first, BSD second — and the order matters. `stat -f` on Linux means
  # "filesystem status", so it *succeeds* and prints block counts instead of
  # failing over to the GNU form. `stat -c` genuinely fails on BSD, so trying it
  # first is the only ordering that works on both.
  [[ -f "$archive" && "$(stat -c '%a' "$archive" 2>/dev/null || stat -f '%Lp' "$archive")" == 600 ]]
  tar -tzf "$archive" >/dev/null
done

restore="$work/restore"
mkdir -p "$restore"
tar -xzf "$archive_one" -C "$restore"
saved_config="$(find "$restore" -type f -name config.json -print -quit)"
saved_db="$(find "$restore" -type f -name readings.db -print -quit)"
saved_alert="$(find "$restore" -type f -name alert-state.json -print -quit)"
saved_env="$(find "$restore" -type f -name .env -print -quit)"
cmp "$install/.env" "$saved_env"
cmp "$install/data/config.json" "$saved_config"
cmp "$install/data/alert-state.json" "$saved_alert"
sqlite3 "$saved_db" 'PRAGMA quick_check;' | grep -qx ok
sqlite3 "$saved_db" 'SELECT value FROM readings;' | grep -qx 71.25
echo "  private, verified config and SQLite archives pass integrity checks"

empty="$(new_fixture empty)"
if run_backup "$empty" > "$empty/out" 2> "$empty/err"; then
  echo "An empty data directory reported a successful backup." >&2
  exit 1
fi
find "$empty/backups" -maxdepth 1 -type f -name 'bask-*.tar.gz' -print -quit | grep -q . && {
  echo "An empty backup left a success-looking archive." >&2
  exit 1
}

corrupt="$(new_fixture corrupt)"
printf 'not sqlite\n' > "$corrupt/data/readings.db"
if run_backup "$corrupt" > "$corrupt/out" 2> "$corrupt/err"; then
  echo "A corrupt SQLite source reported a successful backup." >&2
  exit 1
fi
find "$corrupt/backups" -maxdepth 1 -type f -name 'bask-*.tar.gz' -print -quit | grep -q . && {
  echo "A failed SQLite backup left a success-looking archive." >&2
  exit 1
}

symlinked="$(new_fixture symlinked)"
printf '{"outside":true}\n' > "$symlinked/outside.json"
ln -s "$symlinked/outside.json" "$symlinked/data/config.json"
if run_backup "$symlinked" > "$symlinked/out" 2> "$symlinked/err"; then
  echo "A symlinked private data file was followed into a backup." >&2
  exit 1
fi
echo "  empty, corrupt, and symlinked sources fail without partial archives"

overlap="$(new_fixture overlap)"
printf '{"overlap":true}\n' > "$overlap/data/config.json"
if BASK_INSTALL_DIR="$overlap" BASK_DATA_PATH=./data BASK_BACKUP_PATH=./data/backups \
   BASK_ALLOW_EXTERNAL_PATHS=false bash "$root/scripts/backup.sh" \
   > "$overlap/out" 2> "$overlap/err"; then
  echo "Nested data and backup paths were accepted." >&2
  exit 1
fi
[[ ! -e "$overlap/data/backups" ]]

external="$work/external"
mkdir -p "$external" "$overlap/backups"
if BASK_INSTALL_DIR="$overlap" BASK_DATA_PATH="$external" BASK_BACKUP_PATH=./backups \
   BASK_ALLOW_EXTERNAL_PATHS=false bash "$root/scripts/backup.sh" \
   > "$overlap/external.out" 2> "$overlap/external.err"; then
  echo "An external data path was accepted without explicit opt-in." >&2
  exit 1
fi
echo "  backup destinations stay canonical, separate, and explicitly scoped"

echo "Standalone Bask backup tests passed."
