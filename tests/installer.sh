#!/usr/bin/env bash
# Standalone installer regression: staged updates, verified backups, canonical
# storage, exact runtime rollback, and a checkout that advances only on success.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d "$root/.installer-test.XXXXXX")"
protected_external=""
cleanup() {
  [[ -z "$protected_external" || ! -d "$protected_external" ]] || rmdir "$protected_external" 2>/dev/null || true
  [[ "${KEEP_TEST_WORK:-false}" == true ]] || rm -rf "$work"
}
trap cleanup EXIT
bin="$work/bin"
mkdir -p "$bin"

assert_no_grep() {
  if grep "$@"; then
    echo "Unexpected matching text found by grep $*." >&2
    exit 1
  fi
}

cat > "$bin/docker" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
state="${MOCK_DOCKER_STATE:?}"
mkdir -p "$state"
printf '%s\n' "$*" >> "$state/calls"

value() { local file="$1" fallback="${2:-}"; [[ -f "$state/$file" ]] && cat "$state/$file" || printf '%s' "$fallback"; }
set_value() { printf '%s' "$2" > "$state/$1"; }

command="${1:-}"
shift || true
case "$command" in
  compose)
    joined="compose $*"
    case "$joined" in
      "compose version") exit 0 ;;
      *" config --quiet") [[ "${MOCK_FAIL:-}" != config ]] ;;
      *" pull")
        if [[ "${REQUIRE_BACKUP_BEFORE_PULL:-false}" == true ]]; then
          find "$CURRENT_INSTALL/backups" -maxdepth 1 -type f -name 'bask-*.tar.gz' -print -quit | grep -q . || {
            echo "pull happened before a verified backup" >&2
            exit 19
          }
        fi
        [[ "${MOCK_FAIL:-}" != pull ]]
        ;;
      *" down --remove-orphans")
        for service in bask bask-scanner; do
          set_value "$service.exists" false
          set_value "$service.running" false
        done
        ;;
      *" up -d --no-build --pull never")
        for service in bask bask-scanner; do
          set_value "$service.exists" true
          set_value "$service.running" true
          set_value "$service.health" healthy
          set_value "$service.oom" false
          set_value "$service.image_id" "$(value "$service.old_image" "sha256:old-$service")"
        done
        ;;
      *" up -d")
        [[ "${MOCK_FAIL:-}" != up ]] || exit 20
        for service in bask bask-scanner; do
          set_value "$service.exists" true
          set_value "$service.running" true
          set_value "$service.oom" false
          set_value "$service.health" healthy
          set_value "$service.image_id" "sha256:new-$service"
          set_value "$service.image_ref" "ghcr.io/jlyfshhh/bask:latest"
        done
        if [[ "${MOCK_FAIL:-}" == health ]]; then set_value bask.health unhealthy; fi
        if [[ -n "${CURRENT_INSTALL:-}" && -f "$CURRENT_INSTALL/data/config.json" ]]; then
          printf '{"mutated_by_candidate":true}\n' > "$CURRENT_INSTALL/data/config.json"
          sqlite3 "$CURRENT_INSTALL/data/readings.db" 'UPDATE readings SET value=99.9;' 2>/dev/null || true
        fi
        ;;
      *" stop bask-scanner") set_value bask-scanner.running false ;;
      *" stop bask") set_value bask.running false ;;
      *" stop")
        if [[ "${MOCK_FAIL:-}" == stop ]]; then
          # Model a partially successful Compose stop: rollback must still
          # rebuild the exact prior graph and per-service running state.
          set_value bask.running false
          exit 21
        fi
        for service in bask bask-scanner; do
          [[ "$(value "$service.exists" false)" != true ]] || set_value "$service.running" false
        done
        ;;
      *" rm -sf bask-scanner") set_value bask-scanner.exists false; set_value bask-scanner.running false ;;
      *" rm -sf bask") set_value bask.exists false; set_value bask.running false ;;
      *" logs --tail=80") exit 0 ;;
      *) echo "Unexpected fake Compose invocation: $joined" >&2; exit 97 ;;
    esac
    ;;
  inspect)
    if [[ "${1:-}" == --format ]]; then
      format="$2"
      service="$3"
    else
      format=""
      service="${1:-}"
    fi
    [[ "$(value "$service.exists" false)" == true ]] || exit 1
    case "$format" in
      "") exit 0 ;;
      *'if eq .Destination "/data"'*) printf '%s\n' "${MOCK_DATA_MOUNT:-$CURRENT_INSTALL/data}" ;;
      *'.Mounts'*)
        case "$service" in
          bask) printf '%s|/data|true\n' "${MOCK_DATA_MOUNT:-$CURRENT_INSTALL/data}" ;;
          bask-scanner)
            if [[ "${MOCK_FAIL:-}" == boundary ]]; then
              printf '%s|/data|true\n/var/run/dbus/system_bus_socket|/var/run/dbus/system_bus_socket|true\n' "${MOCK_DATA_MOUNT:-$CURRENT_INSTALL/data}"
            else
              printf '%s|/data|true\n/var/run/dbus/system_bus_socket|/var/run/dbus/system_bus_socket|false\n' "${MOCK_DATA_MOUNT:-$CURRENT_INSTALL/data}"
            fi
            ;;
        esac
        ;;
      '{{.Image}}') value "$service.image_id" "sha256:new-$service" ;;
      '{{.Config.Image}}') value "$service.image_ref" 'ghcr.io/jlyfshhh/bask:latest' ;;
      *'com.docker.compose.project.working_dir'*) printf '%s\n' "${MOCK_WORKING_DIR:-$CURRENT_INSTALL}" ;;
      '{{.State.Running}}') value "$service.running" true ;;
      '{{.State.OOMKilled}}') value "$service.oom" false ;;
      '{{.State.Status}}')
        [[ "$(value "$service.running" false)" == true ]] && printf running || printf exited
        ;;
      *'.State.Health'*)
        [[ "$service" == bask-scanner ]] && value "$service.running" true | sed 's/^true$/running/; s/^false$/exited/' || value "$service.health" healthy
        ;;
      '{{.Config.User}}')
        case "$service" in bask-scanner) printf '0:0' ;; *) printf '%s:%s' "$(id -u)" "$(id -g)" ;; esac
        ;;
      '{{.HostConfig.ReadonlyRootfs}}') printf true ;;
      '{{.HostConfig.NetworkMode}}') [[ "$service" == bask-scanner ]] && printf none || printf default ;;
      '{{json .HostConfig.CapAdd}}') [[ "$service" == bask-scanner ]] && printf '["DAC_OVERRIDE"]' || printf 'null' ;;
      '{{json .HostConfig.CapDrop}}') printf '["ALL"]' ;;
      '{{json .HostConfig.SecurityOpt}}') printf '["no-new-privileges:true"]' ;;
      *) echo "Unexpected fake inspect format: $format" >&2; exit 96 ;;
    esac
    ;;
  image)
    case "${1:-}" in
      inspect) exit 0 ;;
      tag) exit 0 ;;
      *) exit 95 ;;
    esac
    ;;
  rm)
    service="${*: -1}"
    set_value "$service.exists" false
    set_value "$service.running" false
    ;;
  run)
    printf 'test-head-keeper-key\n'
    ;;
  *) echo "Unexpected fake Docker invocation: $command $*" >&2; exit 94 ;;
