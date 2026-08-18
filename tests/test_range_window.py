"""Readings are judged over a window of time out of range, not by average."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.range_window import RangeWindow, MIN_SAMPLES, TEMPERATURE_WINDOW_SECONDS

BP_NIGHT = (80, 100)          # ReptiFiles ball python night humidity
CHAM_NIGHT = (75, 100)        # ReptiFiles panther chameleon night humidity


def fill(window, mac, values, start=0.0, step=60.0):
    for i, value in enumerate(values):
        window.record(mac, value, start + i * step)
    return start + (len(values) - 1) * step


def test_short_spike_does_not_alert():
    """A misting spike inside an otherwise fine night is not a problem."""
    w = RangeWindow()
    # 170 minutes comfortably in range, 10 minutes spiked above the ceiling.
    now = fill(w, "a", [90.0] * 170 + [105.0] * 10)
    ok, fraction, n = w.evaluate("a", 105.0, *BP_NIGHT, now)
    assert ok, (fraction, n)
    assert fraction < 0.1
    # The snapshot test it replaces would have flagged this exact moment.
    assert not (BP_NIGHT[0] <= 105.0 <= BP_NIGHT[1])


def test_short_dip_does_not_alert():
    w = RangeWindow()
    now = fill(w, "a", [85.0] * 170 + [70.0] * 10)
    ok, _, _ = w.evaluate("a", 70.0, *BP_NIGHT, now)
    assert ok


def test_sustained_shortfall_still_alerts():
    """The ball pythons sitting in the 70s all night must still be reported."""
    w = RangeWindow()
    now = fill(w, "a", [74.0] * 180)
    ok, fraction, _ = w.evaluate("a", 74.0, *BP_NIGHT, now)
    assert not ok
    assert fraction == 1.0


def test_a_cycling_fogger_that_never_holds_still_alerts():
    """Pascal: brief fogger peaks over a night spent mostly far too dry."""
    # 14% of the window in range, matching the measured 103 of 720 minutes.
    w = RangeWindow()
    now = fill(w, "a", [80.0] * 25 + [55.0] * 155)
    ok, fraction, _ = w.evaluate("a", 55.0, *CHAM_NIGHT, now)
    assert not ok
    assert fraction > 0.5


def test_half_the_window_is_tolerated_and_past_half_is_not():
    w = RangeWindow()
    now = fill(w, "even", [90.0] * 90 + [50.0] * 90)
    ok, fraction, _ = w.evaluate("even", 50.0, *BP_NIGHT, now)
    assert ok and fraction == 0.5

    w2 = RangeWindow()
    now2 = fill(w2, "worse", [90.0] * 80 + [50.0] * 100)
    ok2, fraction2, _ = w2.evaluate("worse", 50.0, *BP_NIGHT, now2)
    assert not ok2 and fraction2 > 0.5


def test_falls_back_to_the_latest_reading_while_filling():
    """A new sensor must not be silently exempt from alerting."""
    w = RangeWindow()
    now = fill(w, "new", [50.0] * (MIN_SAMPLES - 1))
    ok, _, n = w.evaluate("new", 50.0, *BP_NIGHT, now)
    assert not ok, "a brand-new sensor well out of range should still alert"
    assert n < MIN_SAMPLES


def test_readings_older_than_the_window_are_dropped():
    w = RangeWindow(window_seconds=600)
    w.record("a", 20.0, 0.0)
    w.record("a", 90.0, 10_000.0)
    assert w.samples("a", 10_000.0) == [90.0]


def test_missing_humidity_is_not_recorded_as_a_value():
    w = RangeWindow()
    for i in range(30):
        w.record("a", None, float(i * 60))
    assert w.samples("a", 1800.0) == []
    ok, _, _ = w.evaluate("a", None, *BP_NIGHT, 1800.0)
    assert ok, "no data is not a violation"


def test_an_unpaired_sensor_is_forgotten():
    w = RangeWindow()
    fill(w, "a", [90.0] * 30)
    w.forget("a")
    assert w.samples("a", 1800.0) == []


BP_WARM = (84, 92)   # Bask warm-side air range for a ball python


def test_an_average_would_hide_what_time_in_range_reports():
    """Half an hour hot and half an hour cold averages to a comfortable number."""
    w = RangeWindow(window_seconds=TEMPERATURE_WINDOW_SECONDS)
    values = [98.0] * 30 + [78.0] * 30
    now = fill(w, "swing", values)
    ok, fraction, _ = w.evaluate("swing", values[-1], *BP_WARM, now)
    assert sum(values) / len(values) == 88.0, "the average sits mid-range"
    assert BP_WARM[0] <= 88.0 <= BP_WARM[1], "so an average-based test would pass"
    assert not ok, "but the animal spent half an hour at 98F and this must report"
    assert fraction == 1.0


def test_a_brief_temperature_swing_is_absorbed():
    """A door opened, or a lamp ramping — minutes, not a fault."""
    w = RangeWindow(window_seconds=TEMPERATURE_WINDOW_SECONDS)
    now = fill(w, "blip", [88.0] * 52 + [80.0] * 8)
    ok, _, _ = w.evaluate("blip", 80.0, *BP_WARM, now)
    assert ok


def test_a_sustained_heating_fault_still_reports_within_the_hour():
    w = RangeWindow(window_seconds=TEMPERATURE_WINDOW_SECONDS)
    now = fill(w, "fault", [88.0] * 20 + [105.0] * 40)
    ok, fraction, _ = w.evaluate("fault", 105.0, *BP_WARM, now)
    assert not ok and fraction > 0.5


def test_the_temperature_window_is_shorter_than_the_humidity_one():
    assert TEMPERATURE_WINDOW_SECONDS < 3 * 3600


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); passed += 1
            print("  " + name[len('test_'):].replace('_', ' '))
    print(f"Range window tests passed ({passed}).")
