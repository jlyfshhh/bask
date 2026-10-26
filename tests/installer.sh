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
        for service in bask bask-scanner bask-init; do
          set_value "$service.exists" false
          set_value "$service.running" false
        done
        ;;
      *" up -d --no-build --pull never")
        # The prior Compose graph can only recreate services it actually defined;
        # `.old_image` marks those. A rollback to a two-service release therefore
        # never resurrects an init service that release never had.
        for service in bask bask-scanner bask-init; do
          [[ -f "$state/$service.old_image" ]] || continue
          set_value "$service.exists" true
          set_value "$service.oom" false
          set_value "$service.image_id" "$(value "$service.old_image" "sha256:old-$service")"
          if [[ "$service" == bask-init ]]; then
            set_value "$service.running" false
            set_value "$service.exitcode" 0
          else
            set_value "$service.running" true
            set_value "$service.health" healthy
          fi
        done
        ;;
      *" up -d")
        [[ "${MOCK_FAIL:-}" != up ]] || exit 20
        # Compose runs the one-shot init first and only starts the web once it
        # has exited successfully. Model that gate: a failing init aborts `up`.
        set_value bask-init.exists true
        set_value bask-init.running false
        set_value bask-init.oom false
        set_value bask-init.image_id "sha256:new-bask-init"
        set_value bask-init.image_ref "ghcr.io/jlyfshhh/bask:latest"
        if [[ "${MOCK_FAIL:-}" == init ]]; then
          set_value bask-init.exitcode 1
          exit 1
        fi
        set_value bask-init.exitcode 0
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
      *" stop bask-init") set_value bask-init.running false ;;
      *" stop bask") set_value bask.running false ;;
      *" stop")
        if [[ "${MOCK_FAIL:-}" == stop ]]; then
          # Model a partially successful Compose stop: rollback must still
          # rebuild the exact prior graph and per-service running state.
          set_value bask.running false
          exit 21
        fi
        for service in bask bask-scanner bask-init; do
          [[ "$(value "$service.exists" false)" != true ]] || set_value "$service.running" false
        done
        ;;
      *" rm -sf bask-scanner") set_value bask-scanner.exists false; set_value bask-scanner.running false ;;
      *" rm -sf bask-init") set_value bask-init.exists false; set_value bask-init.running false ;;
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
          bask|bask-init) printf '%s|/data|true\n' "${MOCK_DATA_MOUNT:-$CURRENT_INSTALL/data}" ;;
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
      # Per-service working-directory label so a single foreign container (for
      # example only bask-init) can be modelled as belonging to another project.
      *'com.docker.compose.project.working_dir'*) printf '%s\n' "$(value "$service.working_dir" "${MOCK_WORKING_DIR:-$CURRENT_INSTALL}")" ;;
      '{{.State.Running}}') value "$service.running" true ;;
      '{{.State.OOMKilled}}') value "$service.oom" false ;;
      '{{.State.ExitCode}}') value "$service.exitcode" 0 ;;
      '{{.State.Status}}')
        [[ "$(value "$service.running" false)" == true ]] && printf running || printf exited
        ;;
      *'.State.Health'*)
        [[ "$service" == bask-scanner ]] && value "$service.running" true | sed 's/^true$/running/; s/^false$/exited/' || value "$service.health" healthy
        ;;
      '{{.Config.User}}')
        case "$service" in bask-scanner|bask-init) printf '0:0' ;; *) printf '%s:%s' "$(id -u)" "$(id -g)" ;; esac
        ;;
      '{{.HostConfig.ReadonlyRootfs}}') printf true ;;
      '{{.HostConfig.NetworkMode}}')
        case "$service" in bask-scanner|bask-init) printf none ;; *) printf default ;; esac
        ;;
      # Capabilities are reported with the daemon's CAP_ spelling and in Docker's
      # own ordering (DAC_OVERRIDE before CHOWN), so this also exercises the
      # installer's order- and prefix-independent normalisation. The scanner now
      # carries both DAC_OVERRIDE and CHOWN; init carries CHOWN and
      # DAC_READ_SEARCH; the network-facing web keeps none.
      '{{json .HostConfig.CapAdd}}')
        case "$service" in
          bask-scanner)
            if [[ "${MOCK_EXTRA_SCANNER_CAP:-false}" == true ]]; then
              printf '["CAP_DAC_OVERRIDE","CAP_CHOWN","CAP_SYS_ADMIN"]'
            else
              printf '["CAP_DAC_OVERRIDE","CAP_CHOWN"]'
            fi
            ;;
          bask-init) printf '["CAP_CHOWN","CAP_DAC_READ_SEARCH"]' ;;
          *) printf 'null' ;;
        esac
        ;;
      '{{json .HostConfig.CapDrop}}') printf '["ALL"]' ;;
      '{{json .HostConfig.SecurityOpt}}') printf '["no-new-privileges:true"]' ;;
      # The audit asks which AppArmor profile is actually applied, and on a
      # runner that has AppArmor it really does ask. Model it: the scanner
      # carries whatever the installer resolved into the .env, everything else
      # is unconfined as a container with no profile would report.
      # A fake container has no confinement to report, and the audit treats an
      # empty answer as "cannot tell" rather than as a mismatch. Modelling a
      # specific profile here would only assert what this fake was told to say.
      '{{.AppArmorProfile}}') ;;
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
    # Stand in for server.init_keeper: mint a key only when the config has no
    # keeper block, print nothing when one is already set, and never overwrite.
    cfg=""
    prev=""
    for arg in "$@"; do
      [[ "$prev" != "-v" ]] || cfg="${arg%%:*}/config.json"
      prev="$arg"
    done
    if [[ -n "$cfg" ]] && grep -q '"keeper"' "$cfg" 2>/dev/null; then
      echo "this install already has a Head Keeper key; leaving it alone" >&2
    else
      if [[ -n "$cfg" ]]; then
        mkdir -p "$(dirname "$cfg")"
        [[ -f "$cfg" ]] || printf '{}' >"$cfg"
        python3 -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["keeper"]={"salt":"s","hash":"h"}; json.dump(d,open(p,"w"))' "$cfg"
      fi
      printf 'test-head-keeper-key\n'
    fi
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

