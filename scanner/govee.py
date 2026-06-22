"""Govee H5075 advertisement decoding.

The H5075 broadcasts manufacturer-specific data under company id 0xEC88. After
bleak strips the 2-byte company id, the payload layout is:

    byte 0      flag (0x00)
    bytes 1..3  packed temperature + humidity (big-endian, 24-bit)
    byte 4      battery percent (0..100)

The packed value uses the top bit (0x800000) as a sign flag; the remaining
23 bits encode:  temp_c = (raw // 1000) / 10 ,  humidity = (raw % 1000) / 10.

This is the same proven math the original build used; we additionally pull the
battery byte so the dashboard can warn before a sensor dies.
"""

GOVEE_COMPANY_ID = 0xEC88


def decode(manufacturer_data: dict[int, bytes]) -> tuple[float, float, int | None] | None:
    """Decode a manufacturer-data dict into (temp_c, humidity, battery|None).

    Prefers the known Govee company id, then falls back to any manufacturer
    entry that yields a plausible reading (some clones use a different id).
    """
    candidates: list[tuple[int, bytes]] = []
    preferred = manufacturer_data.get(GOVEE_COMPANY_ID)
    if preferred is not None:
        candidates.append((GOVEE_COMPANY_ID, preferred))
    for cid, data in manufacturer_data.items():
        if cid != GOVEE_COMPANY_ID:
            candidates.append((cid, data))

    for cid, data in candidates:
        if len(data) < 4:
            continue
        try:
            raw = int.from_bytes(data[1:4], "big")
            is_neg = bool(raw & 0x800000)
            raw &= 0x7FFFFF
            temp_c = (raw // 1000) / 10.0
            humidity = (raw % 1000) / 10.0
            if is_neg:
                temp_c = -temp_c
            if not (-20 <= temp_c <= 60 and 0 <= humidity <= 100):
                continue
            battery = None
            if cid == GOVEE_COMPANY_ID and len(data) >= 5 and 0 <= data[4] <= 100:
                battery = data[4]
            return temp_c, humidity, battery
        except Exception:
            continue
    return None


def is_govee(name: str | None, manufacturer_data: dict[int, bytes]) -> bool:
    if GOVEE_COMPANY_ID in manufacturer_data:
        return True
    n = name or ""
    return n.startswith("GVH") or n.startswith("Govee")


def c_to_f(c: float) -> float:
    return round(c * 9 / 5 + 32, 1)
