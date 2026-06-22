#!/usr/bin/env bash
# Run the BLE scanner and web server together (for local/dev use).
# In production, run them as two systemd services instead (see the README).
set -e
cd "$(dirname "$0")"

PYTHON="$(pwd)/venv/bin/python3"
[ -x "$PYTHON" ] || PYTHON="python3"

# Single scanner process owns the Bluetooth adapter.
PYTHONPATH="$(pwd)/scanner" "$PYTHON" scanner/scanner.py &
SCANNER_PID=$!
echo "Scanner started (PID $SCANNER_PID)"

# Web server does no Bluetooth — it only reads the shared SQLite DB.
"$PYTHON" -m uvicorn server.app:app --host 0.0.0.0 --port 8080 &
SERVER_PID=$!
echo "Server started on http://0.0.0.0:8080 (PID $SERVER_PID)"

trap "kill $SCANNER_PID $SERVER_PID 2>/dev/null" EXIT INT TERM
wait
