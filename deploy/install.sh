#!/usr/bin/env bash
# System setup, legacy migration, and transactional Docker deployment for Bask.
set -Eeuo pipefail
umask 077

requested_project_dir="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
run_user="${2:-${SUDO_USER:-$(id -un)}}"
requested_source_dir="${3:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
health_attempts="${BASK_INSTALL_HEALTH_ATTEMPTS:-60}"
health_interval="${BASK_INSTALL_HEALTH_INTERVAL:-1}"
rollback_root=""
rollback_armed=false
service_touched=false
pull_started=false
legacy_touched=false
config_created=false
database_created=false
marker_created=false
verified_backup_contents=""
rollback_data_files=(
  config.json readings.db cielo-secrets.json vesync-secrets.json
  vesync-token.json alert-state.json
)

say() { printf '\n==> %s\n' "$1"; }
die() { printf 'Error: %s\n' "$1" >&2; exit 1; }

if [[ "$EUID" -ne 0 && "${BASK_INSTALLER_TEST_MODE:-false}" != true ]]; then
  die "Run with sudo: sudo bash deploy/install.sh"
fi
[[ "$health_attempts" =~ ^[1-9][0-9]*$ ]] || die "BASK_INSTALL_HEALTH_ATTEMPTS must be a positive whole number."
[[ "$health_interval" =~ ^[0-9]+$ ]] || die "BASK_INSTALL_HEALTH_INTERVAL must be a non-negative whole number."

reject_unsafe_path_text() {
  local label="$1" value="$2"
  case "$value" in
    "") die "$label cannot be blank." ;;
    -*) die "$label cannot begin with a dash." ;;
    *$'\n'*|*$'\r'*) die "$label cannot contain line breaks." ;;
    *\'*|*\"*) die "$label cannot contain quote characters; Docker Compose and this installer do not parse them identically." ;;
    *'$'*|*'`'*|*\\*|*:*|*'#'*) die "$label contains characters that Docker Compose and this installer would interpret differently." ;;
  esac
}

reject_unsafe_path_text BASK_INSTALL_DIR "$requested_project_dir"
reject_unsafe_path_text BASK_SOURCE_DIR "$requested_source_dir"
[[ -d "$requested_project_dir" ]] || die "Bask's install directory does not exist: $requested_project_dir"
[[ -d "$requested_source_dir" ]] || die "Bask's candidate source directory does not exist: $requested_source_dir"
project_dir="$(cd -- "$requested_project_dir" && pwd -P)"
source_dir="$(cd -- "$requested_source_dir" && pwd -P)"
run_group="$(id -gn "$run_user")"
run_uid="$(id -u "$run_user")"
run_gid="$(id -g "$run_user")"
run_home="$(getent passwd "$run_user" 2>/dev/null | awk -F: 'NR == 1 {print $6}' || true)"
host="$(hostname)"

