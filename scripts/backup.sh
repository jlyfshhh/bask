#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

root="${BASK_INSTALL_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
[[ -d "$root" ]] || { echo "No Bask install directory at $root." >&2; exit 1; }
root="$(cd -- "$root" && pwd -P)"
path_is_protected() {
  local path="$1"
  case "$path" in
    /|/bin|/bin/*|/boot|/boot/*|/dev|/dev/*|/etc|/etc/*|/lib|/lib/*|/lib64|/lib64/*|/private/etc|/private/etc/*|/private/tmp|/private/tmp/*|/private/var|/private/var/*|/proc|/proc/*|/run|/run/*|/sbin|/sbin/*|/sys|/sys/*|/tmp|/tmp/*|/usr|/usr/*|/var|/var/*|/home|/mnt|/opt|/root|/srv|"${HOME:-/nonexistent}") return 0 ;;
  esac
  return 1
}
path_is_protected "$root" && {
  echo "BASK_INSTALL_DIR must be a dedicated application directory, not $root." >&2
  exit 1
}

env_value() {
  local key="$1"
  [[ -f "$root/.env" ]] || return 0
  sed -n "s/^${key}=//p" "$root/.env" | tail -n 1 | tr -d '\r'
}

data_setting="${BASK_DATA_PATH:-$(env_value BASK_DATA_PATH)}"
backup_setting="${BASK_BACKUP_PATH:-$(env_value BASK_BACKUP_PATH)}"
allow_external="${BASK_ALLOW_EXTERNAL_PATHS:-$(env_value BASK_ALLOW_EXTERNAL_PATHS)}"
data_setting="${data_setting:-./data}"
backup_setting="${backup_setting:-./backups}"
allow_external="${allow_external:-false}"

reject_path_text() {
  local label="$1" value="$2"
  case "$value" in
    ""|-*|*$'\n'*|*$'\r'*) echo "$label is not a safe path value." >&2; exit 1 ;;
    \'*|\"*|*\'|*\") echo "$label must be an unquoted path value; quote characters at the edges are ambiguous in .env." >&2; exit 1 ;;
    *'$'*|*'`'*|*\\*|*:*|*'#'*) echo "$label contains characters that Docker Compose and this tool would interpret differently." >&2; exit 1 ;;
  esac
}
reject_path_text BASK_DATA_PATH "$data_setting"
reject_path_text BASK_BACKUP_PATH "$backup_setting"
case "$allow_external" in true|false) ;; *) echo "BASK_ALLOW_EXTERNAL_PATHS must be true or false." >&2; exit 1 ;; esac

create_internal_directory() {
  local candidate="$1" remainder component current next
  case "$candidate" in "$root"/*) remainder="${candidate#"$root"/}" ;; *) return 1 ;; esac
  current="$root"
  while [[ -n "$remainder" ]]; do
    case "$remainder" in
      */*) component="${remainder%%/*}"; remainder="${remainder#*/}" ;;
      *) component="$remainder"; remainder="" ;;
    esac
    case "$component" in ""|.|..) return 1 ;; esac
    next="$current/$component"
    [[ ! -L "$next" ]] || return 1
    if [[ -e "$next" ]]; then
      [[ -d "$next" ]] || return 1
    else
      mkdir -- "$next" || return 1
      chmod 0700 "$next" || return 1
    fi
    current="$next"
  done
}

