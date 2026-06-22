"""SQLite layer shared by the scanner (writer) and web server (reader).

Design notes for the Raspberry Pi:
  * The scanner buffers readings in memory and calls the flush_* helpers on an
    interval, so we do a handful of batched writes per minute rather than one
    write per BLE advertisement. That spares the SD card from write thrash.
  * `readings`   - one current row per sensor (what the dashboard reads).
  * `history`    - sampled time-series (throttled), pruned to 24h.
  * `discovered` - every Govee device the scanner currently sees, for the
    "add sensor" UI. This replaces the old second in-server BLE scanner.
"""
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "readings.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL keeps the reader (web server) from blocking the writer (scanner).
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                mac        TEXT PRIMARY KEY,
                temp_c     REAL,
                humidity   REAL,
                battery    INTEGER,
                rssi       INTEGER,
                updated_at INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                mac         TEXT,
                temp_c      REAL,
                humidity    REAL,
                rssi        INTEGER,
                recorded_at INTEGER
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_mac ON history(mac, recorded_at)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS discovered (
                mac       TEXT PRIMARY KEY,
                name      TEXT,
                temp_c    REAL,
                humidity  REAL,
                battery   INTEGER,
                rssi      INTEGER,
                last_seen INTEGER
            )
        """)
        # Migrate an older readings table that predates the battery column.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(readings)")}
        if "battery" not in cols:
            conn.execute("ALTER TABLE readings ADD COLUMN battery INTEGER")


# ── Writer side (scanner) ────────────────────────────────────────────────────

def flush_readings(current: list[tuple[str, dict]], last_history: dict, history_interval: int) -> None:
    """Batch-write current readings. `current` is [(mac, {temp_c,humidity,battery,rssi,ts}), ...].

    A history sample is appended for a sensor only once per `history_interval`
    seconds. `last_history` (mac -> ts) is maintained by the caller across calls.
    """
    if not current:
        return
    with get_conn() as conn:
        for mac, r in current:
            ts = int(r["ts"])
            conn.execute("""
                INSERT INTO readings (mac, temp_c, humidity, battery, rssi, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(mac) DO UPDATE SET
                    temp_c=excluded.temp_c, humidity=excluded.humidity,
                    battery=excluded.battery, rssi=excluded.rssi,
                    updated_at=excluded.updated_at
            """, (mac, r["temp_c"], r["humidity"], r["battery"], r["rssi"], ts))
            if ts - last_history.get(mac, 0) >= history_interval:
                conn.execute(
                    "INSERT INTO history (mac, temp_c, humidity, rssi, recorded_at) VALUES (?, ?, ?, ?, ?)",
                    (mac, r["temp_c"], r["humidity"], r["rssi"], ts),
                )
                last_history[mac] = ts
        conn.execute("DELETE FROM history WHERE recorded_at < ?", (int(time.time()) - 86400,))


def flush_discovered(items: list[tuple[str, dict]], now: int) -> None:
    """Upsert every currently-seen Govee device and prune ones gone for 5 min."""
    with get_conn() as conn:
        for mac, d in items:
            conn.execute("""
                INSERT INTO discovered (mac, name, temp_c, humidity, battery, rssi, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mac) DO UPDATE SET
                    name=excluded.name, temp_c=excluded.temp_c, humidity=excluded.humidity,
                    battery=excluded.battery, rssi=excluded.rssi, last_seen=excluded.last_seen
            """, (mac, d["name"], d["temp_c"], d["humidity"], d["battery"], d["rssi"], int(d["ts"])))
        conn.execute("DELETE FROM discovered WHERE last_seen < ?", (now - 300,))


# ── Reader side (web server) ─────────────────────────────────────────────────

def get_all_readings() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM readings")]


def get_discovered(within_seconds: int = 30) -> list[dict]:
    cutoff = int(time.time()) - within_seconds
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM discovered WHERE last_seen >= ? ORDER BY rssi DESC", (cutoff,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_history(mac: str, hours: int = 6) -> list[dict]:
    cutoff = int(time.time()) - hours * 3600
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM history WHERE mac=? AND recorded_at >= ? ORDER BY recorded_at ASC",
            (mac.upper(), cutoff),
        ).fetchall()
        return [dict(r) for r in rows]
