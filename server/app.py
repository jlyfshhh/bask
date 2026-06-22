"""Web API + static host for the Bask dashboard.

This process does NO Bluetooth. The standalone scanner writes readings to
SQLite; here we only read them, group sensors into enclosures, evaluate them
against per-species ranges, and serve the touch UI. Discovery ("add a sensor")
reads the scanner's `discovered` table instead of starting its own scan.
"""
import datetime
import json
import logging
import sys
import time
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


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
    return {"enclosures": enclosures_out, "ungrouped": ungrouped,
            "counts": counts, "temp_unit": unit, "updated_at": now,
            "period": "day" if is_day else "night",
            "day_start_hour": cfg["settings"]["day_start_hour"],
            "day_end_hour": cfg["settings"]["day_end_hour"]}


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


# Static frontend is mounted last so it doesn't shadow the API routes.
app.mount("/", StaticFiles(directory=str(FRONTEND_PATH), html=True), name="frontend")