resolve_path() {
  local label="$1" value="$2" create="$3" candidate canonical
  case "$value" in /*) candidate="$value" ;; *) candidate="$root/${value#./}" ;; esac
  if [[ ! -e "$candidate" && "$create" == true ]]; then
    if ! create_internal_directory "$candidate"; then
      echo "External $label must be created explicitly before use." >&2
      exit 1
    fi
  fi
  [[ -d "$candidate" ]] || { echo "$label is not a directory: $candidate" >&2; exit 1; }
  canonical="$(cd -- "$candidate" && pwd -P)"
  if path_is_protected "$canonical" || [[ "$canonical" == "$root" ]]; then
    echo "$label must be a dedicated directory, not $canonical." >&2
    exit 1
  fi
  case "$canonical" in "$root"/*) ;; *)
    [[ "$allow_external" == true ]] || {
      echo "$label resolves outside $root. Set BASK_ALLOW_EXTERNAL_PATHS=true only for a dedicated mount." >&2
      exit 1
    }
    ;;
  esac
  printf '%s' "$canonical"
}

data_dir="$(resolve_path BASK_DATA_PATH "$data_setting" false)"
backup_dir="$(resolve_path BASK_BACKUP_PATH "$backup_setting" true)"
case "$data_dir/" in "$backup_dir/"*) echo "Bask data and backup paths cannot contain one another." >&2; exit 1 ;; esac
case "$backup_dir/" in "$data_dir/"*) echo "Bask data and backup paths cannot contain one another." >&2; exit 1 ;; esac

for name in config.json readings.db cielo-secrets.json vesync-secrets.json vesync-token.json alert-state.json; do
  [[ ! -L "$data_dir/$name" ]] || { echo "Refusing a symlinked Bask data file: $data_dir/$name" >&2; exit 1; }
done

stamp="$(date +%Y%m%d-%H%M%S)"
work="$(mktemp -d "$backup_dir/.bask-$stamp.XXXXXX")"
archive="${work}.tar.gz"
final="$backup_dir/bask-$stamp-${work##*.}.tar.gz"
cleanup() { rm -rf -- "$work" "$archive"; }
trap cleanup EXIT

copy_private() {
  local source="$1" target="$2"
  if [[ -r "$source" ]]; then
    install -m 0600 "$source" "$target"
  elif command -v sudo >/dev/null 2>&1; then
    sudo install -m 0600 -o "$(id -u)" -g "$(id -g)" "$source" "$target"
  else
    echo "Cannot read $source. Run this backup as a user with access or install sudo." >&2
    exit 1
  fi
}

for name in config.json cielo-secrets.json vesync-secrets.json vesync-token.json alert-state.json; do
  [[ ! -f "$data_dir/$name" ]] || copy_private "$data_dir/$name" "$work/$name"
done

if [[ -f "$data_dir/readings.db" ]]; then
  command -v sqlite3 >/dev/null 2>&1 || {
    echo "sqlite3 is required for a consistent live database backup." >&2
    exit 1
  }
  sqlite_destination="$work/readings.db"
  sqlite_quoted="${sqlite_destination//\'/\'\'}"
  if [[ -r "$data_dir/readings.db" ]]; then
    sqlite3 "$data_dir/readings.db" ".backup '$sqlite_quoted'"
  elif command -v sudo >/dev/null 2>&1; then
    sudo sqlite3 "$data_dir/readings.db" ".backup '$sqlite_quoted'"
    sudo chown "$(id -u):$(id -g)" "$sqlite_destination"
  else
    echo "Cannot read $data_dir/readings.db. Run this backup as a user with access or install sudo." >&2
    exit 1
  fi
  chmod 0600 "$sqlite_destination"
  [[ "$(sqlite3 "$sqlite_destination" 'PRAGMA quick_check;' 2>/dev/null || true)" == ok ]] || {
    echo "The SQLite backup did not pass its integrity check." >&2
    exit 1
  }
fi

# Refuse to claim success when there was nothing to protect.
find "$work" -type f -print -quit | grep -q . || {
  echo "Bask has no config or SQLite data to back up in $data_dir." >&2
  exit 1
}

tar -C "$(dirname -- "$work")" -czf "$archive" "$(basename -- "$work")"
tar -tzf "$archive" >/dev/null
chmod 0600 "$archive"
mv -- "$archive" "$final"
trap - EXIT
rm -rf -- "$work"
echo "$final"
