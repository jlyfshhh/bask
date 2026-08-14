"""Durable, debounced, bounded phone-alert delivery."""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from server.alerts import (  # noqa: E402
    MAX_ENCLOSURES,
    RETRY_MAX_SECONDS,
    AlertStateStore,
    delivery_failed,
    delivery_status,
    initial_state,
    next_due,
    observe,
)


def item(enclosure_id: str, status: str, marker: str = "") -> dict:
    return {
        "id": enclosure_id,
        "status": status,
        "alert_title": "Bask alert",
        "alert_body": f"{enclosure_id} needs attention {marker}",
        "alert_tags": "warning",
        "alert_priority": "high",
        "recovery_title": "Bask",
        "recovery_body": f"{enclosure_id} recovered",
        "recovery_tags": "white_check_mark",
        "recovery_priority": "",
    }


def test_debounce_flapping_and_retry() -> None:
    state = observe(initial_state(), [item("one", "ok")], enabled=True, now=0)
    assert next_due(state, now=10_000) is None

    state = observe(state, [item("one", "warning")], enabled=True, now=10)
    state = observe(state, [item("one", "warning")], enabled=True, now=129)
    assert next_due(state, now=129) is None
    state = observe(state, [item("one", "warning")], enabled=True, now=130)
    event = next_due(state, now=130)
    assert event and event["kind"] == "alert"

    state = delivery_failed(state, event["id"], now=130)
    assert next_due(state, now=189) is None
    assert next_due(state, now=190)["id"] == event["id"]
    status = delivery_status(state, enabled=True)
    assert status["pending"] == 1 and status["retrying"] is True
    assert "http" not in status["last_error"].lower()

    clock = 190
    previous_delay = 60
    for _ in range(12):
        due = next_due(state, now=clock)
        assert due is not None
        state = delivery_failed(state, due["id"], now=clock)
        delay = next_due(state, now=10 ** 9)["next_attempt_at"] - clock
        assert previous_delay <= delay <= RETRY_MAX_SECONDS
        previous_delay = delay
        clock += delay
    assert previous_delay == RETRY_MAX_SECONDS

    # A brief bad→good→bad flap never reaches the durable outbox.
    flap = observe(initial_state(), [item("one", "ok")], enabled=True, now=0)
    flap = observe(flap, [item("one", "danger")], enabled=True, now=10)
    flap = observe(flap, [item("one", "ok")], enabled=True, now=50)
    flap = observe(flap, [item("one", "stale")], enabled=True, now=100)
    flap = observe(flap, [item("one", "stale")], enabled=True, now=219)
    assert next_due(flap, now=10_000) is None
    flap = observe(flap, [item("one", "stale")], enabled=True, now=220)
    assert next_due(flap, now=220) is not None


def test_failed_warning_is_cancelled_when_enclosure_recovers() -> None:
    state = observe(initial_state(), [item("one", "ok")], enabled=True, now=0)
    state = observe(state, [item("one", "warning")], enabled=True, now=10)
    state = observe(state, [item("one", "warning")], enabled=True, now=130)
    warning = next_due(state, now=130)
    assert warning and warning["kind"] == "alert"

    state = delivery_failed(state, warning["id"], now=130)
    assert next_due(state, now=190)["id"] == warning["id"]

    # Recovery happens before the failed warning's retry. The warning is now
    # false, so it must leave the outbox immediately rather than send during
    # the recovery debounce window.
    state = observe(state, [item("one", "ok")], enabled=True, now=150)
    assert next_due(state, now=190) is None
    status = delivery_status(state, enabled=True)
    assert status["pending"] == 0 and status["retrying"] is False

    # The warning was never delivered, so stabilising at the original healthy
    # baseline must not produce a confusing recovery-only notification.
    state = observe(state, [item("one", "ok")], enabled=True, now=270)
    assert next_due(state, now=10_000) is None
    assert state["enclosures"]["one"]["baseline"] == "ok"

    # A later real transition still starts a fresh delivery with clean retry
    # state, proving cancellation does not suppress future alerts.
    state = observe(state, [item("one", "danger")], enabled=True, now=300)
    state = observe(state, [item("one", "danger")], enabled=True, now=420)
    fresh = next_due(state, now=420)
    assert fresh and fresh["kind"] == "alert"
    assert fresh["id"] != warning["id"] and fresh["attempts"] == 0