esac
SH

cat > "$bin/sudo" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
exec "$@"
SH

cat > "$bin/systemctl" <<'SH'
#!/usr/bin/env bash
[[ "${1:-}" != cat ]] || exit 1
exit 0
SH

cat > "$bin/getent" <<'SH'
#!/usr/bin/env bash
printf 'tester:x:%s:%s::%s:/bin/bash\n' "$(id -u)" "$(id -g)" "$HOME"
SH

cat > "$bin/hostname" <<'SH'
#!/usr/bin/env bash
printf 'bask-test\n'
SH

cat > "$bin/ip" <<'SH'
#!/usr/bin/env bash
exit 0
SH

cat > "$bin/sleep" <<'SH'
#!/usr/bin/env bash
exit 0
SH

cat > "$bin/chown" <<'SH'
#!/usr/bin/env bash
printf 'chown %s\n' "$*" >> "${MOCK_DOCKER_STATE:?}/calls"
exit 0
SH

chmod +x "$bin"/*

write_env() {
  local directory="$1" data="${2:-./data}" backups="${3:-./backups}" external="${4:-false}"
  mkdir -p "$directory"
  cat > "$directory/.env" <<ENV
TZ=America/New_York
BASK_PORT=8080
BASK_BIND_ADDRESS=0.0.0.0
BASK_DATA_PATH=$data
BASK_BACKUP_PATH=$backups
BASK_ALLOW_EXTERNAL_PATHS=$external
BASK_UID=$(id -u)
BASK_GID=$(id -g)
BASK_WEB_MEMORY_LIMIT=512m
BASK_SCANNER_MEMORY_LIMIT=256m
SHED_DISPLAY_URL=
SHED_DISPLAY_TOKEN=
ENV
  chmod 0600 "$directory/.env"
}

seed_update() {
  local fixture="$1" directory="$1/bask" state="$1/docker"
  mkdir -p "$directory/data" "$directory/backups" "$state"
  write_env "$directory"
  cp "$root/compose.yaml" "$directory/compose.yaml"
  # Production Bask is a two-service graph: web plus the isolated BLE scanner.
  printf '{"name":"original-config"}\n' > "$directory/data/config.json"
  sqlite3 "$directory/data/readings.db" 'CREATE TABLE readings(id INTEGER PRIMARY KEY, value REAL); INSERT INTO readings(value) VALUES(72.5);'
  cp "$directory/.env" "$fixture/original.env"
  cp "$directory/data/config.json" "$fixture/original.config"
  for service in bask bask-scanner; do
    printf true > "$state/$service.exists"
    printf true > "$state/$service.running"
    printf healthy > "$state/$service.health"
    printf false > "$state/$service.oom"
    printf 'sha256:old-%s' "$service" > "$state/$service.image_id"
    printf 'sha256:old-%s' "$service" > "$state/$service.old_image"
    printf 'ghcr.io/jlyfshhh/bask:latest' > "$state/$service.image_ref"
  done
  # Preserve and assert a deliberately stopped worker state.
  printf false > "$state/bask-scanner.running"
}

run_deploy() {
  local fixture="$1" source="${2:-$root}"
  local data_setting data_mount
  if [[ -f "$fixture/bask/.env" ]]; then
    data_setting="$(sed -n 's/^BASK_DATA_PATH=//p' "$fixture/bask/.env" | tail -n 1)"
  else
    data_setting=./data
  fi
  case "$data_setting" in
    /*) data_mount="$data_setting" ;;
    *) data_mount="$fixture/bask/${data_setting#./}" ;;
  esac
  if [[ -d "$data_mount" ]]; then
    data_mount="$(cd -- "$data_mount" && pwd -P)"
  fi
  PATH="$bin:/usr/bin:/bin" \
  HOME="$fixture/home" \
  BASK_INSTALLER_TEST_MODE=true \
  BASK_INSTALL_SKIP_PACKAGES=true \
  BASK_INSTALL_HEALTH_ATTEMPTS=2 \
  BASK_INSTALL_HEALTH_INTERVAL=0 \
  MOCK_DOCKER_STATE="$fixture/docker" \
  MOCK_DATA_MOUNT="$data_mount" \
  CURRENT_INSTALL="$fixture/bask" \
  bash "$source/deploy/install.sh" "$fixture/bask" "$(id -un)" "$source"
}

run_deploy_hostile_environment() {
  COMPOSE_FILE="$work/attacker-compose.yaml" \
  COMPOSE_PROJECT_NAME=attacker \
  COMPOSE_PROFILES=attacker \
  DOCKER_CONTEXT=attacker \
  DOCKER_HOST=tcp://attacker.invalid:2375 \
    run_deploy "$@"
}

assert_rollback() {
  local fixture="$1"
  cmp "$fixture/original.env" "$fixture/bask/.env"
  cmp "$fixture/original.config" "$fixture/bask/data/config.json"
  sqlite3 "$fixture/bask/data/readings.db" 'SELECT value FROM readings;' | grep -qx 72.5
  [[ "$(cat "$fixture/docker/bask.image_id")" == sha256:old-bask ]]
  [[ "$(cat "$fixture/docker/bask.running")" == true ]]
  [[ "$(cat "$fixture/docker/bask-scanner.image_id")" == sha256:old-bask-scanner ]]
  [[ "$(cat "$fixture/docker/bask-scanner.running")" == false ]]
  grep -q 'down --remove-orphans' "$fixture/docker/calls"
  grep -q 'up -d --no-build --pull never' "$fixture/docker/calls"
}

# Candidate validation is complete before backup, pull, or service interruption.
invalid="$work/invalid"
mkdir -p "$invalid/home"
seed_update "$invalid"
if MOCK_FAIL=config run_deploy "$invalid" > "$invalid/out" 2> "$invalid/err"; then
  echo "Invalid candidate Compose was accepted." >&2
  exit 1
fi
assert_no_grep -q ' pull$' "$invalid/docker/calls"
assert_no_grep -q ' stop$' "$invalid/docker/calls"
cmp "$invalid/original.env" "$invalid/bask/.env"
cmp "$invalid/original.config" "$invalid/bask/data/config.json"
echo "  invalid candidate leaves the running install untouched"

# Ambient Docker/Compose variables must not redirect validation, rollout, or
# rollback to another project file or daemon.
ambient="$work/ambient"
mkdir -p "$ambient/home"
seed_update "$ambient"
run_deploy_hostile_environment "$ambient" >/dev/null
grep -q -- '--project-directory' "$ambient/docker/calls"
if grep -qE 'attacker|tcp://attacker' "$ambient/docker/calls"; then
  echo "Ambient Docker or Compose routing values reached the deployment command." >&2
  exit 1
fi
echo "  ambient Docker and Compose routing variables are neutralized"

# A globally named container from another Compose project must never be
# adopted, stopped, or replaced. Existing Bask containers also require the old
# Compose file needed for an exact rollback.
foreign="$work/foreign"
mkdir -p "$foreign/home" "$foreign/other"
seed_update "$foreign"
if MOCK_WORKING_DIR="$foreign/other" run_deploy "$foreign" > "$foreign/out" 2> "$foreign/err"; then
  echo "A foreign container was accepted as this Bask deployment." >&2
  exit 1
fi
assert_no_grep -q ' pull$' "$foreign/docker/calls"
assert_no_grep -q ' stop$' "$foreign/docker/calls"

missing_compose="$work/missing-compose"
mkdir -p "$missing_compose/home"
seed_update "$missing_compose"
rm "$missing_compose/bask/compose.yaml"
if run_deploy "$missing_compose" > "$missing_compose/out" 2> "$missing_compose/err"; then
  echo "Prior containers without a restorable Compose file were accepted." >&2
  exit 1
fi
assert_no_grep -q ' pull$' "$missing_compose/docker/calls"
assert_no_grep -q ' stop$' "$missing_compose/docker/calls"
echo "  foreign and non-restorable prior containers are rejected before mutation"

# Pull occurs only after a config+SQLite archive exists and verifies. A failed
# pull never bounces the old service, but restores the mutable image tags.
pull="$work/pull"
mkdir -p "$pull/home"
seed_update "$pull"
if REQUIRE_BACKUP_BEFORE_PULL=true MOCK_FAIL=pull run_deploy "$pull" > "$pull/out" 2> "$pull/err"; then
  echo "A failed image pull reported success." >&2
  exit 1
fi
find "$pull/bask/backups" -type f -name 'bask-*.tar.gz' -print -quit | grep -q .
assert_no_grep -q 'down --remove-orphans' "$pull/docker/calls"
cmp "$pull/original.env" "$pull/bask/.env"
cmp "$pull/original.config" "$pull/bask/data/config.json"
[[ "$(cat "$pull/docker/bask.running")" == true ]]
echo "  verified config and SQLite backup precedes every image pull"

# Startup, health, and runtime-boundary failures restore exact config, image
# IDs, the stopped scanner state, and the old two-service graph.
for failure in stop up health boundary; do
  fixture="$work/fail-$failure"
  mkdir -p "$fixture/home"
  seed_update "$fixture"
  if MOCK_FAIL="$failure" run_deploy "$fixture" > "$fixture/out" 2> "$fixture/err"; then
    echo "The $failure failure path reported success." >&2
    exit 1
  fi
  assert_rollback "$fixture"
done
echo "  startup, health, and boundary failures roll back exactly"

# An older release may not define the scanner service at all. Rollback must not
# ask that older Compose file to resolve the candidate-only name, and must leave
# the prior one-service graph exact.
single="$work/single-service"
mkdir -p "$single/home"
seed_update "$single"
printf false > "$single/docker/bask-scanner.exists"
printf false > "$single/docker/bask-scanner.running"
if MOCK_FAIL=health run_deploy "$single" > "$single/out" 2> "$single/err"; then
  echo "A failed one-service migration reported success." >&2
  exit 1
fi
[[ "$(cat "$single/docker/bask.running")" == true ]]
[[ "$(cat "$single/docker/bask-scanner.exists")" == false ]]
grep -q '^rm -f bask-scanner$' "$single/docker/calls"
echo "  rollback also restores a prior graph that lacks a candidate service"

# External storage is opt-in and is never the target of a recursive installer
# chown. Protected roots and symlink escapes fail before pull.
external="$work/external"
mkdir -p "$external/home" "$external/bask" "$external/data" "$external/backups" "$external/docker"
write_env "$external/bask" "$external/data" "$external/backups" true
cp "$root/compose.yaml" "$external/bask/compose.yaml"
printf '{"external":true}\n' > "$external/data/config.json"
run_deploy "$external" >/dev/null
assert_no_grep -Fq "chown -R -- $(id -u):$(id -g) $external/data" "$external/docker/calls"
assert_no_grep -Fq "chown -R -- $(id -u):$(id -g) $external/backups" "$external/docker/calls"

unsafe="$work/unsafe"
mkdir -p "$unsafe/home" "$unsafe/bask" "$unsafe/docker"
write_env "$unsafe/bask" / ./backups true
cp "$root/compose.yaml" "$unsafe/bask/compose.yaml"
if run_deploy "$unsafe" > "$unsafe/out" 2> "$unsafe/err"; then
  echo "The filesystem root was accepted as Bask data." >&2
  exit 1
fi
assert_no_grep -q ' pull$' "$unsafe/docker/calls"

interpreted="$work/interpreted"
mkdir -p "$interpreted/home" "$interpreted/bask" "$interpreted/docker"
write_env "$interpreted/bask" '${HOME}/data' ./backups false
cp "$root/compose.yaml" "$interpreted/bask/compose.yaml"
if run_deploy "$interpreted" > "$interpreted/out" 2> "$interpreted/err"; then
  echo "A Compose-interpreted storage path was accepted." >&2
  exit 1
fi
assert_no_grep -q ' pull$' "$interpreted/docker/calls"

escaped="$work/escaped"
mkdir -p "$escaped/home" "$escaped/bask" "$escaped/outside" "$escaped/docker"
ln -s "$escaped/outside" "$escaped/bask/data-link"
write_env "$escaped/bask" ./data-link ./backups false
cp "$root/compose.yaml" "$escaped/bask/compose.yaml"
if run_deploy "$escaped" > "$escaped/out" 2> "$escaped/err"; then
  echo "A symlink escape was accepted without external opt-in." >&2
  exit 1
fi
assert_no_grep -q ' pull$' "$escaped/docker/calls"
mkdir -p "$escaped/bask/data"
printf '{"backup":"source"}\n' > "$escaped/bask/data/config.json"
if BASK_INSTALL_DIR="$escaped/bask" \
   BASK_DATA_PATH=./data BASK_BACKUP_PATH=./data-link/new \
   BASK_ALLOW_EXTERNAL_PATHS=false \
   bash "$root/scripts/backup.sh" > "$escaped/backup.out" 2> "$escaped/backup.err"; then
  echo "The standalone backup followed an internal symlink while creating its destination." >&2
  exit 1
fi
[[ ! -e "$escaped/outside/new" ]]
echo "  canonical storage policy prevents arbitrary recursive host ownership changes"

# Protected descendants are rejected, not just the top-level OS directories.
# This assertion also ensures internal ownership repair remains filesystem
# bounded and never uses an unbounded recursive chown.
protected_external="$(mktemp -d "${TMPDIR:-/tmp}/bask-protected.XXXXXX")"
protected="$work/protected-storage"
mkdir -p "$protected/home" "$protected/bask" "$protected/docker"
write_env "$protected/bask" "$protected_external" ./backups true
cp "$root/compose.yaml" "$protected/bask/compose.yaml"
if run_deploy "$protected" > "$protected/out" 2> "$protected/err"; then
  echo "A protected temporary-system descendant was accepted as Bask storage." >&2
  exit 1
fi
assert_no_grep -q ' pull$' "$protected/docker/calls"
assert_no_grep -qE 'chown -R|chown --recursive' "$root/deploy/install.sh"
if BASK_INSTALL_DIR="$protected/bask" \
   BASK_DATA_PATH="$protected_external" \
   BASK_BACKUP_PATH=./backups \
   BASK_ALLOW_EXTERNAL_PATHS=true \
   bash "$root/scripts/backup.sh" > "$protected/backup.out" 2> "$protected/backup.err"; then
  echo "The standalone backup accepted a protected temporary-system descendant." >&2
  exit 1
fi
echo "  protected descendants and nested filesystems are excluded from ownership repair"

for protected_install in "/tmp/bask-installer-$RANDOM-$$" "/etc/bask-installer-$RANDOM-$$" "/root/bask-installer-$RANDOM-$$"; do
  if PATH="$bin:/usr/bin:/bin" HOME="$work/path-home" \
     BASK_INSTALL_DIR="$protected_install" BASK_REPO="$root" \
     bash "$root/get-bask.sh" > "$work/protected-install.out" 2> "$work/protected-install.err"; then
    echo "A protected install target was accepted: $protected_install" >&2
    exit 1
  fi
  [[ ! -e "$protected_install" ]]
done
traversal_target="/etc/bask-traversal-$RANDOM-$$"
if PATH="$bin:/usr/bin:/bin" HOME="$work/path-home" \
   BASK_INSTALL_DIR="$work/nonexistent/../../../etc/${traversal_target##*/}" BASK_REPO="$root" \
   bash "$root/get-bask.sh" > "$work/traversal-install.out" 2> "$work/traversal-install.err"; then
  echo "A traversal-based protected install target was accepted." >&2
  exit 1
