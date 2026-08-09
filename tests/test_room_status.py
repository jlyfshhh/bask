"""QC-17: Haven must never present stale or incomplete data as fully live."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def assert_room_payload_allowlist() -> None:
    """QC-22: internal Bask/Shed fields must not escape through Haven."""
    import server.app as bask_app

    sentinels = {
        "SENSOR-MAC-SENTINEL", "SENSOR-NAME-SENTINEL", "SENSOR-ID-SENTINEL",
        "THERMOSTAT-IP-SENTINEL", "UNGROUPED-SENTINEL", "CLOUD-DEVICE-SENTINEL",
        "SHED-INTERNAL-SENTINEL", "SHED-MEMBER-SENTINEL", "INTERNAL-ERROR-SENTINEL",
    }
    full_dashboard = {
        "enclosures": [{
            "id": "SENSOR-ID-SENTINEL",
            "name": "Example Habitat",
            "species_name": "Example Species",
            "species_id": "SENSOR-ID-SENTINEL",
            "has_ranges": True,
            "status": "warning",
            "violations": 1,
            "warm_temp_ok": False,
            "cool_temp_ok": True,
            "humidity_ok": True,
            "low_battery": True,
            "age_seconds": 12,
            "warm": {
                "temp": 91.5, "humidity": 48.0,
                "mac": "SENSOR-MAC-SENTINEL", "name": "SENSOR-NAME-SENTINEL",
                "id": "SENSOR-ID-SENTINEL", "rssi": -40, "battery": 80,
                "age_seconds": 12, "last_seen": 1234,
            },
            "cool": {
                "temp": 77.0, "humidity": 55.0,
                "mac": "SENSOR-MAC-SENTINEL", "name": "SENSOR-NAME-SENTINEL",
            },
            "sensors": [{"mac": "SENSOR-MAC-SENTINEL", "name": "SENSOR-NAME-SENTINEL"}],
        }],
        "counts": {"ok": 1, "warning": 1, "danger": 0, "stale": 0,
                   "no_data": 0, "no_ranges": 0, "internal": "SENSOR-ID-SENTINEL"},
        "ungrouped": [{"name": "UNGROUPED-SENTINEL", "mac": "SENSOR-MAC-SENTINEL"}],
        "thermostats": [{"ip": "THERMOSTAT-IP-SENTINEL", "outputs": []}],
        "temp_unit": "F", "period": "day", "updated_at": 1234,
        "room_climate": {
            "configured": True, "selected": True, "online": True, "stale": False,
            "error": "INTERNAL-ERROR-SENTINEL", "temperature": 74.2, "humidity": 52.0,
            "name": "CLOUD-DEVICE-SENTINEL", "id": "CLOUD-DEVICE-SENTINEL",
            "target": 73, "fan": "auto", "ip": "THERMOSTAT-IP-SENTINEL",
        },
        "humidifier": {
            "configured": True, "selected": True, "online": True, "stale": False,
            "error": None, "humidity": 51.0, "power": True, "mode": "auto",
            "water_lacks": False, "name": "CLOUD-DEVICE-SENTINEL",
            "id": "CLOUD-DEVICE-SENTINEL", "model": "SHED-INTERNAL-SENTINEL",
            "temperature": 72, "mist_level": 3, "target_humidity": 55,
        },
    }
    shed_display = {
        "configured": True,
        "available": True,
        "last_success": 9_940,
        "error": "INTERNAL-ERROR-SENTINEL",
        "private_connection": "SHED-INTERNAL-SENTINEL",
        "data": {
            "date": "2026-08-09",
            "generatedAt": "2026-08-09T12:00:00Z",
            "summary": {"total": 2, "completed": 1, "remaining": 1, "overdue": 0,
                        "memberId": "SHED-MEMBER-SENTINEL"},
            "tasks": [{
                "animalName": "Example Animal", "species": "Example Species",
                "taskType": "feeding", "title": "Feed", "details": "One feeder",
                "dueDate": "2026-08-09", "animalId": "SHED-INTERNAL-SENTINEL",
                "scheduleId": "SHED-INTERNAL-SENTINEL", "memberId": "SHED-MEMBER-SENTINEL",
            }],
            "overdue": [],
            "history": [{"completedBy": "SHED-MEMBER-SENTINEL"}],
        },
    }

    dto = bask_app._room_dashboard_dto(full_dashboard, shed_display, generated_at=10_000)
    encoded = json.dumps(dto, sort_keys=True)
    for sentinel in sentinels:
        assert sentinel not in encoded, f"private sentinel escaped room-dashboard: {sentinel}"

    forbidden_keys = {
        "id", "species_id", "sensor_id", "sensors", "mac", "rssi", "battery",
        "low_battery", "age_seconds", "last_seen", "ungrouped", "thermostats", "ip",
        "outputs", "animalid", "scheduleid", "memberid", "completedby", "history",
    }

    def walk(value):
        if isinstance(value, dict):
            for key, nested in value.items():
                assert key.lower() not in forbidden_keys, f"forbidden key escaped: {key}"
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(dto)
    assert set(dto) == {"generated_at", "bask", "shed"}
    assert set(dto["bask"]) == {"enclosures", "counts", "room_climate", "humidifier"}
    enclosure = dto["bask"]["enclosures"][0]
    assert set(enclosure) == {
        "name", "species_name", "status", "warm_temp_ok", "cool_temp_ok",
        "humidity_ok", "warm", "cool",
    }
    assert enclosure["warm"] == {"temp": 91.5, "humidity": 48.0}
    assert enclosure["cool"] == {"temp": 77.0, "humidity": 55.0}
    assert dto["bask"]["room_climate"]["error"] is True
    assert set(dto["shed"]) == {"configured", "available", "last_success", "data"}
    assert set(dto["shed"]["data"]) == {"summary", "tasks", "overdue"}
    assert set(dto["shed"]["data"]["tasks"][0]) == {
        "animalName", "species", "taskType", "title", "details", "dueDate",
    }
    malformed = bask_app._room_dashboard_dto(full_dashboard, {
        "configured": True,
        "available": True,
        "last_success": 9_940,
        "data": {"summary": {}, "tasks": [], "overdue": []},
    }, generated_at=10_000)
    assert malformed["shed"]["available"] is False
    assert malformed["shed"]["data"] is None, "partial Shed data must fail closed"

    # Exercise the route functions as well as the pure projector. Haven must
    # use the allowlist while /api/dashboard keeps its existing full contract.
    original_builder = bask_app._build_dashboard
    original_loader = bask_app.load_config
    original_shed = bask_app._shed_display
    try:
        bask_app._build_dashboard = lambda _cfg: full_dashboard
        bask_app.load_config = lambda: {}
        bask_app._shed_display = shed_display
        from fastapi.testclient import TestClient
        client = TestClient(bask_app.app)
        full_response = client.get("/api/dashboard")
        room_response = client.get("/api/room-dashboard")
        assert full_response.status_code == 200
        assert room_response.status_code == 200
        assert full_response.json()["ungrouped"][0]["name"] == "UNGROUPED-SENTINEL"
        routed = room_response.json()
    finally:
        bask_app._build_dashboard = original_builder
        bask_app.load_config = original_loader
        bask_app._shed_display = original_shed

    assert routed["bask"] == dto["bask"]
    assert routed["shed"] == dto["shed"]
    routed_encoded = json.dumps(routed, sort_keys=True)
    for sentinel in sentinels:
        assert sentinel not in routed_encoded, f"private sentinel escaped route: {sentinel}"
    assert "ungrouped" in full_dashboard and "thermostats" in full_dashboard
    print("Room-dashboard privacy allowlist tests passed.")


def node_eval(expression: str):
    if not shutil.which("node"):
        print("SKIP: node is not available for room-dashboard tests")
        return None
    script = f"""