# A transparent pass-through to the real git, except it can be told to fail on a
# chosen subcommand so a retained-data reinstall can be interrupted mid-flight
# (after the .git is placed) to prove the rollback leaves data untouched.
cat > "$bin/git" <<'SH'
#!/usr/bin/env bash
if [[ -n "${MOCK_FAIL_REINSTALL:-}" ]]; then
  for a in "$@"; do
    if [[ "$a" == "$MOCK_FAIL_REINSTALL" ]]; then
      echo "mock git failure on '$MOCK_FAIL_REINSTALL'" >&2
      exit 33
    fi
  done
fi
exec /usr/bin/git "$@"
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
  for service in bask bask-scanner bask-init; do
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
  # The one-shot init has already run and exited cleanly in the prior install:
  # present, not running, exit 0. A `.old_image` is what tells the fake prior
  # Compose graph which services it is allowed to recreate on rollback.
  printf false > "$state/bask-init.running"
  printf 0 > "$state/bask-init.exitcode"
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
# IDs, the stopped scanner state, and the prior three-service graph. `init` is
# the one-shot data-ownership repair: a non-zero exit from it must also roll the
# whole update back rather than start the web against a /data it could not fix.
for failure in stop up init health boundary; do
  fixture="$work/fail-$failure"
  mkdir -p "$fixture/home"
  seed_update "$fixture"
  if MOCK_FAIL="$failure" run_deploy "$fixture" > "$fixture/out" 2> "$fixture/err"; then
    echo "The $failure failure path reported success." >&2
    exit 1
  fi
  assert_rollback "$fixture"
done
echo "  startup, init, health, and boundary failures roll back exactly"

