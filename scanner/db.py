"""SQLite layer shared by the scanner (writer) and web server (reader).

Design notes for the Raspberry Pi:
  * The scanner buffers readings in memory and calls the flush_* helpers on an
    interval, so we do a handful of batched writes per minute rather than one
    write per BLE advertisement. That spares the SD card from write thrash.
  * `readings`   - one current row per sensor (what the dashboard reads).
  * `history`    - sampled time-series (throttled), pruned to 24h.
  * `discovered` - every Govee device the scanner currently sees, for the
    "add sensor" UI. This replaces the old second in-server BLE scanner.
  * `climate_*`  - the long-term cross-instrument log (sensors, thermostats and
    the mini-split on one aligned clock), written by the web server rather than
    the scanner because that is the process holding the Herpstat and Cielo
    state. See the climate section below.
"""
import sqlite3
import os
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
DATA_DIR = Path(os.environ.get("BASK_DATA_DIR", ROOT))
DB_PATH = DATA_DIR / "readings.db"


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

        init_climate(conn)


# ── Climate log ──────────────────────────────────────────────────────────────
# `history` answers "what has this one sensor done lately" and is pruned at 24h.
# The climate log answers a different question: what was the *whole room* doing,
# across every instrument, over months — which is what you need to work out the
# mini-split setpoint that holds an enclosure overnight, or why the morning ramp
# takes three hours to settle when the lamps ramp in one.
#
# Two design decisions worth stating, because both are expensive to change once
# there is a year of data:
#
# 1. Series are interned into `climate_series` and samples reference them by id,
#    so a sample row carries an integer rather than a repeated MAC and metric
#    name. Measured against this room's real shape — 27 sensors, 12 thermostat
#    outputs, one mini-split, 93 series at a 60-second tick — that is 3.1 MB a
#    day of raw, so the fortnight kept below is about 43 MB, and the hourly
#    rollup that outlives it costs 64 KB a day, or roughly 24 MB a year.
#
# 2. Every temperature is stored in CELSIUS, whatever the instrument reported.
#    The sources genuinely disagree — the Govee scanner works in Celsius, Cielo
#    reports Fahrenheit, and Bask labels Herpstat readings with the keeper's
#    *display* preference rather than the thermostat's own unit. A log that
#    stored "whatever arrived" would be silently unusable the day someone
#    toggles that preference, and the damage would be invisible until a trend
#    line bent for no reason. Convert on the way in; format on the way out.

CLIMATE_SCHEMA = (
    # The dimension table. `label` is the human name at the time the series was
    # first seen, kept so a chart of a since-deleted enclosure still reads as
    # something other than a MAC address.
    """
    CREATE TABLE IF NOT EXISTS climate_series (
        id     INTEGER PRIMARY KEY,
        source TEXT NOT NULL,
        series TEXT NOT NULL,
        metric TEXT NOT NULL,
        label  TEXT,
        UNIQUE (source, series, metric)
    )
    """,
    # Raw samples, pruned to RAW_RETENTION_DAYS. WITHOUT ROWID because the
    # primary key *is* the whole row apart from the value.
    """
    CREATE TABLE IF NOT EXISTS climate_samples (
        recorded_at INTEGER NOT NULL,
        series_id   INTEGER NOT NULL,
        value       REAL,
        PRIMARY KEY (recorded_at, series_id)
    ) WITHOUT ROWID
    """,
    # Hourly aggregates, kept indefinitely. Min and max are carried alongside
    # the mean because an average hides exactly the thing that matters here: a
    # mean of 74 is fine, and a mean of 74 that dipped to 66 is not.
    """
    CREATE TABLE IF NOT EXISTS climate_hourly (
        hour      INTEGER NOT NULL,
        series_id INTEGER NOT NULL,
        min_value REAL,
        avg_value REAL,
        max_value REAL,
        samples   INTEGER,
        PRIMARY KEY (hour, series_id)
    ) WITHOUT ROWID
    """,
    # Exclusive high-water mark for the hourly rollup. Without this, the
    # maintenance job has to rediscover its progress by scanning and rewriting
    # the entire raw-retention window every hour. A singleton row is enough:
    # all series advance on the same aligned clock.
    """
    CREATE TABLE IF NOT EXISTS climate_rollup_state (
        singleton    INTEGER PRIMARY KEY CHECK (singleton = 1),
        through_hour INTEGER NOT NULL
    )
    """,
    # Categorical state — Cielo mode, power, fan. Sampling these every minute
    # would store the same string 1,440 times a day to record two changes, so
    # they are written only when the value differs from the last one logged.
    """
    CREATE TABLE IF NOT EXISTS climate_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        recorded_at INTEGER NOT NULL,
        source      TEXT NOT NULL,
        series      TEXT NOT NULL,
        key         TEXT NOT NULL,
        value       TEXT
    )
    """,
    # No secondary index on climate_samples. Its primary key already leads with
    # recorded_at, and every query against raw is a time-range scan — raw only
    # ever covers the last fortnight, so "this one series over all time" is a
    # question for the rollup. Measured: the index was costing 3.7 MB a day to
    # serve nothing.
    "CREATE INDEX IF NOT EXISTS idx_climate_hourly_series ON climate_hourly(series_id, hour)",
    "CREATE INDEX IF NOT EXISTS idx_climate_events_at ON climate_events(recorded_at)",
)

