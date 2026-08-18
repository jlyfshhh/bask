"""Web API + static host for the Bask dashboard.

This process does NO Bluetooth. The standalone scanner writes readings to
SQLite; here we only read them, group sensors into enclosures, evaluate them
against per-species ranges, and serve the touch UI. Discovery ("add a sensor")
reads the scanner's `discovered` table instead of starting its own scan.
"""
import asyncio
import datetime
import json
import logging
import math
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
import ipaddress
import re
from typing import Any, Callable, Literal, TypeVar

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

sys.path.insert(0, str(Path(__file__).parent.parent / "scanner"))
import db  # noqa: E402
from server import keeper
from server.alerts import AlertStateStore
from server.cielo import CieloMonitor
from server.keeper_throttle import KeeperUnlockThrottle, key_fingerprint, source_key
from server.vesync import VeSyncHumidifierMonitor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("web")

ROOT = Path(__file__).parent.parent
DATA_DIR = Path(os.environ.get("BASK_DATA_DIR", ROOT))
CONFIG_PATH = DATA_DIR / "config.json"
FRONTEND_PATH = ROOT / "frontend"
alert_delivery = AlertStateStore(DATA_DIR / "alert-state.json")
keeper_unlock_throttle = KeeperUnlockThrottle()
cielo = CieloMonitor(DATA_DIR / "cielo-secrets.json")
humidifier = VeSyncHumidifierMonitor(
    DATA_DIR / "vesync-secrets.json", DATA_DIR / "vesync-token.json")
SHED_DISPLAY_URL = os.environ.get("SHED_DISPLAY_URL", "").strip()
SHED_DISPLAY_TOKEN = os.environ.get("SHED_DISPLAY_TOKEN", "").strip()
SHED_POLL = 15
_shed_display: dict = {
    "configured": bool(SHED_DISPLAY_URL and SHED_DISPLAY_TOKEN),
    "available": False,
    "data": None,
    "last_success": None,
    "error": None,
}


CONFIG_REVISION_KEY = "_revision"
CONFIG_REVISION_HEADER = "X-Bask-Revision"
CONFIG_REVISION_APPLIED_HEADER = "X-Bask-Revision-Applied"
_config_lock = threading.RLock()
_MutationResult = TypeVar("_MutationResult")


def _normalise_config(cfg: dict) -> dict:
    """Fill schema defaults without writing during a read."""
    revision = cfg.get(CONFIG_REVISION_KEY, 0)  # missing = pre-QC-19 install
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        # Silently resetting a damaged revision to zero could make a very old
        # client token valid again. Fail closed and leave the file untouched.
        raise ValueError("config revision must be a non-negative integer")
    cfg[CONFIG_REVISION_KEY] = revision
    cfg.setdefault("enclosures", [])
    cfg.setdefault("sensors", [])
    cfg.setdefault("species", [])
    cfg.setdefault("settings", {})
    cfg["settings"].setdefault("temp_unit", "F")
    cfg["settings"].setdefault("stale_after_minutes", 10)
    cfg["settings"].setdefault("low_battery_pct", 20)
    cfg["settings"].setdefault("day_start_hour", 8)   # heat on  → day ranges
    cfg["settings"].setdefault("day_end_hour", 20)    # heat off → night ranges
    cfg.setdefault("thermostats", [])                 # optional Herpstat SpyderWeb units
    # Herpstat's RAWSTATUS numbers do not identify their unit.  Older Bask
    # versions implicitly assumed the thermostat matched Bask's display unit;
    # freeze that assumption per thermostat during migration so a later UI
    # display-unit change cannot silently bend the long-term climate history.
    for thermostat in cfg["thermostats"]:
        if isinstance(thermostat, dict):
            thermostat.setdefault("temp_unit", cfg["settings"]["temp_unit"])
    cfg.setdefault("ntfy", {})                        # opt-in phone alerts via ntfy
    cfg["ntfy"].setdefault("server", "https://ntfy.sh")
    cfg["ntfy"].setdefault("topic", "")
    cfg["ntfy"].setdefault("enabled", False)
    cfg.setdefault("keeper", {})                      # Head Keeper key, stored hashed
    return cfg