def test_restart_recovery_disable_and_bounds() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "alert-state.json"
        first = AlertStateStore(path)
        first.observe([item("one", "ok")], enabled=True, now=0)
        first.observe([item("one", "warning", "PRIVATE-MARKER")], enabled=True, now=10)
        first.observe([item("one", "warning", "PRIVATE-MARKER")], enabled=True, now=130)
        pending = first.next_due(now=130)
        assert pending is not None
        first.failed(pending["id"], now=130)

        restarted = AlertStateStore(path)
        assert restarted.next_due(now=189) is None
        retry = restarted.next_due(now=190)
        assert retry and retry["id"] == pending["id"] and retry["attempts"] == 1
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

        restarted.succeeded(retry["id"], now=190)
        restarted.observe([item("one", "ok")], enabled=True, now=200)
        restarted.observe([item("one", "ok")], enabled=True, now=320)
        recovery = restarted.next_due(now=320)
        assert recovery and recovery["kind"] == "recovery"

        restarted.disable()
        assert restarted.status(enabled=False)["pending"] == 0
        restarted.observe([item("one", "danger")], enabled=True, now=500)
        assert restarted.next_due(now=50_000) is None

        path.write_text("not json", encoding="utf-8")
        damaged = AlertStateStore(path, clock=lambda: 555)
        damaged.observe([item("one", "danger")], enabled=True, now=600)
        assert damaged.next_due(now=50_000) is None
        assert damaged.status(enabled=True)["last_error_at"] == 555

        # A local symlink cannot make the outbox writer overwrite another file.
        target = Path(tmp) / "outside.json"
        target.write_text("sentinel", encoding="utf-8")
        path.unlink()
        path.symlink_to(target)
        linked = AlertStateStore(path, clock=lambda: 700)
        linked.observe([item("one", "ok")], enabled=True, now=701)
        assert target.read_text(encoding="utf-8") == "sentinel"
        assert not path.is_symlink()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    many = [item(f"enc-{index}", "ok") for index in range(MAX_ENCLOSURES + 100)]
    bounded = observe(initial_state(), many, enabled=True, now=0)
    assert len(bounded["enclosures"]) == MAX_ENCLOSURES


def test_keeper_only_status_and_sanitized_failure() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp)
        (data / "config.json").write_text(
            (ROOT / "config.example.json").read_text(encoding="utf-8"), encoding="utf-8")
        os.environ["BASK_DATA_DIR"] = str(data)

        sys.path.insert(0, str(ROOT / "scanner"))
        import db
        db.init_db()
        from fastapi.testclient import TestClient
        import server.app as app_module
        from server.app import alert_delivery, app

        keeper = TestClient(app)

        def change(method: str, path: str, body: dict):
            revision = keeper.get("/api/config/revision").json()["revision"]
            return keeper.request(
                method, path, json=body, headers={"X-Bask-Revision": str(revision)})

        assert change("POST", "/api/keeper/key", {"key": "alert-audit-key"}).status_code == 200
        assert change("POST", "/api/ntfy", {"enabled": True}).status_code == 200

        marker = "PRIVATE-ALERT-BODY-MARKER"
        alert_delivery.observe([item("one", "ok")], enabled=True, now=0)
        alert_delivery.observe([item("one", "warning", marker)], enabled=True, now=10)
        alert_delivery.observe([item("one", "warning", marker)], enabled=True, now=130)

        anonymous = TestClient(app)
        assert anonymous.get("/api/ntfy/delivery").status_code == 401
        response = keeper.get("/api/ntfy/delivery")
        assert response.status_code == 200
        payload = response.json()
        assert payload["pending"] == 1
        encoded = json.dumps(payload).lower()
        for forbidden in ("topic", "server", "url", marker.lower()):
            assert forbidden not in encoded

        original_publish = app_module._ntfy_publish
        try:
            app_module._ntfy_publish = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("https://example.invalid/private-topic-marker"))
            failed_test = keeper.post("/api/ntfy/test")
        finally:
            app_module._ntfy_publish = original_publish
        assert failed_test.status_code == 502
        assert "private-topic-marker" not in failed_test.text
        assert "example.invalid" not in failed_test.text

        portable = keeper.get("/api/config/export")
        assert portable.status_code == 200
        assert marker not in portable.text
        assert "alert-state" not in portable.text


def main() -> None:
    test_debounce_flapping_and_retry()
    test_failed_warning_is_cancelled_when_enclosure_recovers()
    test_restart_recovery_disable_and_bounds()
    test_keeper_only_status_and_sanitized_failure()
    print("Durable phone-alert tests passed.")


if __name__ == "__main__":
    main()