fi
[[ ! -e "$traversal_target" ]]
echo "  protected install targets are rejected before filesystem mutation"

# Quote characters are not portable between raw .env values, Compose, and
# filesystem tools. Reject them anywhere in install/data/backup paths rather
# than relying on edge-only quote checks.
for quoted_install in "$work/quo'ted-install" "$work/quo\"ted-install"; do
  if PATH="$bin:/usr/bin:/bin" HOME="$work/path-home" \
     BASK_INSTALL_DIR="$quoted_install" BASK_REPO="$root" \
     bash "$root/get-bask.sh" > "$work/quoted-install.out" 2> "$work/quoted-install.err"; then
    echo "An internally quoted install path was accepted: $quoted_install" >&2
    exit 1
  fi
  [[ ! -e "$quoted_install" ]]
done

for variable in BASK_DATA_PATH BASK_BACKUP_PATH; do
  quoted="$work/quo'ted-storage"
  quoted_storage="$work/quoted-$variable"
  mkdir -p "$quoted_storage/home" "$quoted_storage/bask" "$quoted_storage/docker"
  if [[ "$variable" == BASK_DATA_PATH ]]; then
    write_env "$quoted_storage/bask" "$quoted" ./backups false
  else
    mkdir -p "$quoted_storage/bask/data"
    write_env "$quoted_storage/bask" ./data "$quoted" false
  fi
  cp "$root/compose.yaml" "$quoted_storage/bask/compose.yaml"
  if run_deploy "$quoted_storage" > "$quoted_storage/out" 2> "$quoted_storage/err"; then
    echo "An internally quoted $variable was accepted." >&2
    exit 1
  fi
  [[ ! -e "$quoted" ]]
