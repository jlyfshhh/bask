#!/usr/bin/env bash
# System setup and legacy-to-Docker migration for Bask.
set -euo pipefail

project_dir="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
run_user="${2:-${SUDO_USER:-$(id -un)}}"
run_group="$(id -gn "$run_user")"
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
apt-get install -y -qq avahi-daemon bluez rfkill sqlite3
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
install -d -o "$run_user" -g "$run_group" "$project_dir/data" "$project_dir/backups"

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
  install -d -o "$run_user" -g "$run_group" "$legacy_backup"
  [[ ! -f "$project_dir/config.json" ]] ||
    install -o "$run_user" -g "$run_group" "$project_dir/config.json" "$legacy_backup/config.json"
  if [[ -f "$project_dir/readings.db" ]]; then
    sqlite3 "$project_dir/readings.db" ".backup '$legacy_backup/readings.db'"
    chown "$run_user:$run_group" "$legacy_backup/readings.db"
  fi
  echo "    Original install backed up to $legacy_backup"
fi

if [[ ! -f "$project_dir/data/config.json" ]]; then
  source_config="$project_dir/config.json"
  [[ -f "$source_config" ]] || source_config="$project_dir/config.example.json"
  install -o "$run_user" -g "$run_group" "$source_config" "$project_dir/data/config.json"
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

echo "==> Building and starting Bask"
cd "$project_dir"
docker compose up -d --build

echo
echo "────────────────────────────────────────────────────────────"
echo "  Bask is running at http://${host}.local:8080"
echo "  Persistent data: $project_dir/data"
echo "  Back up now:     $project_dir/scripts/backup.sh"
echo "────────────────────────────────────────────────────────────"