# Raw samples are for reading a transition minute by minute; anything older than
# this is answered from the hourly rollup, which is ~40x smaller.
RAW_RETENTION_DAYS = 14

# Rebuild the two most recently completed hours on every maintenance pass. This
# accepts samples that arrive a little late without turning the 14-day raw
# retention window into a 14-day rescan. The writer normally samples live on an
# aligned clock, so two hours is deliberately generous while remaining bounded.
CLIMATE_ROLLUP_LATE_HOURS = 2


def init_climate(conn: sqlite3.Connection) -> None:
    for statement in CLIMATE_SCHEMA:
        conn.execute(statement)


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


def flush_discovered(items: list[tuple[str, dict]], now: int, configured: set[str] | None = None,
                     max_unconfigured: int | None = None) -> None:
    """Upsert the devices seen since the last flush and prune ones gone for 5 min.

    `items` is only the rows that changed — a full re-upsert of everything the
    scanner is holding would refresh `last_seen` on every row and the age prune
    below could never remove anything.

    `max_unconfigured` additionally caps how many non-configured devices the
    table may hold, keeping the newest by `last_seen`. Age alone is not a bound:
    a device rotating its address emits an unlimited number of distinct rows
    inside any one prune window. Devices in `configured` are exempt from the cap
    (never from the age prune — a sensor out of range should leave the list).
    """
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
        if max_unconfigured is not None:
            exempt = sorted(configured or ())
            keep = "" if not exempt else \
                f" WHERE mac NOT IN ({','.join('?' * len(exempt))})"
            # ORDER BY ... , mac makes the survivors identical for identical
            # input, so the cap behaves the same on every run.
            conn.execute(
                f"""DELETE FROM discovered WHERE mac IN (
                        SELECT mac FROM discovered{keep}
                        ORDER BY last_seen DESC, mac ASC
                        LIMIT -1 OFFSET ?
                    )""",
                (*exempt, max(0, max_unconfigured)),
            )


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


# ── Climate log: writer ──────────────────────────────────────────────────────

def _series_id(conn: sqlite3.Connection, source: str, series: str, metric: str,
               label: str | None) -> int:
    """Intern a series, refreshing the label if the keeper has renamed it."""
    row = conn.execute(
        "SELECT id, label FROM climate_series WHERE source=? AND series=? AND metric=?",
        (source, series, metric),
    ).fetchone()
    if row:
        if label and row["label"] != label:
            conn.execute("UPDATE climate_series SET label=? WHERE id=?", (label, row["id"]))
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO climate_series (source, series, metric, label) VALUES (?, ?, ?, ?)",
        (source, series, metric, label),
    )
    new_id = int(cur.lastrowid)
    # Creation is exactly the right moment to look for history stranded under an
    # older key: it happens once, and only for a series that has just appeared.
    _adopt_legacy_cielo(conn, source, series, metric, new_id)
    return new_id


