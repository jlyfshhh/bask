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
    with db.get_conn() as conn:
        for mac, temp in (("AA:00:01", 29.0), ("AA:00:02", 1.5)):
            conn.execute("INSERT OR REPLACE INTO readings"
                         " (mac,temp_c,humidity,battery,rssi,updated_at)"
                         " VALUES (?,?,?,?,?,?)", (mac, temp, 50.0, 90, -60, 1))

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


def main() -> None:
    checks = [
        test_aligned_tick_joins_sources,
        test_series_are_interned_not_duplicated,
        test_rename_updates_label_keeps_history,
        test_events_written_only_on_change,
        test_rollup_preserves_the_dip,
        test_rollup_excludes_the_hour_in_progress,
        test_prune_drops_raw_but_never_rollups,
        test_auto_resolution_picks_hourly_for_long_windows,
        test_outdoor_sensor_is_filed_apart_from_the_room,
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
        test_units_are_normalised_to_celsius()
    print(f"Climate log: {len(checks) + 2} checks passed.")


if __name__ == "__main__":
    main()
