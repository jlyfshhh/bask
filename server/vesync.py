"""Read-only VeSync humidifier status polling."""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

POLL_SECONDS = 120
STALE_SECONDS = 300
LOGIN_HELP = (
    "VeSync rejected that email/password. Confirm the account can sign in "
    "directly in the VeSync app or reset its password, then try again. If the "
    "account uses Apple or Google sign-in, create an email/password VeSync "
    "account and share the humidifier with it."
)


def _default_factory(*args, **kwargs):
    from pyvesync import VeSync
    return VeSync(*args, **kwargs)


def _plain(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class VeSyncHumidifierMonitor:
    def __init__(self, secrets_path: Path, token_path: Path,
                 client_factory: Callable[..., Any] = _default_factory):
        self.secrets_path = secrets_path
        self.token_path = token_path
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

    @staticmethod
    def _save_private(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle)
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        os.chmod(path, 0o600)

    @staticmethod
    def _summary(device) -> dict:
        raw = device.to_dict(state=True)

        def first(*keys):
            for key in keys:
                value = raw.get(key)
                if value is not None and str(value).lower() != "not_supported":
                    return _plain(value)
            return None

        connection = first("connection_status", "connectionStatus")
        power = first("device_status", "deviceStatus")
        return {
            "id": str(first("cid", "device_cid", "uuid") or ""),
            "name": first("device_name", "deviceName") or "VeSync humidifier",
            "model": first("device_type", "deviceType"),
            "online": str(connection).lower() == "online" if connection is not None else None,
            "power": str(power).lower() == "on" if power is not None else None,
            "humidity": first("humidity"),
            "temperature": first("temperature"),
            "mode": first("mode", "humidifier_mode"),
            "mist_level": first("mist_level", "mistLevel"),
            "target_humidity": first("target_humidity", "auto_target_humidity", "humidity_target"),
            "water_lacks": first("water_lacks", "water_shortage", "waterLacks"),
        }

    async def _fetch(self, credentials: dict, login: bool = False) -> list[dict]:
        async with self.client_factory(
            credentials["username"], credentials["password"],
            country_code=credentials.get("country_code", "US"), redact=True,
        ) as manager:
            loaded = False if login else await manager.load_credentials_from_file(self.token_path)
            if not loaded:
                try:
                    logged_in = await manager.login()
                except Exception as exc:
                    # pyvesync raises VeSyncLoginError when the cloud accepted
                    # the request but rejected the supplied username/password.
                    # Match by name so this module remains testable without
                    # importing pyvesync's optional exception hierarchy.
                    if type(exc).__name__ == "VeSyncLoginError":
                        raise ValueError(LOGIN_HELP) from None
                    raise
                if not logged_in:
                    raise ValueError(LOGIN_HELP)
                await manager.save_credentials(self.token_path)
                if self.token_path.exists():
                    os.chmod(self.token_path, 0o600)
            await manager.update()
            return [self._summary(device)
                    for device in getattr(manager.devices, "humidifiers", [])]

    def _apply_devices(self, credentials: dict, devices: list[dict]) -> None:
        self._devices = [{"id": d["id"], "name": d["name"], "model": d.get("model")}
                         for d in devices]
        self._state = next((d for d in devices if d["id"] == credentials.get("device_id")), None)
        self._updated_at = int(time.time())
        self._error = None

    async def configure(self, username: str, password: str, country_code: str = "US") -> dict:
        username, password = username.strip(), password.strip()
        country_code = country_code.strip().upper() or "US"
        if not username or not password:
            raise ValueError("Enter your VeSync email and password.")
        if len(country_code) != 2 or not country_code.isalpha():
            raise ValueError("Country code must contain two letters, such as US.")
        async with self._lock:
            credentials = {"username": username, "password": password,
                           "country_code": country_code}
            self.token_path.unlink(missing_ok=True)
            devices = await self._fetch(credentials, login=True)
            if len(devices) == 1:
                credentials["device_id"] = devices[0]["id"]
            self._save_private(self.secrets_path, credentials)
            self._apply_devices(credentials, devices)
            return self.settings_status()

    async def select_device(self, device_id: str) -> dict:
        async with self._lock:
            credentials = self._load()
            if not credentials.get("username"):
                raise ValueError("Connect VeSync first.")
            devices = await self._fetch(credentials)
            if not any(d["id"] == device_id for d in devices):
                raise ValueError("That VeSync humidifier is no longer available.")
            credentials["device_id"] = device_id
            self._save_private(self.secrets_path, credentials)
            self._apply_devices(credentials, devices)
            return self.settings_status()

    async def poll_once(self) -> None:
        async with self._lock:
            credentials = self._load()
            if not credentials.get("username"):
                return
            try:
                self._apply_devices(credentials, await self._fetch(credentials))
            except Exception:
                self._error = "VeSync cloud is temporarily unavailable."

    async def loop(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(POLL_SECONDS)

    def public_status(self) -> dict:
        credentials = self._load()
        out = {"configured": bool(credentials.get("username")),
               "selected": bool(credentials.get("device_id")),
               "updated_at": self._updated_at,
               "stale": bool(self._updated_at and time.time() - self._updated_at > STALE_SECONDS),
               "error": self._error}
        if self._state:
            out.update({k: v for k, v in self._state.items() if k != "id"})
        return out

    def settings_status(self) -> dict:
        return {**self.public_status(),
                "selected_device_id": self._load().get("device_id"),
                "devices": list(self._devices)}

    async def clear(self) -> None:
        async with self._lock:
            self.secrets_path.unlink(missing_ok=True)
            self.token_path.unlink(missing_ok=True)
            self._devices = []
            self._state = None
            self._updated_at = None
            self._error = None
