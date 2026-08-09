"""QC-19: config writes are atomic, revisioned, and concurrency-safe."""
import concurrent.futures
import json
import os
import stat
import sys
import tempfile
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp)
        config_path = data / "config.json"
        config_path.write_text(
            (ROOT / "config.example.json").read_text(encoding="utf-8"), encoding="utf-8")
        os.chmod(config_path, 0o644)  # prove the first replacement makes it private
        os.environ["BASK_DATA_DIR"] = str(data)

        from fastapi.testclient import TestClient
        from server.app import _normalise_config, app, load_config, mutate_config

        client = TestClient(app)

        try:
            _normalise_config({"_revision": "damaged"})
        except ValueError:
            pass
        else:
            raise AssertionError("a damaged revision must fail closed, not reset to zero")

        def revision(target=client) -> int:
            response = target.get("/api/config/revision")
            assert response.status_code == 200, response.text
            value = response.json()["revision"]
            assert response.headers["X-Bask-Revision"] == str(value)
            return value

        def change(method: str, path: str, body, expected: int, target=client):
            return target.request(
                method, path, json=body,
                headers={"X-Bask-Revision": str(expected)},
            )

        assert revision() == 0, "an install without a revision starts at zero"
        missing = client.put("/api/settings", json={"temp_unit": "C"})
        assert missing.status_code == 428, missing.text
        malformed = client.put(
            "/api/settings", json={"temp_unit": "C"},
            headers={"X-Bask-Revision": "not-a-number"})
        assert malformed.status_code == 400, malformed.text
        assert revision() == 0, "rejected writes must not advance the revision"

        # New public identifiers are unguessable UUIDs rather than clock values.
        before = revision()
        made_species = change("POST", "/api/species", {"name": "Concurrency Gecko"}, before)
        assert made_species.status_code == 200, made_species.text
        uuid.UUID(made_species.json()["id"])
        after_species = int(made_species.headers["X-Bask-Revision"])
        assert after_species == before + 1
        assert made_species.headers["X-Bask-Revision-Applied"] == "true"

        snapshot = client.get("/api/manage-snapshot")
        assert snapshot.status_code == 200, snapshot.text
        assert snapshot.json()["revision"] == after_species
        assert snapshot.json()["species"] == load_config()["species"]

        made_enclosure = change(
            "POST", "/api/enclosures",
            {"name": "Revision Test", "species_id": made_species.json()["id"], "sensors": []},
            after_species,
        )
        assert made_enclosure.status_code == 200, made_enclosure.text
        uuid.UUID(made_enclosure.json()["id"])
        assert int(made_enclosure.headers["X-Bask-Revision"]) == after_species + 1

        # Two devices edited the same snapshot. The second receives 409 and
        # cannot overwrite the value the first device already saved.
        shared = revision()
        first = change("PUT", "/api/settings", {"temp_unit": "C"}, shared)
        assert first.status_code == 200, first.text
        stale = change("PUT", "/api/settings", {"temp_unit": "F"}, shared)
        assert stale.status_code == 409, stale.text
        assert int(stale.headers["X-Bask-Revision"]) == shared + 1
        assert load_config()["settings"]["temp_unit"] == "C"

        # Reorder is a permutation, never a lossy filter. Missing, duplicate,
        # or unknown IDs leave both data and revision untouched.
        original_ids = [item["id"] for item in load_config()["enclosures"]]
        reorder_revision = revision()
        bad_orders = [
            original_ids[:-1],
            original_ids[:-1] + [original_ids[0]],
            original_ids[:-1] + ["00000000-0000-0000-0000-000000000000"],
        ]
        for order in bad_orders:
            rejected = change("PUT", "/api/enclosures/reorder", {"order": order}, reorder_revision)
            assert rejected.status_code == 409, rejected.text
            assert [item["id"] for item in load_config()["enclosures"]] == original_ids
            assert revision() == reorder_revision
        accepted = change(
            "PUT", "/api/enclosures/reorder", {"order": list(reversed(original_ids))},
            reorder_revision)
        assert accepted.status_code == 200, accepted.text
        assert [item["id"] for item in load_config()["enclosures"]] == list(reversed(original_ids))

        # Internal/background mutations without a browser precondition still
        # serialize their complete read-modify-write cycle.
        worker_count = 32
        errors = []

        def increment() -> None:
            try:
                def update(cfg: dict) -> None:
                    current = cfg["settings"].get("concurrent_counter", 0)
                    cfg["settings"]["concurrent_counter"] = current + 1
                mutate_config(None, update)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: increment(), range(worker_count)))
        assert not errors, errors
        assert load_config()["settings"]["concurrent_counter"] == worker_count

        # Two HTTP writers released together against one revision: exactly one
        # succeeds and the other conflicts, so distinct-field edits cannot race
        # through stale snapshots and erase one another.
        shared = revision()
        prior = load_config()["settings"].copy()
        gate = threading.Barrier(2)

        def simultaneous(field: str, value: int):
            other = TestClient(app)
            gate.wait()
            return change("PUT", "/api/settings", {field: value}, shared, other)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(simultaneous, "stale_after_minutes", 41),
                pool.submit(simultaneous, "low_battery_pct", 37),
            ]
            responses = [future.result() for future in futures]
        assert sorted(response.status_code for response in responses) == [200, 409]
        latest = load_config()["settings"]
        changed = (latest["stale_after_minutes"] == 41) + (latest["low_battery_pct"] == 37)
        assert changed == 1
        if latest["stale_after_minutes"] != 41:
            assert latest["stale_after_minutes"] == prior["stale_after_minutes"]
        if latest["low_battery_pct"] != 37:
            assert latest["low_battery_pct"] == prior["low_battery_pct"]

        # Portable restore is one revisioned replacement, keeps its pre-import
        # recovery file private, and leaves no half-write temp artifacts.
        exported = client.get("/api/config/export")
        assert exported.status_code == 200, exported.text
        import_revision = revision()
        restored = change("POST", "/api/config/import", exported.json(), import_revision)
        assert restored.status_code == 200, restored.text
        assert revision() == import_revision + 1
        backups = list(data.glob("config.json.bak-*-preimport"))
        assert len(backups) == 1
        assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
        assert not list(data.glob(".config.json.*.tmp"))

        # The no-build frontend must participate in the same contract.
        frontend = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        assert 'CONFIG_REVISION_HEADER = "X-Bask-Revision"' in frontend
        assert "recoverConfigConflict" in frontend
        assert "res.status === 409" in frontend

    print("Config concurrency tests passed.")


if __name__ == "__main__":
    main()
