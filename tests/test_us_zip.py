"""Offline ZIP lookup: the friendly way into the solar day/night setting."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import solar, us_zip


class Normalize(unittest.TestCase):
    def test_accepts_the_shapes_people_type(self):
        for raw, want in [("10001", "10001"), (" 02134 ", "02134"), ("10001-1234", "10001")]:
            self.assertEqual(us_zip.normalize(raw), want, raw)

    def test_rejects_everything_else(self):
        for raw in ["", "   ", "abcde", "1234", "123456", "1a2b3", None]:
            self.assertIsNone(us_zip.normalize(raw or ""), repr(raw))


class Lookup(unittest.TestCase):
    # Spread deliberately: a longitude error shows up as a clock error, and
    # Alaska and Hawaii are where a coarser table went badly wrong.
    KNOWN = {
        "10001": (40.75, -74.00),   # Manhattan
        "90210": (34.10, -118.42),  # Beverly Hills
        "99501": (61.22, -149.86),  # Anchorage
        "96813": (21.32, -157.85),  # Honolulu
        "33101": (25.78, -80.20),   # Miami
    }

    def test_places_known_zips(self):
        for zip_code, (lat, lon) in self.KNOWN.items():
            with self.subTest(zip_code):
                found = us_zip.lookup(zip_code)
                self.assertIsNotNone(found, zip_code)
                self.assertAlmostEqual(found[0], lat, delta=0.1)
                self.assertAlmostEqual(found[1], lon, delta=0.1)

    def test_unknown_and_malformed_return_nothing(self):
        for raw in ["00000", "99999", "abcde", "", "1234"]:
            self.assertIsNone(us_zip.lookup(raw), raw)

    def test_every_lookup_is_a_usable_coordinate(self):
        for zip_code in self.KNOWN:
            lat, lon = us_zip.lookup(zip_code)
            self.assertTrue(solar.valid_coordinates(lat, lon), zip_code)

    def test_a_zip_resolves_to_plausible_sun_times(self):
        # The point of the table: a ZIP has to produce sun times a keeper in
        # that place would recognise. Anchorage in midsummer has a very long day.
        import datetime as dt
        from zoneinfo import ZoneInfo
        lat, lon = us_zip.lookup("99501")
        rise, set_ = solar.sun_times(dt.date(2026, 6, 21), lat, lon, ZoneInfo("America/Anchorage"))
        day_length = (set_ - rise).total_seconds() / 3600
        self.assertGreater(day_length, 18, "Anchorage midsummer is a very long day")
        lat, lon = us_zip.lookup("33101")
        rise, set_ = solar.sun_times(dt.date(2026, 6, 21), lat, lon, ZoneInfo("America/New_York"))
        self.assertLess((set_ - rise).total_seconds() / 3600, 14, "Miami's is not")
