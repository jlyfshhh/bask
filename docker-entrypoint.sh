#!/usr/bin/env bash
set -euo pipefail
umask 077

data_dir="${BASK_DATA_DIR:-/data}"
mkdir -p "$data_dir"

if [[ ! -f "$data_dir/config.json" ]]; then
  # Web and scanner may be created together on a manual first start. `mv -n`
  # makes their identical initialization race harmless and never overwrites a
  # config another process has already installed.
  config_tmp="$(mktemp "$data_dir/.config.example.XXXXXX")"
  cp /app/config.example.json "$config_tmp"
  chmod 0600 "$config_tmp"
  mv -n "$config_tmp" "$data_dir/config.json"
  rm -f "$config_tmp"
fi

role="${BASK_ROLE:-all}"

case "$role" in
  web)
    echo "Bask dashboard starting"
    exec python -m uvicorn server.app:app --host 0.0.0.0 --port 8080 --no-server-header
    ;;
  scanner)
    echo "Bask BLE scanner starting"
    exec env PYTHONPATH=/app/scanner python /app/scanner/scanner.py
    ;;
  all)
    # Compatibility for direct `docker run` and older development overrides.
    # Production Compose sets one role per container so only the scanner sees
    # host D-Bus and the network-facing web process cannot reach Bluetooth.
    ;;
  *)
    echo "Unknown BASK_ROLE '$role' (expected web, scanner, or all)" >&2
    exit 64
    ;;
esac

scanner_pid=""
web_pid=""

stop_children() {
  [[ -z "$scanner_pid" ]] || kill "$scanner_pid" 2>/dev/null || true
  [[ -z "$web_pid" ]] || kill "$web_pid" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap stop_children EXIT INT TERM

if [[ "${BASK_SCANNER_ENABLED:-true}" == "true" ]]; then
  PYTHONPATH=/app/scanner python /app/scanner/scanner.py &
  scanner_pid=$!
  echo "Bask BLE scanner started (PID $scanner_pid)"
else
  echo "Bask BLE scanner disabled by BASK_SCANNER_ENABLED"
fi

python -m uvicorn server.app:app --host 0.0.0.0 --port 8080 &
web_pid=$!
echo "Bask dashboard started (PID $web_pid)"

if [[ -n "$scanner_pid" ]]; then
  wait -n "$scanner_pid" "$web_pid"
else
  wait "$web_pid"
fi
