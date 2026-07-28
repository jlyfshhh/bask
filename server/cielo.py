"""Read-only Cielo Breez status polling.

Credentials live in a separate, private file so they never appear in Bask's
portable configuration exports.  The access token is persisted because Cielo
API keys may only be authenticated once.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from cieloconnectapi import AuthenticationError, CieloClient


POLL_SECONDS = 120
STALE_SECONDS = 300


class CieloMonitor:
    def __init__(self, secrets_path: Path, client_factory: Callable[..., Any] = CieloClient):
        self.secrets_path = secrets_path
        self.client_factory = client_factory
        self._devices: list[dict] = []
        self._state: dict | None = None
        self._error: str | None = None
        self._updated_at: int | None = None
        self._lock = asyncio.Lock()

    def _load(self) -> dict:
        if not self.secrets_path.exists():
            return {}
        try:
            data = json.loads(self.secrets_path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict) -> None:
        self.secrets_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.secrets_path.with_suffix(".json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle)
        os.chmod(tmp, 0o600)
        tmp.replace(self.secrets_path)
        os.chmod(self.secrets_path, 0o600)

    @staticmethod
    def _summary(device) -> dict:
        return {
            "id": device.id,
            "name": device.name,
            "online": bool(device.device_status),
            "temperature": device.temp,
            "humidity": device.humidity,
            "temp_unit": device.temp_unit or "F",
            "power": bool(device.device_on),
            "mode": device.hvac_mode,
            "target": device.target_temp,
            "fan": device.fan_mode,
            "swing": device.swing_mode,
            "preset": device.preset_mode,
        }

    async def _fetch(self, credentials: dict, persist_token: bool = False) -> tuple[list[dict], str]:
        async with self.client_factory(
            api_key=credentials["api_key"],
            token=credentials.get("token"),
            timeout=20,
        ) as client:
            await client.get_or_refresh_token()
            # Persist immediately on first setup: even if device discovery then
            # fails, the one-use API key's resulting token is not lost.
            if persist_token and client.token != credentials.get("token"):
                credentials["token"] = client.token
                self._save(credentials)
            result = await client.get_devices_data()
            devices = [self._summary(d) for d in (result.parsed or {}).values()]
            return devices, client.token

    def _apply_devices(self, credentials: dict, devices: list[dict]) -> None:
        self._devices = [{"id": d["id"], "name": d["name"]} for d in devices]
        selected = credentials.get("device_id")
        self._state = next((d for d in devices if d["id"] == selected), None)
        self._updated_at = int(time.time())
        self._error = None

    async def configure(self, api_key: str) -> dict:
        key = api_key.strip()
        if not key:
            raise ValueError("Enter a Cielo Connect API key.")
        async with self._lock:
            # A submitted key is intentionally authenticated only once.
            saved = self._load()
            credentials = saved if saved.get("api_key") == key else {"api_key": key}
            devices, token = await self._fetch(credentials, persist_token=True)
            credentials["token"] = token
            if len(devices) == 1:
                credentials["device_id"] = devices[0]["id"]
            self._save(credentials)
            self._apply_devices(credentials, devices)
            return self.settings_status()

    async def select_device(self, device_id: str) -> dict:
        async with self._lock:
            credentials = self._load()
            if not credentials.get("api_key"):
                raise ValueError("Connect Cielo first.")
            if not any(d["id"] == device_id for d in self._devices):
                devices, token = await self._fetch(credentials)
                credentials["token"] = token
                self._devices = [{"id": d["id"], "name": d["name"]} for d in devices]
                if not any(d["id"] == device_id for d in devices):
                    raise ValueError("That Cielo device is no longer available.")
            credentials["device_id"] = device_id
            self._save(credentials)
            await self._poll_locked(credentials)
            return self.settings_status()

    async def _poll_locked(self, credentials: dict) -> None:
        devices, token = await self._fetch(credentials)
        if token != credentials.get("token"):
            credentials["token"] = token
            self._save(credentials)
        self._apply_devices(credentials, devices)

    async def poll_once(self) -> None:
        async with self._lock:
            credentials = self._load()
            if not credentials.get("api_key"):
                return
            try:
                await self._poll_locked(credentials)
            except AuthenticationError:
                self._error = "Cielo authentication needs attention."
            except Exception:
                self._error = "Cielo cloud is temporarily unavailable."

    async def loop(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(POLL_SECONDS)

    def public_status(self) -> dict:
        credentials = self._load()
        configured = bool(credentials.get("api_key"))
        out = {
            "configured": configured,
            "selected": bool(credentials.get("device_id")),
            "updated_at": self._updated_at,
            "stale": bool(self._updated_at and time.time() - self._updated_at > STALE_SECONDS),
            "error": self._error,
        }
        if self._state:
            out.update({k: v for k, v in self._state.items() if k != "id"})
        return out

    def settings_status(self) -> dict:
        return {
            **self.public_status(),
            "selected_device_id": self._load().get("device_id"),
            "devices": list(self._devices),
        }

    async def clear(self) -> None:
        async with self._lock:
            self.secrets_path.unlink(missing_ok=True)
            self._devices = []
            self._state = None
            self._updated_at = None
            self._error = None
