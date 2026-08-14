"""Herpstat source units must remain independent from Bask display units."""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scanner"))


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BASK_DATA_DIR"] = tmp
        from server import app
        app.db.init_db()

        raw = {
            "system": {"numberofoutputs": 1, "safetyrelay": "Normal"},
            "output1": {
                "outputnickname": "Warm side",
                "probereadingTEMP": 20.04,
                "currentsetting": 21.04,
                "poweroutput": 10,
                "errorcode": 0,
            },
        }
        status = app._parse_herpstat("192.168.1.50", raw, None, "C")
        assert status["temp_unit"] == "C"
        assert app.c_to_f(20.04) == 68.1, "Celsius-to-Fahrenheit output must stay one decimal"

        displayed = app._thermostat_for_display(status, "F")
        assert displayed["outputs"][0]["temp"] == 68.1
        assert displayed["outputs"][0]["setpoint"] == 69.9
        assert status["outputs"][0]["temp"] == 20.04, "display conversion mutated raw cache"

        # The full dashboard must apply the same one-decimal conversion to
        # Bluetooth readings and thermostat readings. This catches a second
        # c_to_f definition silently shadowing the established helper.
        original_readings = app.db.get_all_readings
        app.db.get_all_readings = lambda: [{
            "mac": "AA:BB:CC:DD:EE:FF",
            "temp_c": 20.04,
            "humidity": 50.0,
            "battery": 90,
            "rssi": -60,
            "updated_at": int(time.time()),
        }]
        app._thermostats["192.168.1.50"] = status
        try:
            dashboard = app._build_dashboard({
                "settings": {
                    "temp_unit": "F",
                    "low_battery_pct": 20,
                    "stale_after_minutes": 10,
                    "day_start_hour": 8,
                    "day_end_hour": 20,
                },
                "sensors": [{"mac": "AA:BB:CC:DD:EE:FF", "name": "Test sensor"}],
                "enclosures": [],
                "species": [],
                "thermostats": [{"ip": "192.168.1.50", "temp_unit": "C"}],
            })
        finally:
            app.db.get_all_readings = original_readings
            app._thermostats.clear()
        assert dashboard["ungrouped"][0]["temp"] == 68.1
        assert dashboard["thermostats"][0]["outputs"][0]["temp"] == 68.1
        assert dashboard["thermostats"][0]["outputs"][0]["setpoint"] == 69.9

        class NoCielo:
            def public_status(self):
                return {"configured": False}

        original_cielo, app.cielo = app.cielo, NoCielo()
        app._thermostats.clear()
        app._thermostats["192.168.1.50"] = status
        try:
            samples, _ = app._collect_climate({
                "sensors": [], "enclosures": [], "settings": {"temp_unit": "F"}
            })
        finally:
            app.cielo = original_cielo
            app._thermostats.clear()
        temp = next(sample for sample in samples if sample["metric"] == "temp_c")
        assert temp["value"] == 20.04, "Bask display preference reinterpreted Herpstat data"

    print("Herpstat source-unit tests passed.")


if __name__ == "__main__":
    main()
