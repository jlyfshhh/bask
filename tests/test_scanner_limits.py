"""QC-11: what a flood of rotating BLE addresses does to memory and to SQLite.

A BLE address is whatever the transmitter says it is. Phones rotate theirs by
default and anyone in radio range can rotate one as fast as they can transmit,
so "one map entry and one database row per address ever seen" is unbounded
growth driven from outside the house.

These tests push tens of thousands of distinct addresses through the real
detection callback and the real flush, under an injected clock so a scan that
would take hours of radio time costs no wall-clock time and every eviction
boundary lands on an exact second. What they pin down:

  * the retained maps stop growing, and the configured sensors are never the
    thing that gets dropped,
  * the `discovered` table stops growing, and drains once the flood stops —
    the old flush re-inserted the whole retained map, so the table's own age
    prune could never win,
  * one flush writes only what changed, not everything held,
  * TTL and capacity eviction pick the same victims every run.
"""
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scanner"))

GOVEE_CID = 0xEC88

# Placeholder names. The real ones are the household's animals and do not
# belong in a repository.
CONFIGURED = [("A4:C1:38:00:00:01", "Sensor One"), ("A4:C1:38:00:00:02", "Sensor Two")]

FLOOD = 40_000          # distinct rotating addresses per flood
ADVERT_SPACING = 0.01   # simulated seconds between flood adverts (100/s)


class Clock:
    """Stands in for the `time` module that scanner.py and db.py import."""

    def __init__(self, start: float = 1_700_000_000.0):
        self.now = float(start)

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


class Adv:
    def __init__(self, manufacturer_data: dict, rssi: int = -60, local_name=None):
        self.manufacturer_data = manufacturer_data
        self.rssi = rssi
        self.local_name = local_name


class Device:
    def __init__(self, address: str, name: str = "GVH5075_ABCD"):
        self.address = address
        self.name = name


def payload(temp_c: float = 21.5, humidity: float = 44.0, battery: int = 88) -> dict:
    """A well-formed H5075 manufacturer-data blob."""
    raw = int(round(temp_c * 10)) * 1000 + int(round(humidity * 10))
    return {GOVEE_CID: bytes([0x00]) + raw.to_bytes(3, "big") + bytes([battery])}


def rotating(i: int) -> str:
    """A fresh address per call, the way a randomised-MAC transmitter emits them."""
    return "C0:" + ":".join(f"{b:02X}" for b in i.to_bytes(5, "big"))


def setup(tmp: str, sensors=CONFIGURED):
    """A scanner + db pair rooted at `tmp`, driven by an injected clock."""
    Path(tmp, "config.json").write_text(
        json.dumps({"sensors": [{"mac": m, "name": n} for m, n in sensors]}), encoding="utf-8"
    )
    os.environ["BASK_DATA_DIR"] = tmp
    import db as dbmod
    importlib.reload(dbmod)
    import scanner as scmod
    importlib.reload(scmod)
    clock = Clock()
    dbmod.time = clock   # reload restores the real module, so patch after it
    scmod.time = clock
    scmod.log.disabled = True   # a flood logs per flush; CI output stays readable
    dbmod.init_db()
    return scmod, dbmod, clock


def flush(scmod) -> int:
    """One flush pass, returning the number of discovery rows written.

    Falls back to the pre-QC-11 shape — the whole retained map, upserted every
    interval, inline in the async loop — when `flush_once` is missing, so the
    bounds below are measured against the old code instead of erroring out on a
    name that did not exist yet.
    """
    if hasattr(scmod, "flush_once"):
        return scmod.flush_once()
    known = scmod._load_known() or {}
    current = [(m, r) for m, r in list(scmod._latest.items())
               if m in known and r["ts"] > scmod._last_flushed.get(m, 0)]
    scmod.db.flush_readings(current, scmod._last_history, scmod.HISTORY_INTERVAL)
    for m, r in current:
        scmod._last_flushed[m] = r["ts"]
    items = list(scmod._discovered.items())
    scmod.db.flush_discovered(items, int(scmod.time.time()))
    return len(items)


def retained(scmod) -> dict:
    """Every address held in memory, in either the old or the new shape."""
    if hasattr(scmod, "_discovery"):
        return {**scmod._known_seen, **scmod._discovery}
    return dict(scmod._discovered)


def unconfigured(scmod) -> dict:
    macs = {m for m, _ in CONFIGURED}
    return {m: r for m, r in retained(scmod).items() if m not in macs}