install_path_is_protected() {
  local path="$1"
  case "$path" in
    /|/bin|/bin/*|/boot|/boot/*|/dev|/dev/*|/etc|/etc/*|/lib|/lib/*|/lib64|/lib64/*|/private/etc|/private/etc/*|/private/tmp|/private/tmp/*|/private/var|/private/var/*|/proc|/proc/*|/root|/root/*|/run|/run/*|/sbin|/sbin/*|/sys|/sys/*|/tmp|/tmp/*|/usr|/usr/*|/var|/var/*|/home|/mnt|/opt|/srv|"${run_home:-/nonexistent}") return 0 ;;
  esac
  return 1
}

install_path_is_protected "$project_dir" &&
  die "Bask's install path must be a dedicated application directory, not $project_dir."

for required in compose.yaml .env.example config.example.json scripts/backup.sh; do
  [[ -f "$source_dir/$required" && ! -L "$source_dir/$required" ]] ||
    die "The candidate release is missing a regular $required file."
done
bash -n "$source_dir/scripts/backup.sh" || die "The candidate backup tool has invalid shell syntax."

export DEBIAN_FRONTEND=noninteractive

install_docker() {
  echo "==> Installing Docker Engine and Compose from Docker's apt repository"
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  local arch
  arch="$(dpkg --print-architecture)"
  printf '%s\n' \
    "deb [arch=$arch signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $VERSION_CODENAME stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
}

if [[ "${BASK_INSTALL_SKIP_PACKAGES:-false}" != true ]]; then
  if ! command -v docker >/dev/null 2>&1; then
    install_docker
  elif ! docker compose version >/dev/null 2>&1; then
    die "Docker is installed, but the Compose plugin is missing. Install Docker Compose, then rerun Bask's installer."
  fi

  echo "==> Installing Bluetooth, mDNS, and backup tools"
  apt-get update -qq
  apt-get install -y -qq avahi-daemon bluez rfkill sqlite3 fonts-noto-color-emoji
  systemctl enable --now docker avahi-daemon >/dev/null 2>&1
  usermod -aG docker,bluetooth "$run_user" || true
  rfkill unblock bluetooth || true

  main_conf=/etc/bluetooth/main.conf
  if [[ -f "$main_conf" ]] &&
     ! grep -qE '^[[:space:]]*Experimental[[:space:]]*=[[:space:]]*true' "$main_conf"; then
    echo "==> Enabling BlueZ passive scanning support"
    if grep -qE '^[[:space:]]*#?[[:space:]]*Experimental[[:space:]]*=' "$main_conf"; then
      sed -i 's/^[[:space:]]*#\?[[:space:]]*Experimental[[:space:]]*=.*/Experimental = true/' "$main_conf"
    else
      grep -q '^\[General\]' "$main_conf" || printf '\n[General]\n' >> "$main_conf"
      sed -i '/^\[General\]/a Experimental = true' "$main_conf"
    fi
    systemctl restart bluetooth
  fi
fi

command -v docker >/dev/null 2>&1 || die "Docker is required."
docker compose version >/dev/null 2>&1 || die "Docker Compose is required."
command -v sqlite3 >/dev/null 2>&1 || die "sqlite3 is required for safe Bask backups."

# Exported variables must not silently redirect Compose to paths or ports that
# differ from the private .env file which was validated and will survive reboot.
compose_with() {
  local env_file="$1" compose_file="$2"
  shift 2
  (
    cd "$project_dir"
    env \
      -u BASK_PORT -u BASK_TAG -u BASK_DATA_PATH -u BASK_BACKUP_PATH \
      -u BASK_IMAGE -u BASK_ALLOW_EXTERNAL_PATHS -u BASK_BIND_ADDRESS \
      -u BASK_UID -u BASK_GID -u BASK_WEB_MEMORY_LIMIT \
      -u BASK_SCANNER_MEMORY_LIMIT -u SHED_DISPLAY_URL \
      -u SHED_DISPLAY_TOKEN -u COMPOSE_FILE -u COMPOSE_PROJECT_NAME \
      -u COMPOSE_PROFILES -u DOCKER_CONTEXT -u DOCKER_HOST \
      docker compose --project-directory "$project_dir" \
        --env-file "$env_file" -f "$compose_file" "$@"
  )
}

cleanup_rollback_files() {
  [[ -z "$rollback_root" || ! -d "$rollback_root" ]] || rm -rf -- "$rollback_root"
}
trap cleanup_rollback_files EXIT
rollback_root="$(mktemp -d "${TMPDIR:-/tmp}/bask-install.XXXXXX")"
state="$rollback_root/previous"
stage="$rollback_root/stage"
runtime="$stage/runtime"
mkdir -p -- "$state" "$runtime"

snapshot_file() {
  local source="$1" name="$2"
  if [[ -f "$source" ]]; then
    cp -p -- "$source" "$state/$name"
    : > "$state/$name.present"
  fi
}

restore_file() {
  local destination="$1" name="$2"
  if [[ -f "$state/$name.present" ]]; then
    mkdir -p -- "$(dirname -- "$destination")"
    cp -p -- "$state/$name" "$destination"
  else
    rm -f -- "$destination"
  fi
}

snapshot_file "$project_dir/.env" env
if [[ -f "$project_dir/compose.yaml" ]]; then
  cp -p -- "$project_dir/compose.yaml" "$state/compose.yaml"
  : > "$state/compose.present"
fi
if [[ -f "$state/env.present" ]]; then
  cp -p -- "$state/env" "$state/compose.env"
else
  : > "$state/compose.env"
fi

services=(bask bask-scanner)
previous_runtime=false
for service in "${services[@]}"; do
  if docker inspect "$service" >/dev/null 2>&1; then
    if [[ ! -f "$state/compose.present" ]]; then
      die "A container named $service exists, but $project_dir/compose.yaml is unavailable. Refusing to manage a container whose prior deployment cannot be restored."
    fi
    compose_working_dir="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$service" 2>/dev/null || true)"
    if [[ -z "$compose_working_dir" || ! -d "$compose_working_dir" ]]; then
      die "The existing $service container has no usable Docker Compose working-directory label. Remove or rename that foreign container before installing Bask."
    fi
    compose_working_dir="$(cd -- "$compose_working_dir" && pwd -P)"
    if [[ "$compose_working_dir" != "$project_dir" ]]; then
      die "The existing $service container belongs to $compose_working_dir, not $project_dir. Refusing to stop or replace a foreign container."
    fi
    previous_runtime=true
    : > "$state/$service.present"
    docker inspect --format '{{.Image}}' "$service" > "$state/$service.image.id"
    docker inspect --format '{{.Config.Image}}' "$service" > "$state/$service.image.ref"
    docker inspect --format '{{.State.Running}}' "$service" > "$state/$service.running"
  fi
done

