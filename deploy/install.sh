#!/usr/bin/env bash
# System setup and legacy-to-Docker migration for Bask.
set -euo pipefail
umask 077

project_dir="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
run_user="${2:-${SUDO_USER:-$(id -un)}}"
run_group="$(id -gn "$run_user")"
run_uid="$(id -u "$run_user")"
run_gid="$(id -g "$run_user")"
host="$(hostname)"

if [[ "$EUID" -ne 0 ]]; then
  echo "Run with sudo: sudo bash deploy/install.sh" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

install_docker() {
  echo "==> Installing Docker Engine and Compose from Docker's apt repository"
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg \
    -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  # Raspberry Pi OS is Debian-compatible, but may identify as "raspbian".
  . /etc/os-release
  arch="$(dpkg --print-architecture)"
  printf '%s\n' \
    "deb [arch=$arch signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $VERSION_CODENAME stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
}

if ! command -v docker >/dev/null 2>&1; then
  install_docker
elif ! docker compose version >/dev/null 2>&1; then
  echo "Docker is installed, but the Compose plugin is missing." >&2
  echo "Install Docker Compose, then rerun the Bask installer." >&2
  exit 1
fi

echo "==> Installing Bluetooth, mDNS, and backup tools"
apt-get update -qq
# fonts-noto-color-emoji: the room display uses emoji in its status messages,
# and a bare Raspberry Pi OS install has no emoji font, so they render as
# empty boxes on a wall-mounted kiosk.
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

echo "==> Preparing persistent data"
install -d -m 0700 -o "$run_user" -g "$run_group" "$project_dir/data" "$project_dir/backups"

# Stop the original venv/systemd install before snapshotting its SQLite WAL.
legacy=false
if [[ ! -f "$project_dir/data/.docker-migrated" ]] &&
   [[ -f "$project_dir/config.json" || -f "$project_dir/readings.db" ]]; then
  legacy=true
fi
for unit in bask-scanner.service bask-web.service; do
  if systemctl cat "$unit" >/dev/null 2>&1; then
    systemctl disable --now "$unit" >/dev/null 2>&1 || true
  fi
done

if [[ "$legacy" == true ]]; then
  stamp="$(date +%Y%m%d-%H%M%S)"
  legacy_backup="$project_dir/backups/pre-docker-$stamp"
  install -d -m 0700 -o "$run_user" -g "$run_group" "$legacy_backup"
  [[ ! -f "$project_dir/config.json" ]] ||
    install -m 0600 -o "$run_user" -g "$run_group" "$project_dir/config.json" "$legacy_backup/config.json"
  if [[ -f "$project_dir/readings.db" ]]; then
    sqlite3 "$project_dir/readings.db" ".backup '$legacy_backup/readings.db'"
    chown "$run_user:$run_group" "$legacy_backup/readings.db"
  fi
  echo "    Original install backed up to $legacy_backup"
fi

fresh_config=false
if [[ ! -f "$project_dir/data/config.json" ]]; then
  source_config="$project_dir/config.json"
  [[ -f "$source_config" ]] || source_config="$project_dir/config.example.json"
  install -m 0600 -o "$run_user" -g "$run_group" "$source_config" "$project_dir/data/config.json"
  fresh_config=true
fi

# Give a brand-new install a Head Keeper key, so changing the setup is limited
# to whoever ran the installer. The dashboard stays readable by the whole house.
# Only on a fresh config: an existing install keeps whatever it already has, and
# an upgrade never locks anyone out of a dashboard that used to be open.
bask_image="${BASK_IMAGE:-ghcr.io/jlyfshhh/bask:${BASK_TAG:-latest}}"

keeper_key=""
if [[ "$fresh_config" == true ]]; then
  # The bootstrap below runs in the image, so make sure it is here first. The
  # compose pull later is a no-op once this has run.
  if ! docker image inspect "$bask_image" >/dev/null 2>&1; then
    docker pull "$bask_image" || {
      echo "Could not download $bask_image." >&2
      exit 1
    }
  fi
  # Run inside the published image, which has Python and the real keeper module.
  # This used to be a heredoc executed by the host's python3 — which the
  # installer neither installed nor required — with its failure discarded. A
  # host without python3 therefore finished with no keeper record at all, which
  # reads as "no key set" and leaves every write open, while the installer
  # printed success regardless.
  if ! keeper_key="$(docker run --rm \
      --user "$run_uid:$run_gid" \
      -v "$project_dir/data:/data" \
      --entrypoint python "$bask_image" -m server.init_keeper /data/config.json)"; then
    echo "Could not set up the Head Keeper key; Bask has NOT been left protected." >&2
    echo "Fix the error above and re-run the installer before using Bask." >&2
    exit 1
  fi
  chown "$run_user:$run_group" "$project_dir/data/config.json"