def row_count(dbmod, table: str) -> int:
    with dbmod.get_conn() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_a_rotating_address_flood_cannot_grow_memory_or_the_database():
    with tempfile.TemporaryDirectory() as tmp:
        sc, dbm, clock = setup(tmp)
        cap = getattr(sc, "DISCOVERY_MAX", None)
        # With no cap there is no bound at all; measure against a figure no
        # household reaches so the failure still reports the real numbers.
        ceiling = (cap + len(CONFIGURED)) if cap else 1_024

        worst_flush = 0
        flushes = 0
        next_flush = clock.now + sc.FLUSH_INTERVAL
        for i in range(FLOOD):
            sc._on_advert(Device(rotating(i)), Adv(payload()))
            if i % 200 == 0:  # the real sensors keep broadcasting throughout
                for n, (mac, _) in enumerate(CONFIGURED):
                    sc._on_advert(Device(mac), Adv(payload(24.0 + n, 55.0 + n), rssi=-52))
            clock.advance(ADVERT_SPACING)
            if clock.now >= next_flush:
                worst_flush = max(worst_flush, flush(sc))
                flushes += 1
                next_flush = clock.now + sc.FLUSH_INTERVAL

        held = len(retained(sc))
        rows = row_count(dbm, "discovered")
        print(f"  {FLOOD} rotating addresses over {FLOOD * ADVERT_SPACING:.0f} simulated "
              f"seconds, {flushes} flushes")
        print(f"    retained in memory: {held}   discovered rows: {rows}   "
              f"largest single flush: {worst_flush} rows")

        assert held <= ceiling, f"retained {held} addresses (ceiling {ceiling})"
        assert rows <= ceiling, f"discovered table holds {rows} rows (ceiling {ceiling})"
        assert worst_flush <= ceiling, f"one flush wrote {worst_flush} rows (ceiling {ceiling})"

        # Configured sensors are unaffected by any of it.
        assert len(sc._latest) == len(CONFIGURED), f"_latest holds {len(sc._latest)} readings"
        assert row_count(dbm, "readings") == len(CONFIGURED)
        with dbm.get_conn() as conn:
            reading = dict(conn.execute("SELECT * FROM readings WHERE mac=?",
                                        (CONFIGURED[0][0],)).fetchone())
        assert abs(reading["temp_c"] - 24.0) < 0.05, reading
        assert reading["updated_at"] >= int(clock.now) - 10, reading
        # History is throttled per sensor, so it cannot be inflated by the flood.
        assert row_count(dbm, "history") <= len(CONFIGURED) * (
            FLOOD * ADVERT_SPACING / sc.HISTORY_INTERVAL + 2), row_count(dbm, "history")

        # The flood stops. Everything it left behind must age out of memory AND
        # out of the table — the old flush re-upserted the retained map every
        # interval, which kept resetting last_seen and defeated the age prune.
        clock.advance(sc.DISCOVERY_TTL + sc.FLUSH_INTERVAL)
        for mac, _ in CONFIGURED:
            sc._on_advert(Device(mac), Adv(payload(24.0, 55.0), rssi=-52))
        flush(sc)
        drained = row_count(dbm, "discovered")
        print(f"    after the flood stops and {sc.DISCOVERY_TTL}s pass: "
              f"{len(unconfigured(sc))} held, {drained} discovered rows")
        assert not unconfigured(sc), f"{len(unconfigured(sc))} stale addresses still held"
        assert drained == len(CONFIGURED), f"discovered table did not drain: {drained} rows"


def test_configured_sensors_are_never_evicted():
    with tempfile.TemporaryDirectory() as tmp:
        sc, dbm, clock = setup(tmp)
        for n, (mac, _) in enumerate(CONFIGURED):
            sc._on_advert(Device(mac), Adv(payload(26.0 + n, 61.0 + n), rssi=-48))
        flush(sc)

        # Long enough that every unconfigured entry expires several times over.
        for i in range(5_000):
            sc._on_advert(Device(rotating(i)), Adv(payload()))
            clock.advance(0.2)

        for mac, _ in CONFIGURED:
            assert mac in retained(sc), f"{mac} was evicted while quiet"
            assert mac in sc._latest, f"{mac} lost its reading"
        assert len(sc._latest) == len(CONFIGURED), \
            f"_latest holds {len(sc._latest)} readings for {len(CONFIGURED)} sensors"

        # And they resume updating normally the moment they broadcast again.
        sc._on_advert(Device(CONFIGURED[0][0]), Adv(payload(29.5, 70.0), rssi=-44))
        flush(sc)
        with dbm.get_conn() as conn:
            row = dict(conn.execute("SELECT * FROM readings WHERE mac=?",
                                    (CONFIGURED[0][0],)).fetchone())
        assert abs(row["temp_c"] - 29.5) < 0.05, row
        print("  configured sensors survive 5000 rotating addresses and keep updating")


