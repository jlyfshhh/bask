"""QC-10: an imported settings file must not be able to crash Bask or point it
at somewhere it should never connect."""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from server.app import _validate_import, validated_lan_host  # noqa: E402

BASE = {
    "sensors": [{"mac": "A4:C1:38:00:00:01", "name": "Warm Side"}],
    "enclosures": [{"id": "e1", "name": "Tank", "sensors": []}],
    "species": [{"id": "s1", "name": "Ball Python", "warm_min": 88, "warm_max": 92}],
}


def expect_rejected(payload: dict, why: str) -> None:
    try:
        _validate_import(payload)
    except ValueError:
        return
    raise AssertionError(f"should have been rejected: {why}")


def test_a_sound_file_is_accepted():
    out = _validate_import(dict(BASE))
    assert out["species"][0]["warm_min"] == 88
    assert out["sensors"][0]["mac"] == "A4:C1:38:00:00:01"


def test_non_numeric_species_ranges_are_rejected():
    # These reach the range evaluator as numbers. A string survived import and
    # raised a TypeError the next time the dashboard evaluated that enclosure.
    for bad in ("not-a-number", "88", True, [], {}):
        expect_rejected(
            {**BASE, "species": [{"id": "s1", "name": "Ball Python", "warm_min": bad}]},
            f"warm_min={bad!r}",
        )


def test_absurd_or_non_finite_species_ranges_are_rejected():
    for bad in (float("inf"), float("nan"), -500, 1000):
        expect_rejected(
            {**BASE, "species": [{"id": "s1", "name": "Ball Python", "cool_min": bad}]},
            f"cool_min={bad!r}",
        )


def test_unknown_species_fields_are_dropped_not_carried():
    out = _validate_import({**BASE, "species": [
        {"id": "s1", "name": "Ball Python", "warm_min": 88, "surprise": {"nested": "junk"}},
    ]})
    assert "surprise" not in out["species"][0]


def test_settings_are_held_to_the_same_bounds_as_the_live_endpoint():
    for bad in ({"stale_after_minutes": 0}, {"stale_after_minutes": 99999},
                {"low_battery_pct": -1}, {"day_start_hour": 24}, {"temp_unit": "K"},
                {"stale_after_minutes": "soon"}):
        expect_rejected({**BASE, "settings": bad}, f"settings={bad}")
    out = _validate_import({**BASE, "settings": {"temp_unit": "C", "low_battery_pct": 15}})
    assert out["settings"] == {"temp_unit": "C", "low_battery_pct": 15}


def test_unknown_settings_keys_do_not_survive():
    out = _validate_import({**BASE, "settings": {"temp_unit": "C", "evil": "yes"}})
    assert "evil" not in out["settings"]


def test_thermostat_addresses_must_be_on_the_home_network():
    for bad in ("169.254.169.254", "8.8.8.8", "127.0.0.1", "user:pw@192.168.1.5",
                "192.168.1.5:8080", "192.168.1.5/admin", "http://192.168.1.5",
                "192.168.1.5?x=1", "::1"):
        expect_rejected({**BASE, "thermostats": [{"ip": bad}]}, f"thermostat ip={bad}")
    out = _validate_import({**BASE, "thermostats": [{"ip": "192.168.1.50", "name": "Herpstat"}]})
    assert out["thermostats"][0]["ip"] == "192.168.1.50"


def test_the_notification_server_must_be_https():
    for bad in ("http://ntfy.sh", "ftp://x", "ntfy.sh", "https://user:pw@ntfy.sh"):
        expect_rejected({**BASE, "ntfy": {"server": bad, "enabled": True}}, f"ntfy server={bad}")
    out = _validate_import({**BASE, "ntfy": {"server": "https://ntfy.sh", "enabled": True}})
    assert out["ntfy"]["server"] == "https://ntfy.sh"


def test_an_imported_file_can_never_set_the_topic():
    # The topic is a credential: anyone holding it can push to the household's
    # phones. It is machine-local and is preserved from the running install.
    out = _validate_import({**BASE, "ntfy": {"server": "https://ntfy.sh", "topic": "stolen", "enabled": True}})
    assert "topic" not in out["ntfy"]


def test_hostnames_are_still_allowed():
    assert validated_lan_host("herpstat.local") == "herpstat.local"
    assert validated_lan_host("10.1.2.3") == "10.1.2.3"


def main() -> None:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as exc:
                print(f"FAIL {name}: {exc}")
                failures += 1
            except Exception as exc:  # noqa: BLE001
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
                failures += 1
    if failures:
        print(f"{failures} import-schema test(s) failed")
        raise SystemExit(1)
    print("Import schema tests passed")


if __name__ == "__main__":
    main()