fi
if [[ ! -f "$project_dir/data/readings.db" && -f "$project_dir/readings.db" ]]; then
  sqlite3 "$project_dir/readings.db" ".backup '$project_dir/data/readings.db'"
  chown "$run_user:$run_group" "$project_dir/data/readings.db"
fi
if [[ "$legacy" == true ]]; then
  install -o "$run_user" -g "$run_group" /dev/null "$project_dir/data/.docker-migrated"
fi
if [[ ! -f "$project_dir/.env" ]]; then
  install -o "$run_user" -g "$run_group" -m 0600 \
    "$project_dir/.env.example" "$project_dir/.env"
fi

set_env_value() {
  local key="$1" value="$2" file="$project_dir/.env"
  if grep -q "^${key}=" "$file"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

bluetooth_gid="$(getent group bluetooth 2>/dev/null | cut -d: -f3 || true)"
[[ -n "$bluetooth_gid" ]] || bluetooth_gid="$run_gid"
set_env_value BASK_UID "$run_uid"
set_env_value BASK_GID "$run_gid"
set_env_value BASK_BLUETOOTH_GID "$bluetooth_gid"

echo "==> Downloading and starting Bask"
cd "$project_dir"
# Bask now runs from a published multi-architecture image, so this is a pull
# rather than a build on whatever board the keeper is installing onto.
if ! docker compose pull; then
  echo "Could not download the Bask image from ghcr.io." >&2
  echo "Check this machine's internet access and try again." >&2
  exit 1
fi

# Stop the former root process before changing ownership; otherwise its next
# SQLite checkpoint can recreate a root-owned WAL in the small gap before the
# new non-root services replace it.
docker compose stop >/dev/null 2>&1 || true

# Repair installations created by the former root container before switching
# to the non-root split services. This is idempotent and leaves every private
# settings/database/credential file owner-readable only.
chown -R "$run_uid:$run_gid" "$project_dir/data" "$project_dir/backups"
find "$project_dir/data" "$project_dir/backups" -type d -exec chmod 0700 {} +
find "$project_dir/data" "$project_dir/backups" -type f -exec chmod 0600 {} +
chmod 0600 "$project_dir/.env"

docker compose up -d

# Compose should have created two distinct trust domains. Fail before printing
# success if the browser-facing process accidentally received host D-Bus.
if docker inspect bask --format '{{range .Mounts}}{{println .Source}}{{end}}' \
    | grep -q '/var/run/dbus/system_bus_socket'; then
  echo "Bask web unexpectedly has access to host D-Bus; refusing this deployment." >&2
  docker compose stop >/dev/null 2>&1 || true
  exit 1
fi

healthy=false
for _ in $(seq 1 60); do
  health="$(docker inspect bask --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' 2>/dev/null || true)"
  if [[ "$health" == "healthy" ]]; then
    healthy=true
    break
  fi
  [[ "$health" != "unhealthy" && "$health" != "exited" && "$health" != "dead" ]] || break
  sleep 1
done
scanner_state="$(docker inspect bask-scanner --format '{{.State.Status}}' 2>/dev/null || true)"
if [[ "$healthy" != true || "$scanner_state" != "running" ]]; then
  echo "Bask did not become healthy (web=$health, scanner=${scanner_state:-missing})." >&2
  docker compose logs --tail=80 >&2 || true
  docker compose stop >/dev/null 2>&1 || true
  echo "The installation was stopped instead of reporting a working dashboard." >&2
  exit 1
fi

echo
echo "────────────────────────────────────────────────────────────"
lan_ip="$(ip -4 -o addr show scope global 2>/dev/null | awk '$2 !~ /^(docker|br-|veth|virbr|tun|tap)/ {print $4}' | cut -d/ -f1 \
  | grep -E '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)' | head -n 1 || true)"
bask_port="$(sed -n 's/^BASK_PORT=//p' "$project_dir/.env" | tail -n 1)"
[[ "$bask_port" =~ ^[0-9]+$ ]] || bask_port=8080
if [[ -n "$lan_ip" ]]; then
  # mDNS (.local) is missing on Windows without Bonjour and on many Android
  # phones; the LAN address works everywhere on the network.
  echo "  Bask is running at http://${lan_ip}:${bask_port}"
  echo "                  or http://${host}.local:${bask_port}"
else
  echo "  Bask is running at http://${host}.local:${bask_port}"
fi
echo "  Persistent data: $project_dir/data"
echo "  Back up now:     $project_dir/scripts/backup.sh"
if [[ -n "$keeper_key" ]]; then
  echo "────────────────────────────────────────────────────────────"
  echo "  Head Keeper key:  $keeper_key"
  echo
  echo "  Save this now — it is shown once and stored only as a hash."
  echo "  Anyone in the house can read the dashboard without it."
  echo "  It is needed to change sensors, enclosures, ranges, and"
  echo "  integrations. Change it any time under Manage -> Settings,"
  echo "  including to match your Shed Head Keeper code."
fi
echo "────────────────────────────────────────────────────────────"