# The scanner now carries exactly CHOWN and DAC_OVERRIDE. A capability the audit
# does not expect — here an extra CAP_SYS_ADMIN — must fail the boundary and roll
# back, so the normalised set comparison rejects extras rather than only checking
# that the two required names are present.
extra_cap="$work/extra-scanner-cap"
mkdir -p "$extra_cap/home"
seed_update "$extra_cap"
if MOCK_EXTRA_SCANNER_CAP=true run_deploy "$extra_cap" > "$extra_cap/out" 2> "$extra_cap/err"; then
  echo "A scanner carrying an unexpected extra capability passed the boundary audit." >&2
  exit 1
fi
assert_rollback "$extra_cap"
echo "  an unexpected extra scanner capability fails the boundary audit and rolls back"

# A prior install that predates the init service: only web and scanner exist. A
# failed update that introduced init must roll back to exactly that two-service
# graph and must not strand the candidate's init container.
two_service="$work/two-service"
mkdir -p "$two_service/home"
seed_update "$two_service"
printf false > "$two_service/docker/bask-init.exists"
printf false > "$two_service/docker/bask-init.running"
rm -f "$two_service/docker/bask-init.old_image"
if MOCK_FAIL=health run_deploy "$two_service" > "$two_service/out" 2> "$two_service/err"; then
  echo "A failed update over a prior two-service install reported success." >&2
  exit 1
fi
assert_rollback "$two_service"
[[ "$(cat "$two_service/docker/bask-init.exists")" == false ]] || {
  echo "Rollback to a two-service install left the candidate's init container behind." >&2
  exit 1
}
echo "  rollback to a prior two-service install does not strand the candidate init service"

# A globally named bask-init from another Compose project must be refused before
# any mutation, exactly as a foreign web or scanner container already is.
foreign_init="$work/foreign-init"
mkdir -p "$foreign_init/home" "$foreign_init/other"
seed_update "$foreign_init"
printf '%s' "$foreign_init/other" > "$foreign_init/docker/bask-init.working_dir"
if run_deploy "$foreign_init" > "$foreign_init/out" 2> "$foreign_init/err"; then
  echo "A foreign bask-init container was accepted as this Bask deployment." >&2
  exit 1
fi
assert_no_grep -q ' pull$' "$foreign_init/docker/calls"
assert_no_grep -q ' stop$' "$foreign_init/docker/calls"
echo "  a foreign init container is refused before any mutation"

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
grep -q '^BASK_KEEPER_KEY=test-head-keeper-key$' "$fresh/bask/.env" || {
  echo "The Head Keeper key was shown but not kept where it can be found again." >&2
  exit 1
}
grep -q '^BASK_ALLOW_EXTERNAL_PATHS=false$' "$fresh/bask/.env"
echo "  first-install behavior remains intact"

# The key is stored only as a hash and cannot be read back, and an uninstall
# keeps the data directory, so a keeper who missed it had no way to get another:
# reinstalling found the old record and stayed silent, because minting was gated
# on a brand-new config. Running the installer again must leave a key that is
# already set alone, and must issue a new one once the record is cleared.
printf '{"keeper":{"salt":"s","hash":"h"}}\n' >"$fresh/bask/data/config.json"
update_output="$(run_deploy "$fresh")"
grep -q 'Head Keeper key:  test-head-keeper-key' <<< "$update_output" || {
  echo "An update stopped showing the Head Keeper key already on file." >&2
  exit 1
}
grep -q '^BASK_KEEPER_KEY=test-head-keeper-key$' "$fresh/bask/.env" || {
  echo "An update overwrote or dropped the stored Head Keeper key." >&2
  exit 1
}
printf '{"sensors":{}}\n' >"$fresh/bask/data/config.json"
recovered_output="$(run_deploy "$fresh")"
grep -q 'Head Keeper key:  test-head-keeper-key' <<< "$recovered_output" || {
  echo "Clearing the keeper record did not let the installer issue a new key." >&2
  exit 1
}
echo "  a lost Head Keeper key can be reissued by clearing the record"

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