previous_compose() {
  local args=("$state/compose.env" "$state/compose.yaml")
  if [[ -s "$state/image-override.yaml" ]]; then
    (
      cd "$project_dir"
      env \
        -u BASK_PORT -u BASK_TAG -u BASK_DATA_PATH -u BASK_BACKUP_PATH \
        -u BASK_IMAGE -u BASK_ALLOW_EXTERNAL_PATHS -u BASK_BIND_ADDRESS \
        -u BASK_UID -u BASK_GID -u BASK_WEB_MEMORY_LIMIT \
        -u BASK_SCANNER_MEMORY_LIMIT -u SHED_DISPLAY_URL \
        -u SHED_DISPLAY_TOKEN -u COMPOSE_FILE -u COMPOSE_PROJECT_NAME \
        -u COMPOSE_PROFILES -u DOCKER_CONTEXT -u DOCKER_HOST \
        docker compose --project-directory "$project_dir" \
          --env-file "${args[0]}" -f "${args[1]}" \
          -f "$state/image-override.yaml" "$@"
    )
  else
    compose_with "${args[0]}" "${args[1]}" "$@"
  fi
}

candidate_compose() {
  compose_with "$runtime/.env" "$runtime/compose.yaml" "$@"
}

if [[ "$previous_runtime" == true ]] && ! previous_compose config --quiet; then
  die "The prior Bask Compose configuration is invalid, so an exact rollback cannot be guaranteed. Repair $project_dir/compose.yaml before updating."
fi

restore_legacy_units() {
  local unit enabled active failed=false
  [[ "$legacy_touched" == true ]] || return 0
  for unit in bask-scanner.service bask-web.service; do
    [[ -f "$state/$unit.present" ]] || continue
    enabled="$(<"$state/$unit.enabled")"
    active="$(<"$state/$unit.active")"
    if [[ "$enabled" == enabled ]]; then
      systemctl enable "$unit" >/dev/null 2>&1 || failed=true
    else
      systemctl disable "$unit" >/dev/null 2>&1 || failed=true
    fi
    if [[ "$active" == active ]]; then
      systemctl start "$unit" >/dev/null 2>&1 || failed=true
    else
      systemctl stop "$unit" >/dev/null 2>&1 || failed=true
    fi
  done
  [[ "$failed" == false ]]
}

restore_verified_data() {
  local name source destination
  # A legacy migration can back up the old checkout root while creating a new
  # data directory. In that case the legacy originals were never modified and
  # the created-file flags below are the correct rollback path.
  if [[ -n "$verified_backup_contents" && "$backup_source" == "$data_path" ]]; then
    for name in "${rollback_data_files[@]}"; do
      destination="$data_path/$name"
      if [[ -f "$state/data-$name.present" ]]; then
        source="$verified_backup_contents/$name"
        [[ -f "$source" && ! -L "$source" ]] || return 1
        if [[ "$name" == readings.db ]]; then
          rm -f -- "$destination-wal" "$destination-shm"
        fi
        install -m 0600 -o "$run_user" -g "$run_group" "$source" "$destination" || return 1
      else
        rm -f -- "$destination"
        if [[ "$name" == readings.db ]]; then
          rm -f -- "$destination-wal" "$destination-shm"
        fi
      fi
    done
    return 0
  fi

  # A brand-new install has no archive yet. Put its setup back into the exact
  # pre-install state so a failed attempt cannot leave behind a keeper hash
  # whose one-time plaintext key was never shown to the user.
  restore_file "$data_path/config.json" app-config || return 1
  for name in readings.db cielo-secrets.json vesync-secrets.json vesync-token.json alert-state.json; do
    destination="$data_path/$name"
    [[ -f "$state/data-$name.present" ]] || rm -f -- "$destination"
    if [[ "$name" == readings.db ]]; then
      [[ -f "$state/data-$name.present" ]] || rm -f -- "$destination-wal" "$destination-shm"
    fi
  done
}

rollback_install() {
  local failed=false service image_id image_ref was_running
  rollback_armed=false
  trap - ERR INT TERM
  echo "Restoring the previous Bask configuration..." >&2

  if [[ "$service_touched" == true ]]; then
    candidate_compose down --remove-orphans >/dev/null 2>&1 || failed=true
  fi

  restore_file "$project_dir/.env" env || failed=true
  if ! restore_verified_data; then
    failed=true
    restore_file "$data_path/config.json" app-config || true
  fi
  if [[ "$database_created" == true && ! -f "$state/readings-db.present" ]]; then
    rm -f -- "$data_path/readings.db" "$data_path/readings.db-wal" "$data_path/readings.db-shm" || failed=true
  fi
  if [[ "$marker_created" == true && ! -f "$state/docker-marker.present" ]]; then
    rm -f -- "$data_path/.docker-migrated" || failed=true
  fi

  if [[ "$pull_started" == true ]]; then
    for service in "${services[@]}"; do
      [[ -f "$state/$service.present" ]] || continue
      image_id="$(<"$state/$service.image.id")"
      image_ref="$(<"$state/$service.image.ref")"
      if [[ -n "$image_id" && -n "$image_ref" &&
            "$image_ref" != *@sha256:* && "$image_ref" != sha256:* ]]; then
        docker image tag "$image_id" "$image_ref" >/dev/null 2>&1 || failed=true
      fi
    done
  fi

  if [[ "$service_touched" == true && "$previous_runtime" == true ]]; then
    if [[ ! -f "$state/compose.present" ]]; then
      failed=true
    else
      {
        echo 'services:'
        for service in "${services[@]}"; do
          [[ -f "$state/$service.present" ]] || continue
          printf '  %s:\n    image: "%s"\n' "$service" "$(<"$state/$service.image.id")"
        done
      } > "$state/image-override.yaml"
      previous_compose up -d --no-build --pull never >/dev/null 2>&1 || failed=true
      for service in "${services[@]}"; do
        if [[ -f "$state/$service.present" ]]; then
          was_running="$(<"$state/$service.running")"
          if [[ "$was_running" != true ]]; then
            previous_compose stop "$service" >/dev/null 2>&1 || failed=true
          fi
        else
          # The prior Compose file may not define a service introduced by the
          # candidate release. The candidate graph was already taken down;
          # remove a surviving container directly instead of asking an older
          # Compose schema to resolve an unknown service name.
          if docker inspect "$service" >/dev/null 2>&1; then
            docker rm -f "$service" >/dev/null 2>&1 || failed=true
          fi
        fi
      done
    fi
  fi

  restore_legacy_units || failed=true
  if [[ "$failed" == true ]]; then
    echo "Bask's records and pre-update backup were retained, but the previous service could not be restarted automatically." >&2
    return 1
  fi
  echo "The previous Bask service and settings were restored; no application data was removed." >&2
}

