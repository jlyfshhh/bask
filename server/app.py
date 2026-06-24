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
import sys
import time
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).parent.parent / "scanner"))
import db  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("web")

CONFIG_PATH = Path(__file__).parent.parent / "config.json"
FRONTEND_PATH = Path(__file__).parent.parent / "frontend"


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
    req = urllib.request.Request(f"http://{ip}/RAWSTATUS", headers={"User-Agent": "bask"})
    with urllib.request.urlopen(req, timeout=HERPSTAT_TIMEOUT) as r:
        return json.loads(r.read().decode())


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    poller = asyncio.create_task(_herpstat_loop())
    yield
    poller.cancel()


# No CORS middleware on purpose. The dashboard is served from the SAME origin as
# the API, so cross-origin access is neither needed nor wanted. Omitting it means
# the browser's same-origin policy blocks other websites from reading this API,
# and cross-origin JSON writes fail their preflight — important because the API
# is unauthenticated and meant only for a trusted local network.
app = FastAPI(lifespan=lifespan)


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


@app.get("/api/dashboard")
def dashboard():
    cfg = load_config()
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
            "thermostats": thermostats}


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
def create_species(payload: SpeciesPayload):
    cfg = load_config()
    sp_id = str(int(time.time() * 1000))
    cfg["species"].append({"id": sp_id, **payload.model_dump()})
    save_config(cfg)
    return {"ok": True, "id": sp_id}


@app.put("/api/species/{sp_id}")
def update_species(sp_id: str, payload: SpeciesPayload):
    cfg = load_config()
    for sp in cfg["species"]:
        if sp["id"] == sp_id:
            sp.update(payload.model_dump())
            save_config(cfg)
            return {"ok": True}
    raise HTTPException(404, "Species not found")


@app.delete("/api/species/{sp_id}")
def delete_species(sp_id: str):
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
def create_enclosure(payload: EnclosurePayload):
    cfg = load_config()
    enc_id = str(int(time.time() * 1000))
    cfg["enclosures"].append({
        "id": enc_id, "name": payload.name, "species_id": payload.species_id,
        "sensors": [{"mac": s.mac.upper(), "position": s.position} for s in payload.sensors],
    })
    save_config(cfg)
    return {"ok": True, "id": enc_id}


@app.put("/api/enclosures/reorder")
def reorder_enclosures(payload: ReorderPayload):
    cfg = load_config()
    id_to_enc = {e["id"]: e for e in cfg["enclosures"]}
    cfg["enclosures"] = [id_to_enc[eid] for eid in payload.order if eid in id_to_enc]
    save_config(cfg)
    return {"ok": True}


@app.put("/api/enclosures/{enc_id}")
def update_enclosure(enc_id: str, payload: EnclosurePayload):
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
def delete_enclosure(enc_id: str):
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
def add_sensor(payload: SensorPayload):
    cfg = load_config()
    mac = payload.mac.upper()
    if any(s["mac"].upper() == mac for s in cfg["sensors"]):
        raise HTTPException(400, "Sensor already configured")
    cfg["sensors"].append({"mac": mac, "name": payload.name, "species": payload.species})
    save_config(cfg)
    return {"ok": True}


@app.put("/api/sensors/{mac}")
def update_sensor(mac: str, payload: SensorUpdate):
    cfg = load_config()
    for s in cfg["sensors"]:
        if s["mac"].upper() == mac.upper():
            s["name"] = payload.name
            s["species"] = payload.species
            save_config(cfg)
            return {"ok": True}
    raise HTTPException(404, "Sensor not found")


@app.delete("/api/sensors/{mac}")
def delete_sensor(mac: str):
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
def pair_sensor(payload: PairPayload):
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
def unpair_sensor(payload: PairPayload):
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
def update_settings(payload: SettingsPayload):
    cfg = load_config()
    for k, v in payload.model_dump(exclude_none=True).items():
        cfg["settings"][k] = v
    save_config(cfg)
    return {"ok": True, "settings": cfg["settings"]}


# ── Herpstat thermostat CRUD (optional feature) ──────────────────────────────
# Units are keyed by their LAN IP. The background poller (_herpstat_loop) reads
# this list each cycle, so adds/edits take effect within one poll interval with
# no restart. The dashboard strip stays hidden until at least one unit is added.

class ThermostatPayload(BaseModel):
    ip: str = Field(min_length=1, max_length=64)
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
def test_thermostat(payload: ThermostatTest):
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
def add_thermostat(payload: ThermostatPayload):
    cfg = load_config()
    ip = payload.ip.strip()
    if any(t.get("ip") == ip for t in cfg["thermostats"]):
        raise HTTPException(400, "Thermostat already added")
    cfg["thermostats"].append({"ip": ip, "name": payload.name, "enabled": payload.enabled})
    save_config(cfg)
    return {"ok": True}


@app.put("/api/thermostats/{ip}")
def update_thermostat(ip: str, payload: ThermostatPayload):
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
def delete_thermostat(ip: str):
    cfg = load_config()
    before = len(cfg["thermostats"])
    cfg["thermostats"] = [t for t in cfg["thermostats"] if t.get("ip") != ip]
    if len(cfg["thermostats"]) == before:
        raise HTTPException(404, "Thermostat not found")
    save_config(cfg)
    _thermostats.pop(ip, None)   # so it disappears from the dashboard immediately
    return {"ok": True}


# Static frontend is mounted last so it doesn't shadow the API routes.
app.mount("/", StaticFiles(directory=str(FRONTEND_PATH), html=True), name="frontend")
