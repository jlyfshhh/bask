"""End-to-end: does the Head Keeper gate actually hold on the running app?

Unit tests prove the hashing is sound. These prove the wiring — that reads stay
open, writes refuse without the key, unlock works, and the ways a keeper could
lock themselves out are closed.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp)
        (data / "config.json").write_text(
            (ROOT / "config.example.json").read_text(encoding="utf-8"), encoding="utf-8")
        os.environ["BASK_DATA_DIR"] = str(data)

        # Import order matters: db must create its tables before the app's
        # dashboard route reads from them, same as tests/smoke.py.
        from scanner import db
        db.init_db()

        from fastapi.testclient import TestClient
        from server.app import app
        from server import keeper

        client = TestClient(app)

        # ── With no key configured, nothing changes for anybody ──────────────
        assert client.get("/api/keeper").json() == {"configured": False, "unlocked": True}
        assert client.get("/api/dashboard").status_code == 200
        assert client.get("/api/species").status_code == 200
        # Writes still work: existing installs must not break on update.
        assert client.put("/api/settings", json={"temp_unit": "F"}).status_code == 200
        print("  open install behaves exactly as before")

        # ── Set a key ────────────────────────────────────────────────────────
        key = keeper.generate_key()
        r = client.post("/api/keeper/key", json={"key": key})
        assert r.status_code == 200, r.text
        assert client.get("/api/keeper").json()["configured"] is True
        stored = json.loads((data / "config.json").read_text())["keeper"]
        assert key not in json.dumps(stored), "the key must never be stored in the clear"
        print("  key set, and stored only as a hash")

        # ── The display stays open to everyone ───────────────────────────────
        anon = TestClient(app)   # no cookies
        for path in ("/api/health", "/api/dashboard", "/api/room-dashboard", "/api/species",
                     "/api/enclosures", "/api/sensors", "/api/thermostats",
                     "/api/cielo", "/api/vesync", "/api/update/status", "/api/discovered"):
            assert anon.get(path).status_code == 200, f"{path} should stay readable"
        print("  all 11 display reads stay open without the key")

        # ── Everything that changes setup is refused ─────────────────────────
        refused = [
            ("put", "/api/settings", {"temp_unit": "C"}),
            ("post", "/api/species", {"name": "x"}),
            ("post", "/api/enclosures", {"name": "x"}),
            ("post", "/api/sensors", {"mac": "AA:BB:CC:DD:EE:FF"}),
            ("post", "/api/pair", {}),
            ("post", "/api/unpair", {}),
            ("post", "/api/thermostats", {"ip": "10.0.0.1"}),
            ("post", "/api/cielo/connect", {"api_key": "x"}),
            ("post", "/api/vesync/connect", {"username": "a", "password": "b"}),
            ("post", "/api/ntfy", {"enabled": True}),
            ("post", "/api/config/import", {}),
            ("post", "/api/update", {"confirm": True}),
            ("delete", "/api/vesync", None),
            ("delete", "/api/cielo", None),
        ]
        for method, path, body in refused:
            call = getattr(anon, method)
            r = call(path, json=body) if body is not None else call(path)
            assert r.status_code == 401, f"{method.upper()} {path} returned {r.status_code}, expected 401"
        print(f"  {len(refused)} setup routes refused without the key")

        # ── The two reads that leak the ntfy topic are refused too ───────────
        for path in ("/api/ntfy", "/api/ntfy/qr", "/api/config/export"):
            assert anon.get(path).status_code == 401, f"{path} leaks setup detail; must be gated"
        print("  ntfy topic and config export are gated")

        # ── Unlocking ────────────────────────────────────────────────────────
        assert anon.post("/api/keeper/unlock", json={"key": "wrong-key-entirely"}).status_code == 401
        assert anon.put("/api/settings", json={"temp_unit": "C"}).status_code == 401
        assert anon.post("/api/keeper/unlock", json={"key": key}).status_code == 200
        assert anon.put("/api/settings", json={"temp_unit": "C"}).status_code == 200
        assert anon.get("/api/ntfy").status_code == 200
        print("  wrong key refused, right key unlocks")

        # ── You cannot change the key without proving the current one ────────
        stranger = TestClient(app)
        r = stranger.post("/api/keeper/key", json={"key": "a-brand-new-key"})
        assert r.status_code == 401, "a stranger must not be able to take over the key"
        r = stranger.post("/api/keeper/key", json={"key": "a-brand-new-key", "current": key})
        assert r.status_code == 200, r.text
        print("  key takeover blocked; rotation with the current key works")

        # ── Rotating the key invalidates the old session ─────────────────────
        assert anon.put("/api/settings", json={"temp_unit": "F"}).status_code == 401
        print("  old cookies die when the key is rotated")

        # ── Locking again, and removing the key ──────────────────────────────
        assert stranger.post("/api/keeper/lock").status_code == 200
        assert stranger.put("/api/settings", json={"temp_unit": "F"}).status_code == 401
        assert stranger.post("/api/keeper/unlock", json={"key": "a-brand-new-key"}).status_code == 200
        assert stranger.delete("/api/keeper/key").status_code == 200
        assert client.get("/api/keeper").json() == {"configured": False, "unlocked": True}
        assert TestClient(app).put("/api/settings", json={"temp_unit": "F"}).status_code == 200
        print("  lock, unlock, and removing the key all behave")

    print("Head Keeper API tests passed.")


if __name__ == "__main__":
    main()