fail_with_rollback() {
  local message="$1"
  trap - ERR INT TERM
  if [[ "$rollback_armed" == true ]]; then
    rollback_install || true
  fi
  die "$message"
}

handle_unexpected_error() {
  local line="$1" status="$2"
  trap - ERR INT TERM
  if [[ "$rollback_armed" == true ]]; then
    rollback_install || true
  fi
  echo "Bask installation failed near line $line. No application data was removed." >&2
  exit "$status"
}

handle_signal() {
  local status="$1"
  trap - ERR INT TERM
  if [[ "$rollback_armed" == true ]]; then
    rollback_install || true
  fi
  exit "$status"
}
trap 'handle_unexpected_error "$LINENO" "$?"' ERR
trap 'handle_signal 130' INT
trap 'handle_signal 143' TERM

cp -p -- "$source_dir/compose.yaml" "$runtime/compose.yaml"
if [[ -f "$project_dir/.env" ]]; then
  cp -p -- "$project_dir/.env" "$runtime/.env"
else
  cp -p -- "$source_dir/.env.example" "$runtime/.env"
fi
chmod 0600 "$runtime/.env" "$runtime/compose.yaml"

env_value() {
  local key="$1" file="${2:-$runtime/.env}"
  sed -n "s/^${key}=//p" "$file" | tail -n 1 | tr -d '\r'
}

env_default() {
  local key="$1" value="$2"
  grep -q "^${key}=" "$runtime/.env" || printf '%s=%s\n' "$key" "$value" >> "$runtime/.env"
}

env_set() {
  local key="$1" value="$2"
  if grep -q "^${key}=" "$runtime/.env"; then
    sed -i.bak "s|^${key}=.*|${key}=${value}|" "$runtime/.env"
    rm -f -- "$runtime/.env.bak"
  else
    printf '%s=%s\n' "$key" "$value" >> "$runtime/.env"
  fi
}

env_default BASK_DATA_PATH ./data
env_default BASK_BACKUP_PATH ./backups
env_default BASK_ALLOW_EXTERNAL_PATHS false
env_default BASK_WEB_MEMORY_LIMIT 512m
env_default BASK_SCANNER_MEMORY_LIMIT 256m
env_set BASK_UID "$run_uid"
env_set BASK_GID "$run_gid"
chmod 0600 "$runtime/.env"

bask_uid="$(env_value BASK_UID)"
bask_gid="$(env_value BASK_GID)"
case "$bask_uid" in ''|*[!0-9]*|0) fail_with_rollback "BASK_UID must be a non-zero numeric uid." ;; esac
case "$bask_gid" in ''|*[!0-9]*) fail_with_rollback "BASK_GID must be a numeric gid." ;; esac
allow_external="$(env_value BASK_ALLOW_EXTERNAL_PATHS)"
case "$allow_external" in true|false) ;; *) fail_with_rollback "BASK_ALLOW_EXTERNAL_PATHS must be true or false." ;; esac

