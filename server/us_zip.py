"""
Resolve a US ZIP code to coordinates, offline.

Bask commonly runs on a LAN with no route out, and asking a geocoding service
where a keeper lives in order to work out when the sun sets there is both a
dependency and a disclosure. The table is bundled instead; see data/README.md
for its source and why it is the full table rather than a smaller one.

The file is streamed and scanned rather than parsed into a dictionary. Lookups
happen when a keeper saves their location, not on any hot path, and Bask runs on
boards where a few megabytes of resident dictionary is a real cost.
"""
from __future__ import annotations

import gzip
import re
from pathlib import Path

_DATA = Path(__file__).with_name("data") / "us_zip_centroids.csv.gz"
_ZIP = re.compile(r"^\d{5}$")


def normalize(value: str) -> str | None:
    """A bare five-digit ZIP, or None. Accepts ZIP+4 and surrounding spaces."""
    candidate = (value or "").strip()
    if "-" in candidate:
        candidate = candidate.split("-", 1)[0].strip()
    return candidate if _ZIP.match(candidate) else None


def table_available() -> bool:
    """
    Whether the bundled table shipped with this build.

    Verified today that .dockerignore's `data` rule matches only the top-level
    directory, so server/data survives the image build — but a packaging change
    that dropped it would otherwise turn every ZIP into "not a ZIP we can
    place", which sends the keeper hunting for a typo that is not there.
    """
    return _DATA.is_file()


def lookup(value: str) -> tuple[float, float] | None:
    """Centroid for a ZIP, or None when it is malformed or not a real ZCTA."""
    zip_code = normalize(value)
    if not zip_code:
        return None
    prefix = f"{zip_code},"
    try:
        with gzip.open(_DATA, "rt", encoding="utf-8") as handle:
            for line in handle:
                if not line.startswith(prefix):
                    # Sorted file: once past the target nothing later can match.
                    if line[:5] > zip_code:
                        return None
                    continue
                _, latitude, longitude = line.rstrip("\n").split(",")
                return float(latitude), float(longitude)
    except (OSError, ValueError):
        return None
    return None