def test_only_the_rows_that_changed_are_written():
    with tempfile.TemporaryDirectory() as tmp:
        sc, dbm, clock = setup(tmp)
        seeded = [rotating(i) for i in range(60)]
        for mac in seeded:
            sc._on_advert(Device(mac), Adv(payload()))
            clock.advance(0.1)
        first = flush(sc)

        clock.advance(1.0)
        for mac in seeded[:3]:
            sc._on_advert(Device(mac), Adv(payload(22.5, 46.0)))
        second = flush(sc)

        held = len(retained(sc))
        print(f"  {held} addresses held; first flush wrote {first} rows, "
              f"second wrote {second}")
        assert first == len(seeded), first
        assert second == 3, f"flush wrote {second} rows for 3 changed devices"
        assert second < held, "the whole retained map is still being rewritten"

        # The untouched rows keep their old last_seen, which is what lets the
        # age prune eventually remove them.
        with dbm.get_conn() as conn:
            stale = conn.execute("SELECT last_seen FROM discovered WHERE mac=?",
                                 (seeded[-1],)).fetchone()[0]
        assert stale < int(clock.now), "an unchanged row had its last_seen refreshed"


def test_ttl_eviction_is_deterministic():
    with tempfile.TemporaryDirectory() as tmp:
        sc, dbm, clock = setup(tmp)
        start = clock.now
        marks = {}
        for label, offset in (("A", 0), ("B", 30), ("C", 60)):
            clock.now = start + offset
            marks[label] = rotating(offset)
            sc._on_advert(Device(marks[label]), Adv(payload()))

        # One second past A's expiry: A goes, B and C stay, exactly.
        clock.now = start + sc.DISCOVERY_TTL + 1
        probe = rotating(9001)
        sc._on_advert(Device(probe), Adv(payload()))
        assert set(unconfigured(sc)) == {marks["B"], marks["C"], probe}, sorted(unconfigured(sc))

        # Thirty-one seconds later B crosses the same line and nothing else does.
        clock.now = start + sc.DISCOVERY_TTL + 31
        probe2 = rotating(9002)
        sc._on_advert(Device(probe2), Adv(payload()))
        assert set(unconfigured(sc)) == {marks["C"], probe, probe2}, sorted(unconfigured(sc))
        print(f"  TTL {sc.DISCOVERY_TTL}s evicts on the exact second, oldest first")


def test_capacity_eviction_drops_the_oldest_first():
    with tempfile.TemporaryDirectory() as tmp:
        sc, dbm, clock = setup(tmp)
        cap = sc.DISCOVERY_MAX
        macs = [rotating(i) for i in range(cap + 25)]
        for mac in macs:
            sc._on_advert(Device(mac), Adv(payload()))
            clock.advance(1.0)   # well inside the TTL, so only the cap can evict

        held = list(unconfigured(sc))
        assert len(held) == cap, f"held {len(held)} with a cap of {cap}"
        assert held == macs[25:], "survivors are not the most recently seen, in order"

        # Re-seeing an address renews it: the next eviction takes the one behind.
        clock.advance(1.0)
        sc._on_advert(Device(macs[25]), Adv(payload()))
        clock.advance(1.0)
        sc._on_advert(Device(rotating(99_999)), Adv(payload()))
        held = unconfigured(sc)
        assert macs[25] in held, "a refreshed address was evicted before an older one"
        assert macs[26] not in held, "eviction did not follow last-seen order"
        assert len(held) == cap
        print(f"  cap {cap} holds the newest addresses and evicts in last-seen order")


def test_a_newly_configured_address_leaves_the_bounded_cache():
    with tempfile.TemporaryDirectory() as tmp:
        sc, dbm, clock = setup(tmp)
        adopted = rotating(4242)
        sc._on_advert(Device(adopted), Adv(payload(23.0, 51.0)))
        assert adopted in sc._discovery
        assert adopted not in sc._latest

        # The keeper adds it through the UI; the scanner picks that up without
        # a restart, and from then on it is exempt from every limit.
        Path(tmp, "config.json").write_text(json.dumps({"sensors": [
            *({"mac": m, "name": n} for m, n in CONFIGURED),
            {"mac": adopted, "name": "Sensor Three"},
        ]}), encoding="utf-8")
        clock.advance(sc.KNOWN_REFRESH + 1)
        sc._on_advert(Device(adopted), Adv(payload(23.5, 52.0)))
        assert adopted in sc._known_seen, "a newly configured sensor stayed evictable"
        assert adopted not in sc._discovery
        assert adopted in sc._latest

        for i in range(sc.DISCOVERY_MAX * 3):
            sc._on_advert(Device(rotating(i)), Adv(payload()))
            clock.advance(0.5)
        assert adopted in sc._known_seen, "the flood evicted a configured sensor"
        print("  a sensor adopted mid-run is promoted out of the discovery cache")