done
echo "  internal quote characters are rejected across install and storage paths"

# First installation still creates private settings, app data, and a one-time
# keeper key, while retaining the data directory on a failed attempt.
fresh_failed="$work/fresh-failed"
mkdir -p "$fresh_failed/home" "$fresh_failed/bask" "$fresh_failed/docker"
cp "$root/compose.yaml" "$fresh_failed/bask/compose.yaml"
if MOCK_FAIL=health run_deploy "$fresh_failed" > "$fresh_failed/out" 2> "$fresh_failed/err"; then
  echo "A failed first installation reported success." >&2
  exit 1
fi
[[ ! -e "$fresh_failed/bask/data/config.json" ]]
fresh_retry="$(run_deploy "$fresh_failed")"
grep -q 'Head Keeper key:  test-head-keeper-key' <<< "$fresh_retry"
echo "  a failed first install cannot strand an undisclosed Head Keeper key"

scanner_no_health="$work/scanner-no-health"
mkdir -p "$scanner_no_health/home" "$scanner_no_health/bask" "$scanner_no_health/docker"
cp "$root/compose.yaml" "$scanner_no_health/bask/compose.yaml"
run_deploy "$scanner_no_health" >/dev/null
[[ "$(cat "$scanner_no_health/docker/bask-scanner.running")" == true ]]
echo "  the intentionally healthcheck-free scanner is accepted only while running"

