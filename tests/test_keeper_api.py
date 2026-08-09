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

        # ── QC-01: a settings export must not be a credential ────────────────
        stranger.post("/api/keeper/unlock", json={"key": "a-brand-new-key"})
        # Put a key and a private ntfy topic back in place to export against.
        stranger.post("/api/keeper/key", json={"key": "export-audit-key"})
        keeper_client = TestClient(app)
        assert keeper_client.post("/api/keeper/unlock", json={"key": "export-audit-key"}).status_code == 200
        # Enabling ntfy is what mints the private topic; there is no endpoint
        # that sets one, so read back what the app actually stored.
        assert keeper_client.post("/api/ntfy", json={"enabled": True}).status_code == 200
        stored = json.loads((data / "config.json").read_text(encoding="utf-8"))
        private_topic = stored.get("ntfy", {}).get("topic")
        assert private_topic, "the test needs a real topic to prove it is not exported"

        export = keeper_client.get("/api/config/export")
        assert export.status_code == 200, export.text
        body = export.text
        payload = export.json()

        for secret in (stored["keeper"]["salt"], stored["keeper"]["hash"],
                       stored["keeper"].get("session_secret", "\0"),
                       private_topic):
            assert secret not in body, "a portable export must contain no credential"
        assert "keeper" not in payload
        # And it must still be a useful backup.
        assert "sensors" in payload and "enclosures" in payload and "species" in payload
        print("  a settings export carries no keeper record, secret, or ntfy topic")

        # The old deterministic cookie, computed from everything an export holds.
        import hashlib
        forged = hashlib.sha256(
            f"{stored['keeper']['salt']}:{stored['keeper']['hash']}".encode()).hexdigest()
        forger = TestClient(app)
        forger.cookies.set("bask_keeper", forged)
        assert forger.put("/api/settings", json={"temp_unit": "C"}).status_code == 401
        forger.cookies.set("bask_keeper", f"v2.0.{forged}")
        assert forger.put("/api/settings", json={"temp_unit": "C"}).status_code == 401
        print("  a cookie derived from export contents is rejected")

        # ── QC-02: restoring a backup must not disable authentication ────────
        restored = keeper_client.post("/api/config/import", json=payload)
        assert restored.status_code == 200, restored.text
        after = json.loads((data / "config.json").read_text(encoding="utf-8"))
        assert after.get("keeper", {}).get("hash") == stored["keeper"]["hash"], \
            "restore must preserve the installed Head Keeper record"
        assert after.get("ntfy", {}).get("topic") == private_topic, \
            "restore must preserve the local ntfy topic"
        assert TestClient(app).put("/api/settings", json={"temp_unit": "C"}).status_code == 401, \
            "anonymous writes must still be refused after a restore"
        assert keeper_client.put("/api/settings", json={"temp_unit": "C"}).status_code == 200, \
            "the existing key must still work after a restore"
        print("  restoring a backup leaves authentication exactly as it was")

        # A legacy export that carries a keeper block must not install it.
        hostile = dict(payload)
        hostile["keeper"] = {"salt": "attacker", "hash": "attacker"}
        assert keeper_client.post("/api/config/import", json=hostile).status_code == 200
        after = json.loads((data / "config.json").read_text(encoding="utf-8"))
        assert after["keeper"]["hash"] == stored["keeper"]["hash"], \
            "an imported file must not be able to replace the Head Keeper record"
        print("  an imported keeper block is ignored")

        # ── QC-02: a corrupt record fails closed, not open ───────────────────
        broken = json.loads((data / "config.json").read_text(encoding="utf-8"))
        broken["keeper"] = {"salt": "only-half-a-record"}
        (data / "config.json").write_text(json.dumps(broken), encoding="utf-8")
        blocked = TestClient(app)
        assert blocked.put("/api/settings", json={"temp_unit": "C"}).status_code == 503
        status = blocked.get("/api/keeper").json()
        assert status["configured"] is True and status["unlocked"] is False
        assert "config.json" in status.get("problem", ""), "the error should say how to recover"
        assert blocked.get("/api/dashboard").status_code == 200, "reads stay open"
        print("  a corrupt keeper record refuses writes instead of opening them")

        # ── Upgrading an install that predates signed sessions ───────────────
        # This is the live migration path: a record with salt and hash but no
        # session_secret. If unlocking did not mint one, the keeper would be
        # locked out of their own dashboard by an update.
        legacy = json.loads((data / "config.json").read_text(encoding="utf-8"))
        legacy["keeper"] = {k: v for k, v in stored["keeper"].items() if k != "session_secret"}
        assert "session_secret" not in legacy["keeper"]
        (data / "config.json").write_text(json.dumps(legacy), encoding="utf-8")

        upgraded = TestClient(app)
        assert upgraded.put("/api/settings", json={"temp_unit": "C"}).status_code == 401
        assert upgraded.post("/api/keeper/unlock", json={"key": "export-audit-key"}).status_code == 200
        after_unlock = json.loads((data / "config.json").read_text(encoding="utf-8"))
        assert after_unlock["keeper"].get("session_secret"), "unlocking must mint a signing secret"
        assert upgraded.put("/api/settings", json={"temp_unit": "C"}).status_code == 200, \
            "the existing key must still work after upgrading"
        print("  an install predating signed sessions upgrades on first unlock")

    print("Head Keeper API tests passed.")


if __name__ == "__main__":
    main()