def test_names_and_readings_are_bounded_before_they_reach_sqlite():
    with tempfile.TemporaryDirectory() as tmp:
        sc, dbm, clock = setup(tmp)
        mac = rotating(7)
        sc._on_advert(Device(mac, "GVH" + "A" * 5_000), Adv(payload(), rssi=10 ** 9))
        clock.advance(1.0)
        sc._on_advert(Device(rotating(8), "GVH" + "é" * 900), Adv({}, rssi=-10 ** 9))
        flush(sc)

        entry = retained(sc)[mac]
        assert len(entry["name"]) <= sc.MAX_NAME_LEN, len(entry["name"])
        assert sc.RSSI_FLOOR <= entry["rssi"] <= sc.RSSI_CEIL, entry["rssi"]
        with dbm.get_conn() as conn:
            for row in conn.execute("SELECT name, rssi FROM discovered"):
                assert len(row["name"]) <= sc.MAX_NAME_LEN, len(row["name"])
                assert sc.RSSI_FLOOR <= row["rssi"] <= sc.RSSI_CEIL, row["rssi"]
        print(f"  names clamped to {sc.MAX_NAME_LEN} chars and rssi to "
              f"[{sc.RSSI_FLOOR}, {sc.RSSI_CEIL}] before storage")


def test_counters_report_what_was_dropped():
    with tempfile.TemporaryDirectory() as tmp:
        sc, dbm, clock = setup(tmp)
        for n, (mac, _) in enumerate(CONFIGURED):
            sc._on_advert(Device(mac), Adv(payload(24.0 + n, 55.0 + n)))

        # A handful of devices that go quiet: they leave on the TTL. Under a
        # sustained flood the cap always bites first, so the two counters need
        # separate phases to both move.
        for i in range(5):
            sc._on_advert(Device(rotating(i)), Adv(payload()))
        clock.advance(sc.DISCOVERY_TTL + 1)

        for i in range(100, 100 + sc.DISCOVERY_MAX * 4):
            sc._on_advert(Device(rotating(i)), Adv(payload()))
            clock.advance(0.1)
        flush(sc)

        c = sc.counters()
        assert c["evicted_cap"] > 0, c
        assert c["evicted_ttl"] > 0, c
        assert c["discovery"] == len(sc._discovery) <= sc.DISCOVERY_MAX, c
        assert c["configured"] == len(CONFIGURED), c
        assert c["dirty"] == 0, c
        assert c["evicted_ttl"] == 5, c
        assert c["adverts"] >= sc.DISCOVERY_MAX * 4, c
        print(f"  counters: {c['evicted_cap']} evicted at the cap, "
              f"{c['evicted_ttl']} on TTL, {c['rows_written']} rows written")


def test_a_failed_discovery_flush_is_retried():
    with tempfile.TemporaryDirectory() as tmp:
        sc, dbm, clock = setup(tmp)
        mac = rotating(1234)
        sc._on_advert(Device(mac), Adv(payload()))
        assert mac in sc._dirty

        real_flush = dbm.flush_discovered
        attempts = 0

        def fail_once(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("simulated transient SQLite failure")
            return real_flush(*args, **kwargs)

        dbm.flush_discovered = fail_once
        try:
            try:
                sc.flush_once()
            except OSError:
                pass
            else:
                raise AssertionError("the simulated database failure did not escape")

            assert mac in sc._dirty, "a failed write cleared the only pending copy"
            assert row_count(dbm, "discovered") == 0
            assert sc.flush_once() == 1
            assert mac not in sc._dirty
            assert row_count(dbm, "discovered") == 1
        finally:
            dbm.flush_discovered = real_flush
        print("  a failed discovery write stays dirty and succeeds on retry")


def main() -> None:
    tests = [
        test_a_rotating_address_flood_cannot_grow_memory_or_the_database,
        test_configured_sensors_are_never_evicted,
        test_only_the_rows_that_changed_are_written,
        test_ttl_eviction_is_deterministic,
        test_capacity_eviction_drops_the_oldest_first,
        test_a_newly_configured_address_leaves_the_bounded_cache,
        test_names_and_readings_are_bounded_before_they_reach_sqlite,
        test_counters_report_what_was_dropped,
        test_a_failed_discovery_flush_is_retried,
    ]
    failures = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - report every failure, not just the first
            failures += 1
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
    if failures:
        sys.exit(f"{failures} scanner limit test(s) failed")
    print("Scanner retention and flush-bound tests passed.")


if __name__ == "__main__":
    main()