fresh="$work/fresh"
mkdir -p "$fresh/home" "$fresh/bask" "$fresh/docker"
cp "$root/compose.yaml" "$fresh/bask/compose.yaml"
fresh_output="$(run_deploy "$fresh")"
grep -q 'Head Keeper key:  test-head-keeper-key' <<< "$fresh_output"
[[ -f "$fresh/bask/.env" && -f "$fresh/bask/data/config.json" ]]
grep -q '^BASK_ALLOW_EXTERNAL_PATHS=false$' "$fresh/bask/.env"
echo "  first-install behavior remains intact"

# get-bask stages a real Git worktree and advances HEAD only after deploy has
# passed. A failed update keeps the exact old revision and leaves no worktree.
repo="$work/repository"
mkdir -p "$repo/deploy" "$repo/scripts"
cp "$root/get-bask.sh" "$repo/get-bask.sh"
cp "$root/deploy/install.sh" "$repo/deploy/install.sh"
cp "$root/scripts/backup.sh" "$repo/scripts/backup.sh"
cp "$root/compose.yaml" "$root/.env.example" "$root/.gitignore" "$root/config.example.json" \
   "$root/docker-entrypoint.sh" "$repo/"
git -C "$repo" init -q -b main
git -C "$repo" config user.email tests@example.invalid
git -C "$repo" config user.name 'Bask installer tests'
printf 'old\n' > "$repo/release-marker"
git -C "$repo" add .
git -C "$repo" commit -qm old
old_head="$(git -C "$repo" rev-parse HEAD)"

