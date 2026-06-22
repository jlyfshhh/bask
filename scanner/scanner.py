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
"""
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))  # allow flat imports of db/govee

from bleak import BleakScanner

import db
from govee import GOVEE_COMPANY_ID, decode, is_govee

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scanner")

CONFIG_PATH = Path(__file__).parent.parent / "config.json"

FLUSH_INTERVAL = 5       # seconds between batched SD writes
HISTORY_INTERVAL = 120   # min seconds between history samples per sensor
STALL_TIMEOUT = 90       # restart the scan if no adverts arrive for this long

IS_LINUX = sys.platform.startswith("linux")

# In-memory state. Everything runs in one asyncio event loop (the detection
# callback and the flush loop never run concurrently), so no locking is needed.
_latest: dict[str, dict] = {}        # mac -> {temp_c, humidity, battery, rssi, ts}
_discovered: dict[str, dict] = {}    # mac -> {name, temp_c, humidity, battery, rssi, ts}
_last_history: dict[str, int] = {}   # mac -> ts of last history sample
_last_flushed: dict[str, float] = {} # mac -> advert ts last written to readings
_last_advert = 0.0                   # ts of the most recent advert of any kind


def _load_known() -> dict[str, str]:
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}
    return {s["mac"].upper(): s.get("name", s["mac"]) for s in cfg.get("sensors", [])}


def _on_advert(device, adv) -> None:
    global _last_advert
    name = device.name or getattr(adv, "local_name", None)
    if not is_govee(name, adv.manufacturer_data):
        return
    _last_advert = time.time()
    mac = device.address.upper()
    rssi = adv.rssi if adv.rssi is not None else 0
    decoded = decode(adv.manufacturer_data)
    if decoded:
        temp_c, humidity, battery = decoded
        _latest[mac] = {"temp_c": temp_c, "humidity": humidity, "battery": battery,
                        "rssi": rssi, "ts": _last_advert}
        _discovered[mac] = {"name": name or "Govee", "temp_c": temp_c, "humidity": humidity,
                            "battery": battery, "rssi": rssi, "ts": _last_advert}
    else:
        _discovered.setdefault(mac, {"name": name or "Govee", "temp_c": None, "humidity": None,
                                     "battery": None, "rssi": rssi, "ts": _last_advert})


async def _flush_loop() -> None:
    while True:
        await asyncio.sleep(FLUSH_INTERVAL)
        try:
            known = _load_known()
            # Only flush configured sensors whose reading actually changed.
            current = [
                (mac, r) for mac, r in list(_latest.items())
                if mac in known and r["ts"] > _last_flushed.get(mac, 0)
            ]
            db.flush_readings(current, _last_history, HISTORY_INTERVAL)
            for mac, r in current:
                _last_flushed[mac] = r["ts"]
            db.flush_discovered(list(_discovered.items()), int(time.time()))
            if current:
                preview = ", ".join(f"{known[m]}={r['temp_c']:.1f}C/{r['humidity']:.0f}%"
                                    for m, r in current[:3])
                log.info(f"flushed {len(current)} configured, {len(_discovered)} seen — {preview}")
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
