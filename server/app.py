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
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
import ipaddress
import re
from typing import Any, Literal

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

sys.path.insert(0, str(Path(__file__).parent.parent / "scanner"))
import db  # noqa: E402
from server import keeper
from server.cielo import CieloMonitor
from server.vesync import VeSyncHumidifierMonitor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("web")

ROOT = Path(__file__).parent.parent
DATA_DIR = Path(os.environ.get("BASK_DATA_DIR", ROOT))
CONFIG_PATH = DATA_DIR / "config.json"
FRONTEND_PATH = ROOT / "frontend"
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


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text())
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
    cfg.setdefault("ntfy", {})                         # opt-in phone alerts via ntfy
    cfg["ntfy"].setdefault("server", "https://ntfy.sh")
    cfg["ntfy"].setdefault("topic", "")
    cfg["ntfy"].setdefault("enabled", False)
    cfg.setdefault("keeper", {})                       # Head Keeper key, stored hashed
    return cfg


def save_config(cfg: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(CONFIG_PATH)  # atomic so the scanner never reads a half-written file


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


def _parse_herpstat(ip: str, raw: dict, name_override) -> dict:
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
        "safety_ok": safety_ok, "reachable": True,
        "last_seen": int(time.time()), "outputs": outputs,
    }


async def _herpstat_loop():
    while True:
        try:
            for t in load_config().get("thermostats", []):
                ip = t.get("ip")
                if not ip or not t.get("enabled", True):
                    continue
                try:
                    raw = await asyncio.to_thread(_fetch_herpstat, ip)
                    _thermostats[ip] = _parse_herpstat(ip, raw, t.get("name"))
                except Exception as e:
                    prev = _thermostats.get(ip, {})
                    _thermostats[ip] = {"ip": ip, "name": t.get("name") or prev.get("name") or ip,
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    poller = asyncio.create_task(_herpstat_loop())
    cielo_poller = asyncio.create_task(cielo.loop())
    humidifier_poller = asyncio.create_task(humidifier.loop())
    notifier = asyncio.create_task(_notify_loop())
    shed_poller = asyncio.create_task(_shed_display_loop())
    yield
    poller.cancel()
    cielo_poller.cancel()
    humidifier_poller.cancel()
    notifier.cancel()
    shed_poller.cancel()


# No CORS middleware on purpose. The dashboard is served from the SAME origin as
# the API, so cross-origin access is neither needed nor wanted. Omitting it means
# the browser's same-origin policy blocks other websites from reading this API,
# and cross-origin JSON writes fail their preflight — important because the API
# is unauthenticated and meant only for a trusted local network.
app = FastAPI(lifespan=lifespan)


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


@app.middleware("http")
async def no_store_api_responses(request: Request, call_next):
    """API data is live; only the static frontend should ever be cached."""
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


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
def keeper_unlock(payload: KeeperUnlock, response: Response):
    record = load_config().get("keeper")
    if not keeper.is_configured(record):
        return {"ok": True, "configured": False, "unlocked": True}
    if not keeper.verify_key(payload.key, record):
        raise HTTPException(401, "That key does not match.")
    # Older installs have no signing secret yet; mint one on first unlock.
    cfg = load_config()
    keeper.ensure_session_secret(cfg["keeper"])
    save_config(cfg)
    response.set_cookie(
        keeper.COOKIE_NAME, keeper.issue_session(cfg["keeper"]), **keeper.cookie_kwargs())
    return {"ok": True, "configured": True, "unlocked": True}


@app.post("/api/keeper/lock")
def keeper_lock(response: Response):
    response.delete_cookie(keeper.COOKIE_NAME, path="/")
    return {"ok": True}


@app.post("/api/keeper/key")
def keeper_set_key(payload: KeeperSetKey, request: Request, response: Response):
    """
    Set or change the key.

    Changing it requires the current one — otherwise anybody on the network
    could lock the Head Keeper out of their own dashboard. Setting the first
    key needs no proof, because until then there is nothing to prove.
    """
    cfg = load_config()
    record = cfg.get("keeper")
    if keeper.is_configured(record) and not (
        keeper.session_is_valid(request.cookies.get(keeper.COOKIE_NAME), record)
        or keeper.verify_key(payload.current, record)
    ):
        raise HTTPException(401, "Enter the current Head Keeper key to change it.")
    try:
        new_key = keeper.validate_new_key(payload.key)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    # A fresh record gets a fresh signing secret, so rotating the key ends every
    # existing session on top of the salt and hash changing under the signature.
    cfg["keeper"] = keeper.ensure_session_secret(keeper.hash_key(new_key))
    save_config(cfg)
    response.set_cookie(
        keeper.COOKIE_NAME, keeper.issue_session(cfg["keeper"]), **keeper.cookie_kwargs())
    return {"ok": True, "configured": True, "unlocked": True}


@app.delete("/api/keeper/key")
def keeper_clear_key(request: Request, response: Response, _: None = Keeper):
    """Remove the lock and go back to open. Requires being unlocked already."""
    cfg = load_config()
    cfg["keeper"] = {}
    save_config(cfg)
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
    has_ranges = False
    if sp:
        wt_lo, wt_hi, ct_lo, ct_hi, hm_lo, hm_hi = species_ranges(sp, is_day)
        has_ranges = any(v is not None for v in [wt_lo, wt_hi, ct_lo, ct_hi, hm_lo, hm_hi])
        if warm and not warm["stale"] and warm["temp"] is not None:
            warm_temp_ok = _check(warm["temp"], wt_lo, wt_hi)
            if not warm_temp_ok:
                violations += 1
        if cool and not cool["stale"] and cool["temp"] is not None:
            cool_temp_ok = _check(cool["temp"], ct_lo, ct_hi)
            if not cool_temp_ok:
                violations += 1
        if cool and not cool["stale"] and cool["humidity"] is not None:
            humidity_ok = _check(cool["humidity"], hm_lo, hm_hi)
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
        "humidity_ok": humidity_ok, "low_battery": low_battery,
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

    ungrouped = [
        {**build_sensor_reading(s["mac"], readings_by_mac, sensor_defs, unit, stale_cutoff, now, low_batt),
         "species": s.get("species")}
        for s in cfg["sensors"] if s["mac"].upper() not in grouped
    ]

    counts = {"ok": 0, "warning": 0, "danger": 0, "stale": 0, "no_data": 0, "no_ranges": 0}
    for e in enclosures_out:
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    thermostats = [_thermostats[t["ip"]] for t in cfg.get("thermostats", [])
                   if t.get("ip") in _thermostats]
    return {"enclosures": enclosures_out, "ungrouped": ungrouped,
            "counts": counts, "temp_unit": unit, "updated_at": now,
            "period": "day" if is_day else "night",
            "day_start_hour": cfg["settings"]["day_start_hour"],
            "day_end_hour": cfg["settings"]["day_end_hour"],
            "thermostats": thermostats,
            "room_climate": cielo.public_status(),
            "humidifier": humidifier.public_status()}


@app.get("/api/dashboard")
def dashboard():
    return _build_dashboard(load_config())


@app.get("/api/room-dashboard")
def room_dashboard():
    """Combined, read-only feed for the attached animal-room display."""
    return {
        "generated_at": int(time.time()),
        "bask": _build_dashboard(load_config()),
        "shed": _shed_display.copy(),
    }


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
def create_species(payload: SpeciesPayload, _: None = Keeper):
    cfg = load_config()
    sp_id = str(int(time.time() * 1000))
    cfg["species"].append({"id": sp_id, **payload.model_dump()})
    save_config(cfg)
    return {"ok": True, "id": sp_id}


@app.put("/api/species/{sp_id}")
def update_species(sp_id: str, payload: SpeciesPayload, _: None = Keeper):
    cfg = load_config()
    for sp in cfg["species"]:
        if sp["id"] == sp_id:
            sp.update(payload.model_dump())
            save_config(cfg)
            return {"ok": True}
    raise HTTPException(404, "Species not found")


@app.delete("/api/species/{sp_id}")
def delete_species(sp_id: str, _: None = Keeper):
    cfg = load_config()
    before = len(cfg["species"])
    cfg["species"] = [sp for sp in cfg["species"] if sp["id"] != sp_id]
    if len(cfg["species"]) == before:
        raise HTTPException(404, "Species not found")
    save_config(cfg)
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


@app.get("/api/enclosures")
def list_enclosures():
    return {"enclosures": load_config()["enclosures"]}


@app.post("/api/enclosures")
def create_enclosure(payload: EnclosurePayload, _: None = Keeper):
    cfg = load_config()
    enc_id = str(int(time.time() * 1000))
    cfg["enclosures"].append({
        "id": enc_id, "name": payload.name, "species_id": payload.species_id,
        "sensors": [{"mac": s.mac.upper(), "position": s.position} for s in payload.sensors],
    })
    save_config(cfg)
    return {"ok": True, "id": enc_id}


@app.put("/api/enclosures/reorder")
def reorder_enclosures(payload: ReorderPayload, _: None = Keeper):
    cfg = load_config()
    id_to_enc = {e["id"]: e for e in cfg["enclosures"]}
    cfg["enclosures"] = [id_to_enc[eid] for eid in payload.order if eid in id_to_enc]
    save_config(cfg)
    return {"ok": True}


@app.put("/api/enclosures/{enc_id}")
def update_enclosure(enc_id: str, payload: EnclosurePayload, _: None = Keeper):
    cfg = load_config()
    for enc in cfg["enclosures"]:
        if enc["id"] == enc_id:
            enc["name"] = payload.name
            enc["species_id"] = payload.species_id
            enc["sensors"] = [{"mac": s.mac.upper(), "position": s.position} for s in payload.sensors]
            save_config(cfg)
            return {"ok": True}
    raise HTTPException(404, "Enclosure not found")


@app.delete("/api/enclosures/{enc_id}")
def delete_enclosure(enc_id: str, _: None = Keeper):
    cfg = load_config()
    before = len(cfg["enclosures"])
    cfg["enclosures"] = [e for e in cfg["enclosures"] if e["id"] != enc_id]
    if len(cfg["enclosures"]) == before:
        raise HTTPException(404, "Enclosure not found")
    save_config(cfg)
    return {"ok": True}


# ── Sensor CRUD ──────────────────────────────────────────────────────────────

class SensorPayload(BaseModel):
    mac: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=64)
    species: str | None = Field(None, max_length=64)


class SensorUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    species: str | None = Field(None, max_length=64)


@app.get("/api/sensors")
def list_sensors():
    cfg = load_config()
    return {"sensors": cfg["sensors"], "settings": cfg["settings"]}


@app.post("/api/sensors")
def add_sensor(payload: SensorPayload, _: None = Keeper):
    cfg = load_config()
    mac = payload.mac.upper()
    if any(s["mac"].upper() == mac for s in cfg["sensors"]):
        raise HTTPException(400, "Sensor already configured")
    cfg["sensors"].append({"mac": mac, "name": payload.name, "species": payload.species})
    save_config(cfg)
    return {"ok": True}


@app.put("/api/sensors/{mac}")
def update_sensor(mac: str, payload: SensorUpdate, _: None = Keeper):
    cfg = load_config()
    for s in cfg["sensors"]:
        if s["mac"].upper() == mac.upper():
            s["name"] = payload.name
            s["species"] = payload.species
            save_config(cfg)
            return {"ok": True}
    raise HTTPException(404, "Sensor not found")


@app.delete("/api/sensors/{mac}")
def delete_sensor(mac: str, _: None = Keeper):
    cfg = load_config()
    before = len(cfg["sensors"])
    target = mac.upper()
    cfg["sensors"] = [s for s in cfg["sensors"] if s["mac"].upper() != target]
    if len(cfg["sensors"]) == before:
        raise HTTPException(404, "Sensor not found")
    # Also unlink it from any enclosure slot it was assigned to.
    for enc in cfg["enclosures"]:
        enc["sensors"] = [sl for sl in enc.get("sensors", []) if sl["mac"].upper() != target]
    save_config(cfg)
    return {"ok": True}