checkout="$work/checkout"
git clone -q "$repo" "$checkout"
mkdir -p "$checkout/data" "$checkout/backups"
write_env "$checkout"
printf '{"checkout":"old"}\n' > "$checkout/data/config.json"
sqlite3 "$checkout/data/readings.db" 'CREATE TABLE readings(id INTEGER); INSERT INTO readings VALUES(1);'
cp "$checkout/data/config.json" "$work/checkout-original.config"
printf 'new\n' > "$repo/release-marker"
git -C "$repo" add release-marker
git -C "$repo" commit -qm new
new_head="$(git -C "$repo" rev-parse HEAD)"

state="$work/checkout-docker"
mkdir -p "$state"
for service in bask bask-scanner; do
  printf true > "$state/$service.exists"
  printf true > "$state/$service.running"
  printf healthy > "$state/$service.health"
  printf false > "$state/$service.oom"
  printf 'sha256:old-%s' "$service" > "$state/$service.image_id"
  printf 'sha256:old-%s' "$service" > "$state/$service.old_image"
  printf 'ghcr.io/jlyfshhh/bask:latest' > "$state/$service.image_ref"
done

run_get_bask() {
  PATH="$bin:/usr/bin:/bin" \
  HOME="$work/home" \
  BASK_REPO="$repo" \
  BASK_BRANCH=main \
  BASK_INSTALL_DIR="$checkout" \
  BASK_INSTALLER_TEST_MODE=true \
  BASK_INSTALL_SKIP_PACKAGES=true \
  BASK_INSTALL_HEALTH_ATTEMPTS=2 \
  BASK_INSTALL_HEALTH_INTERVAL=0 \
  MOCK_DOCKER_STATE="$state" \
  CURRENT_INSTALL="$checkout" \
  bash "$root/get-bask.sh"
}

if MOCK_FAIL=health run_get_bask > "$work/get-fail.out" 2> "$work/get-fail.err"; then
  echo "A failed staged update reported success." >&2
  exit 1
fi
[[ "$(git -C "$checkout" rev-parse HEAD)" == "$old_head" ]]
cmp "$work/checkout-original.config" "$checkout/data/config.json"
[[ -z "$(git -C "$checkout" worktree list --porcelain | grep '^worktree ' | tail -n +2)" ]]

run_get_bask >/dev/null
[[ "$(git -C "$checkout" rev-parse HEAD)" == "$new_head" ]]
[[ "$(cat "$checkout/release-marker")" == new ]]
echo "  staged Git update advances the checkout only after verified deployment"

echo "Standalone Bask installer tests passed."
