"""Sunrise/sunset, and the day-night decision that rides on it."""
import datetime as dt
import sys
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import solar
from server.solar import is_daytime


def minutes(moment):
    return moment.hour * 60 + moment.minute


class SunTimes(unittest.TestCase):
    # Published local times. A basking lamp does not care about seconds, but it
    # does care about being an hour out, which is what a sign or timezone slip
    # would produce.
    CASES = [
        ("New York", 40.7128, -74.0060, "America/New_York", (2026, 6, 21), "05:25", "20:31"),
        ("New York winter", 40.7128, -74.0060, "America/New_York", (2026, 12, 21), "07:16", "16:32"),
        ("Phoenix", 33.4484, -112.0740, "America/Phoenix", (2026, 6, 21), "05:18", "19:42"),
        ("London", 51.5074, -0.1278, "Europe/London", (2026, 6, 21), "04:43", "21:21"),
        ("Sydney", -33.8688, 151.2093, "Australia/Sydney", (2026, 6, 21), "07:00", "16:53"),
    ]

    def test_matches_published_tables(self):
        for name, lat, lon, tzname, (y, m, d), rise_s, set_s in self.CASES:
            with self.subTest(name):
                rise, set_ = solar.sun_times(dt.date(y, m, d), lat, lon, ZoneInfo(tzname))
                want_rise = int(rise_s[:2]) * 60 + int(rise_s[3:])
                want_set = int(set_s[:2]) * 60 + int(set_s[3:])
                self.assertLessEqual(abs(minutes(rise) - want_rise), 2, f"{name} sunrise")
                self.assertLessEqual(abs(minutes(set_) - want_set), 2, f"{name} sunset")

    def test_southern_hemisphere_seasons_are_not_flipped(self):
        oslo = ZoneInfo("Europe/Oslo")
        sydney = ZoneInfo("Australia/Sydney")
        _, north_set = solar.sun_times(dt.date(2026, 6, 21), 59.91, 10.75, oslo)
        _, south_set = solar.sun_times(dt.date(2026, 6, 21), -33.87, 151.21, sydney)
        # June is high summer in Oslo and midwinter in Sydney.
        self.assertGreater(minutes(north_set), 20 * 60)
        self.assertLess(minutes(south_set), 18 * 60)

    def test_no_sunrise_is_reported_not_invented(self):
        tromso = ZoneInfo("Europe/Oslo")
        self.assertIsNone(solar.sun_times(dt.date(2026, 6, 21), 69.6496, 18.9560, tromso))
        self.assertIsNone(solar.sun_times(dt.date(2026, 12, 21), 69.6496, 18.9560, tromso))

    def test_offsets_move_each_edge_independently(self):
        tz = ZoneInfo("America/New_York")
        rise, _ = solar.sun_times(dt.date(2026, 6, 21), 40.7128, -74.0060, tz)
        just_before = rise - dt.timedelta(minutes=5)
        self.assertFalse(solar.is_daylight(just_before, 40.7128, -74.0060))
        # Bringing sunrise forward by ten minutes puts that moment in daylight.
        self.assertTrue(solar.is_daylight(just_before, 40.7128, -74.0060, sunrise_offset_minutes=-10))


class DayNightDecision(unittest.TestCase):
    SOLAR = {
        "day_mode": "solar", "latitude": 40.7128, "longitude": -74.0060,
        "day_start_hour": 8, "day_end_hour": 20,
        "sunrise_offset_minutes": 0, "sunset_offset_minutes": 0,
    }

    def at(self, hour, minute=0, day=21, month=6):
        return dt.datetime(2026, month, day, hour, minute, tzinfo=ZoneInfo("America/New_York"))

    def test_solar_beats_the_clock(self):
        # 06:00 on the longest day is daylight, but outside the fixed 08:00 window.
        self.assertTrue(is_daytime(self.SOLAR, self.at(6)))
        self.assertFalse(is_daytime({**self.SOLAR, "day_mode": "fixed"}, self.at(6)))

    def test_winter_evening_is_night_though_the_clock_says_day(self):
        # 17:00 in December is after sunset, but inside the fixed window.
        self.assertFalse(is_daytime(self.SOLAR, self.at(17, month=12)))
        self.assertTrue(is_daytime({**self.SOLAR, "day_mode": "fixed"}, self.at(17, month=12)))

    def test_falls_back_to_fixed_hours_without_coordinates(self):
        settings = {**self.SOLAR, "latitude": None, "longitude": None}
        self.assertTrue(is_daytime(settings, self.at(9)))
        self.assertFalse(is_daytime(settings, self.at(6)))

    def test_falls_back_where_the_sun_does_not_set(self):
        # Solar mode must not strand a keeper above the Arctic Circle.
        settings = {**self.SOLAR, "latitude": 78.22, "longitude": 15.65}
        self.assertTrue(is_daytime(settings, self.at(9)))
        self.assertFalse(is_daytime(settings, self.at(23)))

    def test_existing_installs_are_untouched(self):
        # No day_mode at all is what every upgraded config looks like.
        self.assertTrue(is_daytime({"day_start_hour": 8, "day_end_hour": 20}, self.at(9)))
        self.assertFalse(is_daytime({"day_start_hour": 8, "day_end_hour": 20}, self.at(21)))