# ── Pairing (proximity-based sensor → enclosure assignment) ──────────────────

class PairPayload(BaseModel):
    mac: str = Field(min_length=1, max_length=64)
    enclosure_id: str = Field(max_length=64)
    position: str = Field(max_length=48)
    name: str | None = Field(None, max_length=64)


@app.post("/api/pair")
def pair_sensor(payload: PairPayload, _: None = Keeper):
    """Assign a discovered sensor to an enclosure slot in one atomic step.

    Used by the touch "Pair by proximity" wizard: the user holds a sensor near
    the Pi (strongest RSSI) and taps a Warm/Cool target. We (1) ensure a sensor
    record exists, (2) detach the mac from any other enclosure, and (3) put it in
    the chosen position slot, replacing whatever held that position before.
    """
    cfg = load_config()
    mac = payload.mac.upper()
    pos = payload.position.strip()
    if not pos:
        raise HTTPException(400, "position is required")
    enc = next((e for e in cfg["enclosures"] if e["id"] == payload.enclosure_id), None)
    if enc is None:
        raise HTTPException(404, "Enclosure not found")

    name = (payload.name or f"{enc['name']} {pos}").strip()
    existing = next((s for s in cfg["sensors"] if s["mac"].upper() == mac), None)
    if existing:
        existing["name"] = name
    else:
        cfg["sensors"].append({"mac": mac, "name": name, "species": None})

    # A sensor lives in exactly one place — detach it from every enclosure first.
    for e in cfg["enclosures"]:
        e["sensors"] = [sl for sl in e.get("sensors", []) if sl["mac"].upper() != mac]
    # Replace whatever currently holds this position in the target enclosure.
    enc["sensors"] = [sl for sl in enc.get("sensors", [])
                      if sl.get("position", "").lower() != pos.lower()]
    enc["sensors"].append({"mac": mac, "position": pos})
    save_config(cfg)
    return {"ok": True, "sensor_name": name, "enclosure": enc["name"], "position": pos}