# F13: a directory a normal uninstall left behind — data/, backups/, and .env
# kept, .git removed, with the retained-data marker — reinstalls in place and
# preserves records. Arbitrary non-checkout directories are still refused, and a
# reinstall that fails partway rolls back with the preserved data untouched.
seed_retained_dir() {
  local dir="$1" marker_line="${2:-}"
  rm -rf -- "$dir"
  mkdir -p -- "$dir/data" "$dir/backups"
  write_env "$dir"
  # A sentinel the fake Compose never rewrites, so data preservation is asserted
  # independently of the config-mutation the mock performs on a successful up.
  printf 'KEEP-THESE-RECORDS\n' > "$dir/data/records.keep"
  printf '{"keeper":{"salt":"s","hash":"h"},"records":"kept"}\n' > "$dir/data/config.json"
  sqlite3 "$dir/data/readings.db" 'CREATE TABLE readings(id INTEGER); INSERT INTO readings VALUES(7);'
  printf 'an old archive\n' > "$dir/backups/bask-old.tar.gz"
  [[ -z "$marker_line" ]] || printf '%s\n' "$marker_line" > "$dir/.animal-room-retained-data"
}

run_get_bask_dir() {  # dir, [extra VAR=val ...]
  local dir="$1"; shift
  local docker_state="$dir.docker"
  rm -rf -- "$docker_state"; mkdir -p -- "$docker_state"
  env PATH="$bin:/usr/bin:/bin" \
    HOME="$work/home" \
    BASK_REPO="$repo" \
    BASK_BRANCH=main \
    BASK_INSTALL_DIR="$dir" \
    BASK_INSTALLER_TEST_MODE=true \
    BASK_INSTALL_SKIP_PACKAGES=true \
    BASK_INSTALL_HEALTH_ATTEMPTS=2 \
    BASK_INSTALL_HEALTH_INTERVAL=0 \
    MOCK_DOCKER_STATE="$docker_state" \
    CURRENT_INSTALL="$dir" \
    "$@" \
    bash "$root/get-bask.sh"
}

assert_retained_data_intact() {  # dir
  local dir="$1"
  [[ "$(cat "$dir/data/records.keep" 2>/dev/null)" == "KEEP-THESE-RECORDS" ]] || {
    echo "The retained data/records.keep was lost." >&2; exit 1; }
  [[ "$(cat "$dir/backups/bask-old.tar.gz" 2>/dev/null)" == "an old archive" ]] || {
    echo "The retained backups archive was lost." >&2; exit 1; }
  [[ -f "$dir/.env" ]] || { echo "The retained .env was lost." >&2; exit 1; }
  [[ "$(sqlite3 "$dir/data/readings.db" 'SELECT id FROM readings;' 2>/dev/null)" == 7 ]] || {
    echo "The retained readings.db row was lost." >&2; exit 1; }
}

# A valid marker reinstalls in place: the checkout returns at the published HEAD,
# the records survive, and the marker is consumed.
retained_ok="$work/retained-valid"
seed_retained_dir "$retained_ok" "animal-room retained-data v1"
run_get_bask_dir "$retained_ok" >/dev/null
[[ -d "$retained_ok/.git" ]] || { echo "Retained-data reinstall left no Git checkout." >&2; exit 1; }
[[ "$(git -C "$retained_ok" rev-parse HEAD)" == "$new_head" ]] || {
  echo "Retained-data reinstall did not land on the published HEAD." >&2; exit 1; }
[[ -f "$retained_ok/compose.yaml" ]] || { echo "Reinstall did not materialise tracked files." >&2; exit 1; }
[[ ! -e "$retained_ok/.animal-room-retained-data" ]] || {
  echo "The retained-data marker was not removed after a successful reinstall." >&2; exit 1; }
assert_retained_data_intact "$retained_ok"
echo "  a valid retained-data directory reinstalls in place and preserves records"

# A directory with other files but no marker is still refused, untouched.
retained_none="$work/retained-no-marker"
seed_retained_dir "$retained_none" ""
printf 'stray\n' > "$retained_none/leftover.txt"
if run_get_bask_dir "$retained_none" > "$retained_none.out" 2> "$retained_none.err"; then
  echo "A non-checkout directory without a marker was accepted." >&2
  exit 1