LEGACY_CIELO_SERIES = "cielo"


def merge_legacy_cielo(conn: sqlite3.Connection) -> int:
    """Sweep any mini-split history still filed under the old constant key.

    Covers the case series creation cannot: a database where both keys already
    exist because the upgrade ran and wrote a tick before this repair existed.
    Nothing is ever created again for that key, so creation-time adoption would
    never fire and the split would be permanent.

    Deliberately NOT called from init_climate. A first attempt ran it there and
    took the whole application down — the rewrite raised a disk I/O error, the
    exception escaped startup, and the container crash-looped with no dashboard
    and no logging while the migration retried and failed on every boot. A data
    repair is never worth more than the service it runs inside, so this is
    driven from the maintenance loop, where a failure is already caught, logged,
    and retried on the next pass with the app still serving.

    Idempotent, and a no-op once there is no legacy series left.
    """
    merged = 0
    rows = conn.execute(
        "SELECT DISTINCT metric FROM climate_series WHERE source='cielo' AND series=?",
        (LEGACY_CIELO_SERIES,),
    ).fetchall()
    for row in rows:
        metric = row["metric"]
        target = conn.execute(
            "SELECT id, series FROM climate_series "
            "WHERE source='cielo' AND metric=? AND series != ?",
            (metric, LEGACY_CIELO_SERIES),
        ).fetchall()
        if len(target) != 1:
            continue
        _adopt_legacy_cielo(conn, "cielo", target[0]["series"], metric,
                            int(target[0]["id"]))
        merged += 1
    return merged


def _adopt_legacy_cielo(conn: sqlite3.Connection, source: str, series: str,
                        metric: str, new_id: int) -> None:
    """Carry pre-pseudonym mini-split history onto the new series key.

    The mini-split used to be logged under the constant key "cielo". That was a
    bug: the public status deliberately omits the cloud device id, so the lookup
    always fell through to the fallback, and two controllers would have been
    spliced into a single line. The key is now a stable per-device pseudonym.

    Correct, but on its own it strands every reading taken before the change
    under the old key — and a trend that stops dead on an upgrade date is the
    one thing a log built for months of comparison must never do.

    Only merged where it is unambiguous: exactly one pseudonymous key exists, so
    the legacy rows can only have come from that device. A house running two
    controllers cannot know which one they belong to, so there the old series is
    left alone as history rather than guessed at.
    """
    if source != "cielo" or series == LEGACY_CIELO_SERIES:
        return
    legacy = conn.execute(
        "SELECT id FROM climate_series WHERE source='cielo' AND series=? AND metric=?",
        (LEGACY_CIELO_SERIES, metric),
    ).fetchone()
    if legacy is None:
        return
    distinct = conn.execute(
        "SELECT COUNT(DISTINCT series) FROM climate_series "
        "WHERE source='cielo' AND series != ?", (LEGACY_CIELO_SERIES,),
    ).fetchone()[0]
    if distinct != 1:
        return

    legacy_id = int(legacy["id"])
    # OR REPLACE rather than a plain UPDATE: if both keys somehow hold a row for
    # the same instant, the newer one wins instead of the migration aborting on
    # a primary-key conflict and leaving the history half-moved.
    conn.execute("UPDATE OR REPLACE climate_samples SET series_id=? WHERE series_id=?",
                 (new_id, legacy_id))
    conn.execute("UPDATE OR REPLACE climate_hourly SET series_id=? WHERE series_id=?",
                 (new_id, legacy_id))
    conn.execute("DELETE FROM climate_series WHERE id=?", (legacy_id,))
    # Events key on the text, not the interned id.
    conn.execute("UPDATE climate_events SET series=? WHERE source='cielo' AND series=?",
                 (series, LEGACY_CIELO_SERIES))


