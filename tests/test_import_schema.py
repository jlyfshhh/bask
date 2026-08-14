"""QC-10: imported settings stay safe and preserve the current Bask schema."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from server.app import _portable_export, _validate_import, validated_lan_host  # noqa: E402

RANGE_FIELDS = (
    "warm_temp_min", "warm_temp_max", "cool_temp_min", "cool_temp_max",
    "humidity_min", "humidity_max",
    "night_warm_temp_min", "night_warm_temp_max",
    "night_cool_temp_min", "night_cool_temp_max",
    "night_humidity_min", "night_humidity_max",
)

BASE = {
    "sensors": [{"mac": "A4:C1:38:00:00:01", "name": "Warm Side"}],
    "enclosures": [{"id": "e1", "name": "Tank", "sensors": []}],
    "species": [{"id": "s1", "name": "Ball Python", "warm_temp_min": 88, "warm_temp_max": 92}],
}


def expect_rejected(payload: dict, why: str) -> None:
    try:
        _validate_import(payload)
    except ValueError:
        return
    raise AssertionError(f"should have been rejected: {why}")


def test_a_sound_file_is_accepted():
    out = _validate_import(dict(BASE))
    assert out["species"][0]["warm_temp_min"] == 88
    assert out["sensors"][0]["mac"] == "A4:C1:38:00:00:01"


def test_current_example_ranges_survive_export_and_restore():
    """Use the shipped schema, not a second hand-written mock of old fields."""
    current = json.loads((ROOT / "config.example.json").read_text())
    restored = _validate_import(_portable_export(current))
    before = {species["id"]: species for species in current["species"]}
    after = {species["id"]: species for species in restored["species"]}
    assert after.keys() == before.keys()
    for species_id, original in before.items():
        for field in RANGE_FIELDS:
            assert after[species_id][field] == original.get(field), (
                f"{species_id}.{field} changed during export→restore: "
                f"{original.get(field)!r} → {after[species_id][field]!r}"
            )


def test_non_numeric_species_ranges_are_rejected():
    # These reach the range evaluator as numbers. A string survived import and
    # raised a TypeError the next time the dashboard evaluated that enclosure.
    for bad in ("not-a-number", "88", True, [], {}):
        expect_rejected(
            {**BASE, "species": [{"id": "s1", "name": "Ball Python", "warm_temp_min": bad}]},
            f"warm_temp_min={bad!r}",
        )


def test_absurd_or_non_finite_species_ranges_are_rejected():
    for bad in (float("inf"), float("nan"), -500, 1000):
        expect_rejected(
            {**BASE, "species": [{"id": "s1", "name": "Ball Python", "cool_temp_min": bad}]},
            f"cool_temp_min={bad!r}",
        )


def test_unknown_species_fields_are_dropped_not_carried():
    out = _validate_import({**BASE, "species": [
        {"id": "s1", "name": "Ball Python", "warm_temp_min": 88, "surprise": {"nested": "junk"}},
    ]})
    assert "surprise" not in out["species"][0]


def test_duplicate_identifiers_are_rejected_after_normalization():
    for key, entries in (
        ("enclosures", [
            {"id": "duplicate", "name": "One", "sensors": []},
            {"id": "duplicate", "name": "Two", "sensors": []},
        ]),
        ("species", [
            {"id": "duplicate", "name": "One"},
            {"id": "duplicate", "name": "Two"},
        ]),
        ("sensors", [
            {"mac": "aa:bb:cc:dd:ee:ff", "name": "One"},
            {"mac": "AA:BB:CC:DD:EE:FF", "name": "Two"},
        ]),
    ):
        payload = {**BASE, key: entries}
        expect_rejected(payload, f"duplicate normalized {key}")

    # IDs are capped at 64 characters, so distinct uploaded strings that
    # collide after normalization are duplicates too.
    prefix = "x" * 64
    expect_rejected({**BASE, "enclosures": [
        {"id": prefix + "a", "name": "One", "sensors": []},
        {"id": prefix + "b", "name": "Two", "sensors": []},
    ]}, "IDs colliding after length cap")


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


def test_thermostat_source_unit_is_validated_and_portable():
    out = _validate_import({
        **BASE,
        "settings": {"temp_unit": "C"},
        "thermostats": [{"ip": "192.168.1.50", "name": "Herpstat", "temp_unit": "F"}],
    })
    assert out["thermostats"][0]["temp_unit"] == "F"
    expect_rejected({
        **BASE,
        "thermostats": [{"ip": "192.168.1.50", "temp_unit": "Kelvin"}],
    }, "invalid Herpstat source unit")

    restored = _validate_import(_portable_export({
        **BASE,
        "settings": {"temp_unit": "C"},
        "thermostats": [{"ip": "192.168.1.50", "temp_unit": "F", "enabled": True}],
    }))
    assert restored["thermostats"][0]["temp_unit"] == "F"


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