def _load_config_unlocked() -> dict:
    cfg = _normalise_config(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    # Older installs may have created config.json under a permissive umask.
    # Merely starting/reading Bask hardens that existing credential-bearing
    # file; the next atomic replacement preserves these owner-only bits.
    current_mode = CONFIG_PATH.stat().st_mode & 0o600
    private_mode = current_mode or 0o600
    if CONFIG_PATH.stat().st_mode & 0o777 != private_mode:
        os.chmod(CONFIG_PATH, private_mode)
    return cfg


def load_config() -> dict:
    """Return one consistent config snapshot.

    Every reader takes the same process-wide lock as writers. This matters
    because FastAPI runs sync handlers in a thread pool while background tasks
    read the file at the same time.
    """
    with _config_lock:
        return _load_config_unlocked()


def _write_config_unlocked(cfg: dict) -> None:
    """Durably replace config.json with owner-only permissions.

    The temporary file lives beside the destination so os.replace is atomic.
    Existing owner read/write bits are preserved, while group/world access is
    always stripped because the file can contain authentication material.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        previous_mode = CONFIG_PATH.stat().st_mode & 0o600
    except FileNotFoundError:
        previous_mode = 0
    mode = previous_mode or 0o600
    tmp = CONFIG_PATH.with_name(
        f".{CONFIG_PATH.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
    fd = None
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            json.dump(cfg, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, CONFIG_PATH)
        os.chmod(CONFIG_PATH, mode)
        # Persist the rename as well as the file contents where supported.
        directory_fd = os.open(DATA_DIR, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def mutate_config(expected_revision: int | None,
                  mutation: Callable[[dict], _MutationResult]) -> _MutationResult:
    """Apply a complete read-modify-write transaction under one process lock."""
    with _config_lock:
        cfg = _load_config_unlocked()
        current_revision = cfg[CONFIG_REVISION_KEY]
        if expected_revision is not None and expected_revision != current_revision:
            raise HTTPException(
                409,
                "Bask changed on another device. The latest setup has been reloaded; try again.",
                headers={CONFIG_REVISION_HEADER: str(current_revision)},
            )
        result = mutation(cfg)
        cfg[CONFIG_REVISION_KEY] = current_revision + 1
        _write_config_unlocked(cfg)
        return result


def require_config_revision(request: Request) -> int:
    """Require the revision the editing client actually displayed."""
    raw = request.headers.get(CONFIG_REVISION_HEADER)
    if raw is None:
        raise HTTPException(
            428,
            f"This change requires the current {CONFIG_REVISION_HEADER} header. Reload Bask and try again.",
        )
    try:
        revision = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{CONFIG_REVISION_HEADER} must be a non-negative integer")
    if revision < 0:
        raise HTTPException(400, f"{CONFIG_REVISION_HEADER} must be a non-negative integer")
    # Middleware uses this exact precondition to label a successful transaction
    # with expected+1. Reading the file after the handler would be racy: another
    # device could already have advanced it again.
    request.state.bask_expected_revision = revision
    return revision


ConfigWrite = Depends(require_config_revision)


def c_to_f(c: float) -> float:
    return round(c * 9 / 5 + 32, 1)


def is_warm_position(position: str) -> bool:
    return any(kw in position.lower() for kw in {"warm", "hot", "basking"})


def _check(value, lo, hi) -> bool:
    if value is None:
        return True
    if lo is not None and value < lo:
        return False
    if hi is not None and value > hi:
        return False
    return True


def is_daytime(settings) -> bool:
    """True when the current local hour is inside the day (heat-on) window."""
    start = settings.get("day_start_hour", 8)
    end = settings.get("day_end_hour", 20)
    h = datetime.datetime.now().hour
    return start <= h < end if start <= end else (h >= start or h < end)


NIGHT_KEYS = ("night_warm_temp_min", "night_warm_temp_max", "night_cool_temp_min",
              "night_cool_temp_max", "night_humidity_min", "night_humidity_max")


def species_ranges(sp, is_day):
    """Active (wt_lo, wt_hi, ct_lo, ct_hi, hm_lo, hm_hi) for the time of day.

    At night, use the species' night ranges if it has any set; otherwise fall
    back to the day ranges (so species without night config behave as before).
    """
    day = (sp.get("warm_temp_min"), sp.get("warm_temp_max"),
           sp.get("cool_temp_min"), sp.get("cool_temp_max"),
           sp.get("humidity_min"), sp.get("humidity_max"))
    if is_day:
        return day
    night = tuple(sp.get(k) for k in NIGHT_KEYS)
    return night if any(v is not None for v in night) else day


# ── Herpstat thermostat polling (optional; local LAN, no cloud) ──────────────
# Each Herpstat SpyderWeb unit serves its live state as JSON at /RAWSTATUS. A
# background task polls the configured units and caches the latest reading, so a
# slow or offline unit never blocks a dashboard request.

HERPSTAT_TIMEOUT = 5    # seconds per request
HERPSTAT_POLL = 10      # seconds between poll cycles
_thermostats: dict[str, dict] = {}   # ip -> parsed status


def _fetch_herpstat(ip: str) -> dict:
    # Some SpyderWeb units occasionally return a partially-updated JSON document
    # while their status page is being regenerated. One short retry prevents a
    # healthy thermostat from flashing offline without hiding persistent failures.
    last_error: json.JSONDecodeError | None = None
    for attempt in range(2):
        req = urllib.request.Request(f"http://{ip}/RAWSTATUS", headers={"User-Agent": "bask"})
        with urllib.request.urlopen(req, timeout=HERPSTAT_TIMEOUT) as response:
            raw = response.read().decode()
        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            last_error = error
            if attempt == 0:
                time.sleep(0.15)
    assert last_error is not None
    raise last_error


def _parse_herpstat(ip: str, raw: dict, name_override, temp_unit: str = "F") -> dict:
    sysv = raw.get("system", {})
    safety_ok = "normal" in str(sysv.get("safetyrelay", "")).lower()
    outputs = []
    for i in range(1, int(sysv.get("numberofoutputs", 0)) + 1):
        o = raw.get(f"output{i}")
        if not o:
            continue
        temp = o.get("probereadingTEMP")
        err = o.get("errorcode", 0)
        hi, lo = o.get("highalarm"), o.get("lowalarm")
        temp_alarm = bool(o.get("enablehighlowalarm") and temp is not None
                          and hi is not None and lo is not None and (temp > hi or temp < lo))
        outputs.append({
            "name": o.get("outputnickname") or f"Output {i}",
            "mode": o.get("outputmode"),
            "temp": temp,
            "setpoint": o.get("currentsetting"),
            "output_pct": o.get("poweroutput"),
            "heating": (o.get("poweroutput") or 0) > 0,
            "error": None if err == 0 else o.get("errorcodedescription", "Error"),
            "alarm": err != 0 or not safety_ok or temp_alarm,
        })
    return {
        "ip": ip, "name": name_override or sysv.get("nickname") or ip,
        "temp_unit": temp_unit,
        "safety_ok": safety_ok, "reachable": True,
        "last_seen": int(time.time()), "outputs": outputs,
    }


# A dimming output modulates the mains waveform, so `poweroutput` is whatever
# the poll happened to catch. A single sample can read 0 on a bulb that is
# working perfectly — which is exactly what happened: Pascal showed 0% while
# the thermostat was at 100%, and the obvious reading was "his heat is off".
#
# Smoothing over the last minute keeps the number honest in both directions.
# An output genuinely stuck at zero stays at zero; one that is modulating stops
# flickering between extremes and reads as the duty cycle it actually is.
from .range_window import RangeWindow, HUMIDITY_WINDOW_SECONDS, TEMPERATURE_WINDOW_SECONDS

# Both are judged over a window of time spent out of range rather than on the
# last reading, because a single sample answers "what was it the instant I
# looked" and every source in this room cycles. Humidity gets the longer window:
# it cycles on the period of a fogger or misting, while a temperature swing is
# usually a lamp ramping or a door opening and is over in minutes.
_humidity_window = RangeWindow(HUMIDITY_WINDOW_SECONDS)
_temp_window = RangeWindow(TEMPERATURE_WINDOW_SECONDS)

OUTPUT_SMOOTHING = 6          # polls, at HERPSTAT_POLL seconds each
_output_history: dict[str, list[float]] = {}


def _smooth_outputs(ip: str, parsed: dict) -> dict:
    for index, o in enumerate(parsed.get("outputs", []), start=1):
        raw_pct = o.get("output_pct")
        if raw_pct is None:
            continue
        key = f"{ip}#{index}"
        window = _output_history.setdefault(key, [])
        window.append(float(raw_pct))
        del window[:-OUTPUT_SMOOTHING]
        # Keep the instantaneous value too: the climate log wants the raw
        # sample, and a caller that needs "right now" should not have to
        # un-average it.
        o["output_pct_raw"] = raw_pct
        o["output_pct"] = round(sum(window) / len(window))
        o["heating"] = o["output_pct"] > 0
    return parsed


async def _herpstat_loop():
    while True:
        try:
            for t in load_config().get("thermostats", []):
                ip = t.get("ip")
                if not ip or not t.get("enabled", True):
                    continue
                try:
                    raw = await asyncio.to_thread(_fetch_herpstat, ip)
                    source_unit = t.get("temp_unit", "F")
                    _thermostats[ip] = _smooth_outputs(ip, _parse_herpstat(
                        ip, raw, t.get("name"), source_unit))
                except Exception as e:
                    prev = _thermostats.get(ip, {})
                    _thermostats[ip] = {"ip": ip, "name": t.get("name") or prev.get("name") or ip,
                                        "temp_unit": t.get("temp_unit", prev.get("temp_unit", "F")),
                                        "reachable": False, "outputs": prev.get("outputs", [])}
                    log.warning(f"herpstat {ip} unreachable: {e}")
        except Exception as e:
            log.warning(f"herpstat loop error: {e}")
        await asyncio.sleep(HERPSTAT_POLL)


def _fetch_shed_display() -> dict:
    request = urllib.request.Request(
        SHED_DISPLAY_URL,
        headers={
            "User-Agent": "bask-room-display",
            "X-Shed-Display-Token": SHED_DISPLAY_TOKEN,
        },
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.loads(response.read().decode())


async def _shed_display_loop():
    if not _shed_display["configured"]:
        return
    while True:
        try:
            payload = await asyncio.to_thread(_fetch_shed_display)
            _shed_display.update({
                "available": True,
                "data": payload,
                "last_success": int(time.time()),
                "error": None,
            })
        except Exception as exc:
            _shed_display["available"] = False
            _shed_display["error"] = "Shed is temporarily unavailable"
            log.warning("Shed display feed unavailable: %s", exc)
        await asyncio.sleep(SHED_POLL)


# ── Climate log ──────────────────────────────────────────────────────────────
# Every other poller in this file keeps only the latest value. That is the right
# shape for a dashboard and useless for the question a keeper actually ends up
# asking: what setpoint holds this room overnight in February, and why does the
# morning still take three hours to settle when the lamps ramp in one?
#
# This samples every instrument onto one clock and writes it down.

CLIMATE_INTERVAL = 60      # seconds between ticks
CLIMATE_ROLLUP_EVERY = 3600


def f_to_c(f: float) -> float:
    return (f - 32) * 5 / 9


def _to_c(value, unit: str | None):
    """Normalise a temperature to Celsius given the unit it was reported in."""
    if value is None:
        return None
    return f_to_c(float(value)) if (unit or "F").upper() == "F" else float(value)


def _convert_temp(value, source_unit: str | None, display_unit: str):
    """Convert an instrument value without confusing source and display units."""
    if value is None:
        return None
    celsius = _to_c(value, source_unit)
    return c_to_f(celsius) if display_unit.upper() == "F" else celsius


def _thermostat_for_display(status: dict, display_unit: str) -> dict:
    """Return a display-unit copy while leaving the poller's raw cache intact."""
    source_unit = status.get("temp_unit", display_unit)
    converted = {**status, "temp_unit": display_unit}
    converted["outputs"] = [
        {
            **output,
            "temp": _convert_temp(output.get("temp"), source_unit, display_unit),
            "setpoint": _convert_temp(output.get("setpoint"), source_unit, display_unit),
        }
        for output in status.get("outputs", [])
    ]
    return converted


def _collect_climate(cfg: dict) -> tuple[list[dict], list[dict]]:
    """Snapshot every instrument. Returns (samples, events)."""
    samples: list[dict] = []
    events: list[dict] = []

    # Sensor labels: prefer the enclosure and position, because "Vivarium 1 Warm
    # Side" is what the keeper will look for in a chart a year from now, and the
    # bare sensor nickname may not say which enclosure it ended up in.
    labels: dict[str, str] = {}
    for s in cfg.get("sensors", []):
        mac = (s.get("mac") or "").upper()
        if s.get("name"):
            labels[mac] = s["name"]
    for e in cfg.get("enclosures", []):
        for s in e.get("sensors", []):
            mac = (s.get("mac") or "").upper()
            labels[mac] = f"{e.get('name')} {s.get('position', '')}".strip()

    # Govee sensors. The scanner already stores these in Celsius.
    #
    # A sensor marked `role: outdoor` is filed under its own source. It is the
    # same hardware read the same way — the distinction exists because the
    # outdoor reading is the *independent* variable in every question this log
    # is for. Filed as an ordinary sensor it would be swept into any average of
    # the room, and a porch at 95F in July or 35F in January would drag that
    # average somewhere the room never went.
    roles = {(s.get("mac") or "").upper(): s.get("role") for s in cfg.get("sensors", [])}
    configured = set(roles)
    # `readings` holds the latest value per sensor forever, with no notion of
    # whether it is still true. Logging it unconditionally means a sensor that
    # drops out — a weak one on a porch, a dead battery — writes its last
    # reading once a minute indefinitely, and the trend shows a flat line where
    # the honest answer is a gap. Reuse the keeper's own staleness threshold
    # rather than inventing a second definition of "too old to believe".
    stale_cutoff = int(time.time()) - cfg.get("settings", {}).get("stale_after_minutes", 10) * 60
    for r in db.get_all_readings():
        mac = (r.get("mac") or "").upper()
        if mac not in configured:
            continue
        if (r.get("updated_at") or 0) < stale_cutoff:
            continue
        label = labels.get(mac, mac)
        source = "outdoor" if roles.get(mac) == "outdoor" else "sensor"
        samples.append({"source": source, "series": mac, "metric": "temp_c",
                        "value": r.get("temp_c"), "label": label})
        samples.append({"source": source, "series": mac, "metric": "humidity",
                        "value": r.get("humidity"), "label": label})
        # Same fixed clock the log runs on, so the window is a real duration and
        # not a function of how often someone refreshes the dashboard.
        _now = time.time()
        _humidity_window.record(mac, r.get("humidity"), _now)
        _temp_window.record(mac, r.get("temp_c"), _now)
        # Battery and signal explain the gaps. When a series stops, the next
        # question is always "did it die or did it move out of range", and
        # without these the log cannot answer it — the porch sensor cost an
        # hour of guessing precisely because nothing recorded its signal.
        samples.append({"source": source, "series": mac, "metric": "battery_pct",
                        "value": r.get("battery"), "label": label})
        samples.append({"source": source, "series": mac, "metric": "rssi_dbm",
                        "value": r.get("rssi"), "label": label})

    # Herpstat RAWSTATUS does not carry a unit. Each configured thermostat has
    # an explicit source unit; it is deliberately independent of Bask's display
    # preference so changing the latter cannot corrupt future history.
    for ip, status in _thermostats.items():
        if not status.get("reachable"):
            continue
        source_unit = status.get("temp_unit", "F")
        unit_name = status.get("name") or ip
        for index, o in enumerate(status.get("outputs", []), start=1):
            # Keyed by position, not by nickname: renaming an output in the
            # Herpstat UI must not orphan its history.
            key = f"{ip}#{index}"
            label = f"{unit_name} / {o.get('name') or index}"
            samples.append({"source": "herpstat", "series": key, "metric": "temp_c",
                            "value": _to_c(o.get("temp"), source_unit), "label": label})
            samples.append({"source": "herpstat", "series": key, "metric": "setpoint_c",
                            "value": _to_c(o.get("setpoint"), source_unit), "label": label})
            # The raw sample, not the smoothed one shown on the dashboard. A
            # trend can average for itself; it cannot recover detail that was
            # averaged away before it was stored.
            samples.append({"source": "herpstat", "series": key, "metric": "output_pct",
                            "value": o.get("output_pct_raw", o.get("output_pct")),
                            "label": label})
            # An output going into alarm or error is a discrete moment, and the
            # thing you want when a trend bends is the timestamp it started.
            events.append({"source": "herpstat", "series": key, "key": "alarm",
                           "value": "yes" if o.get("alarm") else "no"})
            events.append({"source": "herpstat", "series": key, "key": "error",
                           "value": o.get("error") or "none"})

    # The mini-split. Its own thermometer is the one the unit acts on, so it is
    # logged as a first-class series rather than trusted as "the room" — the
    # gap between it and the sensors is itself the thing worth measuring.
    # The public Cielo status intentionally omits its cloud device ID. The
    # internal climate status supplies only a stable pseudonymous series key,
    # preventing two selected controllers from being spliced into one line.
    climate_status = getattr(cielo, "climate_status", cielo.public_status)
    c = climate_status()
    if c.get("configured") and not c.get("stale") and c.get("online"):
        key = c.get("series_key") or "cielo"
        label = c.get("name") or "Mini-split"
        cu = c.get("temp_unit", "F")
        samples.append({"source": "cielo", "series": key, "metric": "temp_c",
                        "value": _to_c(c.get("temperature"), cu), "label": label})
        samples.append({"source": "cielo", "series": key, "metric": "humidity",
                        "value": c.get("humidity"), "label": label})
        samples.append({"source": "cielo", "series": key, "metric": "target_c",
                        "value": _to_c(c.get("target"), cu), "label": label})
        for field in ("mode", "power", "fan"):
            events.append({"source": "cielo", "series": key, "key": field,
                           "value": c.get(field)})

    # The humidifier is an actuator, not decoration: it moves the same room the
    # mini-split does, and it was invisible to this log while sitting switched
    # off and out of water. mist_level is its output percentage by another
    # name, and target_humidity is its setpoint — the humidity equivalents of
    # what is already recorded for the thermostats.
    h = humidifier.public_status()
    if h.get("configured") and not h.get("stale") and h.get("online"):
        hkey = "humidifier"
        hlabel = h.get("name") or "Humidifier"
        for metric, value in (("humidity", h.get("humidity")),
                              ("target_humidity", h.get("target_humidity")),
                              ("mist_level", h.get("mist_level"))):
            samples.append({"source": "humidifier", "series": hkey,
                            "metric": metric, "value": value, "label": hlabel})
        for field in ("power", "mode", "water_lacks"):
            events.append({"source": "humidifier", "series": hkey, "key": field,
                           "value": h.get(field)})

    # Which range set was in force. Derived from the clock today, but the
    # schedule is editable: without recording it, changing day_start_hour next
    # month silently rewrites whether last month's nights were ever in range.
    settings = cfg.get("settings", {})
    events.append({"source": "schedule", "series": "room", "key": "phase",
                   "value": "day" if is_daytime(settings) else "night"})
    events.append({"source": "schedule", "series": "room", "key": "day_hours",
                   "value": f"{settings.get('day_start_hour')}-{settings.get('day_end_hour')}"})

    return samples, events


def _tick_stamp(now: float) -> int:
    """The interval boundary this sample belongs to.

    Rounds to the nearest boundary rather than flooring. asyncio.sleep may
    return a hair early, and flooring 14:31:59.99 puts the sample in the 14:31
    bucket — overwriting the tick already there, while the next full-interval
    sleep skips 14:32 altogether. Observed in production as a 60s gap followed
    by a 120s one, with nothing in the log to show for it.
    """
    return int(round(now / CLIMATE_INTERVAL) * CLIMATE_INTERVAL)


_cielo_merge_done = False


def _merge_legacy_cielo_once() -> int:
    """Attempt the legacy mini-split merge, and stop trying once it is settled.

    Returns the number of series merged. Marked done when there is nothing left
    to do, so a database that never had the old key costs one query on the
    first maintenance pass and nothing afterwards.
    """
    global _cielo_merge_done
    with db.get_conn() as conn:
        merged = db.merge_legacy_cielo(conn)
        remaining = conn.execute(
            "SELECT COUNT(*) FROM climate_series WHERE source='cielo' AND series=?",
            (db.LEGACY_CIELO_SERIES,),
        ).fetchone()[0]
    if not remaining:
        _cielo_merge_done = True
    return merged


async def _climate_loop():
    # Not zero: that would make the first tick after every restart also run a
    # rollup, delaying the sample that restart was meant to start producing.
    last_rollup = time.time()
    while True:
        # Align to the interval so every tick lands on a round timestamp and
        # series from different instruments line up exactly. Without this the
        # tick drifts by however long the last write took, and a week later no
        # two sources share a single timestamp.
        now = time.time()
        target = (now // CLIMATE_INTERVAL + 1) * CLIMATE_INTERVAL
        await asyncio.sleep(max(0.0, target - now))
        try:
            recorded_at = _tick_stamp(time.time())
            samples, events = _collect_climate(load_config())
            if samples:
                await asyncio.to_thread(db.write_climate_tick, recorded_at, samples, events)
        except Exception as e:
            log.warning(f"climate sample failed: {e}")
        try:
            if time.time() - last_rollup >= CLIMATE_ROLLUP_EVERY:
                pruned = await asyncio.to_thread(db.roll_up_climate)
                last_rollup = time.time()
                if pruned:
                    log.info(f"climate rollup complete, pruned {pruned} raw samples")
        except Exception as e:
            log.warning(f"climate rollup failed: {e}")
        # One-off data repairs live here rather than at startup. A failing
        # migration must cost a log line and a retry, never the dashboard: run
        # from init_db this same call crash-looped the container, taking
        # monitoring down to fix a chart.
        try:
            if not _cielo_merge_done:
                merged = await asyncio.to_thread(_merge_legacy_cielo_once)
                if merged:
                    log.info(f"merged {merged} legacy mini-split series")
        except Exception as e:
            log.warning(f"legacy mini-split merge deferred: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    poller = asyncio.create_task(_herpstat_loop())
    cielo_poller = asyncio.create_task(cielo.loop())
    humidifier_poller = asyncio.create_task(humidifier.loop())
    notifier = asyncio.create_task(_notify_loop())
    shed_poller = asyncio.create_task(_shed_display_loop())
    climate_logger = asyncio.create_task(_climate_loop())
    yield
    poller.cancel()
    cielo_poller.cancel()
    humidifier_poller.cancel()
    notifier.cancel()
    shed_poller.cancel()
    climate_logger.cancel()


# No CORS middleware on purpose. The dashboard is served from the SAME origin as
# the API, so cross-origin access is neither needed nor wanted. Omitting it means
# the browser's same-origin policy blocks other websites from reading this API,
# and cross-origin JSON writes fail their preflight — important because the API
# is unauthenticated and meant only for a trusted local network.
# Bask is an appliance UI, not an API-development host. The interactive docs
# and schema publish a complete route inventory and are unnecessary here.
app = FastAPI(
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


# The frontend predates a bundler and still has generated inline event-handler
# attributes. `unsafe-inline` remains explicit until those are removed; the
# rest of the policy confines code, connections, media, and framing to Bask.
CONTENT_SECURITY_POLICY = "; ".join((
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    # CSP3 browsers can distinguish external script elements from the legacy
    # inline button handlers. Older browsers safely fall back to script-src.
    "script-src-elem 'self'",
    "script-src-attr 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "media-src 'self'",
    "connect-src 'self'",
    "worker-src 'self'",
    "manifest-src 'self'",
    "frame-src 'none'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
))

SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": (
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
        "magnetometer=(), microphone=(), payment=(), usb=()"
    ),
}


# ── Shared API conventions with Shed ─────────────────────────────────────────
# Shed returns {"error": "..."} and marks every API response no-store. FastAPI
# defaults to {"detail": "..."} and no cache headers, so anything reading both
# apps had to special-case each one. These two hooks bring Bask in line without
# changing any handler: the error body carries `error` *and* the original
# `detail`, so existing callers keep working.

# Registered against Starlette's base class so router-raised errors (404s,
# 405s) get the same shape as the ones handlers raise themselves.
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail, "detail": exc.detail},
        headers={"Cache-Control": "no-store", **(exc.headers or {})},
    )