@app.post("/api/unpair")
def unpair_sensor(payload: PairPayload, _: None = Keeper):
    """Remove a sensor from a given enclosure slot (undo a mis-tap in the wizard)."""
    cfg = load_config()
    mac = payload.mac.upper()
    enc = next((e for e in cfg["enclosures"] if e["id"] == payload.enclosure_id), None)
    if enc is None:
        raise HTTPException(404, "Enclosure not found")
    enc["sensors"] = [sl for sl in enc.get("sensors", []) if sl["mac"].upper() != mac]
    save_config(cfg)
    return {"ok": True}


# ── Settings ─────────────────────────────────────────────────────────────────

class SettingsPayload(BaseModel):
    temp_unit: Literal["F", "C"] | None = None
    stale_after_minutes: int | None = Field(None, ge=1, le=1440)
    low_battery_pct: int | None = Field(None, ge=0, le=100)
    day_start_hour: int | None = Field(None, ge=0, le=23)
    day_end_hour: int | None = Field(None, ge=0, le=23)


@app.put("/api/settings")
def update_settings(payload: SettingsPayload, _: None = Keeper):
    cfg = load_config()
    for k, v in payload.model_dump(exclude_none=True).items():
        cfg["settings"][k] = v
    save_config(cfg)
    return {"ok": True, "settings": cfg["settings"]}


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


class ThermostatTest(BaseModel):
    ip: str = Field(min_length=1, max_length=64)


@app.get("/api/thermostats")
def list_thermostats():
    cfg = load_config()
    out = [{**t, "status": _thermostats.get(t.get("ip"), {})}
           for t in cfg.get("thermostats", [])]
    return {"thermostats": out, "temp_unit": cfg["settings"]["temp_unit"]}


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
def add_thermostat(payload: ThermostatPayload, _: None = Keeper):
    cfg = load_config()
    ip = payload.ip.strip()
    if any(t.get("ip") == ip for t in cfg["thermostats"]):
        raise HTTPException(400, "Thermostat already added")
    cfg["thermostats"].append({"ip": ip, "name": payload.name, "enabled": payload.enabled})
    save_config(cfg)
    return {"ok": True}


@app.put("/api/thermostats/{ip}")
def update_thermostat(ip: str, payload: ThermostatPayload, _: None = Keeper):
    cfg = load_config()
    new_ip = payload.ip.strip()
    for t in cfg["thermostats"]:
        if t.get("ip") == ip:
            t["ip"], t["name"], t["enabled"] = new_ip, payload.name, payload.enabled
            save_config(cfg)
            if new_ip != ip:
                _thermostats.pop(ip, None)   # drop stale cache under the old IP
            return {"ok": True}
    raise HTTPException(404, "Thermostat not found")


@app.delete("/api/thermostats/{ip}")
def delete_thermostat(ip: str, _: None = Keeper):
    cfg = load_config()
    before = len(cfg["thermostats"])
    cfg["thermostats"] = [t for t in cfg["thermostats"] if t.get("ip") != ip]
    if len(cfg["thermostats"]) == before:
        raise HTTPException(404, "Thermostat not found")
    save_config(cfg)
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


