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
# Seed the exact adversarial state: valid WAL sidecars that EXIST on disk and are
# root-owned. Two bugs made the old version of this prove nothing:
#   * `docker exec` without -i never forwarded the heredoc, so this python ran an
#     empty program and no root-owned sidecars were ever created;
#   * a clean SQLite close checkpoints and can delete -wal/-shm, so even when it
#     ran, the sidecars could vanish — and the assertion then accepted their
#     absence, happily "passing" a case where no root-owned sidecars existed.
# Forward stdin with -i, disable autocheckpoint, and hard-exit without a clean
# close so the root-owned sidecars are still on disk for the reconcile to repair.
docker exec -i -u 0 bask-scanner python - <<'PY'
import os, sqlite3, sys
c = sqlite3.connect("/data/readings.db", timeout=10)
c.execute("PRAGMA journal_mode=WAL")
c.execute("PRAGMA wal_autocheckpoint=0")
c.execute("CREATE TABLE IF NOT EXISTS _probe(x)")
c.execute("INSERT INTO _probe VALUES (1)")
c.commit()
# The main database stays web-owned (the web created it first); only the WAL
# sidecars become root-owned, which is the state that stranded a recreated web.
for suffix in ("-wal", "-shm"):
    p = "/data/readings.db" + suffix
    if os.path.exists(p):
        os.chown(p, 0, 0)
ok = all(os.path.exists("/data/readings.db" + s) for s in ("-wal", "-shm"))
# Do NOT close: a clean close would checkpoint and could remove the sidecars we
# must leave behind. Hard-exit so they persist on disk, root-owned.
sys.stdout.flush()
os._exit(0 if ok else 17)
PY
if [[ $? -ne 0 ]]; then
  echo "Could not seed root-owned WAL sidecars; the adversarial state was never created." >&2
  exit 1
fi
# Both sidecars must EXIST and be root-owned BEFORE reconciliation, or there is
# nothing adversarial to reconcile and the test is vacuous.
for suffix in -wal -shm; do
  f="$work/data/readings.db$suffix"
  if [[ ! -e "$f" ]]; then
    echo "readings.db$suffix was not seeded; the WAL adversarial state is missing." >&2
    exit 1
  fi
  owner="$(stat -c '%u' "$f")"
  if [[ "$owner" != 0 ]]; then
    echo "readings.db$suffix is uid $owner before reconcile, not root — adversarial state not seeded." >&2
    exit 1
  fi
done
echo "  seeded root-owned WAL sidecars that genuinely exist on disk"
# The scanner's reconcile must hand them back to the web uid (needs CAP_CHOWN).
docker exec -u 0 bask-scanner python -c "import sys; sys.path.insert(0,'/app/scanner'); import db; db._reconcile_owner()"
for suffix in -wal -shm; do
  f="$work/data/readings.db$suffix"
  if [[ ! -e "$f" ]]; then
    echo "readings.db$suffix disappeared during reconcile; it must be reconciled, not removed." >&2
    exit 1
  fi
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

# F04 regression: the both-services-stopped cold start. With every service down,
# existing data can be a web-owned mode-0700 /data holding root-owned WAL
# sidecars (a prior root scanner's leftovers). The one-shot bask-init must repair
# ownership before the web starts — which needs CAP_DAC_READ_SEARCH just to
# traverse and read the 0700 directory. CAP_CHOWN alone cannot, which is why the
# old glob silently repaired nothing and still exited 0. Prove recovery end to
# end: without the fix, init either silently does nothing (web crash-loops and
# never goes healthy) or, with the explicit allowlist, fails non-zero and the
# web never starts. Either way this cold start would not become healthy.
echo "  cold-start repair regression (both services stopped, 0700 /data, root-owned WAL)"
compose down >/dev/null 2>&1
# The test user cannot chown to root, so seed the stranded state from a transient
# root container: valid root-owned WAL sidecars under a web-owned 0700 /data.
docker run --rm -i -u 0 -v "$work/data:/data" -e BASK_DATA_DIR=/data \
  --entrypoint python "$image" - <<'PY'
import os, sqlite3, sys
c = sqlite3.connect("/data/readings.db", timeout=10)
c.execute("PRAGMA journal_mode=WAL")
c.execute("PRAGMA wal_autocheckpoint=0")
c.execute("CREATE TABLE IF NOT EXISTS _probe(x)")
c.execute("INSERT INTO _probe VALUES (1)")
c.commit()
for suffix in ("-wal", "-shm"):
    p = "/data/readings.db" + suffix
    if os.path.exists(p):
        os.chown(p, 0, 0)
# Lock the private directory root must traverse and read to reach the sidecars.
os.chmod("/data", 0o700)
ok = all(os.path.exists("/data/readings.db" + s) for s in ("-wal", "-shm"))
sys.stdout.flush()
os._exit(0 if ok else 17)
PY
if [[ $? -ne 0 ]]; then
  echo "Could not seed the cold-start adversarial state." >&2
  exit 1
fi
for suffix in -wal -shm; do
  owner="$(stat -c '%u' "$work/data/readings.db$suffix" 2>/dev/null || echo missing)"
  if [[ "$owner" != 0 ]]; then
    echo "cold-start setup: readings.db$suffix is '$owner', not root — adversarial state not seeded." >&2
    exit 1
  fi
done
echo "  seeded a web-owned 0700 /data with root-owned WAL sidecars, all services stopped"
# Cold start. Compose runs bask-init to completion before the web; a failed init
# (for example CAP_DAC_READ_SEARCH missing) aborts `up` here.
if ! compose up -d >/dev/null 2>&1; then
  echo "The cold start did not come up; bask-init could not repair ownership under 0700 /data." >&2
  docker logs bask-init 2>&1 | tail -20 >&2 || true
  exit 1
fi
init_status="$(docker inspect --format '{{.State.Status}}' bask-init 2>/dev/null || true)"
init_exit="$(docker inspect --format '{{.State.ExitCode}}' bask-init 2>/dev/null || true)"
if [[ "$init_status" != exited || "$init_exit" != 0 ]]; then
  echo "bask-init did not complete the cold-start repair cleanly (status=${init_status:-missing}, exit=${init_exit:-missing})." >&2
  docker logs bask-init 2>&1 | tail -20 >&2 || true
  exit 1
fi
# Ownership must be back with the web uid. bask-init runs and exits before the
# web starts, so any sidecar still on disk must already be web-owned; a sidecar
# the first healthy web open folds away is also acceptable — what must never
# survive is a root-owned sidecar the non-root web cannot read.
for suffix in -wal -shm; do
  f="$work/data/readings.db$suffix"
  [[ -e "$f" ]] || continue
  owner="$(stat -c '%u' "$f")"
  if [[ "$owner" != "$want_uid" ]]; then
    echo "after the cold-start repair readings.db$suffix is uid $owner, not web uid $want_uid." >&2
    exit 1
  fi
done
cold=""
for _ in $(seq 1 30); do
  state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}nohc{{end}}' bask 2>/dev/null || true)"
  if [[ "$state" == "healthy" ]]; then cold=1; break; fi
  if [[ "$state" == "unhealthy" ]]; then break; fi
  sleep 2
done
if [[ -z "$cold" ]]; then
  echo "The web never became healthy on a cold start with pre-existing root-owned WAL under 0700 /data." >&2
  docker logs bask 2>&1 | tail -20 >&2 || true
  exit 1
fi
echo "  bask-init repaired ownership and the web came up healthy from a cold start"

echo "First-start test passed."
