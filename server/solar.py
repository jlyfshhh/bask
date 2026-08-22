"""
Sunrise and sunset for a location, computed locally.

Bask runs on a LAN that often has no route out, and a keeper's coordinates are
not something to hand to a third party in order to find out when the sun sets.
The NOAA solar position algorithm is a few dozen lines of arithmetic, needs no
data beyond the date and the coordinates, and lands within about a minute of the
published tables — far finer than a basking lamp cares about.

Returns None at latitudes and dates where the sun does not rise or set at all.
Callers must have an answer for that rather than assuming a time exists.
"""
from __future__ import annotations

import datetime as _dt
import math

# Sunrise is defined at the moment the sun's upper limb touches the horizon,
# which with refraction puts the centre 0.833 degrees below it.
_ZENITH = 90.833


def _julian_day(date: _dt.date) -> float:
    y, m, d = date.year, date.month, date.day
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + d + b - 1524.5


def _sun_events_minutes(date: _dt.date, latitude: float, longitude: float) -> tuple[float, float] | None:
    """Sunrise and sunset as minutes from UTC midnight, or None if neither occurs."""
    century = (_julian_day(date) - 2451545.0) / 36525.0

    mean_long = (280.46646 + century * (36000.76983 + century * 0.0003032)) % 360.0
    mean_anom = 357.52911 + century * (35999.05029 - 0.0001537 * century)
    eccentricity = 0.016708634 - century * (0.000042037 + 0.0000001267 * century)

    anom_rad = math.radians(mean_anom)
    centre = (
        math.sin(anom_rad) * (1.914602 - century * (0.004817 + 0.000014 * century))
        + math.sin(2 * anom_rad) * (0.019993 - 0.000101 * century)
        + math.sin(3 * anom_rad) * 0.000289
    )
    true_long = mean_long + centre
    apparent_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(125.04 - 1934.136 * century))

    mean_obliquity = (
        23.0 + (26.0 + ((21.448 - century * (46.815 + century * (0.00059 - century * 0.001813)))) / 60.0) / 60.0
    )
    obliquity = mean_obliquity + 0.00256 * math.cos(math.radians(125.04 - 1934.136 * century))

    declination = math.degrees(
        math.asin(math.sin(math.radians(obliquity)) * math.sin(math.radians(apparent_long)))
    )

    var_y = math.tan(math.radians(obliquity / 2)) ** 2
    eq_time = 4 * math.degrees(
        var_y * math.sin(2 * math.radians(mean_long))
        - 2 * eccentricity * math.sin(anom_rad)
        + 4 * eccentricity * var_y * math.sin(anom_rad) * math.cos(2 * math.radians(mean_long))
        - 0.5 * var_y * var_y * math.sin(4 * math.radians(mean_long))
        - 1.25 * eccentricity * eccentricity * math.sin(2 * anom_rad)
    )

    lat_rad = math.radians(latitude)
    dec_rad = math.radians(declination)
    cos_ha = (
        math.cos(math.radians(_ZENITH)) / (math.cos(lat_rad) * math.cos(dec_rad))
        - math.tan(lat_rad) * math.tan(dec_rad)
    )
    # Polar day or polar night: no crossing of the horizon to report.
    if cos_ha > 1 or cos_ha < -1:
        return None
    hour_angle = math.degrees(math.acos(cos_ha))

    solar_noon = 720 - 4 * longitude - eq_time
    return solar_noon - 4 * hour_angle, solar_noon + 4 * hour_angle


def sun_times(
    date: _dt.date,
    latitude: float,
    longitude: float,
    tzinfo: _dt.tzinfo,
) -> tuple[_dt.datetime, _dt.datetime] | None:
    """Local sunrise and sunset for `date`, or None where the sun does not set."""
    events = _sun_events_minutes(date, latitude, longitude)
    if events is None:
        return None
    midnight_utc = _dt.datetime(date.year, date.month, date.day, tzinfo=_dt.timezone.utc)
    rise, set_ = (
        (midnight_utc + _dt.timedelta(minutes=minutes)).astimezone(tzinfo) for minutes in events
    )
    return rise, set_


def is_daylight(
    moment: _dt.datetime,
    latitude: float,
    longitude: float,
    sunrise_offset_minutes: int = 0,
    sunset_offset_minutes: int = 0,
) -> bool | None:
    """
    Whether `moment` falls between sunrise and sunset at that location.

    Offsets shift each edge, which is how real lighting is usually run: on a
    little after first light, off a little after dusk. Returns None when the sun
    neither rises nor sets, leaving the decision to the caller.
    """
    tzinfo = moment.tzinfo or _dt.timezone.utc
    # The local calendar date is what "today's sunrise" means to a keeper.
    times = sun_times(moment.date(), latitude, longitude, tzinfo)
    if times is None:
        return None
    rise, set_ = times
    rise += _dt.timedelta(minutes=sunrise_offset_minutes)
    set_ += _dt.timedelta(minutes=sunset_offset_minutes)
    return rise <= moment < set_


def valid_coordinates(latitude: float, longitude: float) -> bool:
    return -90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0


def is_daytime(settings, now: _dt.datetime | None = None) -> bool:
    """
    True when it is currently day, by the clock or by the sun.

    Solar mode falls back to the fixed hours whenever it cannot answer — no
    coordinates set, or a latitude and date where the sun neither rises nor
    sets. A keeper who turns it on and then travels past the Arctic Circle
    should get sensible ranges, not an exception in the climate loop.
    """
    moment = now or _dt.datetime.now().astimezone()
    if settings.get("day_mode") == "solar":
        latitude, longitude = settings.get("latitude"), settings.get("longitude")
        if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
            daylight = is_daylight(
                moment,
                float(latitude),
                float(longitude),
                int(settings.get("sunrise_offset_minutes") or 0),
                int(settings.get("sunset_offset_minutes") or 0),
            )
            if daylight is not None:
                return daylight
    start = settings.get("day_start_hour", 8)
    end = settings.get("day_end_hour", 20)
    h = moment.hour
    return start <= h < end if start <= end else (h >= start or h < end)