def _ntfy_topic(cfg) -> str:
    """Return the persisted random topic, creating one on first use."""
    nt = cfg["ntfy"]
    if not nt.get("topic"):
        nt["topic"] = "bask-" + secrets.token_hex(8)
        save_config(cfg)
    return nt["topic"]


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
BAD_STATES = {"warning", "danger", "stale"}
_last_status: dict[str, str] = {}
_notify_seeded = False


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


async def _notify_loop():
    """Compare each enclosure's status to last cycle; push on meaningful changes.

    The first pass only seeds the baseline so we never fire a burst of alerts for
    conditions that were already true when the server started.
    """
    global _notify_seeded
    while True:
        try:
            cfg = load_config()
            if cfg["ntfy"].get("enabled") and cfg["ntfy"].get("topic"):
                for e in _build_dashboard(cfg)["enclosures"]:
                    prev, cur = _last_status.get(e["id"]), e["status"]
                    _last_status[e["id"]] = cur
                    if not _notify_seeded or cur == prev:
                        continue
                    if cur in BAD_STATES:
                        await asyncio.to_thread(_ntfy_publish, cfg, "Bask alert",
                                                _alert_text(e), "warning", "high")
                    elif cur == "ok" and prev in BAD_STATES:
                        await asyncio.to_thread(_ntfy_publish, cfg, "Bask",
                                                f"{e['name']} is back to normal", "white_check_mark")
                _notify_seeded = True
        except Exception as e:
            log.warning(f"notify loop error: {e}")
        await asyncio.sleep(NOTIFY_POLL)


class NtfyToggle(BaseModel):
    enabled: bool


@app.get("/api/ntfy")
def ntfy_status(_: None = Keeper):
    cfg = load_config()
    return {"topic": _ntfy_topic(cfg), "server": cfg["ntfy"]["server"],
            "enabled": cfg["ntfy"]["enabled"], "subscribe_url": _subscribe_url(cfg),
            "qr": _QR_OK}


@app.post("/api/ntfy")
def ntfy_set(payload: NtfyToggle, _: None = Keeper):
    cfg = load_config()
    cfg["ntfy"]["enabled"] = payload.enabled
    _ntfy_topic(cfg)
    save_config(cfg)
    return {"ok": True, "enabled": payload.enabled}


@app.post("/api/ntfy/test")
def ntfy_test(_: None = Keeper):
    cfg = load_config()
    try:
        _ntfy_publish(cfg, "Bask",
                      "Alerts are working — I'll ping you if an enclosure needs attention.", "lizard")
    except Exception as e:
        raise HTTPException(502, f"Could not reach the ntfy server ({e})")
    return {"ok": True}


@app.get("/api/ntfy/qr")
def ntfy_qr(_: None = Keeper):
    if not _QR_OK:
        raise HTTPException(404, "QR rendering not available")
    import io
    cfg = load_config()
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
            {"ip": t.get("ip"), "name": t.get("name"), "enabled": t.get("enabled", True)}
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
def import_config(payload: dict = Body(), _: None = Keeper):
    if len(json.dumps(payload)) > IMPORT_MAX_BYTES:
        raise HTTPException(413, "Settings file too large")
    try:
        clean = _validate_import(payload)
    except ValueError as e:
        raise HTTPException(422, f"Not a valid Bask settings file: {e}")
    if CONFIG_PATH.exists():
        ts = time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(CONFIG_PATH, CONFIG_PATH.with_name(f"config.json.bak-{ts}-preimport"))
    # Authentication is machine-local state, not portable husbandry data.
    # Replacing the whole config used to drop the keeper block, and a missing
    # block read as "no key set" — so restoring a backup silently unlocked the
    # install. The private ntfy topic is preserved for the same reason: it is a
    # local credential, and an imported file must not be able to set it.
    current = load_config()
    if isinstance(current.get("keeper"), dict):
        clean["keeper"] = current["keeper"]
    current_ntfy = current.get("ntfy")
    if isinstance(current_ntfy, dict) and current_ntfy.get("topic"):
        imported_ntfy = clean.get("ntfy") if isinstance(clean.get("ntfy"), dict) else {}
        imported_ntfy["topic"] = current_ntfy["topic"]
        clean["ntfy"] = imported_ntfy
    save_config(clean)
    return {"ok": True, "enclosures": len(clean["enclosures"]),
            "sensors": len(clean["sensors"]), "species": len(clean["species"])}


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