def write_climate_tick(recorded_at: int, samples: list[dict], events: list[dict]) -> int:
    """Persist one aligned sample tick.

    Every row carries the same `recorded_at` on purpose. The whole point of this
    log is to compare instruments against each other — what the mini-split was
    doing while a given enclosure fell two degrees — and joining series that
    were each stamped whenever their own poller happened to fire means
    interpolating before you can ask the question. One tick, one timestamp.

    `samples` is [{source, series, metric, value, label}, ...] with value in
    canonical units (Celsius for temperatures). `events` is the same shape with
    `key`/`value` strings, and is written only where the value has changed.
    """
    written = 0
    with get_conn() as conn:
        for s in samples:
            if s.get("value") is None:
                continue
            sid = _series_id(conn, s["source"], s["series"], s["metric"], s.get("label"))
            # A tick that fires twice for the same second must not explode; the
            # later value simply wins.
            conn.execute(
                "INSERT INTO climate_samples (recorded_at, series_id, value) VALUES (?, ?, ?) "
                "ON CONFLICT(recorded_at, series_id) DO UPDATE SET value=excluded.value",
                (recorded_at, sid, float(s["value"])),
            )
            written += 1

        for e in events:
            value = None if e.get("value") is None else str(e["value"])
            prior = conn.execute(
                "SELECT value FROM climate_events WHERE source=? AND series=? AND key=? "
                "ORDER BY recorded_at DESC, id DESC LIMIT 1",
                (e["source"], e["series"], e["key"]),
            ).fetchone()
            if prior is not None and prior["value"] == value:
                continue
            conn.execute(
                "INSERT INTO climate_events (recorded_at, source, series, key, value) "
                "VALUES (?, ?, ?, ?, ?)",
                (recorded_at, e["source"], e["series"], e["key"], value),
            )
            written += 1
    return written


def roll_up_climate(now: int | None = None) -> int:
    """Fold completed hours of raw samples into `climate_hourly`, then prune.

    Only whole elapsed hours are folded: rolling up the hour currently in
    progress would write a partial aggregate that the next run would have to
    correct, and a min/max that is wrong until the hour ends is worse than one
    that is briefly absent.

    The high-water mark makes steady-state work incremental. We still rebuild a
    small, bounded window immediately behind it so a delayed tick can correct
    an already-written aggregate. On the first run after upgrading from a
    version without the high-water mark, all retained raw data is folded once;
    subsequent runs touch only that late window plus hours not yet processed.
    """
    now = int(time.time()) if now is None else int(now)
    current_hour = now - (now % 3600)
    cutoff = now - RAW_RETENTION_DAYS * 86400
    with get_conn() as conn:
        state = conn.execute(
            "SELECT through_hour FROM climate_rollup_state WHERE singleton=1"
        ).fetchone()
        if state is None:
            # One-time backfill/migration. Starting at the first retained row
            # preserves existing installations that already have a fortnight
            # of raw data when this state table first appears.
            first = conn.execute("SELECT MIN(recorded_at) FROM climate_samples").fetchone()[0]
            roll_from = current_hour if first is None else int(first) - (int(first) % 3600)
        else:
            through_hour = int(state["through_hour"])
            # `through_hour` is exclusive. Revisit only the completed hours in
            # the late-arrival window, then include any backlog since the last
            # successful pass. Clamping also makes an accidentally older `now`
            # harmless instead of moving the durable watermark backwards.
            roll_from = max(
                0,
                min(through_hour, current_hour)
                - CLIMATE_ROLLUP_LATE_HOURS * 3600,
            )

        rollup_sql = """
            INSERT INTO climate_hourly (hour, series_id, min_value, avg_value, max_value, samples)
            SELECT recorded_at - (recorded_at % 3600) AS hour,
                   series_id, MIN(value), AVG(value), MAX(value), COUNT(*)
              FROM climate_samples
             WHERE recorded_at >= ? AND recorded_at < ?
             GROUP BY hour, series_id
            ON CONFLICT(hour, series_id) DO UPDATE SET
                min_value=excluded.min_value, avg_value=excluded.avg_value,
                max_value=excluded.max_value, samples=excluded.samples
            """
        conn.execute(rollup_sql, (roll_from, current_hour))

        # A clock correction, restored database, or pre-watermark installation
        # can leave a raw row behind the incremental window. Never prune such a
        # row before folding it. If that hour already has a durable aggregate,
        # however, the bounded correction window has closed: replacing a full
        # hour with one very-late raw sample would corrupt history, so preserve
        # the existing row. This range is normally empty and uses the
        # recorded_at-leading primary key, so it does not reintroduce the full-
        # retention scan that the watermark removes.
        if cutoff < roll_from:
            conn.execute(
                """
                INSERT INTO climate_hourly
                    (hour, series_id, min_value, avg_value, max_value, samples)
                SELECT recorded_at - (recorded_at % 3600) AS hour,
                       series_id, MIN(value), AVG(value), MAX(value), COUNT(*)
                  FROM climate_samples
                 WHERE recorded_at >= ? AND recorded_at < ?
                 GROUP BY hour, series_id
                ON CONFLICT(hour, series_id) DO NOTHING
                """,
                (0, cutoff),
            )
        conn.execute(
            "INSERT INTO climate_rollup_state (singleton, through_hour) VALUES (1, ?) "
            "ON CONFLICT(singleton) DO UPDATE SET through_hour=MAX(through_hour, excluded.through_hour)",
            (current_hour,),
        )
        # Raw rows go; the rollup above has already absorbed them, and it is
        # kept indefinitely.
        cur = conn.execute("DELETE FROM climate_samples WHERE recorded_at < ?", (cutoff,))
        return cur.rowcount or 0


