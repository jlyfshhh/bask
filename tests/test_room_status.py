"""QC-17: Haven must never present stale or incomplete data as fully live."""
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent


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