@app.exception_handler(Exception)
async def unexpected_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Keep unexpected failures client-safe and inside the browser boundary."""
    log.exception("Unhandled request failure on %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "detail": "Internal server error"},
        headers={"Cache-Control": "no-store", **SECURITY_HEADERS},
    )


@app.middleware("http")
async def response_policy(request: Request, call_next):
    """Apply the browser boundary to static files, API data, and errors."""
    response = await call_next(request)
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
        expected = getattr(request.state, "bask_expected_revision", None)
        if expected is not None and 200 <= response.status_code < 300:
            # A successful strict config transaction advances exactly once.
            response.headers[CONFIG_REVISION_HEADER] = str(expected + 1)
            response.headers[CONFIG_REVISION_APPLIED_HEADER] = "true"
    return response


@app.get("/api/config/revision")
def config_revision(response: Response):
    """Tiny bootstrap endpoint for a client that has not made a read yet."""
    revision = load_config()[CONFIG_REVISION_KEY]
    response.headers[CONFIG_REVISION_HEADER] = str(revision)
    return {"revision": revision}


@app.get("/api/manage-snapshot")
def manage_snapshot(response: Response):
    """One coherent, non-secret snapshot for all setup editors."""
    cfg = load_config()
    response.headers[CONFIG_REVISION_HEADER] = str(cfg[CONFIG_REVISION_KEY])
    display_unit = cfg["settings"]["temp_unit"]
    thermostats = [{**item, "status": _thermostat_for_display(
                        _thermostats.get(item.get("ip"), {}), display_unit)}
                   for item in cfg.get("thermostats", [])]
    return {
        "revision": cfg[CONFIG_REVISION_KEY],
        "sensors": cfg["sensors"],
        "enclosures": cfg["enclosures"],
        "species": cfg["species"],
        "settings": cfg["settings"],
        "thermostats": thermostats,
        "temp_unit": cfg["settings"]["temp_unit"],
    }


# ── Head Keeper key ──────────────────────────────────────────────────────────
# Reading Bask stays open to the whole home network — it is a wall display.
# Changing its setup does not. Every mutating route, plus the two reads that
# would reveal the ntfy topic, depends on require_keeper below.
#
# With no key configured Bask is fully open, exactly as it behaved before, so
# updating an existing install never locks anyone out. New installs get a key
# from install.sh instead.

def require_keeper(request: Request) -> None:
    record = load_config().get("keeper")
    state = keeper.keeper_state(record)
    if state == "unconfigured":
        return  # No key set: unchanged, open behaviour.
    if state == "corrupt":
        # Protection was configured and is now unusable. Treating that as "open"
        # would silently drop it; refuse writes and say how to recover.
        raise HTTPException(503, keeper.RECOVERY)
    if keeper.session_is_valid(request.cookies.get(keeper.COOKIE_NAME), record):
        return
    raise HTTPException(401, "Head Keeper key required")


Keeper = Depends(require_keeper)


class KeeperUnlock(BaseModel):
    key: str = Field(min_length=1, max_length=512)


class KeeperSetKey(BaseModel):
    key: str = Field(min_length=1, max_length=512)
    current: str = Field(default="", max_length=512)


@app.get("/api/keeper")
def keeper_status(request: Request):
    """Open on purpose: the UI needs to know whether to show manage controls."""
    record = load_config().get("keeper")
    state = keeper.keeper_state(record)
    if state == "corrupt":
        # Must match require_keeper, or the UI offers controls every write then
        # refuses.
        return {"configured": True, "unlocked": False, "problem": keeper.RECOVERY}
    return {
        "configured": state == "configured",
        # With no key set everyone is effectively the Head Keeper.
        "unlocked": state == "unconfigured"
        or keeper.session_is_valid(request.cookies.get(keeper.COOKIE_NAME), record),
    }


@app.post("/api/keeper/unlock")
def keeper_unlock(payload: KeeperUnlock, request: Request, response: Response):
    record = load_config().get("keeper")
    state = keeper.keeper_state(record)
    if state == "unconfigured":
        return {"ok": True, "configured": False, "unlocked": True}
    if state == "corrupt":
        raise HTTPException(503, keeper.RECOVERY)
    source = source_key(request)
    fingerprint = key_fingerprint(payload.key)
    retry_after = keeper_unlock_throttle.check(source, fingerprint)
    if retry_after:
        raise HTTPException(
            429,
            "Too many unlock attempts. Wait a moment and try again.",
            headers={"Retry-After": str(retry_after)},
        )
    if not keeper.verify_key(payload.key, record):
        retry_after = keeper_unlock_throttle.fail(source, fingerprint)
        if retry_after:
            raise HTTPException(
                429,
                "Too many unlock attempts. Wait a moment and try again.",
                headers={"Retry-After": str(retry_after)},
            )
        raise HTTPException(401, "That key does not match.")
    keeper_unlock_throttle.succeed(source, fingerprint)
    # Older installs have no signing secret yet; mint one on first unlock.
    if not isinstance(record.get("session_secret"), str):
        def migrate_legacy_session(cfg: dict) -> dict:
            current = cfg.get("keeper")
            if not keeper.verify_key(payload.key, current):
                raise HTTPException(401, "That key does not match.")
            keeper.ensure_session_secret(current)
            return current.copy()

        record = mutate_config(None, migrate_legacy_session)
    response.set_cookie(
        keeper.COOKIE_NAME, keeper.issue_session(record), **keeper.cookie_kwargs())
    return {"ok": True, "configured": True, "unlocked": True}


@app.post("/api/keeper/lock")
def keeper_lock(response: Response):
    response.delete_cookie(keeper.COOKIE_NAME, path="/")
    return {"ok": True}


@app.post("/api/keeper/key")
def keeper_set_key(payload: KeeperSetKey, request: Request, response: Response,
                   revision: int = ConfigWrite):
    """
    Set or change the key.

    Changing it requires the current one — otherwise anybody on the network
    could lock the Head Keeper out of their own dashboard. Setting the first
    key needs no proof, because until then there is nothing to prove.
    """
    try:
        new_key = keeper.validate_new_key(payload.key)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    def replace_key(cfg: dict) -> dict:
        record = cfg.get("keeper")
        if keeper.is_configured(record) and not (
            keeper.session_is_valid(request.cookies.get(keeper.COOKIE_NAME), record)
            or keeper.verify_key(payload.current, record)
        ):
            raise HTTPException(401, "Enter the current Head Keeper key to change it.")
        # A fresh record gets a fresh signing secret, so rotating the key ends
        # every existing session as well as replacing the hash.
        cfg["keeper"] = keeper.ensure_session_secret(keeper.hash_key(new_key))
        return cfg["keeper"].copy()

    record = mutate_config(revision, replace_key)
    response.set_cookie(
        keeper.COOKIE_NAME, keeper.issue_session(record), **keeper.cookie_kwargs())
    return {"ok": True, "configured": True, "unlocked": True}


@app.delete("/api/keeper/key")
def keeper_clear_key(request: Request, response: Response, _: None = Keeper,
                     revision: int = ConfigWrite):
    """Remove the lock and go back to open. Requires being unlocked already."""
    def clear_key(cfg: dict) -> None:
        # Re-check authorization against the record inside the same critical
        # section as removal; a concurrent rotation must not be bypassed.
        record = cfg.get("keeper")
        if keeper.is_configured(record) and not keeper.session_is_valid(
                request.cookies.get(keeper.COOKIE_NAME), record):
            raise HTTPException(401, "Head Keeper key required")
        cfg["keeper"] = {}

    mutate_config(revision, clear_key)
    response.delete_cookie(keeper.COOKIE_NAME, path="/")
    return {"ok": True, "configured": False, "unlocked": True}


# ── Service health ───────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    """Small dependency-free probe used by Docker and installers."""
    try:
        db.init_db()
        load_config()
    except Exception as exc:
        log.warning("health check failed: %s", exc)
        raise HTTPException(503, "Bask data is unavailable")
    # `status` matches Shed's probe; `ok` is kept for anything already reading it.
    return {"ok": True, "status": "ok"}


# ── Dashboard ────────────────────────────────────────────────────────────────

def build_sensor_reading(mac, readings_by_mac, sensor_defs, unit, stale_cutoff, now, low_batt):
    mac = mac.upper()
    sdef = sensor_defs.get(mac, {})
    reading = readings_by_mac.get(mac)
    if reading:
        temp_c = reading["temp_c"]
        humidity = reading["humidity"]
        temp = c_to_f(temp_c) if unit == "F" else round(temp_c, 1)
        age = now - reading["updated_at"]
        stale = reading["updated_at"] < stale_cutoff
        battery = reading["battery"]
    else:
        temp = humidity = age = battery = None
        stale = True
    return {
        "mac": mac,
        "name": sdef.get("name", mac),
        "temp": temp,
        "temp_unit": unit,
        "humidity": humidity,
        "battery": battery,
        "low_battery": battery is not None and battery <= low_batt,
        "age_seconds": age,
        "stale": stale,
        "rssi": reading["rssi"] if reading else None,
    }


def analyze_enclosure(enc_cfg, readings_by_mac, sensor_defs, unit, stale_cutoff, now,
                      species_by_id, low_batt, is_day):
    sp = species_by_id.get(enc_cfg.get("species_id"))
    sensors = []
    for slot in enc_cfg.get("sensors", []):
        sr = build_sensor_reading(slot["mac"], readings_by_mac, sensor_defs, unit,
                                  stale_cutoff, now, low_batt)
        sr["position"] = slot.get("position", "")
        sr["is_warm"] = is_warm_position(sr["position"])
        sensors.append(sr)

    warm = next((s for s in sensors if s["is_warm"]), None)
    cool = next((s for s in sensors if not s["is_warm"]), None)

    violations = 0
    warm_temp_ok = cool_temp_ok = humidity_ok = True
    humidity_out_fraction, humidity_samples = 0.0, 0
    warm_out_fraction = cool_out_fraction = 0.0
    has_ranges = False
    if sp:
        wt_lo, wt_hi, ct_lo, ct_hi, hm_lo, hm_hi = species_ranges(sp, is_day)
        has_ranges = any(v is not None for v in [wt_lo, wt_hi, ct_lo, ct_hi, hm_lo, hm_hi])
        _now = time.time()
        if warm and not warm["stale"] and warm["temp"] is not None:
            # The window stores Celsius as the scanner reports it, while the
            # reading here is already in the keeper's display unit. Convert the
            # reading rather than the samples so one unit governs the whole test.
            warm_temp_ok, warm_out_fraction, _n = _temp_window.evaluate(
                (warm.get("mac") or "").upper(), _to_c(warm["temp"], unit),
                _to_c(wt_lo, unit), _to_c(wt_hi, unit), _now)
            if not warm_temp_ok:
                violations += 1
        if cool and not cool["stale"] and cool["temp"] is not None:
            cool_temp_ok, cool_out_fraction, _n = _temp_window.evaluate(
                (cool.get("mac") or "").upper(), _to_c(cool["temp"], unit),
                _to_c(ct_lo, unit), _to_c(ct_hi, unit), _now)
            if not cool_temp_ok:
                violations += 1
        if cool and not cool["stale"] and cool["humidity"] is not None:
            humidity_ok, humidity_out_fraction, humidity_samples = _humidity_window.evaluate(
                (cool.get("mac") or "").upper(), cool["humidity"], hm_lo, hm_hi, time.time())
            if not humidity_ok:
                violations += 1

    any_stale = any(s["stale"] for s in sensors)
    any_data = any(s["temp"] is not None for s in sensors)
    low_battery = any(s["low_battery"] for s in sensors)
    if not any_data:
        status = "no_data"
    elif any_stale:
        status = "stale"
    elif not has_ranges:
        status = "no_ranges"
    elif violations == 0:
        status = "ok"
    elif violations == 1:
        status = "warning"
    else:
        status = "danger"

    ages = [s["age_seconds"] for s in sensors if s["age_seconds"] is not None]
    return {
        "id": enc_cfg["id"], "name": enc_cfg["name"],
        "species_name": sp["name"] if sp else enc_cfg.get("species"),
        "species_id": enc_cfg.get("species_id"), "has_ranges": has_ranges, "status": status,
        "violations": violations, "warm_temp_ok": warm_temp_ok, "cool_temp_ok": cool_temp_ok,
        "humidity_ok": humidity_ok,
        "humidity_out_fraction": round(humidity_out_fraction, 3),
        "warm_out_fraction": round(warm_out_fraction, 3),
        "cool_out_fraction": round(cool_out_fraction, 3),
        "humidity_window_samples": humidity_samples,
        "low_battery": low_battery,
        "age_seconds": max(ages) if ages else None,
        "warm": warm, "cool": cool, "sensors": sensors,
    }


def _build_dashboard(cfg):
    unit = cfg["settings"]["temp_unit"]
    low_batt = cfg["settings"]["low_battery_pct"]
    is_day = is_daytime(cfg["settings"])
    stale_cutoff = int(time.time()) - cfg["settings"]["stale_after_minutes"] * 60
    now = int(time.time())
    readings_by_mac = {r["mac"].upper(): r for r in db.get_all_readings()}
    sensor_defs = {s["mac"].upper(): s for s in cfg["sensors"]}
    species_by_id = {sp["id"]: sp for sp in cfg["species"]}

    grouped = set()
    enclosures_out = []
    for enc in cfg["enclosures"]:
        enclosures_out.append(analyze_enclosure(
            enc, readings_by_mac, sensor_defs, unit, stale_cutoff, now, species_by_id, low_batt, is_day))
        for slot in enc.get("sensors", []):
            grouped.add(slot["mac"].upper())

    def reading_for(sensor):
        return {**build_sensor_reading(sensor["mac"], readings_by_mac, sensor_defs, unit,
                                       stale_cutoff, now, low_batt),
                "species": sensor.get("species")}

    loose = [s for s in cfg["sensors"] if s["mac"].upper() not in grouped]
    # An outdoor sensor is reference, not a habitat. Kept out of `ungrouped` so
    # it stops rendering as a card among the enclosures — buried under sixteen
    # of them it may as well not exist, and it is the one reading that explains
    # what the others are fighting against.
    outdoor = [reading_for(s) for s in loose if s.get("role") == "outdoor"]
    ungrouped = [reading_for(s) for s in loose if s.get("role") != "outdoor"]

    counts = {"ok": 0, "warning": 0, "danger": 0, "stale": 0, "no_data": 0, "no_ranges": 0}
    for e in enclosures_out:
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    thermostats = [_thermostat_for_display(_thermostats[t["ip"]], unit)
                   for t in cfg.get("thermostats", [])
                   if t.get("ip") in _thermostats]
    return {"enclosures": enclosures_out, "ungrouped": ungrouped,
            "outdoor": outdoor,
            "counts": counts, "temp_unit": unit, "updated_at": now,
            "period": "day" if is_day else "night",
            "day_start_hour": cfg["settings"]["day_start_hour"],
            "day_end_hour": cfg["settings"]["day_end_hour"],
            "thermostats": thermostats,
            "room_climate": cielo.public_status(),
            "humidifier": humidifier.public_status()}


_ROOM_ENCLOSURE_STATUSES = {
    "ok", "warning", "danger", "stale", "no_data", "no_ranges",
}
_ROOM_COUNT_KEYS = ("ok", "warning", "danger", "stale", "no_data", "no_ranges")
_ROOM_SHED_TASK_KEYS = (
    "animalName", "species", "taskType", "title", "details", "dueDate",
)


def _room_number(value: Any) -> int | float | None:
    """Return a JSON-safe reading, never a bool, string, NaN, or infinity."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _room_count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _room_text(value: Any, *, limit: int = 500) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _room_reading(reading: Any) -> dict | None:
    """Project a sensor down to the two measurements the room UI renders."""
    if not isinstance(reading, dict):
        return None
    projected = {
        "temp": _room_number(reading.get("temp")),
        "humidity": _room_number(reading.get("humidity")),
    }
    return projected if any(value is not None for value in projected.values()) else None