# ── Climate log: reader ──────────────────────────────────────────────────────

def get_climate_series() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM climate_series ORDER BY source, label, metric")]


def get_climate(start: int, end: int, resolution: str = "auto",
                sources: list[str] | None = None,
                metrics: list[str] | None = None) -> dict:
    """Series over a window. `resolution` is 'raw', 'hourly', or 'auto'.

    'auto' picks hourly for anything longer than two days, because a week of
    raw samples is a quarter of a million points and no chart — and no reader —
    benefits from that.
    """
    if resolution == "auto":
        resolution = "raw" if (end - start) <= 2 * 86400 else "hourly"

    where = ["s.recorded_at >= ?", "s.recorded_at <= ?"] if resolution == "raw" \
        else ["s.hour >= ?", "s.hour <= ?"]
    args: list = [start, end]
    if sources:
        where.append("c.source IN (%s)" % ",".join("?" * len(sources)))
        args += sources
    if metrics:
        where.append("c.metric IN (%s)" % ",".join("?" * len(metrics)))
        args += metrics

    if resolution == "raw":
        sql = ("SELECT s.recorded_at AS at, s.value AS avg_value, s.value AS min_value, "
               "s.value AS max_value, c.id, c.source, c.series, c.metric, c.label "
               "FROM climate_samples s JOIN climate_series c ON c.id = s.series_id "
               "WHERE " + " AND ".join(where) + " ORDER BY s.recorded_at")
    else:
        sql = ("SELECT s.hour AS at, s.avg_value, s.min_value, s.max_value, "
               "c.id, c.source, c.series, c.metric, c.label "
               "FROM climate_hourly s JOIN climate_series c ON c.id = s.series_id "
               "WHERE " + " AND ".join(where) + " ORDER BY s.hour")

    out: dict[int, dict] = {}
    with get_conn() as conn:
        for r in conn.execute(sql, args):
            key = int(r["id"])
            entry = out.setdefault(key, {
                "source": r["source"], "series": r["series"],
                "metric": r["metric"], "label": r["label"], "points": [],
            })
            entry["points"].append({
                "at": int(r["at"]),
                "avg": r["avg_value"],
                "min": r["min_value"],
                "max": r["max_value"],
            })
    return {"resolution": resolution, "start": start, "end": end,
            "series": list(out.values())}


def get_climate_events(start: int, end: int) -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT recorded_at, source, series, key, value FROM climate_events "
            "WHERE recorded_at >= ? AND recorded_at <= ? ORDER BY recorded_at",
            (start, end))]
