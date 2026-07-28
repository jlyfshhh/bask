#!/usr/bin/env bash
set -euo pipefail

data_dir="${BASK_DATA_DIR:-/data}"
mkdir -p "$data_dir"

if [[ ! -f "$data_dir/config.json" ]]; then
  cp /app/config.example.json "$data_dir/config.json"
fi

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
