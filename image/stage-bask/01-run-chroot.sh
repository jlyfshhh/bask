#!/bin/bash -e
# ============================================================================
#  pi-gen stage script — runs inside the image chroot (as root) at build time.
#
#  Bakes Bask into the image: clones it, builds the Python venv, installs the
#  systemd services, and enables them so the dashboard comes up automatically
#  on the real first boot. Nothing is *started* here (there's no init in the
#  chroot) — services are only enabled.
#
#  @BASK_REF@ is replaced by the build workflow with the git ref to bake in
#  (a release tag, or "main").
# ============================================================================

USER_NAME="${FIRST_USER_NAME:-bask}"
USER_HOME="/home/${USER_NAME}"
BASK_DIR="${USER_HOME}/bask"
PYTHON_BIN="${BASK_DIR}/venv/bin/python3"
BASK_REF="@BASK_REF@"

echo "==> Installing Bask (ref ${BASK_REF}) for user ${USER_NAME}"

git clone --depth 1 --branch "${BASK_REF}" https://github.com/jlyfshhh/bask.git "${BASK_DIR}"

# First-run config from the shipped example.
cp "${BASK_DIR}/config.example.json" "${BASK_DIR}/config.json"

# Python venv + dependencies, baked in so the first boot needs no internet.
python3 -m venv "${BASK_DIR}/venv"
"${PYTHON_BIN}" -m pip install --no-cache-dir --upgrade pip
"${PYTHON_BIN}" -m pip install --no-cache-dir -r "${BASK_DIR}/requirements.txt"

# Hand the whole tree to the unprivileged user that runs the web server.
chown -R "${USER_NAME}:${USER_NAME}" "${BASK_DIR}"

# BlueZ passive scanning needs Experimental = true.
CONF=/etc/bluetooth/main.conf
if ! grep -qE '^\s*Experimental\s*=\s*true' "$CONF" 2>/dev/null; then
	if grep -qE '^\s*#?\s*Experimental' "$CONF" 2>/dev/null; then
		sed -i 's/^\s*#\?\s*Experimental\s*=.*/Experimental = true/' "$CONF"
	else
		grep -q '^\[General\]' "$CONF" 2>/dev/null || echo '[General]' >> "$CONF"
		sed -i '/^\[General\]/a Experimental = true' "$CONF"
	fi
fi
usermod -aG bluetooth "${USER_NAME}" || true

# ── systemd units ───────────────────────────────────────────────────────────
cat > /etc/systemd/system/bask-scanner.service <<UNIT
[Unit]
Description=Bask BLE scanner (Govee H5075, passive)
After=bluetooth.target network.target
Wants=bluetooth.target

[Service]
Type=simple
User=root
WorkingDirectory=${BASK_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${PYTHON_BIN} ${BASK_DIR}/scanner/scanner.py
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
User=${USER_NAME}
WorkingDirectory=${BASK_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${PYTHON_BIN} -m uvicorn server.app:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

# Allow the in-app updater (running as the unprivileged web user) to restart
# the root-owned scanner — one exactly-scoped sudo rule, nothing else.
echo "${USER_NAME} ALL=(root) NOPASSWD: /usr/bin/systemctl restart bask-scanner.service" \
  > /etc/sudoers.d/012_bask-update
chmod 440 /etc/sudoers.d/012_bask-update
visudo -cf /etc/sudoers.d/012_bask-update

# Enable services + mDNS so they start on the real first boot.
systemctl enable bask-scanner.service
systemctl enable bask-web.service
systemctl enable avahi-daemon.service || true

echo "==> Bask install complete."
