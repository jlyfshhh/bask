"""Passive BLE scanner for Govee H5075 sensors — the reliability core.

Why passive scanning: on Linux the BlueZ kernel driver de-duplicates repeated
BLE advertisements, so when a Govee sensor re-broadcasts a changed reading the
repeat is suppressed and the dashboard silently goes stale. Passive scanning
with an `or_pattern` (BlueZ "experimental" feature) delivers EVERY advertisement
instead, which is what makes detection reliable on the Pi. macOS/CoreBluetooth
has no such dedup, so there we just use normal active scanning.

Readings are buffered in memory and flushed to SQLite on an interval to spare
the Pi's SD card. Only ONE scanner runs in the whole system (the web server no
longer scans), so nothing competes for the Bluetooth adapter.

Everything in the air is attacker-controlled, including the address a device
advertises from. Modern phones rotate their MAC by default and anyone in radio
range can rotate one deliberately, so retention is split in two: configured
sensors are held forever, and everything else lives in a bounded, time-limited
discovery cache. Nothing outside those two sets survives, and only rows that
actually changed are written back — otherwise the flush re-inserts every
address it has ever seen and the database's own pruning can never catch up.
"""
import asyncio
import json
import logging
import os
import sys
import time
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))  # allow flat imports of db/govee

from bleak import BleakScanner

import db
from govee import GOVEE_COMPANY_ID, decode, is_govee

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scanner")

ROOT = Path(__file__).parent.parent
DATA_DIR = Path(os.environ.get("BASK_DATA_DIR", ROOT))
CONFIG_PATH = DATA_DIR / "config.json"

FLUSH_INTERVAL = 5       # seconds between batched SD writes
HISTORY_INTERVAL = 120   # min seconds between history samples per sensor
STALL_TIMEOUT = 90       # restart the scan if no adverts arrive for this long

# Retention limits for devices that are NOT in config.json. The TTL matches the
# window db.flush_discovered prunes on, so memory and the table agree on what
# "currently in range" means. The cap is the hard ceiling a flood runs into: a
# real household sees a handful of Govee devices, so 128 is generous.
DISCOVERY_TTL = 300
DISCOVERY_MAX = 128
KNOWN_REFRESH = 30       # seconds between config.json re-reads on the advert path
EVICT_WARN_INTERVAL = 600  # a sustained flood must not become a log flood of its own
MAX_CONFIGURED = 256     # a hand-edited config must not make the maps unbounded either
MAX_NAME_LEN = 64        # advertised names are ~12 chars; the rest is someone playing
MAX_MAC_LEN = 32         # a BlueZ address is 17
RSSI_FLOOR, RSSI_CEIL = -127, 20

IS_LINUX = sys.platform.startswith("linux")

# In-memory state. Everything runs in one asyncio event loop (the detection
# callback and the flush loop never run concurrently), so no locking is needed.
_latest: dict[str, dict] = {}        # configured mac -> {temp_c, humidity, battery, rssi, ts}
_known_seen: dict[str, dict] = {}    # configured mac -> discovery row; never evicted
_discovery: OrderedDict[str, dict] = OrderedDict()  # unconfigured mac -> row, oldest first
_dirty: set[str] = set()             # macs whose discovery row changed since the last flush
_last_history: dict[str, int] = {}   # mac -> ts of last history sample
_last_flushed: dict[str, float] = {} # mac -> advert ts last written to readings
_last_advert = 0.0                   # ts of the most recent advert of any kind

_known: dict[str, str] = {}          # cached config.json sensors: mac -> name
_known_read_at = 0.0
_evict_warned_at = 0.0

_stats = {
    "adverts": 0,          # matching adverts accepted
    "evicted_ttl": 0,      # discovery entries dropped for going quiet
    "evicted_cap": 0,      # discovery entries dropped to stay under DISCOVERY_MAX
    "truncated": 0,        # adverts whose name or rssi had to be clamped
    "rows_written": 0,     # discovery rows actually persisted
    "flushes": 0,
}


def counters() -> dict:
    """Snapshot of scanner health. A climbing `evicted_cap` is the tell that
    something in range is rotating its address faster than the cache can hold."""
    return {**_stats, "configured": len(_known_seen), "discovery": len(_discovery),
            "dirty": len(_dirty)}