def _room_enclosure(enclosure: Any) -> dict:
    source = enclosure if isinstance(enclosure, dict) else {}
    status = source.get("status")
    if status not in _ROOM_ENCLOSURE_STATUSES:
        status = "no_data"
    return {
        "name": _room_text(source.get("name"), limit=120) or "Unnamed enclosure",
        "species_name": _room_text(source.get("species_name"), limit=120),
        "status": status,
        "warm_temp_ok": source.get("warm_temp_ok") is True,
        "cool_temp_ok": source.get("cool_temp_ok") is True,
        "humidity_ok": source.get("humidity_ok") is True,
        "warm": _room_reading(source.get("warm")),
        "cool": _room_reading(source.get("cool")),
    }


def _room_error(value: Any) -> bool:
    """The display only needs to know that an integration has an error."""
    return bool(value)


def _room_climate_status(status: Any) -> dict:
    source = status if isinstance(status, dict) else {}
    online = source.get("online")
    return {
        "configured": source.get("configured") is True,
        "online": online if isinstance(online, bool) else None,
        "stale": source.get("stale") is True,
        "error": _room_error(source.get("error")),
        "temperature": _room_number(source.get("temperature")),
        "humidity": _room_number(source.get("humidity")),
    }


def _room_humidifier_status(status: Any) -> dict:
    source = status if isinstance(status, dict) else {}
    online = source.get("online")
    power = source.get("power")
    water_lacks = source.get("water_lacks")
    return {
        "configured": source.get("configured") is True,
        "online": online if isinstance(online, bool) else None,
        "stale": source.get("stale") is True,
        "error": _room_error(source.get("error")),
        "humidity": _room_number(source.get("humidity")),
        "power": power if isinstance(power, bool) else None,
        "mode": _room_text(source.get("mode"), limit=64),
        "water_lacks": (
            water_lacks is True
            or (isinstance(water_lacks, str) and water_lacks.lower() == "on")
        ),
    }


def _room_shed_task(task: Any) -> dict | None:
    if not isinstance(task, dict):
        return None
    required_text = ("animalName", "species", "taskType", "title", "dueDate")
    if any(not isinstance(task.get(key), str) for key in required_text):
        return None
    if "details" not in task or (
            task["details"] is not None and not isinstance(task["details"], str)):
        return None
    return {
        key: _room_text(task[key]) if task[key] is not None else None
        for key in _ROOM_SHED_TASK_KEYS
    }


