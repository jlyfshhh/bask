"""Judge humidity on how long it has been wrong, not on the last reading.

Every humidity source in a keeper's room is cyclic. A fogger runs for two
minutes and the enclosure sits above target for twenty; a misting spikes a
screen cage to 95% and it vents back to 60% within the hour. A snapshot test
against a range asks "what was it the instant I looked", which for a cycling
source is close to a coin toss — and the guides say so outright. ReptiFiles on
ball pythons: "occasional short dips and spikes outside of the given ranges are
not likely to be harmful."

So humidity alerts on the fraction of a recent window spent out of range.

Temperature deliberately does *not* use this. A heat source that fails is an
emergency, and delaying that alert by hours to smooth out a transient would
trade a nuisance for a risk to the animal. Cycling is normal for humidity and
abnormal for a thermostat.
"""

from collections import deque

# Long enough that a fogger or misting cycle is a small part of it, short
# enough that a genuine failure is reported the same evening.
WINDOW_SECONDS = 3 * 3600
# Alert once the majority of the window is out of range. A reading that is
# wrong half the time is a cycle; wrong nearly all the time is a shortfall.
MAX_OUT_OF_RANGE = 0.5
# Below this many samples the window cannot say anything useful, so the latest
# reading is used instead — a fresh install still alerts on its first night.
MIN_SAMPLES = 20


class HumidityWindow:
    """Per-sensor ring of recent (timestamp, humidity) readings."""

    def __init__(self, window_seconds: int = WINDOW_SECONDS):
        self._window = window_seconds
        self._by_mac: dict[str, deque] = {}

    def record(self, mac: str, humidity: float | None, now: float) -> None:
        if humidity is None:
            return
        samples = self._by_mac.setdefault(mac, deque())
        samples.append((now, float(humidity)))
        cutoff = now - self._window
        while samples and samples[0][0] < cutoff:
            samples.popleft()

    def forget(self, mac: str) -> None:
        self._by_mac.pop(mac, None)

    def samples(self, mac: str, now: float) -> list[float]:
        samples = self._by_mac.get(mac)
        if not samples:
            return []
        cutoff = now - self._window
        return [value for at, value in samples if at >= cutoff]

    def evaluate(self, mac: str, latest: float | None, low, high, now: float):
        """Return (ok, fraction_out_of_range, sample_count).

        Falls back to the latest reading while the window is still filling, so
        this can never be quieter than the snapshot test it replaces on a
        sensor that has only just started reporting.
        """
        values = self.samples(mac, now)
        if len(values) < MIN_SAMPLES:
            if latest is None:
                return True, 0.0, len(values)
            return _in_range(latest, low, high), 0.0, len(values)
        out = sum(1 for value in values if not _in_range(value, low, high))
        fraction = out / len(values)
        return fraction <= MAX_OUT_OF_RANGE, fraction, len(values)


def _in_range(value, low, high) -> bool:
    if low is not None and value < low:
        return False
    if high is not None and value > high:
        return False
    return True