def _load_known() -> dict[str, str] | None:
    """config.json sensors as mac -> name, or None if it could not be read.

    None and {} are different answers: an unreadable config must not be taken
    as "no sensors are configured", which would demote every real sensor into
    the evictable cache and lose its readings.
    """
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        sensors = cfg.get("sensors", [])
        if not isinstance(sensors, list):
            return None
    except Exception:
        return None
    if len(sensors) > MAX_CONFIGURED:
        log.warning(f"config.json lists {len(sensors)} sensors; only the first "
                    f"{MAX_CONFIGURED} are tracked")
    known: dict[str, str] = {}
    for s in sensors[:MAX_CONFIGURED]:
        mac = s.get("mac") if isinstance(s, dict) else None
        if not isinstance(mac, str) or not mac:
            continue
        name = s.get("name")
        known[mac.upper()[:MAX_MAC_LEN]] = name if isinstance(name, str) and name else mac
    return known


def _known_macs(now: float) -> dict[str, str]:
    """Cached view of the configured sensors.

    The advert path runs once per packet and cannot stat and parse a file that
    often, but a sensor added through the UI has to start recording without a
    restart — hence a short refresh interval rather than a load per advert.
    """
    global _known, _known_read_at
    if now - _known_read_at >= KNOWN_REFRESH:
        _known_read_at = now
        loaded = _load_known()
        if loaded is not None and loaded != _known:
            _known = loaded
            _reconcile(loaded)
    return _known


def _reconcile(known: dict[str, str]) -> None:
    """Follow a config edit: promote newly configured addresses out of the
    bounded cache, and forget sensors the keeper removed so the configured maps
    stay the size of the config rather than of its whole edit history."""
    for mac in [m for m in _discovery if m in known]:
        _known_seen[mac] = _discovery.pop(mac)
    for state in (_latest, _known_seen, _last_flushed, _last_history):
        for mac in [m for m in state if m not in known]:
            del state[mac]


def _bounded_name(name: str | None) -> str:
    if not isinstance(name, str) or not name:
        return "Govee"
    if len(name) > MAX_NAME_LEN:
        _stats["truncated"] += 1
        return name[:MAX_NAME_LEN]
    return name


def _bounded_rssi(rssi) -> int:
    try:
        value = int(rssi)
    except (TypeError, ValueError):
        return 0
    if not (RSSI_FLOOR <= value <= RSSI_CEIL):
        _stats["truncated"] += 1
        return max(RSSI_FLOOR, min(RSSI_CEIL, value))
    return value


def _expire(now: float) -> None:
    """Drop discovery entries that have gone quiet. The cache is kept in
    last-seen order, so everything expired sits at the front."""
    cutoff = now - DISCOVERY_TTL
    while _discovery:
        mac, entry = next(iter(_discovery.items()))
        if entry["ts"] > cutoff:
            break
        _discovery.popitem(last=False)
        _dirty.discard(mac)
        _stats["evicted_ttl"] += 1


def _admit(mac: str, row: dict, now: float) -> None:
    """Insert or refresh an unconfigured address, evicting oldest-first at the
    cap. Refreshing moves the entry to the back, so eviction order is exactly
    least-recently-seen and does not depend on dict iteration luck."""
    _expire(now)
    if mac in _discovery:
        _discovery[mac] = row
        _discovery.move_to_end(mac)
    else:
        while len(_discovery) >= DISCOVERY_MAX:
            old, _ = _discovery.popitem(last=False)
            _dirty.discard(old)
            _stats["evicted_cap"] += 1
        _discovery[mac] = row
    _dirty.add(mac)


def _on_advert(device, adv) -> None:
    global _last_advert
    name = device.name or getattr(adv, "local_name", None)
    manufacturer_data = getattr(adv, "manufacturer_data", None) or {}
    if not is_govee(name, manufacturer_data):
        return
    now = time.time()
    _last_advert = now
    _stats["adverts"] += 1
    mac = str(device.address).upper()[:MAX_MAC_LEN]
    known = _known_macs(now)
    configured = mac in known
    rssi = _bounded_rssi(getattr(adv, "rssi", None))
    decoded = decode(manufacturer_data)

    if decoded:
        temp_c, humidity, battery = decoded
        row = {"name": _bounded_name(name), "temp_c": temp_c, "humidity": humidity,
               "battery": battery, "rssi": rssi, "ts": now}
    else:
        prev = _known_seen.get(mac) if configured else _discovery.get(mac)
        if prev is not None:
            # A payload-less advert still proves the device is alive; keep the
            # last good reading rather than blanking the entry.
            row = {**prev, "name": _bounded_name(name), "rssi": rssi, "ts": now}
        else:
            row = {"name": _bounded_name(name), "temp_c": None, "humidity": None,
                   "battery": None, "rssi": rssi, "ts": now}

    if configured:
        if decoded:
            _latest[mac] = {k: row[k] for k in ("temp_c", "humidity", "battery", "rssi", "ts")}
        _known_seen[mac] = row
        _dirty.add(mac)
    else:
        _admit(mac, row, now)