def _room_shed_data(data: Any) -> dict | None:
    if not isinstance(data, dict) or not isinstance(data.get("summary"), dict):
        return None
    summary = data["summary"]
    count_keys = (
        "total", "completed", "refused", "skipped", "missed", "remaining", "overdue",
    )
    if any(not isinstance(summary.get(key), int) or isinstance(summary.get(key), bool)
           or summary[key] < 0 for key in count_keys):
        return None
    if not isinstance(data.get("tasks"), list) or not isinstance(data.get("overdue"), list):
        return None

    pending = [_room_shed_task(item) for item in data["tasks"]]
    overdue = [_room_shed_task(item) for item in data["overdue"]]
    if any(item is None for item in pending) or any(item is None for item in overdue):
        return None
    # Refusals are completed care, not a fifth mutually-exclusive disposition.
    # Every scheduled task must otherwise be exactly completed, skipped, missed,
    # or remaining; rejecting a broken total prevents Haven from inventing an
    # all-clear when Shed and Bask disagree about the contract.
    if (summary["refused"] > summary["completed"]
            or summary["total"] != (
                summary["completed"] + summary["skipped"]
                + summary["missed"] + summary["remaining"])
            or summary["remaining"] != len(pending)
            or summary["overdue"] != len(overdue)):
        return None

    return {
        "summary": {key: summary[key] for key in count_keys},
        "tasks": pending,
        "overdue": overdue,
    }


def _room_dashboard_dto(bask_dashboard: Any, shed_display: Any,
                        *, generated_at: int | None = None) -> dict:
    """Strict public DTO for Haven's unauthenticated wall-display endpoint.

    This is deliberately an allowlist rather than a copy-and-delete filter.
    The full dashboard contains sensor MACs/names, RSSI, battery and age data,
    thermostat IPs/details, internal IDs, and ungrouped devices. Haven does not
    render any of those fields, so they must not cross this privacy boundary.
    """
    bask = bask_dashboard if isinstance(bask_dashboard, dict) else {}
    shed = shed_display if isinstance(shed_display, dict) else {}
    enclosures = bask.get("enclosures")
    counts = bask.get("counts") if isinstance(bask.get("counts"), dict) else {}
    last_success = shed.get("last_success")
    shed_data = _room_shed_data(shed.get("data"))
    return {
        "generated_at": generated_at if generated_at is not None else int(time.time()),
        "bask": {
            "enclosures": [_room_enclosure(item) for item in enclosures]
            if isinstance(enclosures, list) else [],
            "counts": {key: _room_count(counts.get(key)) for key in _ROOM_COUNT_KEYS},
            "room_climate": _room_climate_status(bask.get("room_climate")),
            "humidifier": _room_humidifier_status(bask.get("humidifier")),
        },
        "shed": {
            "configured": shed.get("configured") is True,
            # A partial/schema-drifted upstream response must never be turned
            # into a false zero-task all-clear on the wall display.
            "available": shed.get("available") is True and shed_data is not None,
            "last_success": _room_number(last_success),
            "data": shed_data,
        },
    }


@app.get("/api/dashboard")
def dashboard():
    return _build_dashboard(load_config())


@app.get("/api/room-dashboard")
def room_dashboard():
    """Minimal combined, read-only feed for the attached room display."""
    return _room_dashboard_dto(_build_dashboard(load_config()), _shed_display.copy())


# ── Discovery (reads the scanner's table; no BLE here) ───────────────────────

@app.get("/api/discovered")
def discovered():
    cfg = load_config()
    known = {s["mac"].upper(): s["name"] for s in cfg["sensors"]}
    unit = cfg["settings"]["temp_unit"]
    out = []
    for d in db.get_discovered(within_seconds=30):
        mac = d["mac"].upper()
        temp = None
        if d["temp_c"] is not None:
            temp = c_to_f(d["temp_c"]) if unit == "F" else round(d["temp_c"], 1)
        out.append({
            "mac": mac, "name": d["name"], "rssi": d["rssi"],
            "temp": temp, "temp_unit": unit, "humidity": d["humidity"], "battery": d["battery"],
            "already_configured": mac in known, "configured_as": known.get(mac),
        })
    return {"devices": out}


# ── Species CRUD ─────────────────────────────────────────────────────────────

_Temp = Field(None, ge=-100, le=300)     # generous bounds, just rejects absurd values
_Humidity = Field(None, ge=0, le=100)


class SpeciesPayload(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    warm_temp_min: float | None = _Temp
    warm_temp_max: float | None = _Temp
    cool_temp_min: float | None = _Temp
    cool_temp_max: float | None = _Temp
    humidity_min: float | None = _Humidity
    humidity_max: float | None = _Humidity
    # Optional night ranges (heat-off window); null → fall back to the day range.
    night_warm_temp_min: float | None = _Temp
    night_warm_temp_max: float | None = _Temp
    night_cool_temp_min: float | None = _Temp
    night_cool_temp_max: float | None = _Temp
    night_humidity_min: float | None = _Humidity
    night_humidity_max: float | None = _Humidity


@app.get("/api/species")
def list_species():
    cfg = load_config()
    return {"species": cfg["species"], "temp_unit": cfg["settings"]["temp_unit"]}


@app.post("/api/species")
def create_species(payload: SpeciesPayload, _: None = Keeper, revision: int = ConfigWrite):
    sp_id = str(uuid.uuid4())
    mutate_config(revision, lambda cfg: cfg["species"].append(
        {"id": sp_id, **payload.model_dump()}))
    return {"ok": True, "id": sp_id}


@app.put("/api/species/{sp_id}")
def update_species(sp_id: str, payload: SpeciesPayload, _: None = Keeper,
                   revision: int = ConfigWrite):
    def update(cfg: dict) -> None:
        for species in cfg["species"]:
            if species["id"] == sp_id:
                species.update(payload.model_dump())
                return
        raise HTTPException(404, "Species not found")

    mutate_config(revision, update)
    return {"ok": True}


@app.delete("/api/species/{sp_id}")
def delete_species(sp_id: str, _: None = Keeper, revision: int = ConfigWrite):
    def delete(cfg: dict) -> None:
        before = len(cfg["species"])
        cfg["species"] = [sp for sp in cfg["species"] if sp["id"] != sp_id]
        if len(cfg["species"]) == before:
            raise HTTPException(404, "Species not found")

    mutate_config(revision, delete)
    return {"ok": True}


# ── Enclosure CRUD ───────────────────────────────────────────────────────────

class EnclosureSensorRef(BaseModel):
    mac: str = Field(min_length=1, max_length=64)
    position: str = Field(max_length=48)


class EnclosurePayload(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    species_id: str | None = Field(None, max_length=64)
    sensors: list[EnclosureSensorRef] = Field(default_factory=list, max_length=16)


class ReorderPayload(BaseModel):
    order: list[str] = Field(max_length=500)

    @field_validator("order")
    @classmethod
    def bounded_ids(cls, order: list[str]) -> list[str]:
        if any(len(enclosure_id) > 64 for enclosure_id in order):
            raise ValueError("enclosure IDs may not exceed 64 characters")
        return order


@app.get("/api/enclosures")
def list_enclosures():
    return {"enclosures": load_config()["enclosures"]}


@app.post("/api/enclosures")
def create_enclosure(payload: EnclosurePayload, _: None = Keeper,
                     revision: int = ConfigWrite):
    enc_id = str(uuid.uuid4())
    mutate_config(revision, lambda cfg: cfg["enclosures"].append({
        "id": enc_id, "name": payload.name, "species_id": payload.species_id,
        "sensors": [{"mac": s.mac.upper(), "position": s.position} for s in payload.sensors],
    }))
    return {"ok": True, "id": enc_id}


@app.put("/api/enclosures/reorder")
def reorder_enclosures(payload: ReorderPayload, _: None = Keeper,
                       revision: int = ConfigWrite):
    def reorder(cfg: dict) -> None:
        current_ids = [enclosure["id"] for enclosure in cfg["enclosures"]]
        requested = payload.order
        if (len(requested) != len(current_ids)
                or len(set(requested)) != len(requested)
                or set(requested) != set(current_ids)):
            raise HTTPException(
                409,
                "Enclosure order must contain every current enclosure exactly once. Reload and try again.",
            )
        by_id = {enclosure["id"]: enclosure for enclosure in cfg["enclosures"]}
        cfg["enclosures"] = [by_id[enclosure_id] for enclosure_id in requested]

    mutate_config(revision, reorder)
    return {"ok": True}


@app.put("/api/enclosures/{enc_id}")
def update_enclosure(enc_id: str, payload: EnclosurePayload, _: None = Keeper,
                     revision: int = ConfigWrite):
    def update(cfg: dict) -> None:
        for enclosure in cfg["enclosures"]:
            if enclosure["id"] == enc_id:
                enclosure["name"] = payload.name
                enclosure["species_id"] = payload.species_id
                enclosure["sensors"] = [
                    {"mac": sensor.mac.upper(), "position": sensor.position}
                    for sensor in payload.sensors
                ]
                return
        raise HTTPException(404, "Enclosure not found")

    mutate_config(revision, update)
    return {"ok": True}


@app.delete("/api/enclosures/{enc_id}")
def delete_enclosure(enc_id: str, _: None = Keeper, revision: int = ConfigWrite):
    def delete(cfg: dict) -> None:
        before = len(cfg["enclosures"])
        cfg["enclosures"] = [e for e in cfg["enclosures"] if e["id"] != enc_id]
        if len(cfg["enclosures"]) == before:
            raise HTTPException(404, "Enclosure not found")

    mutate_config(revision, delete)
    return {"ok": True}


# ── Sensor CRUD ──────────────────────────────────────────────────────────────

class SensorPayload(BaseModel):
    mac: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=64)
    species: str | None = Field(None, max_length=64)


class SensorUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    species: str | None = Field(None, max_length=64)
    # "outdoor" marks a sensor as the reference reading outside the room. It
    # changes nothing about how the sensor is read — it is the same hardware
    # broadcasting the same packet — only how it is filed in the climate log.
    # Without it a porch sensor is indistinguishable from an enclosure sensor,
    # and every "average across the room" quietly averages in the weather.
    role: Literal["outdoor"] | None = None


@app.get("/api/sensors")
def list_sensors():
    cfg = load_config()
    return {"sensors": cfg["sensors"], "settings": cfg["settings"]}


@app.post("/api/sensors")
def add_sensor(payload: SensorPayload, _: None = Keeper, revision: int = ConfigWrite):
    mac = payload.mac.upper()

    def add(cfg: dict) -> None:
        if any(sensor["mac"].upper() == mac for sensor in cfg["sensors"]):
            raise HTTPException(400, "Sensor already configured")
        cfg["sensors"].append({"mac": mac, "name": payload.name, "species": payload.species})

    mutate_config(revision, add)
    return {"ok": True}


@app.put("/api/sensors/{mac}")
def update_sensor(mac: str, payload: SensorUpdate, _: None = Keeper,
                  revision: int = ConfigWrite):
    def update(cfg: dict) -> None:
        for sensor in cfg["sensors"]:
            if sensor["mac"].upper() == mac.upper():
                sensor["name"] = payload.name
                sensor["species"] = payload.species
                # Only touch the role when the caller actually sent the field.
                # The rename dialog in the frontend posts {name, species} and
                # nothing else, so treating "absent" as "clear it" would quietly
                # demote the outdoor sensor back to a room sensor every time it
                # was renamed — and the only symptom would be the weather
                # reappearing in the room average weeks later.
                if "role" in payload.model_fields_set:
                    if payload.role:
                        sensor["role"] = payload.role
                    else:
                        sensor.pop("role", None)
                return
        raise HTTPException(404, "Sensor not found")

    mutate_config(revision, update)
    return {"ok": True}


@app.delete("/api/sensors/{mac}")
def delete_sensor(mac: str, _: None = Keeper, revision: int = ConfigWrite):
    target = mac.upper()

    def delete(cfg: dict) -> None:
        before = len(cfg["sensors"])
        cfg["sensors"] = [sensor for sensor in cfg["sensors"]
                          if sensor["mac"].upper() != target]
        if len(cfg["sensors"]) == before:
            raise HTTPException(404, "Sensor not found")
        # Also unlink it from any enclosure slot it was assigned to.
        for enclosure in cfg["enclosures"]:
            enclosure["sensors"] = [slot for slot in enclosure.get("sensors", [])
                                    if slot["mac"].upper() != target]

    mutate_config(revision, delete)
    return {"ok": True}


# ── Pairing (proximity-based sensor → enclosure assignment) ──────────────────

class PairPayload(BaseModel):
    mac: str = Field(min_length=1, max_length=64)
    enclosure_id: str = Field(max_length=64)
    position: str = Field(max_length=48)
    name: str | None = Field(None, max_length=64)


@app.post("/api/pair")
def pair_sensor(payload: PairPayload, _: None = Keeper, revision: int = ConfigWrite):
    """Assign a discovered sensor to an enclosure slot in one atomic step.

    Used by the touch "Pair by proximity" wizard: the user holds a sensor near
    the Pi (strongest RSSI) and taps a Warm/Cool target. We (1) ensure a sensor
    record exists, (2) detach the mac from any other enclosure, and (3) put it in
    the chosen position slot, replacing whatever held that position before.
    """
    mac = payload.mac.upper()
    pos = payload.position.strip()
    if not pos:
        raise HTTPException(400, "position is required")

    def pair(cfg: dict) -> dict:
        enclosure = next((item for item in cfg["enclosures"]
                          if item["id"] == payload.enclosure_id), None)
        if enclosure is None:
            raise HTTPException(404, "Enclosure not found")

        name = (payload.name or f"{enclosure['name']} {pos}").strip()
        existing = next((sensor for sensor in cfg["sensors"]
                         if sensor["mac"].upper() == mac), None)
        if existing:
            existing["name"] = name
        else:
            cfg["sensors"].append({"mac": mac, "name": name, "species": None})

        # A sensor lives in exactly one place — detach it everywhere first.
        for item in cfg["enclosures"]:
            item["sensors"] = [slot for slot in item.get("sensors", [])
                               if slot["mac"].upper() != mac]
        enclosure["sensors"] = [slot for slot in enclosure.get("sensors", [])
                                if slot.get("position", "").lower() != pos.lower()]
        enclosure["sensors"].append({"mac": mac, "position": pos})
        return {"ok": True, "sensor_name": name,
                "enclosure": enclosure["name"], "position": pos}

    return mutate_config(revision, pair)


@app.post("/api/unpair")
def unpair_sensor(payload: PairPayload, _: None = Keeper, revision: int = ConfigWrite):
    """Remove a sensor from a given enclosure slot (undo a mis-tap in the wizard)."""
    mac = payload.mac.upper()

    def unpair(cfg: dict) -> None:
        enclosure = next((item for item in cfg["enclosures"]
                          if item["id"] == payload.enclosure_id), None)
        if enclosure is None:
            raise HTTPException(404, "Enclosure not found")
        enclosure["sensors"] = [slot for slot in enclosure.get("sensors", [])
                                if slot["mac"].upper() != mac]

    mutate_config(revision, unpair)
    # A slot re-paired to a different animal must not inherit the previous
    # occupant's humidity history, which would judge it against a window it was
    # never in.
    _humidity_window.forget(mac)
    _temp_window.forget(mac)
    return {"ok": True}


# ── Settings ─────────────────────────────────────────────────────────────────

class SettingsPayload(BaseModel):
    temp_unit: Literal["F", "C"] | None = None
    stale_after_minutes: int | None = Field(None, ge=1, le=1440)
    low_battery_pct: int | None = Field(None, ge=0, le=100)
    day_start_hour: int | None = Field(None, ge=0, le=23)
    day_end_hour: int | None = Field(None, ge=0, le=23)


@app.put("/api/settings")
def update_settings(payload: SettingsPayload, _: None = Keeper,
                    revision: int = ConfigWrite):
    values = payload.model_dump(exclude_none=True)

    def update(cfg: dict) -> dict:
        cfg["settings"].update(values)
        return cfg["settings"].copy()

    settings = mutate_config(revision, update)
    return {"ok": True, "settings": settings}


# ── Herpstat thermostat CRUD (optional feature) ──────────────────────────────
# Units are keyed by their LAN IP. The background poller (_herpstat_loop) reads
# this list each cycle, so adds/edits take effect within one poll interval with
# no restart. The dashboard strip stays hidden until at least one unit is added.

def validated_lan_host(value: str) -> str:
    """
    Accept a plain LAN host or IP and nothing else.

    Bask connects to whatever this names, so a length-capped free string is the
    wrong shape: it admits credentials, ports, paths, and cloud metadata
    addresses. Anything that is not a bare hostname or address is refused.
    """
    host = (value or "").strip()
    if not host or len(host) > 64:
        raise ValueError("Enter the device's address on your network.")
    if any(character in host for character in "@/\\?#:[] \t"):
        raise ValueError("Use just the address, with no scheme, port, path, or sign-in details.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # A hostname: letters, digits, hyphens, and dots only.
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}", host):
            raise ValueError("That does not look like a device address.")
        return host
    if address.is_loopback or address.is_multicast or address.is_reserved or address.is_unspecified:
        raise ValueError("That address cannot be a device on your network.")
    # 169.254.169.254 and friends are how a compromised import would reach a
    # cloud metadata service. Bask talks to home networks only.
    if address.is_link_local:
        raise ValueError("Link-local addresses are not accepted.")
    if not address.is_private:
        raise ValueError("Bask only connects to devices on your own network.")
    return host


class ThermostatPayload(BaseModel):
    ip: str = Field(min_length=1, max_length=64)

    @field_validator("ip")
    @classmethod
    def _check_ip(cls, value: str) -> str:
        return validated_lan_host(value)

    name: str | None = Field(None, max_length=64)
    enabled: bool = True
    # The unit configured on the Herpstat itself, not Bask's display preference.
    temp_unit: Literal["F", "C"] | None = None


class ThermostatTest(BaseModel):
    ip: str = Field(min_length=1, max_length=64)


@app.get("/api/thermostats")
def list_thermostats():
    cfg = load_config()
    display_unit = cfg["settings"]["temp_unit"]
    out = [{**t, "status": _thermostat_for_display(
                _thermostats.get(t.get("ip"), {}), display_unit)}
           for t in cfg.get("thermostats", [])]
    return {"thermostats": out, "temp_unit": cfg["settings"]["temp_unit"]}


@app.get("/api/climate/series")
def climate_series():
    """Every series the climate log has ever seen, for building a query."""
    return {"series": db.get_climate_series(), "temp_unit": load_config()["settings"]["temp_unit"]}


def _climate_filter(value: str | None, label: str) -> list[str] | None:
    """Bound an open-LAN query before it becomes a SQLite placeholder list."""
    if not value:
        return None
    if len(value) > 2_048:
        raise HTTPException(400, f"{label} filter is too long")
    values = list(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))
    if len(values) > 32 or any(len(part) > 64 for part in values):
        raise HTTPException(400, f"{label} filter has too many or oversized values")
    return values or None


