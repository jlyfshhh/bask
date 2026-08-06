"""Unit tests for the read-only VeSync humidifier monitor; no cloud calls."""
import asyncio
import json
import stat
import tempfile
from pathlib import Path

from server.vesync import VeSyncHumidifierMonitor


class FakeDevice:
    def __init__(self, cid="humid-1", name="Animal Room", humidity=54):
        self.values = {
            "cid": cid, "device_name": name, "device_type": "Classic300S",
            "connection_status": "online", "device_status": "on",
            "humidity": humidity, "mode": "auto", "mist_level": 2,
            "target_humidity": 55, "water_lacks": False,
        }

    def to_dict(self, state=True):
        assert state is True
        return self.values.copy()


class FakeContainer:
    humidifiers = [FakeDevice()]


class FakeClient:
    devices = FakeContainer()
    login_calls = 0

    def __init__(self, username, password, **kwargs):
        self.username, self.password, self.kwargs = username, password, kwargs

    async def __aenter__(self): return self
    async def __aexit__(self, *_args): return None

    async def load_credentials_from_file(self, path):
        return Path(path).exists()

    async def login(self):
        type(self).login_calls += 1
        return self.password == "correct"

    async def save_credentials(self, path):
        Path(path).write_text(json.dumps({"token": "private-token"}))

    async def update(self): return None


async def run_tests():
    with tempfile.TemporaryDirectory() as td:
        secret = Path(td) / "vesync-secrets.json"
        token = Path(td) / "vesync-token.json"
        monitor = VeSyncHumidifierMonitor(secret, token, FakeClient)

        status = await monitor.configure("keeper@example.com", "correct")
        assert status["name"] == "Animal Room"
        assert status["humidity"] == 54
        assert status["target_humidity"] == 55
        assert status["mist_level"] == 2
        assert status["power"] is True
        assert stat.S_IMODE(secret.stat().st_mode) == 0o600
        assert stat.S_IMODE(token.stat().st_mode) == 0o600
        public = json.dumps(status)
        assert "keeper@example.com" not in public
        assert "correct" not in public
        assert "private-token" not in public

        await monitor.poll_once()
        assert FakeClient.login_calls == 1, "saved token should be reused"

        FakeClient.devices.humidifiers = [
            FakeDevice(), FakeDevice("humid-2", "Nursery", 48)]
        second = VeSyncHumidifierMonitor(Path(td) / "second-secret.json",
                                         Path(td) / "second-token.json", FakeClient)
        multi = await second.configure("keeper@example.com", "correct")
        assert multi["selected"] is False
        await second.select_device("humid-2")
        assert second.public_status()["name"] == "Nursery"

        await monitor.clear()
        assert not secret.exists() and not token.exists()
        assert monitor.public_status()["configured"] is False


if __name__ == "__main__":
    asyncio.run(run_tests())
    print("VeSync humidifier monitor tests passed")