def flush_once() -> int:
    """One flush pass; returns the number of discovery rows written.

    Only dirty rows go to SQLite. The retained map is a cache of what is in
    range, and re-upserting all of it every interval is exactly what let a
    flood of rotating addresses outrun the table's own pruning.
    """
    now = time.time()
    known = _known_macs(now)
    _expire(now)

    current = [
        (mac, r) for mac, r in list(_latest.items())
        if mac in known and r["ts"] > _last_flushed.get(mac, 0)
    ]
    db.flush_readings(current, _last_history, HISTORY_INTERVAL)
    for mac, r in current:
        _last_flushed[mac] = r["ts"]

    rows = [(mac, row) for mac, row in _known_seen.items() if mac in _dirty]
    rows += [(mac, row) for mac, row in _discovery.items() if mac in _dirty]
    _dirty.clear()
    db.flush_discovered(rows, int(now), configured=set(known),
                        max_unconfigured=DISCOVERY_MAX)

    _stats["rows_written"] += len(rows)
    _stats["flushes"] += 1
    if current:
        preview = ", ".join(f"{known[m]}={r['temp_c']:.1f}C/{r['humidity']:.0f}%"
                            for m, r in current[:3])
        log.info(f"flushed {len(current)} configured, {len(rows)} rows, "
                 f"{len(_discovery)} in discovery — {preview}")
    global _evict_warned_at
    if _stats["evicted_cap"] and now - _evict_warned_at >= EVICT_WARN_INTERVAL:
        _evict_warned_at = now
        log.warning(f"discovery cache at capacity: {_stats['evicted_cap']} address(es) "
                    f"evicted since start — something nearby is rotating its address")
    return len(rows)


async def _flush_loop() -> None:
    while True:
        await asyncio.sleep(FLUSH_INTERVAL)
        try:
            flush_once()
        except Exception as e:
            log.warning(f"flush error: {e}")


def _passive_kwargs() -> dict | None:
    """Best-effort BlueZ passive-scan kwargs; None if unavailable (→ active scan)."""
    try:
        from bleak.assigned_numbers import AdvertisementDataType
        try:
            from bleak.args.bluez import BlueZScannerArgs, OrPattern
        except ImportError:  # older bleak module layout
            from bleak.backends.bluezdbus.scanner import BlueZScannerArgs
            from bleak.backends.bluezdbus.advertisement_monitor import OrPattern
        cid = GOVEE_COMPANY_ID.to_bytes(2, "little")  # 0xEC88 -> b"\x88\xec"
        return {
            "scanning_mode": "passive",
            "bluez": BlueZScannerArgs(
                or_patterns=[OrPattern(0, AdvertisementDataType.MANUFACTURER_SPECIFIC_DATA, cid)]
            ),
        }
    except Exception as e:
        log.warning(f"passive scanning unavailable ({e}); falling back to active scan")
        return None


async def _scan_forever() -> None:
    global _last_advert
    db.init_db()
    kwargs: dict = {"detection_callback": _on_advert}
    if IS_LINUX:
        passive = _passive_kwargs()
        if passive:
            kwargs.update(passive)
            log.info("Linux/BlueZ passive scanning enabled (advert dedup disabled)")
        else:
            log.info("Linux active scanning (passive unavailable)")
    else:
        log.info(f"{sys.platform}: active scanning")

    while True:
        try:
            scanner = BleakScanner(**kwargs)
            await scanner.start()
            _last_advert = time.time()  # arm the stall timer from scan start
            log.info("scan started")
            while time.time() - _last_advert <= STALL_TIMEOUT:
                await asyncio.sleep(5)
            log.warning(f"no adverts for {STALL_TIMEOUT}s — restarting scan")
            await scanner.stop()
        except Exception as e:
            log.warning(f"scan error: {e}; retrying in 5s")
            await asyncio.sleep(5)


async def main() -> None:
    await asyncio.gather(_scan_forever(), _flush_loop())


if __name__ == "__main__":
    asyncio.run(main())
