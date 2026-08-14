"""The climate log: one aligned clock, canonical units, bounded raw storage.

Each check here stands for a way this could quietly produce a year of data that
cannot answer the question it was built for.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def test_aligned_tick_joins_sources(db) -> None:
    """Instruments sampled in one tick must share a timestamp exactly.

    Comparing the mini-split against an enclosure is the entire purpose. If each
    source is stamped whenever its own poller fired, every comparison needs
    interpolation first, and the log is a pile of unrelated series.
    """
    at = 1786600000
    db.write_climate_tick(at, [
        {"source": "sensor", "series": "AA:BB", "metric": "temp_c", "value": 20.0,
         "label": "Vivarium 1 Warm Side"},
        {"source": "herpstat", "series": "10.0.0.1#1", "metric": "temp_c", "value": 21.0,
         "label": "Rack / Output 1"},
        {"source": "cielo", "series": "DEV", "metric": "temp_c", "value": 18.9,
         "label": "Animal Room"},
    ], [])

    with db.get_conn() as conn:
        stamps = [r[0] for r in conn.execute(
            "SELECT DISTINCT recorded_at FROM climate_samples")]
    assert stamps == [at], f"sources did not share one timestamp: {stamps}"

    got = db.get_climate(at - 60, at + 60, "raw")
    assert {s["source"] for s in got["series"]} == {"sensor", "herpstat", "cielo"}
    assert all(len(s["points"]) == 1 for s in got["series"])
    print("  ✓ one tick, one timestamp across every instrument")


def test_series_are_interned_not_duplicated(db) -> None:
    """Repeated ticks must reuse a series row, or the dimension table grows
    without bound and every query fans out."""
    for i in range(5):
        db.write_climate_tick(1786600100 + i * 60, [
            {"source": "sensor", "series": "AA:BB", "metric": "temp_c",
             "value": 20.0 + i, "label": "Vivarium 1 Warm Side"},
        ], [])
    rows = [s for s in db.get_climate_series() if s["series"] == "AA:BB"]
    assert len(rows) == 1, f"series duplicated {len(rows)} times"
    print("  ✓ series interned once, not per tick")


def test_rename_updates_label_keeps_history(db) -> None:
    """Renaming an enclosure must relabel the series, not orphan its history."""
    db.write_climate_tick(1786600200, [
        {"source": "sensor", "series": "CC:DD", "metric": "temp_c", "value": 20.0,
         "label": "Old Name"}], [])
    db.write_climate_tick(1786600260, [
        {"source": "sensor", "series": "CC:DD", "metric": "temp_c", "value": 21.0,
         "label": "New Name"}], [])

    series = [s for s in db.get_climate_series() if s["series"] == "CC:DD"]
    assert len(series) == 1, "rename created a second series"
    assert series[0]["label"] == "New Name"
    got = db.get_climate(1786600100, 1786600400, "raw")
    points = [p for s in got["series"] if s["series"] == "CC:DD" for p in s["points"]]
    assert len(points) == 2, "history lost across the rename"
    print("  ✓ rename relabels without orphaning history")


def test_events_written_only_on_change(db) -> None:
    """Categorical state changes twice a day. Sampling it every minute would
    store 1,440 identical rows to record that."""
    for i in range(4):
        db.write_climate_tick(1786600300 + i * 60, [], [
            {"source": "cielo", "series": "DEV", "key": "mode", "value": "auto"}])
    db.write_climate_tick(1786600600, [], [
        {"source": "cielo", "series": "DEV", "key": "mode", "value": "heat"}])

    events = db.get_climate_events(1786600000, 1786601000)
    modes = [e["value"] for e in events if e["key"] == "mode"]
    assert modes == ["auto", "heat"], f"expected one row per change, got {modes}"
    print("  ✓ events recorded on change, not on every tick")


def test_rollup_preserves_the_dip(db) -> None:
    """The mean is not enough. An hour averaging 22C that dipped to 19C is the
    difference between a fine night and a cold one, and only min/max shows it."""
    hour = 1786604000 - (1786604000 % 3600)
    for i, value in enumerate([22.0, 19.0, 25.0, 22.0]):
        db.write_climate_tick(hour + i * 60, [
            {"source": "sensor", "series": "EE:FF", "metric": "temp_c",
             "value": value, "label": "Vivarium 2 Warm Side"}], [])

    db.roll_up_climate(now=hour + 7200)
    got = db.get_climate(hour, hour + 3600, "hourly")
    point = got["series"][0]["points"][0]
    assert point["min"] == 19.0, point
    assert point["max"] == 25.0, point
    assert abs(point["avg"] - 22.0) < 0.001, point
    print("  ✓ rollup keeps min and max, not just the mean")


def test_rollup_excludes_the_hour_in_progress(db) -> None:
    """Folding the current hour writes an aggregate that is wrong until the hour
    ends, and a min/max that is briefly absent beats one that is briefly false."""
    hour = 1786608000 - (1786608000 % 3600)
    db.write_climate_tick(hour + 60, [
        {"source": "sensor", "series": "GG:HH", "metric": "temp_c", "value": 20.0,
         "label": "Vivarium 3"}], [])

    db.roll_up_climate(now=hour + 120)   # still inside that hour
    assert not db.get_climate(hour, hour + 3600, "hourly")["series"], \
        "rolled up an hour that had not finished"

    db.roll_up_climate(now=hour + 3700)  # hour has now closed
    assert db.get_climate(hour, hour + 3600, "hourly")["series"], \
        "completed hour was never rolled up"
    print("  ✓ only completed hours are rolled up")


def test_rollup_is_incremental_but_accepts_recent_late_samples(db) -> None:
    """Steady-state maintenance must not rewrite the full raw-retention window.

    Rebuilding a bounded recent window is intentional: it lets a delayed sample
    correct the aggregate for a just-completed hour. An update trigger makes the
    amount of work observable, so this fails if the old 14-day rescan returns.
    """
    first_hour = 1786615200 - (1786615200 % 3600)
    series = "ROLLUP:INCREMENTAL"
    for h in range(12):
        db.write_climate_tick(first_hour + h * 3600 + 60, [
            {"source": "sensor", "series": series, "metric": "temp_c",
             "value": 20.0 + h, "label": "Rollup Probe"}], [])

    current_hour = first_hour + 12 * 3600
    db.roll_up_climate(now=current_hour + 10)
    with db.get_conn() as conn:
        sid = conn.execute(
            "SELECT id FROM climate_series WHERE series=?", (series,)
        ).fetchone()[0]
        conn.execute("CREATE TABLE rollup_updates (hour INTEGER)")
        conn.execute(f"""
            CREATE TRIGGER count_rollup_updates
            AFTER UPDATE ON climate_hourly
            WHEN NEW.series_id = {int(sid)}
            BEGIN
                INSERT INTO rollup_updates VALUES (NEW.hour);
            END
        """)

    # This sample arrived after its hour had already been rolled up, but is
    # inside the two-hour correction window.
    late_hour = current_hour - 3600
    db.write_climate_tick(late_hour + 120, [
        {"source": "sensor", "series": series, "metric": "temp_c",
         "value": 42.0, "label": "Rollup Probe"}], [])
    # The real maintenance loop runs hourly, so advance to the next boundary.
    # The new, not-yet-seen hour is included as backlog while only the two
    # previously aggregated hours are rewritten.
    db.roll_up_climate(now=current_hour + 3600 + 20)

    with db.get_conn() as conn:
        touched = [r[0] for r in conn.execute(
            "SELECT hour FROM rollup_updates ORDER BY hour")]
        conn.execute("DROP TRIGGER count_rollup_updates")
        conn.execute("DROP TABLE rollup_updates")

    expected = [current_hour - 2 * 3600, current_hour - 3600]
    assert touched == expected, \
        f"steady-state rollup should touch {expected}, touched {touched}"

    got = db.get_climate(late_hour, late_hour + 3599, "hourly")
    point = next(s for s in got["series"] if s["series"] == series)["points"][0]
    original = 20.0 + 11
    assert point["min"] == original, point
    assert point["max"] == 42.0, point
    assert abs(point["avg"] - ((original + 42.0) / 2)) < 0.001, point
    print("  ✓ rollup is incremental and incorporates bounded late arrivals")


def test_too_late_sample_cannot_replace_a_durable_hour(db) -> None:
    """Once raw retention has passed, a lone delayed sample is not a full hour.

    The prune-safety pass may discover it, but must not overwrite the complete
    aggregate that survived after the original raw rows were discarded.
    """
    now = int(time.time())
    old_hour = now - (db.RAW_RETENTION_DAYS + 2) * 86400
    old_hour -= old_hour % 3600
    series = "ROLLUP:TOO-LATE"
    db.write_climate_tick(old_hour + 60, [
        {"source": "sensor", "series": series, "metric": "temp_c",
         "value": 99.0, "label": "Too-late Probe"}], [])

    with db.get_conn() as conn:
        sid = conn.execute(
            "SELECT id FROM climate_series WHERE series=?", (series,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO climate_hourly VALUES (?, ?, ?, ?, ?, ?)",
            (old_hour, sid, 18.0, 20.0, 23.0, 60),
        )

    db.roll_up_climate(now=now)
    got = db.get_climate(old_hour, old_hour + 3599, "hourly")
    point = next(s for s in got["series"] if s["series"] == series)["points"][0]
    assert point == {"at": old_hour, "avg": 20.0, "min": 18.0, "max": 23.0}, point
    with db.get_conn() as conn:
        raw = conn.execute(
            "SELECT COUNT(*) FROM climate_samples WHERE recorded_at=?",
            (old_hour + 60,),
        ).fetchone()[0]
    assert raw == 0, "out-of-retention raw row was not pruned"
    print("  ✓ too-late sample cannot replace a durable hourly aggregate")


def test_prune_drops_raw_but_never_rollups(db) -> None:
    """Raw is bounded so the SD card survives; the rollup is the long memory and
    must outlive it."""
    now = int(time.time())
    old = now - (db.RAW_RETENTION_DAYS + 2) * 86400
    old -= old % 3600
    db.write_climate_tick(old + 60, [
        {"source": "sensor", "series": "II:JJ", "metric": "temp_c", "value": 20.0,
         "label": "Vivarium 4"}], [])

    db.roll_up_climate(now=now)

    with db.get_conn() as conn:
        raw = conn.execute("SELECT COUNT(*) FROM climate_samples WHERE recorded_at=?",
                           (old + 60,)).fetchone()[0]
    assert raw == 0, "raw sample older than the retention window survived"
    rolled = db.get_climate(old, old + 3600, "hourly")["series"]
    assert rolled, "pruning raw destroyed the hourly history it had been folded into"
    assert rolled[0]["points"][0]["avg"] == 20.0
    print("  ✓ raw pruned, rollup kept")


def test_auto_resolution_picks_hourly_for_long_windows(db) -> None:
    """A week of raw is a quarter of a million points; nothing benefits."""
    assert db.get_climate(0, 2 * 86400, "auto")["resolution"] == "raw"
    assert db.get_climate(0, 30 * 86400, "auto")["resolution"] == "hourly"
    print("  ✓ auto resolution scales with the window")


def test_outdoor_sensor_is_filed_apart_from_the_room(db) -> None:
    """The porch sensor is the independent variable, not part of the room.

    Same hardware, same packet, same reader — but filed as an ordinary sensor
    it gets swept into any average of the room, and a porch at 35F in January
    drags that average somewhere the room never went. This is the whole reason
    the role exists, so it is worth a check that fails if it stops working.
    """
    import server.app as app

    cfg = {
        "sensors": [
            {"mac": "AA:00:01", "name": "Vivarium 1 Warm Side"},
            {"mac": "AA:00:02", "name": "Porch", "role": "outdoor"},
        ],
        "enclosures": [], "settings": {"temp_unit": "F"},
    }
    # Timestamped now: a reading old enough to be stale is deliberately dropped,
    # which is what test_a_dropped_out_sensor_leaves_a_gap_not_a_flat_line
    # covers. This check is about *where* a live reading is filed.
    fresh = int(time.time())
    with db.get_conn() as conn:
        for mac, temp in (("AA:00:01", 29.0), ("AA:00:02", 1.5)):
            conn.execute("INSERT OR REPLACE INTO readings"
                         " (mac,temp_c,humidity,battery,rssi,updated_at)"
                         " VALUES (?,?,?,?,?,?)", (mac, temp, 50.0, 90, -60, fresh))

    app._thermostats.clear()
    class NoCielo:
        def public_status(self): return {"configured": False}
    original, app.cielo = app.cielo, NoCielo()
    try:
        samples, _ = app._collect_climate(cfg)
    finally:
        app.cielo = original

    by_source = {}
    for s in samples:
        by_source.setdefault(s["source"], set()).add(s["series"])
    assert by_source.get("outdoor") == {"AA:00:02"}, by_source
    assert by_source.get("sensor") == {"AA:00:01"}, by_source
    print("  ✓ an outdoor sensor is filed apart from the room")


def test_a_dropped_out_sensor_leaves_a_gap_not_a_flat_line(db) -> None:
    """A sensor that stops reporting must stop appearing in the log.

    `readings` keeps the last value per sensor forever with no notion of whether
    it is still true. Logged unconditionally, a marginal sensor that drops out
    writes that same value once a minute indefinitely — and a flat line is a
    far worse lie than a gap, because it looks like data.
    """
    import server.app as app

    now = int(time.time())
    cfg = {
        "sensors": [
            {"mac": "BB:00:01", "name": "Live"},
            {"mac": "BB:00:02", "name": "Porch", "role": "outdoor"},
        ],
        "enclosures": [], "settings": {"temp_unit": "F", "stale_after_minutes": 10},
    }
    with db.get_conn() as conn:
        # One reporting now, one last heard from half an hour ago.
        conn.execute("INSERT OR REPLACE INTO readings VALUES (?,?,?,?,?,?)",
                     ("BB:00:01", 22.0, 50.0, 90, -60, now))
        conn.execute("INSERT OR REPLACE INTO readings VALUES (?,?,?,?,?,?)",
                     ("BB:00:02", 29.0, 97.0, 100, -88, now - 1800))

    app._thermostats.clear()
    class NoCielo:
        def public_status(self): return {"configured": False}
    original, app.cielo = app.cielo, NoCielo()
    try:
        samples, _ = app._collect_climate(cfg)
    finally:
        app.cielo = original

    series = {s["series"] for s in samples}
    assert "BB:00:01" in series, "a live sensor was dropped"
    assert "BB:00:02" not in series, "a stale sensor was logged as if it were current"
    print("  ✓ a dropped-out sensor leaves a gap, not a flat line")


def _clear_cielo(db) -> None:
    """Give the mini-split checks a clean slate inside the shared database.

    These two are about how many controller keys exist, and earlier checks in
    this file legitimately create a cielo series of their own. Without this the
    merge correctly refuses as ambiguous and the check fails for a reason that
    has nothing to do with what it is testing.
    """
    with db.get_conn() as conn:
        conn.execute("DELETE FROM climate_samples WHERE series_id IN "
                     "(SELECT id FROM climate_series WHERE source='cielo')")
        conn.execute("DELETE FROM climate_hourly WHERE series_id IN "
                     "(SELECT id FROM climate_series WHERE source='cielo')")
        conn.execute("DELETE FROM climate_events WHERE source='cielo'")
        conn.execute("DELETE FROM climate_series WHERE source='cielo'")


def test_pseudonymous_cielo_key_adopts_its_own_history(db) -> None:
    """Changing the mini-split's series key must not sever its trend.

    The key moved from the constant "cielo" to a per-device pseudonym, which
    fixed a real bug — two controllers would have been spliced into one line.
    But the readings taken before that change are the baseline every setpoint
    comparison rests on, and a line that stops dead on an upgrade date is
    exactly the failure this log exists to avoid.
    """
    _clear_cielo(db)
    for i in range(5):
        db.write_climate_tick(1786700000 + i * 60, [
            {"source": "cielo", "series": "cielo", "metric": "temp_c",
             "value": 21.0 + i, "label": "Animal Room"}],
            [{"source": "cielo", "series": "cielo", "key": "mode", "value": "auto"}])

    db.write_climate_tick(1786700300, [
        {"source": "cielo", "series": "cielo-abc123", "metric": "temp_c",
         "value": 26.0, "label": "Animal Room"}], [])

    keys = [s for s in db.get_climate_series() if s["source"] == "cielo"]
    assert len(keys) == 1, f"history was left split across {len(keys)} keys: {keys}"
    assert keys[0]["series"] == "cielo-abc123"

    got = db.get_climate(1786699000, 1786701000, "raw")
    pts = [p for s in got["series"] if s["source"] == "cielo" for p in s["points"]]
    assert len(pts) == 6, f"expected 5 legacy points plus 1 new, got {len(pts)}"
    assert min(p["avg"] for p in pts) == 21.0, "the oldest reading did not survive"

    events = [e for e in db.get_climate_events(1786699000, 1786701000)
              if e["source"] == "cielo"]
    assert events and all(e["series"] == "cielo-abc123" for e in events), events
    print("  ✓ a new mini-split key adopts the history logged under the old one")


def test_startup_repairs_an_already_split_mini_split(db) -> None:
    """The split can predate the repair, and creation-time adoption misses it.

    If the upgrade ran and wrote even one tick before this existed, both keys
    are already in the table. The new key is never created again, so a hook on
    creation would never fire and the trend would stay severed for good. This
    is the case the live database was actually in.
    """
    _clear_cielo(db)
    db.write_climate_tick(1786720000, [
        {"source": "cielo", "series": "cielo", "metric": "temp_c",
         "value": 20.0, "label": "Animal Room"}], [])
    with db.get_conn() as conn:
        # Both keys present, exactly as an interrupted upgrade leaves them.
        conn.execute("INSERT INTO climate_series (source, series, metric, label) "
                     "VALUES ('cielo', 'cielo-live', 'temp_c', 'Animal Room')")
    db.write_climate_tick(1786720060, [
        {"source": "cielo", "series": "cielo-live", "metric": "temp_c",
         "value": 21.0, "label": "Animal Room"}], [])

    keys = {s["series"] for s in db.get_climate_series()
            if s["source"] == "cielo" and s["metric"] == "temp_c"}
    assert keys == {"cielo", "cielo-live"}, f"expected a split to repair: {keys}"

    with db.get_conn() as conn:
        assert db.merge_legacy_cielo(conn) == 1

    keys = {s["series"] for s in db.get_climate_series()
            if s["source"] == "cielo" and s["metric"] == "temp_c"}
    assert keys == {"cielo-live"}, keys
    got = db.get_climate(1786719000, 1786721000, "raw")
    pts = sorted(p["avg"] for s in got["series"] if s["source"] == "cielo"
                 for p in s["points"])
    assert pts == [20.0, 21.0], f"history did not survive the repair: {pts}"

    # Running it again must do nothing rather than thrash.
    with db.get_conn() as conn:
        assert db.merge_legacy_cielo(conn) == 0
    print("  ✓ startup repairs a split that already happened, and is idempotent")


def test_a_second_controller_cannot_take_the_first_ones_history(db) -> None:
    """Swapping to another mini-split must start a new line, not inherit one.

    Bask tracks one selected controller, so the adoption above happens at the
    moment the first pseudonymous key appears. What has to hold afterwards is
    that a *different* controller showing up later gets its own series and
    leaves the adopted history where it belongs — otherwise the two units'
    behaviour is silently averaged into one trend.
    """
    _clear_cielo(db)
    db.write_climate_tick(1786710000, [
        {"source": "cielo", "series": "cielo", "metric": "humidity",
         "value": 50.0, "label": "Old"}], [])
    db.write_climate_tick(1786710060, [
        {"source": "cielo", "series": "cielo-aaa", "metric": "humidity",
         "value": 51.0, "label": "One"}], [])
    # The legacy rows have now been adopted by the only controller there was.
    db.write_climate_tick(1786710120, [
        {"source": "cielo", "series": "cielo-bbb", "metric": "humidity",
         "value": 52.0, "label": "Two"}], [])

    by_key = {s["series"]: s for s in db.get_climate_series()
              if s["source"] == "cielo" and s["metric"] == "humidity"}
    assert set(by_key) == {"cielo-aaa", "cielo-bbb"}, by_key

    got = db.get_climate(1786709000, 1786711000, "raw")
    points = {s["series"]: sorted(p["avg"] for p in s["points"])
              for s in got["series"] if s["source"] == "cielo"}
    assert points["cielo-aaa"] == [50.0, 51.0], points
    assert points["cielo-bbb"] == [52.0], points
    print("  ✓ a second controller starts its own line and steals nothing")


def test_renaming_a_sensor_keeps_its_outdoor_role() -> None:
    """The frontend's rename posts {name, species} and nothing else.

    If an absent role means "clear it", renaming the porch sensor demotes it to
    a room sensor, and the only symptom is the weather quietly reappearing in
    the room average weeks later.
    """
    from server.app import SensorUpdate

    renamed = SensorUpdate(name="Porch")
    assert "role" not in renamed.model_fields_set, "rename must not carry a role"

    explicit_clear = SensorUpdate(name="Porch", role=None)
    assert "role" in explicit_clear.model_fields_set, "an explicit null must be distinguishable"

    set_outdoor = SensorUpdate(name="Porch", role="outdoor")
    assert set_outdoor.role == "outdoor"
    print("  ✓ a rename cannot silently clear the outdoor role")


def test_early_wake_does_not_swallow_a_tick() -> None:
    """A sleep that returns a hair early must not land in the previous bucket.

    Found in production, not in review: the first deployment logged 14:30,
    14:31, 14:33. Flooring the clock put the 14:32 sample into the 14:31 bucket,
    where it overwrote a real reading, and the following full-interval sleep
    skipped 14:32 entirely. Nothing errored, so nothing said so.
    """
    from server.app import CLIMATE_INTERVAL, _tick_stamp

    boundary = 1786600020 - (1786600020 % CLIMATE_INTERVAL)
    assert _tick_stamp(boundary - 0.01) == boundary, "early wake fell into the previous bucket"
    assert _tick_stamp(boundary + 0.30) == boundary, "late wake left its own bucket"
    assert _tick_stamp(boundary) == boundary
    # Two consecutive boundaries must stay distinct, or a tick is lost to an
    # overwrite rather than recorded.
    assert _tick_stamp(boundary + CLIMATE_INTERVAL - 0.01) == boundary + CLIMATE_INTERVAL
    print("  ✓ a marginally early wake still records its own tick")


def test_units_are_normalised_to_celsius() -> None:
    """The sources genuinely disagree, and a display preference must never be
    able to retroactively reinterpret stored history."""
    from server.app import _to_c, f_to_c

    assert abs(f_to_c(68.0) - 20.0) < 0.001
    assert abs(_to_c(68.0, "F") - 20.0) < 0.001
    assert _to_c(20.0, "C") == 20.0
    assert _to_c(None, "F") is None
    # Unknown unit falls back to Fahrenheit, matching Bask's own default, rather
    # than passing a Fahrenheit number through as if it were Celsius.
    assert abs(_to_c(68.0, None) - 20.0) < 0.001
    print("  ✓ every temperature normalised to Celsius on the way in")


def test_history_query_bounds() -> None:
    """An open LAN read must not create an unbounded SQLite/JSON workload."""
    from fastapi import HTTPException
    from server.app import _climate_filter, climate_history

    assert _climate_filter(" sensor,herpstat,sensor ", "source") == ["sensor", "herpstat"]
    for value in (",".join(f"source-{i}" for i in range(33)), "x" * 65, "x" * 2049):
        try:
            _climate_filter(value, "source")
        except HTTPException as error:
            assert error.status_code == 400
        else:
            raise AssertionError("oversized climate filter was accepted")
    try:
        climate_history(hours=49, resolution="raw")
    except HTTPException as error:
        assert error.status_code == 400 and "48 hours" in str(error.detail)
    else:
        raise AssertionError("oversized raw-history response was accepted")
    print("  ✓ history query work is bounded before SQLite and JSON rendering")


def main() -> None:
    checks = [
        test_aligned_tick_joins_sources,
        test_series_are_interned_not_duplicated,
        test_rename_updates_label_keeps_history,
        test_events_written_only_on_change,
        test_rollup_preserves_the_dip,
        test_rollup_excludes_the_hour_in_progress,
        test_rollup_is_incremental_but_accepts_recent_late_samples,
        test_too_late_sample_cannot_replace_a_durable_hour,
        test_prune_drops_raw_but_never_rollups,
        test_auto_resolution_picks_hourly_for_long_windows,
        test_outdoor_sensor_is_filed_apart_from_the_room,
        test_a_dropped_out_sensor_leaves_a_gap_not_a_flat_line,
        test_pseudonymous_cielo_key_adopts_its_own_history,
        test_startup_repairs_an_already_split_mini_split,
        test_a_second_controller_cannot_take_the_first_ones_history,
    ]
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BASK_DATA_DIR"] = tmp
        (Path(tmp) / "config.json").write_text(
            (ROOT / "config.example.json").read_text(encoding="utf-8"), encoding="utf-8")
        from scanner import db
        db.init_db()
        for check in checks:
            check(db)
        test_early_wake_does_not_swallow_a_tick()
        test_renaming_a_sensor_keeps_its_outdoor_role()
        test_units_are_normalised_to_celsius()
        test_history_query_bounds()
    print(f"Climate log: {len(checks) + 4} checks passed.")


if __name__ == "__main__":
    main()