@app.get("/api/climate")
def climate_history(hours: int = 24, resolution: str = "auto",
                    source: str | None = None, metric: str | None = None):
    """Cross-instrument history over the last `hours`.

    Temperatures come back in Celsius, as stored. Converting here would mean
    the same endpoint returned different numbers depending on a display setting,
    which is a poor property for something whose whole job is comparison over
    time; `temp_unit` is reported so a caller can format.
    """
    hours = max(1, min(int(hours), 24 * 400))
    if resolution not in ("auto", "raw", "hourly"):
        raise HTTPException(400, "resolution must be auto, raw or hourly")
    # Raw is one row per minute per metric. Long-range queries should use the
    # hourly rollup; allowing an explicit 400-day raw request would make a LAN
    # client ask the server to build a needlessly huge response even though raw
    # data is retained for only a fortnight.
    if resolution == "raw" and hours > 48:
        raise HTTPException(400, "raw resolution is limited to 48 hours; use hourly or auto")
    end = int(time.time())
    start = end - hours * 3600
    data = db.get_climate(
        start, end, resolution,
        sources=_climate_filter(source, "source"),
        metrics=_climate_filter(metric, "metric"),
    )
    data["temp_unit"] = load_config()["settings"]["temp_unit"]
    return data


@app.get("/api/climate/events")
def climate_events(hours: int = 24):
    """Mode, power and fan changes — the things that explain a step in a chart."""
    hours = max(1, min(int(hours), 24 * 400))
    end = int(time.time())
    return {"events": db.get_climate_events(end - hours * 3600, end)}


@app.post("/api/thermostats/test")
def test_thermostat(payload: ThermostatTest, _: None = Keeper):
    """Probe an IP for a Herpstat /RAWSTATUS page before saving it.

    Sync handler → FastAPI runs it in a threadpool, so the (up to 5s) blocking
    fetch never stalls the event loop. Lets the Manage UI tell the user up front
    whether the unit's status page is enabled and reachable.
    """
    ip = payload.ip.strip()
    try:
        parsed = _parse_herpstat(ip, _fetch_herpstat(ip), None)
    except Exception as e:
        return {"ok": False, "error": f"Could not reach {ip} — is the status page enabled? ({e})"}
    return {"ok": True, "name": parsed["name"],
            "outputs": [o["name"] for o in parsed["outputs"]]}


@app.post("/api/thermostats")
def add_thermostat(payload: ThermostatPayload, _: None = Keeper,
                   revision: int = ConfigWrite):
    ip = payload.ip.strip()

    def add(cfg: dict) -> None:
        if any(thermostat.get("ip") == ip for thermostat in cfg["thermostats"]):
            raise HTTPException(400, "Thermostat already added")
        cfg["thermostats"].append(
            {"ip": ip, "name": payload.name, "enabled": payload.enabled,
             "temp_unit": payload.temp_unit or cfg["settings"]["temp_unit"]})

    mutate_config(revision, add)
    return {"ok": True}


@app.put("/api/thermostats/{ip}")
def update_thermostat(ip: str, payload: ThermostatPayload, _: None = Keeper,
                      revision: int = ConfigWrite):
    new_ip = payload.ip.strip()

    def update(cfg: dict) -> None:
        for thermostat in cfg["thermostats"]:
            if thermostat.get("ip") == ip:
                thermostat["ip"] = new_ip
                thermostat["name"] = payload.name
                thermostat["enabled"] = payload.enabled
                if payload.temp_unit is not None:
                    thermostat["temp_unit"] = payload.temp_unit
                return
        raise HTTPException(404, "Thermostat not found")

    mutate_config(revision, update)
    if new_ip != ip:
        _thermostats.pop(ip, None)   # drop stale cache under the old IP
    return {"ok": True}


@app.delete("/api/thermostats/{ip}")
def delete_thermostat(ip: str, _: None = Keeper, revision: int = ConfigWrite):
    def delete(cfg: dict) -> None:
        before = len(cfg["thermostats"])
        cfg["thermostats"] = [thermostat for thermostat in cfg["thermostats"]
                              if thermostat.get("ip") != ip]
        if len(cfg["thermostats"]) == before:
            raise HTTPException(404, "Thermostat not found")

    mutate_config(revision, delete)
    _thermostats.pop(ip, None)   # so it disappears from the dashboard immediately
    return {"ok": True}


# ── Cielo Breez room climate (optional, cloud-polled, read-only) ─────────────

class CieloConnectPayload(BaseModel):
    api_key: str = Field(min_length=1, max_length=512)


class CieloDevicePayload(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)


@app.get("/api/cielo")
def cielo_status():
    return cielo.settings_status()


@app.post("/api/cielo/connect")
async def connect_cielo(payload: CieloConnectPayload, _: None = Keeper):
    try:
        return await cielo.configure(payload.api_key)
    except AuthenticationError:
        raise HTTPException(400, "Cielo rejected that API key.")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception:
        log.exception("Cielo setup failed")
        raise HTTPException(503, "Cielo could not be reached. Try again later.")


