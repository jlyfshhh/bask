#!/usr/bin/env bash
# A first start on a clean, empty data directory must leave both services able
# to use the shared files.
#
# The web (non-root) and the scanner (root) both run db.init_db(), and the DB is
# WAL mode, so whichever creates readings.db and its -wal owns them. If the root
# scanner wins, the files are root-owned mode 0600 and the non-root web can never
# read them — no config, no dashboard. compose.yaml fixes this by giving the web
# a /api/health healthcheck (which calls init_db) and making the scanner wait for
# it, so the web always creates the files first. This proves that end to end
# against real Docker, which is the only place the race exists.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! docker compose version >/dev/null 2>&1; then
  echo "First-start test skipped — no working docker compose on this host." >&2
  exit 0
fi

# Use the image the CI build step produced; build it if running standalone.
if ! docker image inspect bask:test >/dev/null 2>&1; then
  echo "  building bask:test (not already present)"
  docker build -q -t bask:test "$root" >/dev/null
fi
tag="firststart-$$"
image="ghcr.io/jlyfshhh/bask:${tag}"
docker tag bask:test "$image"

work="$(mktemp -d "${TMPDIR:-/tmp}/bask-first-start.XXXXXX")"
cleanup() {
  (cd "$work" && docker compose --project-directory "$work" --env-file "$work/.env" \
     -f "$work/compose.yaml" down -v --remove-orphans >/dev/null 2>&1) || true
  docker rmi -f "$image" >/dev/null 2>&1 || true
  rm -rf -- "$work"
}
trap cleanup EXIT

cp "$root/compose.yaml" "$work/compose.yaml"
cp "$root/.env.example" "$work/.env"
sed -i.bak 's/replace-with-[a-z-]*/first-start-test-value-not-a-real-secret/g' "$work/.env"
# Run the web as this user (as a real install does), pin the image to the one we
# just tagged, and bind a high port to avoid clashing with anything on the host.
{
  printf 'BASK_UID=%s\n' "$(id -u)"
  printf 'BASK_GID=%s\n' "$(id -g)"
  printf 'BASK_TAG=%s\n' "$tag"
  printf 'BASK_PORT=%s\n' "18080"
  printf 'BASK_BIND_ADDRESS=127.0.0.1\n'
} >> "$work/.env"
rm -f "$work/.env.bak"

# A genuinely empty data directory, owned by this user — a keeper's first start.
mkdir -p "$work/data" "$work/backups"

cd "$work"
compose() { docker compose --project-directory "$work" --env-file "$work/.env" -f "$work/compose.yaml" "$@"; }
compose up -d

# Wait for the web to become healthy (which means it ran init_db and is serving).
healthy=""
for _ in $(seq 1 60); do
  state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}nohc{{end}}' bask 2>/dev/null || true)"
  if [[ "$state" == "healthy" ]]; then healthy=1; break; fi
  if [[ "$state" == "unhealthy" ]]; then break; fi
  sleep 2
done
if [[ -z "$healthy" ]]; then
  echo "The web service never became healthy on a first start." >&2
  docker logs bask 2>&1 | tail -20 >&2 || true
  exit 1
fi
echo "  the web service came up healthy on an empty data directory"

# The scanner depends on the web being healthy, so give it a moment to start and
# possibly touch /data. (It finds no Bluetooth in CI; that is fine — the race we
# are testing is about file creation ownership, not whether it sees a sensor.)
sleep 4

# Both shared files must exist and be owned by the web uid, so the web can read
# them. This is the assertion the whole compose ordering exists to guarantee.
want_uid="$(id -u)"
for f in config.json readings.db; do
  if [[ ! -f "$work/data/$f" ]]; then
    echo "$f was not created on first start." >&2
    exit 1
  fi
  owner="$(stat -c '%u' "$work/data/$f")"
  if [[ "$owner" != "$want_uid" ]]; then
    echo "data/$f is owned by uid $owner, not the web uid $want_uid — the root scanner won the race." >&2
    ls -lan "$work/data" >&2
    exit 1
  fi
done
echo "  config.json and readings.db are owned by the web uid, readable by the web"

# And the web genuinely serves, proving it can read what was created.
if ! docker exec bask python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4).status==200 else 1)" >/dev/null 2>&1; then
  echo "The web service is healthy but /api/health did not return 200." >&2
  exit 1
fi
echo "  /api/health responds, so the web is reading its own config and database"

# Regression for the 2026-10-09 web-recreate WAL-ownership race. While the root
# scanner keeps running, a web recreate must recover. The scanner's DB layer hands
# the WAL sidecars back to the web uid, which needs CAP_CHOWN — without it the
# chown fails EPERM, is swallowed, and the web can never reopen the database.
echo "  web-recreate ownership regression (scanner keeps running)"
# As root (the scanner's identity) create valid WAL sidecars, root-owned: the
# exact state that stranded the web.
docker exec -u 0 bask-scanner python - <<'PY'
import os, sqlite3
c = sqlite3.connect("/data/readings.db", timeout=10)
c.execute("PRAGMA journal_mode=WAL")
c.execute("CREATE TABLE IF NOT EXISTS _probe(x)")
c.execute("INSERT INTO _probe VALUES (1)")
c.commit(); c.close()
for suffix in ("-wal", "-shm"):
    p = "/data/readings.db" + suffix
    if os.path.exists(p):
        os.chown(p, 0, 0)
PY
# The scanner's reconcile must hand them back to the web uid (needs CAP_CHOWN).
docker exec -u 0 bask-scanner python -c "import sys; sys.path.insert(0,'/app/scanner'); import db; db._reconcile_owner()"
for suffix in -wal -shm; do
  f="$work/data/readings.db$suffix"
  [[ -e "$f" ]] || continue
  owner="$(stat -c '%u' "$f")"
  if [[ "$owner" != "$want_uid" ]]; then
    echo "readings.db$suffix is uid $owner after reconcile, not web uid $want_uid — the scanner cannot chown (missing CAP_CHOWN?)." >&2
    exit 1
  fi
done
echo "  the scanner reconciled root-owned WAL sidecars back to the web uid"
# And a real web recreate while the scanner keeps running must come back healthy.
compose up -d --no-deps --force-recreate bask >/dev/null 2>&1
recreated=""
for _ in $(seq 1 30); do
  state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}nohc{{end}}' bask 2>/dev/null || true)"
  if [[ "$state" == "healthy" ]]; then recreated=1; break; fi
  sleep 2
done
if [[ -z "$recreated" ]]; then
  echo "The web did not recover healthy after a recreate while the scanner was running." >&2
  docker logs bask 2>&1 | tail -20 >&2 || true
  exit 1
fi
echo "  the web recovered healthy after a recreate alongside the running scanner"

echo "First-start test passed."
