"""Humidity is judged over a window; temperature deliberately is not."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.humidity_window import HumidityWindow, MIN_SAMPLES

BP_NIGHT = (80, 100)          # ReptiFiles ball python night humidity
CHAM_NIGHT = (75, 100)        # ReptiFiles panther chameleon night humidity


def fill(window, mac, values, start=0.0, step=60.0):
    for i, value in enumerate(values):
        window.record(mac, value, start + i * step)
    return start + (len(values) - 1) * step


def test_short_spike_does_not_alert():
    """A misting spike inside an otherwise fine night is not a problem."""
    w = HumidityWindow()
    # 170 minutes comfortably in range, 10 minutes spiked above the ceiling.
    now = fill(w, "a", [90.0] * 170 + [105.0] * 10)
    ok, fraction, n = w.evaluate("a", 105.0, *BP_NIGHT, now)
    assert ok, (fraction, n)
    assert fraction < 0.1
    # The snapshot test it replaces would have flagged this exact moment.
    assert not (BP_NIGHT[0] <= 105.0 <= BP_NIGHT[1])


def test_short_dip_does_not_alert():
    w = HumidityWindow()
    now = fill(w, "a", [85.0] * 170 + [70.0] * 10)
    ok, _, _ = w.evaluate("a", 70.0, *BP_NIGHT, now)
    assert ok


def test_sustained_shortfall_still_alerts():
    """The ball pythons sitting in the 70s all night must still be reported."""
    w = HumidityWindow()
    now = fill(w, "a", [74.0] * 180)
    ok, fraction, _ = w.evaluate("a", 74.0, *BP_NIGHT, now)
    assert not ok
    assert fraction == 1.0


def test_a_cycling_fogger_that_never_holds_still_alerts():
    """Pascal: brief fogger peaks over a night spent mostly far too dry."""
    # 14% of the window in range, matching the measured 103 of 720 minutes.
    w = HumidityWindow()
    now = fill(w, "a", [80.0] * 25 + [55.0] * 155)
    ok, fraction, _ = w.evaluate("a", 55.0, *CHAM_NIGHT, now)
    assert not ok
    assert fraction > 0.5


def test_half_the_window_is_tolerated_and_past_half_is_not():
    w = HumidityWindow()
    now = fill(w, "even", [90.0] * 90 + [50.0] * 90)
    ok, fraction, _ = w.evaluate("even", 50.0, *BP_NIGHT, now)
    assert ok and fraction == 0.5

    w2 = HumidityWindow()
    now2 = fill(w2, "worse", [90.0] * 80 + [50.0] * 100)
    ok2, fraction2, _ = w2.evaluate("worse", 50.0, *BP_NIGHT, now2)
    assert not ok2 and fraction2 > 0.5


def test_falls_back_to_the_latest_reading_while_filling():
    """A new sensor must not be silently exempt from alerting."""
    w = HumidityWindow()
    now = fill(w, "new", [50.0] * (MIN_SAMPLES - 1))
    ok, _, n = w.evaluate("new", 50.0, *BP_NIGHT, now)
    assert not ok, "a brand-new sensor well out of range should still alert"
    assert n < MIN_SAMPLES


def test_readings_older_than_the_window_are_dropped():
    w = HumidityWindow(window_seconds=600)
    w.record("a", 20.0, 0.0)
    w.record("a", 90.0, 10_000.0)
    assert w.samples("a", 10_000.0) == [90.0]


def test_missing_humidity_is_not_recorded_as_a_value():
    w = HumidityWindow()
    for i in range(30):
        w.record("a", None, float(i * 60))
    assert w.samples("a", 1800.0) == []
    ok, _, _ = w.evaluate("a", None, *BP_NIGHT, 1800.0)
    assert ok, "no data is not a violation"


def test_an_unpaired_sensor_is_forgotten():
    w = HumidityWindow()
    fill(w, "a", [90.0] * 30)
    w.forget("a")
    assert w.samples("a", 1800.0) == []


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); passed += 1
            print("  " + name[len('test_'):].replace('_', ' '))
    print(f"Humidity window tests passed ({passed}).")
