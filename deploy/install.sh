#!/usr/bin/env bash
# ============================================================================
#  Install Bask on a Raspberry Pi.
#
#  Most people don't run this directly — the one-line installer does it for you:
#      curl -fsSL https://raw.githubusercontent.com/jlyfshhh/bask/main/get-bask.sh | bash
#
#  To run it by hand, from the project directory on the Pi:
#      sudo bash deploy/install.sh
#
#  It is idempotent and self-adapting: it installs system + Python deps, enables
#  BlueZ passive scanning, sets up mDNS so the dashboard is reachable at
#  <hostname>.local, and installs two systemd services (scanner + web).
#  Kiosk autostart is handled separately (kiosk.sh).
# ============================================================================
set -euo pipefail

# Resolve project dir (parent of this script) and the non-root user to run as.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
PYTHON_BIN="$PROJECT_DIR/venv/bin/python3"
HOST="$(hostname)"

echo "==> Project : $PROJECT_DIR"
echo "==> User    : $RUN_USER"

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo: sudo bash deploy/install.sh" >&2
  exit 1
fi

# ── 1. System packages ──────────────────────────────────────────────────────
# python3-venv: virtualenv; avahi-daemon: mDNS (so <hostname>.local resolves);
# bluez + rfkill: Bluetooth. These ship on Raspberry Pi OS but we make sure.
echo "==> Installing system packages (this can take a minute)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip avahi-daemon bluez rfkill >/dev/null

# ── 2. Python venv + deps ───────────────────────────────────────────────────
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "==> Creating virtualenv"
  sudo -u "$RUN_USER" python3 -m venv "$PROJECT_DIR/venv"
fi
echo "==> Installing Python requirements"
sudo -u "$RUN_USER" "$PYTHON_BIN" -m pip install --upgrade pip >/dev/null
sudo -u "$RUN_USER" "$PYTHON_BIN" -m pip install -r "$PROJECT_DIR/requirements.txt"

# ── 3. First-run config ─────────────────────────────────────────────────────
if [[ ! -f "$PROJECT_DIR/config.json" ]]; then
  echo "==> Creating config.json from the example"
  sudo -u "$RUN_USER" cp "$PROJECT_DIR/config.example.json" "$PROJECT_DIR/config.json"
fi

# ── 4. BlueZ: enable experimental features (needed for passive scanning) ────
MAIN_CONF=/etc/bluetooth/main.conf
if ! grep -qE '^\s*Experimental\s*=\s*true' "$MAIN_CONF" 2>/dev/null; then
  echo "==> Enabling BlueZ Experimental (passive scanning)"
  if grep -qE '^\s*#?\s*Experimental' "$MAIN_CONF" 2>/dev/null; then
    sed -i 's/^\s*#\?\s*Experimental\s*=.*/Experimental = true/' "$MAIN_CONF"
  else
    # ensure a [General] section exists, then append the key under it
    grep -q '^\[General\]' "$MAIN_CONF" 2>/dev/null || echo '[General]' >> "$MAIN_CONF"
    sed -i '/^\[General\]/a Experimental = true' "$MAIN_CONF"
  fi
  systemctl restart bluetooth
fi

# Make sure the user can talk to BlueZ over DBus, and the radio is on.
usermod -aG bluetooth "$RUN_USER" || true
rfkill unblock bluetooth || true

# ── 5. mDNS: make the dashboard reachable at <hostname>.local ────────────────
# avahi advertises the Pi on the LAN so people open http://<hostname>.local:8080
# instead of hunting for an IP. (Set the hostname to "bask" in Raspberry Pi
# Imager and this becomes bask.local — see docs/SETUP.md.)
systemctl enable --now avahi-daemon >/dev/null 2>&1 || true

# ── 6. systemd units ────────────────────────────────────────────────────────
echo "==> Writing systemd units"

cat > /etc/systemd/system/bask-scanner.service <<UNIT
[Unit]
Description=Bask BLE scanner (Govee H5075, passive)
After=bluetooth.target network.target
Wants=bluetooth.target

[Service]
Type=simple
# Root guarantees Bluetooth adapter access on a single-purpose appliance.
User=root
WorkingDirectory=$PROJECT_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$PYTHON_BIN $PROJECT_DIR/scanner/scanner.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/bask-web.service <<UNIT
[Unit]
Description=Bask dashboard web server
After=network.target bask-scanner.service

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$PROJECT_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$PYTHON_BIN -m uvicorn server.app:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

# ── 7. Enable + (re)start ───────────────────────────────────────────────────
systemctl daemon-reload
systemctl enable bask-scanner.service bask-web.service
systemctl restart bask-scanner.service bask-web.service

echo
echo "==> Done. Service status:"
systemctl --no-pager --lines=0 status bask-scanner.service bask-web.service || true
echo
echo "────────────────────────────────────────────────────────────"
echo "  Bask is running. Open the dashboard from any device on your"
echo "  network at:"
echo
echo "        http://${HOST}.local:8080"
echo
echo "  (or http://<this-pi-ip>:8080 if .local doesn't resolve)"
echo
echo "  Then tap  ⚙ Manage → Sensors → Pair by proximity  to add"
echo "  your Govee sensors."
echo "────────────────────────────────────────────────────────────"
echo "  Scanner log:  journalctl -u bask-scanner -f"