fi
[[ ! -d "$retained_none/.git" ]] || { echo "A refused directory gained a .git." >&2; exit 1; }
assert_retained_data_intact "$retained_none"

# A marker whose version line does not match exactly is not trusted.
retained_badver="$work/retained-bad-version"
seed_retained_dir "$retained_badver" "animal-room retained-data v2"
if run_get_bask_dir "$retained_badver" > "$retained_badver.out" 2> "$retained_badver.err"; then
  echo "A marker with the wrong version line was trusted." >&2
  exit 1
fi
[[ ! -d "$retained_badver/.git" ]]
assert_retained_data_intact "$retained_badver"

# A marker without the records it promises is not trusted either.
retained_bare="$work/retained-bare-marker"
rm -rf -- "$retained_bare"; mkdir -p -- "$retained_bare"
printf 'animal-room retained-data v1\n' > "$retained_bare/.animal-room-retained-data"
if run_get_bask_dir "$retained_bare" > "$retained_bare.out" 2> "$retained_bare.err"; then
  echo "A bare marker with no preserved data was trusted." >&2
  exit 1
fi
[[ ! -d "$retained_bare/.git" ]]
echo "  non-checkout directories without a valid marker are still refused"

# A reinstall interrupted after the checkout metadata is placed must roll back,
# removing only what it added and leaving the preserved records untouched. The
# marker survives so the keeper can simply retry.
retained_fail="$work/retained-failed"
seed_retained_dir "$retained_fail" "animal-room retained-data v1"
if MOCK_FAIL_REINSTALL=reset run_get_bask_dir "$retained_fail" > "$retained_fail.out" 2> "$retained_fail.err"; then
  echo "A reinstall that failed mid-flight reported success." >&2
  exit 1
fi
[[ ! -e "$retained_fail/.git" ]] || { echo "A failed reinstall left a partial .git behind." >&2; exit 1; }
[[ ! -e "$retained_fail/compose.yaml" ]] || {
  echo "A failed reinstall left tracked files behind instead of rolling them back." >&2; exit 1; }
[[ -f "$retained_fail/.animal-room-retained-data" ]] || {
  echo "A failed reinstall removed the retained-data marker, blocking a retry." >&2; exit 1; }
assert_retained_data_intact "$retained_fail"
# And the rolled-back directory can be reinstalled cleanly on a retry.
run_get_bask_dir "$retained_fail" >/dev/null
[[ -d "$retained_fail/.git" && "$(git -C "$retained_fail" rev-parse HEAD)" == "$new_head" ]] || {
  echo "A rolled-back retained directory could not be reinstalled on retry." >&2; exit 1; }
assert_retained_data_intact "$retained_fail"
echo "  a failed retained-data reinstall rolls back with data intact and can be retried"

echo "Standalone Bask installer tests passed."

# A data directory the keeper cannot enter is the normal steady state: the
# installer creates it 0700 and the container is chowned to its own uid. If
# resolving the path requires entering it, every update after the first fails
# with "Permission denied" and rolls back. Reproduced on a real install.
unenterable="$work/unenterable"
mkdir -p "$unenterable/home" "$unenterable/bask/data" "$unenterable/docker"
cp "$root/compose.yaml" "$unenterable/bask/compose.yaml"
printf 'records\n' >"$unenterable/bask/data/readings.db"
chmod 000 "$unenterable/bask/data"
if ! run_deploy "$unenterable" >"$unenterable/out" 2>&1; then
  chmod 755 "$unenterable/bask/data"
  echo "An install failed on a data directory it could not enter." >&2
  tail -5 "$unenterable/out" >&2
  exit 1
fi
chmod 755 "$unenterable/bask/data"
[[ "$(cat "$unenterable/bask/data/readings.db")" == "records" ]] || {
  echo "The unenterable-directory install disturbed existing data." >&2; exit 1; }
echo "  a data directory the keeper cannot enter still updates"