const room = require({json.dumps(str(ROOT / 'frontend' / 'room.js'))});
const result = ({expression});
process.stdout.write(JSON.stringify(result));
"""
    return json.loads(subprocess.check_output(["node", "-e", script], text=True))


def fixture(*, shed_available=True, shed_data=True, counts=None):
    return {
        "bask": {"counts": counts or {"ok": 4, "warning": 0, "danger": 0,
                                       "stale": 0, "no_data": 0, "no_ranges": 0},
                 "enclosures": []},
        "shed": {
            "configured": True,
            "available": shed_available,
            "last_success": 9_940,
            "data": ({"summary": {"remaining": 3, "completed": 1, "overdue": 0, "total": 4},
                      "tasks": [{"title": "Feed", "animalName": "Example Animal", "taskType": "feeding"}],
                      "overdue": []} if shed_data else None),
        },
    }


def state(data):
    return node_eval(f"room.dashboardState({json.dumps(data)}, 10000)")


def main():
    assert_room_payload_allowlist()
    if not shutil.which("node"):
        print("SKIP: node is not available for room-dashboard tests")
        return

    healthy = state(fixture())
    assert healthy["shedStatus"] == "live"
    assert healthy["connection"]["label"] == "Live"
    assert healthy["carePart"] == "3 care tasks remaining"
    assert healthy["climatePart"] == "All configured climate targets look good"

    cached = state(fixture(shed_available=False))
    assert cached["shedStatus"] == "stale"
    assert cached["connection"]["label"] == "Shed offline"
    assert "last synced 1 minute ago" in cached["carePart"]
    assert "3 care tasks" not in cached["carePart"], "cached task counts were presented as current"

    cold = state(fixture(shed_available=False, shed_data=False))
    assert cold["shedStatus"] == "offline"
    assert "no successful sync" in cold["carePart"]

    incomplete = state(fixture(counts={"ok": 1, "warning": 0, "danger": 0,
                                               "stale": 1, "no_data": 1, "no_ranges": 1}))
    assert incomplete["uncertain"] == 3
    assert incomplete["connection"]["label"] == "Waiting on data"
    assert incomplete["climatePart"].startswith("3 enclosures are waiting")

    mixed = state(fixture(counts={"ok": 1, "warning": 1, "danger": 1,
                                          "stale": 1, "no_data": 0, "no_ranges": 1}))
    assert mixed["alert"] == 2 and mixed["uncertain"] == 2
    assert "2 climate alerts" in mixed["climatePart"]
    assert "2 waiting" in mixed["climatePart"]

    unconfigured = fixture()
    unconfigured["shed"] = {"configured": False, "available": False, "data": None,
                              "last_success": None}
    standalone = state(unconfigured)
    assert standalone["shedStatus"] == "unconfigured"
    assert standalone["connection"]["label"] == "Bask only"

    messages = node_eval(f"room.buildMessages({json.dumps(fixture(shed_available=False))})")
    joined = " ".join(messages)
    assert "cached care tasks are hidden" in joined
    assert "Example Animal" not in joined, "stale task details leaked into the live ticker"

    setup_incomplete = fixture(counts={"ok": 1, "warning": 0, "danger": 0,
                                               "stale": 0, "no_data": 0, "no_ranges": 1})
    setup_incomplete["bask"]["enclosures"] = [
        {"name": "Ready Habitat", "status": "ok"},
        {"name": "Setup Habitat", "status": "no_ranges"},
    ]
    setup_messages = " ".join(node_eval(f"room.buildMessages({json.dumps(setup_incomplete)})"))
    assert "Setup Habitat" in setup_messages
    assert "all 1 enclosures" not in setup_messages
    assert "every habitat" not in setup_messages.lower()

    cielo_error = fixture()
    cielo_error["bask"]["room_climate"] = {
        "configured": True, "online": True, "stale": False,
        "error": "cloud request failed", "temperature": 72, "humidity": 55,
    }
    climate_messages = " ".join(node_eval(f"room.buildMessages({json.dumps(cielo_error)})"))
    assert "comfy 72" not in climate_messages, "errored cached Cielo data was described as live"

    recovered = state(fixture())
    assert recovered["shedStatus"] == "live" and recovered["connection"]["label"] == "Live"
    print("Room-dashboard source-state tests passed.")


if __name__ == "__main__":
    main()
