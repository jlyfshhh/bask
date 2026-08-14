"""Unit tests for the read-only Cielo monitor; no cloud calls are made."""
import asyncio
import json
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from server.cielo import CieloMonitor


@dataclass
class FakeDevice:
    id: str
    name: str
    device_status: bool = True
    temp: float = 76
    humidity: int = 48
    temp_unit: str = "F"
    device_on: bool = True
    hvac_mode: str = "cool"
    target_temp: float = 74
    fan_mode: str = "auto"
    swing_mode: str = "auto"
    preset_mode: int = 0


class FakeData:
    def __init__(self, devices):
        self.parsed = {d.id: d for d in devices}


class FakeClient:
    devices = [FakeDevice("aa:bb", "Animal Room")]
    tokens = []
    auth_calls = 0

    def __init__(self, api_key, token=None, **_kwargs):
        self.api_key = api_key
        self.token = token

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get_or_refresh_token(self):
        if not self.token:
            type(self).auth_calls += 1
            self.token = f"token-{type(self).auth_calls}"
        type(self).tokens.append(self.token)
        return self.token

    async def get_devices_data(self):
        await self.get_or_refresh_token()
        return FakeData(type(self).devices)


async def run_tests():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "cielo-secrets.json"
        monitor = CieloMonitor(path, FakeClient)

        status = await monitor.configure("private-key")
        saved = json.loads(path.read_text())
        assert saved == {
            "api_key": "private-key",
            "token": "token-1",
            "device_id": "aa:bb",
        }
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert status["name"] == "Animal Room"
        assert status["temperature"] == 76
        assert "api_key" not in json.dumps(status)
        assert "token-1" not in json.dumps(status)

        await monitor.poll_once()
        assert FakeClient.auth_calls == 1, "saved token must prevent reusing the API key"

        FakeClient.devices = [
            FakeDevice("aa:bb", "Animal Room"),
            FakeDevice("cc:dd", "Other Room", temp=70),
        ]
        second_path = Path(td) / "second.json"
        second = CieloMonitor(second_path, FakeClient)
        multi = await second.configure("another-key")
        assert multi["selected"] is False
        assert len(multi["devices"]) == 2
        await second.select_device("cc:dd")
        assert second.public_status()["name"] == "Other Room"
        assert second.public_status()["temperature"] == 70

        public = second.public_status()
        assert "devices" not in public
        assert "selected_device_id" not in public
        climate = second.climate_status()
        assert climate["series_key"].startswith("cielo-")
        assert "cc:dd" not in json.dumps(climate), "raw cloud device ID escaped"
        first_key = climate["series_key"]
        await second.select_device("aa:bb")
        assert second.climate_status()["series_key"] != first_key, \
            "two controllers would be spliced into one climate series"
        await second.clear()
        assert not second_path.exists()
        assert second.public_status()["configured"] is False


if __name__ == "__main__":
    asyncio.run(run_tests())
    print("Cielo monitor tests passed")