create_internal_directory() {
  local candidate="$1" remainder component current next
  case "$candidate" in "$project_dir"/*) remainder="${candidate#"$project_dir"/}" ;; *) return 1 ;; esac
  current="$project_dir"
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

path_is_protected() {
  local path="$1"
  case "$path" in
    /|/bin|/bin/*|/boot|/boot/*|/dev|/dev/*|/etc|/etc/*|/lib|/lib/*|/lib64|/lib64/*|/private/etc|/private/etc/*|/private/tmp|/private/tmp/*|/private/var|/private/var/*|/proc|/proc/*|/root|/root/*|/run|/run/*|/sbin|/sbin/*|/sys|/sys/*|/tmp|/tmp/*|/usr|/usr/*|/var|/var/*|/home|/mnt|/opt|/srv|"${run_home:-/nonexistent}"|"$project_dir") return 0 ;;
  esac
  return 1
}

prepare_storage_path() {
  local label="$1" raw_value="$2" path_var="$3" external_var="$4"
  local candidate canonical is_external=false
  reject_unsafe_path_text "$label" "$raw_value"
  case "$raw_value" in /*) candidate="$raw_value" ;; *) candidate="$project_dir/${raw_value#./}" ;; esac
  if [[ -e "$candidate" ]]; then
    [[ -d "$candidate" ]] || fail_with_rollback "$label is not a directory: $candidate"
  elif ! create_internal_directory "$candidate"; then
    fail_with_rollback "External $label must already be a dedicated directory, and internal paths cannot traverse symlinks or '..': $candidate"
  fi
  canonical="$(cd -- "$candidate" && pwd -P)"
  path_is_protected "$canonical" && fail_with_rollback "$label must be a dedicated directory, not $canonical."
  case "$canonical" in "$project_dir"/*) ;; *) is_external=true ;; esac
  if [[ "$is_external" == true && "$allow_external" != true ]]; then
    fail_with_rollback "$label resolves outside $project_dir. Set BASK_ALLOW_EXTERNAL_PATHS=true only for a dedicated, pre-created mount."
  fi
  printf -v "$path_var" '%s' "$canonical"
  printf -v "$external_var" '%s' "$is_external"
}

data_setting="$(env_value BASK_DATA_PATH)"
backup_setting="$(env_value BASK_BACKUP_PATH)"
prepare_storage_path BASK_DATA_PATH "${data_setting:-./data}" data_path data_external
prepare_storage_path BASK_BACKUP_PATH "${backup_setting:-./backups}" backup_path backup_external
case "$data_path/" in "$backup_path/"*) fail_with_rollback "BASK_DATA_PATH and BASK_BACKUP_PATH cannot contain one another." ;; esac
case "$backup_path/" in "$data_path/"*) fail_with_rollback "BASK_DATA_PATH and BASK_BACKUP_PATH cannot contain one another." ;; esac

device_id() {
  stat -c '%d' "$1" 2>/dev/null || stat -f '%d' "$1" 2>/dev/null
}

tree_has_nested_filesystem() {
  local path="$1" root_device entry entry_device
  root_device="$(device_id "$path")" || return 0
  while IFS= read -r -d '' entry; do
    entry_device="$(device_id "$entry")" || return 0
    [[ "$entry_device" == "$root_device" ]] || return 0
  done < <(find "$path" -xdev -mindepth 1 -type d -print0 2>/dev/null)
  return 1
}

verify_external_tree() {
  local path="$1" wrong
  [[ -z "$(find "$path" -xdev -type l -print -quit 2>/dev/null)" ]] ||
    fail_with_rollback "External Bask storage cannot contain symlinks: $path"
  ! tree_has_nested_filesystem "$path" ||
    fail_with_rollback "External Bask storage cannot contain nested filesystem mounts: $path"
  if ! wrong="$(find "$path" -xdev ! -user "$bask_uid" -print -quit 2>/dev/null)"; then
    fail_with_rollback "Cannot safely inspect external storage at $path."
  fi
  if [[ -n "$wrong" ]]; then
    printf 'External Bask storage is not owned by uid %s. Review and repair the dedicated mount explicitly; the installer will not recursively change it: %s\n' \
      "$bask_uid" "$path" >&2
    fail_with_rollback "Refusing to change an external directory recursively."
  fi
}
[[ "$data_external" != true ]] || verify_external_tree "$data_path"
[[ "$backup_external" != true ]] || verify_external_tree "$backup_path"

# Compose validation uses an isolated copy of both the candidate file and its
# .env. --project-directory keeps relative bind mounts anchored at the real
# install, not at the temporary worktree.
if ! candidate_compose config --quiet; then
  fail_with_rollback "The downloaded Bask Compose configuration is invalid. The running service was left unchanged."
fi

live_data_path="$data_path"
if [[ -f "$state/bask.present" ]]; then
  mounted_data="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{println .Source}}{{end}}{{end}}' bask 2>/dev/null | head -n 1 || true)"
  if [[ -n "$mounted_data" && -d "$mounted_data" ]]; then
    live_data_path="$(cd -- "$mounted_data" && pwd -P)"
    path_is_protected "$live_data_path" && fail_with_rollback "The running Bask container has an unsafe /data mount: $live_data_path"
    case "$live_data_path" in "$project_dir"/*) ;; *)
      [[ "$allow_external" == true ]] || fail_with_rollback "The running Bask data mount is outside $project_dir. Set BASK_ALLOW_EXTERNAL_PATHS=true after verifying it."
      ;;
    esac
  fi
fi
if [[ -f "$state/bask.present" && "$live_data_path" != "$data_path" ]]; then
  fail_with_rollback "The running Bask /data mount ($live_data_path) does not match BASK_DATA_PATH ($data_path). Reconcile .env before updating so rollback remains exact."
fi

for sensitive in "$live_data_path/config.json" "$live_data_path/readings.db" \
                 "$data_path/config.json" "$data_path/readings.db"; do
  [[ ! -L "$sensitive" ]] || fail_with_rollback "Refusing a symlinked Bask data file: $sensitive"
done
snapshot_file "$data_path/config.json" app-config
[[ ! -f "$data_path/readings.db" ]] || : > "$state/readings-db.present"
[[ ! -f "$data_path/.docker-migrated" ]] || : > "$state/docker-marker.present"
for name in "${rollback_data_files[@]}"; do
  [[ ! -f "$data_path/$name" ]] || : > "$state/data-$name.present"
done

verify_backup_archive() {
  local archive="$1" source_data="$2" verify_dir saved_env saved_config saved_db check
  [[ -f "$archive" && -s "$archive" ]] || return 1
  tar -tzf "$archive" >/dev/null 2>&1 || return 1
  verify_dir="$stage/verified-backup"
  rm -rf -- "$verify_dir"
  mkdir -p -- "$verify_dir"
  tar -xzf "$archive" -C "$verify_dir" || return 1
  verified_backup_contents="$(find "$verify_dir" -mindepth 1 -maxdepth 1 -type d -print -quit)"
  [[ -n "$verified_backup_contents" ]] || return 1
  if [[ -f "$project_dir/.env" ]]; then
    saved_env="$verified_backup_contents/.env"
    [[ -f "$saved_env" ]] && cmp -s "$project_dir/.env" "$saved_env" || return 1
  fi
  if [[ -f "$source_data/config.json" ]]; then
    saved_config="$(find "$verify_dir" -type f -name config.json -print -quit)"
    [[ -n "$saved_config" ]] && cmp -s "$source_data/config.json" "$saved_config" || return 1
  fi
  if [[ -f "$source_data/readings.db" ]]; then
    saved_db="$(find "$verify_dir" -type f -name readings.db -print -quit)"
    [[ -n "$saved_db" ]] || return 1
    check="$(sqlite3 "$saved_db" 'PRAGMA quick_check;' 2>/dev/null || true)"
    [[ "$check" == ok ]] || return 1
  fi
}

preupdate_backup=""
backup_source="$live_data_path"
legacy=false
if [[ ! -f "$data_path/.docker-migrated" &&
      ( -f "$project_dir/config.json" || -f "$project_dir/readings.db" ) ]]; then
  legacy=true
  if [[ ! -f "$live_data_path/config.json" && ! -f "$live_data_path/readings.db" ]]; then
    backup_source="$project_dir"
  fi
fi
if [[ -f "$backup_source/config.json" || -f "$backup_source/readings.db" ]]; then
  backup_error="$stage/backup.err"
  if ! preupdate_backup="$(BASK_INSTALL_DIR="$project_dir" \
      BASK_DATA_PATH="$backup_source" BASK_BACKUP_PATH="$backup_path" \
      BASK_ALLOW_EXTERNAL_PATHS="$allow_external" \
      bash "$source_dir/scripts/backup.sh" 2>"$backup_error")"; then
    [[ ! -s "$backup_error" ]] || cat "$backup_error" >&2
    fail_with_rollback "Bask could not create a pre-update config and SQLite backup, so the running service was left unchanged."
  fi
  verify_backup_archive "$preupdate_backup" "$backup_source" ||
    fail_with_rollback "Bask's pre-update backup could not be verified, so the running service was left unchanged."
  backup_parent="$(cd -- "$(dirname -- "$preupdate_backup")" && pwd -P)"
  case "$backup_parent" in "$backup_path") ;; *)
    fail_with_rollback "Bask's backup tool returned a file outside the validated backup directory."
    ;;
  esac
  chown "$run_user:$run_group" "$preupdate_backup"
  chmod 0600 "$preupdate_backup"
  echo "Verified pre-update backup: $preupdate_backup"
fi

# From here on a mutable image tag can move and the existing service can be
# interrupted. Every failure path restores the prior tag, settings, image IDs,
# Compose graph, and per-service running state.
rollback_armed=true
pull_started=true
if ! candidate_compose pull; then
  fail_with_rollback "Could not download the Bask image from ghcr.io. Check this machine's internet access and try again."
fi

for unit in bask-scanner.service bask-web.service; do
  if systemctl cat "$unit" >/dev/null 2>&1; then
    : > "$state/$unit.present"
    systemctl is-enabled "$unit" > "$state/$unit.enabled" 2>/dev/null || printf 'disabled\n' > "$state/$unit.enabled"
    systemctl is-active "$unit" > "$state/$unit.active" 2>/dev/null || printf 'inactive\n' > "$state/$unit.active"
    legacy_touched=true
    if ! systemctl disable --now "$unit" >/dev/null 2>&1; then
      fail_with_rollback "The legacy $unit unit could not be stopped safely; its previous service state was restored."
    fi
  fi
done

if [[ "$previous_runtime" == true ]]; then
  service_touched=true
  if ! previous_compose stop >/dev/null 2>&1; then
    fail_with_rollback "The prior Bask Compose graph could not be stopped cleanly; its exact previous state was restored."
  fi
else
  service_touched=true
fi

secure_internal_tree() {
  local path="$1"
  # Only canonical directories proven to be descendants of Bask's dedicated
  # install may be repaired recursively. External mounts are inspected above
  # and are never changed by the installer.
  case "$path" in "$project_dir"/*) ;; *) return 1 ;; esac
  # Never follow a symlink or cross into a nested filesystem while repairing
  # installer-owned storage. External trees are only inspected, never changed.
  [[ -z "$(find "$path" -xdev -type l -print -quit 2>/dev/null)" ]] || return 1
  ! tree_has_nested_filesystem "$path" || return 1
  chown -- "$bask_uid:$bask_gid" "$path"
  chmod 0700 "$path"
  find "$path" -xdev -mindepth 1 -exec chown -- "$bask_uid:$bask_gid" {} +
  find "$path" -xdev -mindepth 1 -type d -exec chmod 0700 {} +
  find "$path" -xdev -mindepth 1 -type f -exec chmod 0600 {} +
}
[[ "$data_external" == true ]] || secure_internal_tree "$data_path" ||
  fail_with_rollback "Bask could not secure its internal data directory."
[[ "$backup_external" == true ]] || secure_internal_tree "$backup_path" ||
  fail_with_rollback "Bask could not secure its internal backup directory."

cp -p -- "$runtime/.env" "$project_dir/.env"
chown "$run_user:$run_group" "$project_dir/.env"
chmod 0600 "$project_dir/.env"

if [[ "$legacy" == true ]]; then
  if [[ ! -f "$data_path/config.json" && -f "$project_dir/config.json" ]]; then
    install -m 0600 -o "$run_user" -g "$run_group" "$project_dir/config.json" "$data_path/config.json"
    config_created=true
  fi
  if [[ ! -f "$data_path/readings.db" && -f "$project_dir/readings.db" ]]; then
    (cd -- "$data_path" && sqlite3 "$project_dir/readings.db" '.backup readings.db')
    chown "$run_user:$run_group" "$data_path/readings.db"
    chmod 0600 "$data_path/readings.db"
    database_created=true
  fi
  if [[ ! -f "$data_path/.docker-migrated" ]]; then
    install -m 0600 -o "$run_user" -g "$run_group" /dev/null "$data_path/.docker-migrated"
    marker_created=true
  fi
fi

fresh_config=false
if [[ ! -f "$data_path/config.json" ]]; then
  install -m 0600 -o "$run_user" -g "$run_group" "$source_dir/config.example.json" "$data_path/config.json"
  fresh_config=true
  config_created=true
fi

keeper_key=""
if [[ "$fresh_config" == true ]]; then
  bask_tag="$(env_value BASK_TAG)"
  bask_image="ghcr.io/jlyfshhh/bask:${bask_tag:-latest}"
  docker image inspect "$bask_image" >/dev/null 2>&1 ||
    fail_with_rollback "The downloaded Bask image is unavailable for Head Keeper setup."
  if ! keeper_key="$(docker run --rm \
      --user "$run_uid:$run_gid" \
      -v "$data_path:/data" \
      --entrypoint python "$bask_image" -m server.init_keeper /data/config.json)"; then
    fail_with_rollback "Could not set up the Head Keeper key; Bask was not left unprotected."
  fi
fi

if ! candidate_compose up -d; then
  fail_with_rollback "Bask's updated containers could not start."
fi

verify_container_boundary() {
  local web_mounts scanner_mounts web_user scanner_user web_read_only
  local web_caps_add web_caps_drop web_security
  local scanner_read_only scanner_network scanner_caps_add scanner_caps_drop scanner_security
  local expected_data_mount scanner_bus_pattern unexpected_web unexpected_scanner
  web_mounts="$(docker inspect --format '{{range .Mounts}}{{printf "%s|%s|%t\n" .Source .Destination .RW}}{{end}}' bask 2>/dev/null || true)"
  scanner_mounts="$(docker inspect --format '{{range .Mounts}}{{printf "%s|%s|%t\n" .Source .Destination .RW}}{{end}}' bask-scanner 2>/dev/null || true)"
  web_user="$(docker inspect --format '{{.Config.User}}' bask 2>/dev/null || true)"
  scanner_user="$(docker inspect --format '{{.Config.User}}' bask-scanner 2>/dev/null || true)"
  web_read_only="$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' bask 2>/dev/null || true)"
  scanner_read_only="$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' bask-scanner 2>/dev/null || true)"
  web_caps_add="$(docker inspect --format '{{json .HostConfig.CapAdd}}' bask 2>/dev/null || true)"
  web_caps_drop="$(docker inspect --format '{{json .HostConfig.CapDrop}}' bask 2>/dev/null || true)"
  web_security="$(docker inspect --format '{{json .HostConfig.SecurityOpt}}' bask 2>/dev/null || true)"
  scanner_network="$(docker inspect --format '{{.HostConfig.NetworkMode}}' bask-scanner 2>/dev/null || true)"
  scanner_caps_add="$(docker inspect --format '{{json .HostConfig.CapAdd}}' bask-scanner 2>/dev/null || true)"
  scanner_caps_drop="$(docker inspect --format '{{json .HostConfig.CapDrop}}' bask-scanner 2>/dev/null || true)"
  scanner_security="$(docker inspect --format '{{json .HostConfig.SecurityOpt}}' bask-scanner 2>/dev/null || true)"
  expected_data_mount="$data_path|/data|true"
  scanner_bus_pattern='^/(var/)?run/dbus/system_bus_socket\|/var/run/dbus/system_bus_socket\|false$'
  unexpected_web="$(printf '%s\n' "$web_mounts" | grep -Fvx "$expected_data_mount" || true)"
  unexpected_scanner="$(printf '%s\n' "$scanner_mounts" | grep -Fvx "$expected_data_mount" | grep -Ev "$scanner_bus_pattern" || true)"
  [[ -z "$unexpected_web" && -z "$unexpected_scanner" ]] &&
    grep -Fxq "$expected_data_mount" <<<"$web_mounts" &&
    grep -Fxq "$expected_data_mount" <<<"$scanner_mounts" &&
    grep -Eq "$scanner_bus_pattern" <<<"$scanner_mounts" &&
    [[ -n "$web_user" && "$web_user" != 0 && "$web_user" != 0:* ]] &&
    [[ "$scanner_user" == 0 || "$scanner_user" == 0:* ]] &&
    [[ "$web_read_only" == true && "$scanner_read_only" == true && "$scanner_network" == none ]] &&
    [[ "$web_caps_add" == null && "$web_caps_drop" == '["ALL"]' ]] &&
    [[ "$web_security" == *'no-new-privileges:true'* ]] &&
    [[ "$scanner_caps_add" == '["DAC_OVERRIDE"]' ]] &&
    [[ "$scanner_caps_drop" == '["ALL"]' ]] &&
    [[ "$scanner_security" == *'no-new-privileges:true'* ]]
}

if ! verify_container_boundary; then
  candidate_compose logs --tail=80 >&2 || true
  fail_with_rollback "Bask's Bluetooth security boundary does not match the audited configuration."
fi

healthy=false
health="missing"
scanner_state="missing"
scanner_health="missing"
for _ in $(seq 1 "$health_attempts"); do
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' bask 2>/dev/null || true)"
  web_oom="$(docker inspect --format '{{.State.OOMKilled}}' bask 2>/dev/null || true)"
  scanner_state="$(docker inspect --format '{{.State.Status}}' bask-scanner 2>/dev/null || true)"
  scanner_health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' bask-scanner 2>/dev/null || true)"
  scanner_oom="$(docker inspect --format '{{.State.OOMKilled}}' bask-scanner 2>/dev/null || true)"
  if [[ "$health" == healthy && "$web_oom" != true &&
        "$scanner_state" == running && "$scanner_health" == running && "$scanner_oom" != true ]]; then
    healthy=true
    break
  fi
  if [[ "$health" == unhealthy || "$health" == exited || "$health" == dead || "$web_oom" == true ||
        "$scanner_health" == unhealthy || "$scanner_state" == exited || "$scanner_state" == dead || "$scanner_oom" == true ]]; then
    break
  fi
  sleep "$health_interval"
done
if [[ "$healthy" != true ]]; then
  candidate_compose logs --tail=80 >&2 || true
  fail_with_rollback "Bask did not become healthy (web=${health:-missing}, scanner=${scanner_health:-missing})."
fi

rollback_armed=false
echo
echo "────────────────────────────────────────────────────────────"
lan_ip="$(ip -4 -o addr show scope global 2>/dev/null | awk '$2 !~ /^(docker|br-|veth|virbr|tun|tap)/ {print $4}' | cut -d/ -f1 \
  | grep -E '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)' | head -n 1 || true)"
bask_port="$(env_value BASK_PORT "$project_dir/.env")"
[[ "$bask_port" =~ ^[0-9]+$ ]] || bask_port=8080
if [[ -n "$lan_ip" ]]; then
  echo "  Bask is running at http://${lan_ip}:${bask_port}"
  echo "                  or http://${host}.local:${bask_port}"
else
  echo "  Bask is running at http://${host}.local:${bask_port}"
fi
echo "  Persistent data: $data_path"
echo "  Backups:        $backup_path"
echo "  Back up now:     $project_dir/scripts/backup.sh"
if [[ -n "$preupdate_backup" ]]; then
  echo "  Update backup:   $preupdate_backup"
fi
if [[ -n "$keeper_key" ]]; then
  echo "────────────────────────────────────────────────────────────"
  echo "  Head Keeper key:  $keeper_key"
  echo
  echo "  Save this now — it is shown once and stored only as a hash."
  echo "  Anyone in the house can read the dashboard without it."
  echo "  It is needed to change sensors, enclosures, ranges, and integrations."
fi
echo "────────────────────────────────────────────────────────────"