@app.put("/api/cielo/device")
async def select_cielo_device(payload: CieloDevicePayload, _: None = Keeper):
    try:
        return await cielo.select_device(payload.device_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception:
        log.exception("Cielo device selection failed")
        raise HTTPException(503, "Cielo could not be reached. Try again later.")


@app.delete("/api/cielo")
async def disconnect_cielo(_: None = Keeper):
    await cielo.clear()
    return {"ok": True}


# ── Levoit/VeSync room humidifier (optional, cloud-polled, read-only) ───────

class VeSyncConnectPayload(BaseModel):
    username: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=512)
    country_code: str = Field(default="US", min_length=2, max_length=2)


class VeSyncDevicePayload(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)


@app.get("/api/vesync")
def vesync_status():
    return humidifier.settings_status()


@app.post("/api/vesync/connect")
async def connect_vesync(payload: VeSyncConnectPayload, _: None = Keeper):
    try:
        return await humidifier.configure(
            payload.username, payload.password, payload.country_code)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception:
        log.exception("VeSync setup failed")
        raise HTTPException(400, "VeSync rejected that login or could not be reached.")


@app.put("/api/vesync/device")
async def select_vesync_device(payload: VeSyncDevicePayload, _: None = Keeper):
    try:
        return await humidifier.select_device(payload.device_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception:
        log.exception("VeSync humidifier selection failed")
        raise HTTPException(503, "VeSync could not be reached. Try again later.")


@app.delete("/api/vesync")
async def disconnect_vesync(_: None = Keeper):
    await humidifier.clear()
    return {"ok": True}


# ── Phone alerts via ntfy (optional, opt-in) ────────────────────────────────
# The Pi POSTs a notification to an ntfy server (ntfy.sh by default) on its own
# random, unguessable topic; the user subscribes to that topic in the free ntfy
# app. This works over a plain-HTTP LAN because the Pi only makes an OUTBOUND
# request — nothing about the Pi is exposed. Fully opt-in.

try:
    import segno  # optional: renders the subscribe QR code
    _QR_OK = True
except Exception:  # pragma: no cover - optional dependency
    _QR_OK = False


def _ntfy_topic(cfg: dict) -> str:
    topic = cfg["ntfy"].get("topic")
    if not isinstance(topic, str) or not topic:
        raise RuntimeError("ntfy topic has not been configured")
    return topic


def _subscribe_url(cfg) -> str:
    server = cfg["ntfy"].get("server", "https://ntfy.sh").rstrip("/")
    return f"{server}/{_ntfy_topic(cfg)}"


def _ntfy_publish(cfg, title: str, body: str, tags: str = "", priority: str = "") -> None:
    headers = {"Title": title}          # ASCII only — emoji is sent via Tags
    if tags:
        headers["Tags"] = tags
    if priority:
        headers["Priority"] = priority
    req = urllib.request.Request(_subscribe_url(cfg), data=body.encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=8) as r:
        r.read()


# ── Alert loop: notify on transitions into (and back out of) a problem state ──
NOTIFY_POLL = 60
MAX_DELIVERIES_PER_CYCLE = 5


def _alert_text(e: dict) -> str:
    if e["status"] == "stale":
        return f"{e['name']}: no sensor signal"
    issues = []
    if e.get("warm_temp_ok") is False:
        issues.append("warm temp")
    if e.get("cool_temp_ok") is False:
        issues.append("cool temp")
    if e.get("humidity_ok") is False:
        issues.append("humidity")
    return f"{e['name']}: " + (" + ".join(issues) or "out of range")


def _alert_observations(enclosures: list[dict]) -> list[dict]:
    """Project dashboard data down to the durable delivery state machine."""
    return [{
        "id": e["id"],
        "status": e["status"],
        "alert_title": "Bask alert",
        "alert_body": _alert_text(e),
        "alert_tags": "warning",
        "alert_priority": "high",
        "recovery_title": "Bask",
        "recovery_body": f"{e['name']} is back to normal",
        "recovery_tags": "white_check_mark",
        "recovery_priority": "",
    } for e in enclosures]


async def _notify_loop():
    """Persist stable transitions before publishing and retry until confirmed.

    The first enabled pass only seeds a durable baseline. A failed send remains
    in the owner-only outbox across retries and process restarts.
    """
    while True:
        try:
            cfg = load_config()
            enabled = bool(cfg["ntfy"].get("enabled") and cfg["ntfy"].get("topic"))
            enclosures = _build_dashboard(cfg)["enclosures"] if enabled else []
            now = time.time()
            await asyncio.to_thread(
                alert_delivery.observe,
                _alert_observations(enclosures),
                enabled=enabled,
                now=now,
            )

            if enabled:
                # Each enclosure has at most one pending event. Bound work per
                # cycle so an outage cannot monopolize the event loop.
                for _ in range(MAX_DELIVERIES_PER_CYCLE):
                    pending = alert_delivery.next_due(now=time.time())
                    if pending is None:
                        break
                    try:
                        await asyncio.to_thread(
                            _ntfy_publish,
                            cfg,
                            pending["title"],
                            pending["body"],
                            pending["tags"],
                            pending["priority"],
                        )
                    except Exception:
                        await asyncio.to_thread(
                            alert_delivery.failed, pending["id"], now=time.time())
                        # urllib errors can include the private topic URL.
                        log.warning("Phone alert delivery failed; retry scheduled")
                    else:
                        await asyncio.to_thread(
                            alert_delivery.succeeded, pending["id"], now=time.time())
        except Exception:
            log.exception("Phone alert loop failed")
        await asyncio.sleep(NOTIFY_POLL)


class NtfyToggle(BaseModel):
    enabled: bool


@app.get("/api/ntfy")
def ntfy_status(_: None = Keeper):
    cfg = load_config()
    # Reading Settings never modifies config. A legacy enabled/no-topic record
    # is presented as disabled; tapping setup repairs it through ntfy_set.
    topic = cfg["ntfy"].get("topic", "")
    subscribe_url = _subscribe_url(cfg) if topic else ""
    return {"topic": topic, "server": cfg["ntfy"]["server"],
            "enabled": bool(cfg["ntfy"]["enabled"] and topic),
            "subscribe_url": subscribe_url,
            "qr": _QR_OK}


@app.get("/api/ntfy/delivery")
def ntfy_delivery_status(_: None = Keeper):
    """Return keeper-only delivery health without secrets or message text."""
    cfg = load_config()
    enabled = bool(cfg["ntfy"].get("enabled") and cfg["ntfy"].get("topic"))
    return alert_delivery.status(enabled=enabled)


@app.post("/api/ntfy")
def ntfy_set(payload: NtfyToggle, _: None = Keeper, revision: int = ConfigWrite):
    def update(cfg: dict) -> bool:
        was_enabled = bool(cfg["ntfy"].get("enabled") and cfg["ntfy"].get("topic"))
        cfg["ntfy"]["enabled"] = payload.enabled
        if not cfg["ntfy"].get("topic"):
            cfg["ntfy"]["topic"] = "bask-" + secrets.token_hex(8)
        return was_enabled

    was_enabled = mutate_config(revision, update)
    # A genuine off→on transition starts from the next current snapshot rather
    # than replaying conditions that changed while notifications were disabled.
    if not payload.enabled or not was_enabled:
        alert_delivery.disable()
    return {"ok": True, "enabled": payload.enabled}


@app.post("/api/ntfy/test")
def ntfy_test(_: None = Keeper):
    cfg = load_config()
    if not cfg["ntfy"].get("topic"):
        raise HTTPException(400, "Set up phone alerts before sending a test")
    try:
        _ntfy_publish(cfg, "Bask",
                      "Alerts are working — I'll ping you if an enclosure needs attention.", "lizard")
    except Exception:
        # urllib exceptions commonly contain the complete destination URL,
        # whose final path component is the private ntfy topic.
        log.warning("Phone alert test delivery failed")
        raise HTTPException(502, "Could not reach the notification service. Try again later.")
    return {"ok": True}


@app.get("/api/ntfy/qr")
def ntfy_qr(_: None = Keeper):
    if not _QR_OK:
        raise HTTPException(404, "QR rendering not available")
    import io
    cfg = load_config()
    if not cfg["ntfy"].get("topic"):
        raise HTTPException(404, "Set up phone alerts first")
    buf = io.BytesIO()
    segno.make(_subscribe_url(cfg), error="m").save(
        buf, kind="svg", scale=4, border=2, dark="#0d0f15", light="#ffffff")
    return Response(content=buf.getvalue(), media_type="image/svg+xml")


# ── Settings backup & restore ────────────────────────────────────────────────
# Everything the user configures lives in config.json, so backup = one file.
# Import validates structure (so a bad file can't crash the dashboard), and the
# current config is snapshotted first so a restore is always reversible.

IMPORT_MAX_BYTES = 512_000


def _clean_str(v, fallback="", limit=64) -> str:
    return str(v)[:limit] if isinstance(v, (str, int, float)) else fallback


def _reject_duplicate_values(values, label: str) -> None:
    seen = set()
    for value in values:
        if value in seen:
            raise ValueError(f"duplicate {label} '{value}'")
        seen.add(value)


# Derive the portable schema from the live API model. A separate handwritten
# list previously drifted to the obsolete names `warm_min`, `cool_min`, etc.,
# while every real config uses `warm_temp_min`, `cool_temp_min`, and their night
# equivalents. CI tested the obsolete names too, so restore silently discarded
# every real temperature range while reporting success.
_SPECIES_NUMERIC_FIELDS = frozenset(SpeciesPayload.model_fields) - {"name"}


def _validate_import(data: dict) -> dict:
    """Reduce an uploaded settings file to a structurally safe config.

    Keeps only known top-level keys, drops entries missing required fields, and
    length-caps strings. Numeric range fields pass through as-is — the range
    evaluator already treats non-numeric/absent values as 'no limit'.
    """
    if not isinstance(data, dict):
        raise ValueError("not a settings object")
    out = {}
    out["sensors"] = [
        {"mac": _clean_str(s.get("mac")).upper(), "name": _clean_str(s.get("name"), "sensor"),
         "species": _clean_str(s.get("species"), None) if s.get("species") is not None else None}
        for s in data.get("sensors", []) if isinstance(s, dict) and s.get("mac")]
    out["sensors"] = [sensor for sensor in out["sensors"] if sensor["mac"]]
    _reject_duplicate_values((sensor["mac"] for sensor in out["sensors"]), "sensor MAC")
    out["enclosures"] = []
    for e in data.get("enclosures", []):
        if not (isinstance(e, dict) and e.get("id") and e.get("name")):
            continue
        slots = [{"mac": _clean_str(sl.get("mac")).upper(), "position": _clean_str(sl.get("position"), "", 48)}
                 for sl in e.get("sensors", []) if isinstance(sl, dict) and sl.get("mac")]
        out["enclosures"].append({"id": _clean_str(e["id"]), "name": _clean_str(e["name"]),
                                  "species_id": _clean_str(e.get("species_id"), None)
                                  if e.get("species_id") is not None else None,
                                  "sensors": slots})
    out["enclosures"] = [enclosure for enclosure in out["enclosures"]
                         if enclosure["id"] and enclosure["name"]]
    _reject_duplicate_values(
        (enclosure["id"] for enclosure in out["enclosures"]), "enclosure ID")
    # Species carried every field the file happened to contain. The range
    # evaluator reads these as numbers, so a string like "warm" survived import
    # and raised a TypeError when the dashboard next evaluated that enclosure.
    out["species"] = []
    for sp in data.get("species", []):
        if not (isinstance(sp, dict) and sp.get("id") and sp.get("name")):
            continue
        name = _clean_str(sp["name"])
        ranges: dict[str, float | int | None] = {}
        for key in _SPECIES_NUMERIC_FIELDS:
            if key not in sp:
                continue
            value = sp[key]
            if value is None:
                ranges[key] = None
            elif isinstance(value, bool):
                raise ValueError(f"species '{name}' field {key} must be a number")
            elif isinstance(value, (int, float)):
                number = float(value)
                if number != number or number in (float("inf"), float("-inf")):
                    raise ValueError(f"species '{name}' field {key} is not a finite number")
                ranges[key] = value
            else:
                raise ValueError(f"species '{name}' field {key} must be a number, not {type(value).__name__}")
        try:
            validated = SpeciesPayload(name=name, **ranges).model_dump()
        except Exception as exc:
            raise ValueError(f"species '{name}': {exc}") from exc
        out["species"].append({"id": _clean_str(sp["id"]), **validated})
    out["species"] = [species for species in out["species"] if species["id"]]
    _reject_duplicate_values((species["id"] for species in out["species"]), "species ID")

    # Settings and ntfy were copied straight through, bypassing the bounds the
    # live endpoints enforce. Run them through the same models.
    if isinstance(data.get("settings"), dict):
        try:
            known = {k: v for k, v in data["settings"].items() if k in SettingsPayload.model_fields}
            out["settings"] = SettingsPayload(**known).model_dump(exclude_none=True)
        except Exception as exc:
            raise ValueError(f"settings: {exc}") from exc
    if isinstance(data.get("ntfy"), dict):
        # The topic is machine-local and never imported; the server must be a
        # real https endpoint rather than any string.
        server = data["ntfy"].get("server")
        clean_ntfy: dict[str, Any] = {"enabled": bool(data["ntfy"].get("enabled", False))}
        if server is not None:
            server_text = _clean_str(server, "", 200)
            if server_text:
                if not server_text.startswith("https://"):
                    raise ValueError("the notification server must be an https:// address")
                if any(character in server_text for character in " \t@"):
                    raise ValueError("that notification server address is not valid")
                clean_ntfy["server"] = server_text
        out["ntfy"] = clean_ntfy

    out["thermostats"] = []
    for t in data.get("thermostats", []):
        if not (isinstance(t, dict) and t.get("ip")):
            continue
        try:
            host = validated_lan_host(_clean_str(t.get("ip")))
        except ValueError as exc:
            raise ValueError(f"thermostat address: {exc}") from exc
        out["thermostats"].append({
            "ip": host,
            "name": _clean_str(t.get("name"), None) if t.get("name") is not None else None,
            "enabled": bool(t.get("enabled", True)),
            "temp_unit": ThermostatPayload(
                ip=host, temp_unit=t.get("temp_unit")
            ).temp_unit or out.get("settings", {}).get("temp_unit", "F"),
        })
    if not (out["sensors"] or out["enclosures"] or out["species"]):
        raise ValueError("no recognizable Bask settings in this file")
    return out


# Keys that must never appear in a portable export, checked recursively before
# the file is handed over. The allowlist below already excludes them; this is
# the assertion that a future field cannot quietly reintroduce one.
_EXPORT_FORBIDDEN = {
    "keeper", "session_secret", "salt", "hash", "topic", "password", "passwd",
    "token", "secret", "api_key", "apikey", "access_code", "authorization",
}


def _assert_no_secrets(value: Any, path: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _EXPORT_FORBIDDEN:
                raise RuntimeError(f"export would leak {path}{key}")
            _assert_no_secrets(item, f"{path}{key}.")
    elif isinstance(value, list):
        for item in value:
            _assert_no_secrets(item, path)


def _portable_export(cfg: dict) -> dict:
    """
    The settings worth carrying to another machine, and nothing else.

    Serializing the live config wholesale made the export a credential: it
    carried the Head Keeper salt and hash, from which the session cookie was
    directly derived, plus the private ntfy topic — anyone holding that topic
    can push notifications to the household's phones.
    """
    out: dict[str, Any] = {
        "sensors": [
            {"mac": s.get("mac"), "name": s.get("name"), "species": s.get("species")}
            for s in cfg.get("sensors", []) if isinstance(s, dict)
        ],
        "enclosures": cfg.get("enclosures", []),
        "species": cfg.get("species", []),
        "thermostats": [
            {"ip": t.get("ip"), "name": t.get("name"), "enabled": t.get("enabled", True),
             "temp_unit": t.get("temp_unit", cfg.get("settings", {}).get("temp_unit", "F"))}
            for t in cfg.get("thermostats", []) if isinstance(t, dict)
        ],
        "settings": cfg.get("settings", {}),
    }
    # The ntfy server address is portable; the topic is the credential.
    ntfy = cfg.get("ntfy")
    if isinstance(ntfy, dict):
        out["ntfy"] = {"server": ntfy.get("server"), "enabled": bool(ntfy.get("enabled", False))}
    _assert_no_secrets(out)
    return out


@app.get("/api/config/export")
def export_config(_: None = Keeper):
    payload = _portable_export(load_config())
    return Response(content=json.dumps(payload, indent=2), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="bask-settings.json"'})


@app.post("/api/config/import")
def import_config(payload: dict = Body(), _: None = Keeper, revision: int = ConfigWrite):
    if len(json.dumps(payload)) > IMPORT_MAX_BYTES:
        raise HTTPException(413, "Settings file too large")
    try:
        clean = _validate_import(payload)
    except ValueError as e:
        raise HTTPException(422, f"Not a valid Bask settings file: {e}")

    def replace(current: dict) -> dict:
        if CONFIG_PATH.exists():
            ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup = CONFIG_PATH.with_name(f"config.json.bak-{ts}-preimport")
            shutil.copy2(CONFIG_PATH, backup)
            os.chmod(backup, 0o600)
        # Authentication is machine-local state, not portable husbandry data.
        # A restore also cannot replace the private ntfy topic.
        if isinstance(current.get("keeper"), dict):
            clean["keeper"] = current["keeper"]
        current_ntfy = current.get("ntfy")
        if isinstance(current_ntfy, dict) and current_ntfy.get("topic"):
            imported_ntfy = clean.get("ntfy") if isinstance(clean.get("ntfy"), dict) else {}
            imported_ntfy["topic"] = current_ntfy["topic"]
            clean["ntfy"] = imported_ntfy
        current.clear()
        current.update(clean)
        return {"ok": True, "enclosures": len(current["enclosures"]),
                "sensors": len(current["sensors"]), "species": len(current["species"])}

    return mutate_config(revision, replace)


# ── In-app updates ───────────────────────────────────────────────────────────
# One-tap update from the Settings screen. Security posture (the API is
# unauthenticated on a trusted LAN, so this endpoint must not add new risk):
#   * No client input reaches any command — the repo URL is whatever the
#     install was cloned from (the official repo), and the target is resolved
#     server-side as "newest release tag" / "tip of the tracked branch".
#     The worst a hostile LAN client can do is trigger a legitimate update.
#   * POST requires a JSON body — cross-site forms can't send application/json
#     without a CORS preflight, which this same-origin-only API rejects. So a
#     malicious website can't trigger updates (CSRF-safe).
#   * git/pip run unprivileged with list-args (no shell). The only privilege
#     used is an optional sudoers rule scoped to restarting bask-scanner.
#   * Refuses to run over local code modifications, compile-checks the new
#     code, and rolls back to the previous commit if anything fails.
# config.json and readings.db are untracked, so updates never touch user data.

_update_state = {"state": "idle", "error": None, "from": None, "to": None}
_update_lock = threading.Lock()


def _git(*args, timeout=120) -> str:
    # versionsort.suffix=- makes v1.0.1-rc1 sort BEFORE v1.0.1 (pre-release),
    # so "newest tag" never picks an rc over the release.
    r = subprocess.run(["git", "-c", "versionsort.suffix=-", *args],
                       cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300] or f"git {args[0]} failed")
    return r.stdout.strip()


def _update_supported() -> bool:
    return (ROOT / ".git").is_dir() and shutil.which("git") is not None


def _current_version() -> str:
    try:
        return _git("describe", "--tags", "--always", timeout=10)
    except Exception:
        return "unknown"


def _tracked_branch() -> str | None:
    """Current branch name, or None for detached HEAD (image installs)."""
    r = subprocess.run(["git", "symbolic-ref", "-q", "--short", "HEAD"],
                       cwd=ROOT, capture_output=True, text=True, timeout=10)
    return r.stdout.strip() or None


def _latest_tag() -> str | None:
    tags = _git("tag", "--list", "v*", "--sort=-v:refname", timeout=10).splitlines()
    return tags[0] if tags else None


@app.get("/api/update/status")
def update_status(refresh: bool = False):
    out = {"supported": _update_supported(), **_update_state,
           "version": _current_version() if _update_supported() else None}
    if not out["supported"] or _update_state["state"] == "updating":
        return out
    if refresh:
        try:
            _git("fetch", "--tags", "--quiet", "origin", timeout=90)
            branch = _tracked_branch()
            if branch:
                behind = int(_git("rev-list", "--count", f"HEAD..origin/{branch}", timeout=15) or 0)
                out["available"] = behind > 0
                out["latest"] = f"{branch} (+{behind} update{'s' if behind != 1 else ''})" if behind else out["version"]
            else:
                latest = _latest_tag()
                try:
                    current = _git("describe", "--tags", "--exact-match", "HEAD", timeout=10)
                except Exception:
                    current = None
                out["available"] = bool(latest) and latest != current
                out["latest"] = latest
            out["checked"] = True
        except Exception as e:
            out["check_error"] = str(e)[:200]
    return out


def _do_update():
    try:
        prev = _git("rev-parse", "HEAD", timeout=10)
        _update_state.update(state="updating", error=None)
        _update_state["from"] = _current_version()
        if _git("status", "--porcelain", "--untracked-files=no", timeout=15):
            raise RuntimeError("Local code changes detected — update manually to avoid losing them")
        _git("fetch", "--tags", "--quiet", "origin", timeout=300)
        branch = _tracked_branch()
        if branch:
            _git("merge", "--ff-only", f"origin/{branch}", timeout=60)
        else:
            latest = _latest_tag()
            if not latest:
                raise RuntimeError("No release tags found")
            _git("checkout", "--quiet", latest, timeout=60)
        if _git("rev-parse", "HEAD", timeout=10) == prev:
            _update_state.update(state="idle", to=_current_version())
            return
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                            "-r", str(ROOT / "requirements.txt")],
                           cwd=ROOT, check=True, capture_output=True, timeout=900)
            subprocess.run([sys.executable, "-m", "py_compile",
                            "server/app.py", "scanner/scanner.py", "scanner/govee.py", "scanner/db.py"],
                           cwd=ROOT, check=True, capture_output=True, timeout=120)
        except subprocess.CalledProcessError as e:
            _git("reset", "--hard", prev, timeout=30)   # tree was clean; safe to rewind
            raise RuntimeError("New version failed checks — rolled back "
                               f"({(e.stderr or b'').decode(errors='replace')[:150]})")
        _update_state.update(state="restarting", to=_current_version())
        log.info(f"updated {_update_state['from']} -> {_update_state['to']}; restarting")
        # Scanner restart needs root: use the narrowly-scoped sudoers rule if
        # present; otherwise the scanner simply picks the update up on next boot.
        subprocess.run(["sudo", "-n", "systemctl", "restart", "bask-scanner.service"],
                       capture_output=True, timeout=30)
        time.sleep(1.0)
        os._exit(0)   # systemd (Restart=always) relaunches us on the new code
    except Exception as e:
        _update_state.update(state="failed", error=str(e)[:300])
        log.warning(f"update failed: {e}")


@app.post("/api/update")
def start_update(payload: dict = Body(), _: None = Keeper):
    if payload.get("confirm") is not True:      # JSON body → CSRF preflight protection
        raise HTTPException(422, "confirm required")
    if not _update_supported():
        raise HTTPException(400, "This install isn't a git checkout — update manually")
    with _update_lock:
        if _update_state["state"] == "updating":
            raise HTTPException(409, "Update already in progress")
        _update_state.update(state="updating", error=None)
    threading.Thread(target=_do_update, daemon=True).start()
    return {"ok": True, "state": "updating"}


# Static frontend is mounted last so it doesn't shadow the API routes.
app.mount("/", StaticFiles(directory=str(FRONTEND_PATH), html=True), name="frontend")
