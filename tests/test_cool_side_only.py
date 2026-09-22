"""An enclosure with only a cool-side sensor is judged, not broken.

Three warm-side sensors were physically moved into new enclosures ahead of the
cool-side-only redesign, leaving six enclosures with a single sensor. These pin
the behaviour that makes that safe without a code change.
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.app import analyze_enclosure, _temp_window, _humidity_window

GARG = {
    "id": "sp-garg", "name": "Gargoyle Gecko",
    "warm_temp_min": 75.0, "warm_temp_max": 82.0,
    "cool_temp_min": 70.0, "cool_temp_max": 78.0,
    "humidity_min": 50.0, "humidity_max": 80.0,
    "night_warm_temp_min": 65.0, "night_warm_temp_max": 75.0,
    "night_cool_temp_min": 65.0, "night_cool_temp_max": 75.0,
    "night_humidity_min": 70.0, "night_humidity_max": 100.0,
}

def f_to_c(f):
    return (f - 32) * 5 / 9

def run(mac, temp_f, humidity, position="Cool Side"):
    now = time.time()
    enc = {"id": "e1", "name": "Onions", "species_id": "sp-garg",
           "sensors": [{"mac": mac, "position": position}]}
    readings = {mac: {"temp_c": f_to_c(temp_f), "humidity": humidity,
                      "updated_at": now, "battery": 90, "rssi": -60}}
    return analyze_enclosure(enc, readings, {mac: {"name": "Onions Cool Side"}},
                             "F", now - 600, now, {"sp-garg": GARG}, 20, True)

def soak(window, mac, value, lo, hi, minutes=180):
    """Fill a range window so an out-of-range reading can actually trip."""
    base = time.time() - minutes * 60
    for i in range(minutes):
        window.record(mac, value, base + i * 60)
    return window.evaluate(mac, value, lo, hi, time.time())


def test_single_cool_sensor_reads_ok():
    """No warm sensor must not count as a warm violation."""
    out = run("AA:00:00:00:00:01", 74.0, 60.0)
    assert out["status"] == "ok", out["status"]
    assert out["warm"] is None
    assert out["cool"] is not None


def test_single_cool_sensor_still_catches_a_cold_room():
    """The cool side is the safety signal, so it must still alert."""
    mac = "AA:00:00:00:00:02"
    ok, fraction, _n = soak(_temp_window, mac, f_to_c(62.0), f_to_c(70.0), f_to_c(78.0))
    assert not ok, "a sustained cold cool side should not read as ok"
    assert fraction > 0.5
    assert run(mac, 62.0, 60.0)["status"] in {"warning", "danger"}


def test_humidity_is_read_from_the_cool_sensor():
    """Humidity only ever reads from `cool`, so a lone sensor must sit there.

    Naming the surviving sensor "Warm Side" would silently stop humidity from
    being evaluated at all - the failure mode this test exists to catch.
    """
    mac = "AA:00:00:00:00:03"
    # 76 F sits inside BOTH the warm and cool bands, so the only thing that can
    # differ between the two positions below is whether humidity gets checked.
    ok, _fraction, _n = soak(_humidity_window, mac, 20.0, 50.0, 80.0)
    assert not ok, "sustained dry air should trip the humidity window"
    assert run(mac, 76.0, 20.0)["status"] in {"warning", "danger"}

    # Same reading, same sensor, but filed as the warm side: humidity unchecked.
    assert run(mac, 76.0, 20.0, position="Warm Side")["status"] == "ok"


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); passed += 1
            print("  " + name[len('test_'):].replace('_', ' '))
    print(f"Cool-side-only tests passed ({passed}).")
