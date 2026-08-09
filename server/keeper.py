"""Head Keeper key: who is allowed to change Bask's setup.

Bask is a wall display, so reading it stays open to anyone on the home network
— that is the point of the thing. What this guards is *changing* it: sensors,
enclosures, species, thermostats, cloud integrations, the update endpoint, and
the two reads that would hand out the ntfy topic (which is itself a secret,
since anyone holding it can publish alerts to the household's phones).

The key is stored only as a PBKDF2 hash. Bask never writes it back in the
clear, and there is no endpoint that returns it.

If no key is configured the app is wide open, exactly as it behaved before
this existed, so upgrading an install never locks anyone out of their own
dashboard. Fresh installs get a key from the installer instead.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from typing import Any

COOKIE_NAME = "bask_keeper"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30
_ITERATIONS = 200_000
_MIN_KEY_LENGTH = 8
_TOKEN_VERSION = "v2"
# A cookie minted far in the future would otherwise outlive its expiry.
_CLOCK_SKEW = 300


def generate_key() -> str:
    """A fresh Head Keeper key, in the same shape the installer prints."""
    return "bask_" + secrets.token_urlsafe(18)


def hash_key(key: str, salt: str | None = None) -> dict[str, Any]:
    """Return the stored record for a key. Never store the key itself."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", key.strip().encode(), salt.encode(), _ITERATIONS)
    return {"salt": salt, "hash": digest.hex(), "iterations": _ITERATIONS}


def verify_key(key: str, record: dict[str, Any] | None) -> bool:
    """Constant-time check of a candidate key against the stored record."""
    if not record or not isinstance(record, dict):
        return False
    salt, expected = record.get("salt"), record.get("hash")
    if not isinstance(salt, str) or not isinstance(expected, str):
        return False
    iterations = record.get("iterations")
    if not isinstance(iterations, int) or iterations < 1000:
        iterations = _ITERATIONS
    candidate = hashlib.pbkdf2_hmac("sha256", key.strip().encode(), salt.encode(), iterations)
    return hmac.compare_digest(candidate.hex(), expected)


def keeper_state(record: dict[str, Any] | None) -> str:
    """
    One of "unconfigured", "configured", or "corrupt".

    These are three states, not two. An install with no key is open on purpose:
    that is how Bask behaved before keys existed and how a wall display upgrades
    without locking anyone out. But a `keeper` block that is present and
    unusable is a different thing entirely — it means protection was configured
    and is now broken, and treating that as "open" silently removes the
    protection. It fails closed instead, and RECOVERY below says how to get back
    in without a working key.
    """
    if record is None:
        return "unconfigured"
    if not isinstance(record, dict):
        return "corrupt"
    if not record:
        return "unconfigured"
    salt, digest = record.get("salt"), record.get("hash")
    if isinstance(salt, str) and isinstance(digest, str) and salt and digest:
        return "configured"
    return "corrupt"


RECOVERY = (
    "Bask's Head Keeper record is unreadable, so changes are refused. "
    "Remove the \"keeper\" block from data/config.json on the machine running "
    "Bask and restart it; the dashboard stays readable throughout."
)


def is_configured(record: dict[str, Any] | None) -> bool:
    """True only for a record this module can actually verify against."""
    return keeper_state(record) == "configured"


def ensure_session_secret(record: dict[str, Any]) -> dict[str, Any]:
    """
    Give the record a random signing secret if it has none.

    This is what stops a settings export from being a credential. Cookies used
    to be sha256(salt:hash) — both of which the export contained — so anyone
    holding a backup file could compute a valid session without knowing the key.
    The secret is machine-local and never leaves in an export.
    """
    existing = record.get("session_secret")
    if not isinstance(existing, str) or len(existing) < 32:
        record["session_secret"] = secrets.token_hex(32)
    return record


def _sign(record: dict[str, Any], issued: int) -> str:
    secret = record.get("session_secret")
    if not isinstance(secret, str) or not secret:
        raise ValueError("no session secret on this keeper record")
    message = f"{issued}:{record.get('salt', '')}:{record.get('hash', '')}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def issue_session(record: dict[str, Any], now: float | None = None) -> str:
    """Mint a cookie value. Requires a record that already has a secret."""
    issued = int(now if now is not None else time.time())
    return f"{_TOKEN_VERSION}.{issued}.{_sign(record, issued)}"


def validate_new_key(key: str) -> str:
    """Reject keys too short to be worth hashing. Returns the cleaned key."""
    key = (key or "").strip()
    if len(key) < _MIN_KEY_LENGTH:
        raise ValueError(f"The Head Keeper key must be at least {_MIN_KEY_LENGTH} characters.")
    if len(key) > 512:
        raise ValueError("That key is too long.")
    return key


def session_is_valid(token: str | None, record: dict[str, Any] | None,
                     now: float | None = None) -> bool:
    """
    Check a cookie: right version, unexpired, and signed by this machine.

    The signature covers the stored salt and hash as well as the issue time, so
    changing or clearing the key still invalidates every existing cookie —
    the property the old derived token had — without the token being derivable
    from anything that appears in an export.
    """
    if not token or keeper_state(record) != "configured":
        return False
    assert record is not None
    if not isinstance(record.get("session_secret"), str):
        # Pre-v2 install that has not minted a secret yet. Old cookies are not
        # accepted; unlocking once issues a new one.
        return False
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _TOKEN_VERSION:
        return False
    try:
        issued = int(parts[1])
    except (TypeError, ValueError):
        return False
    current = int(now if now is not None else time.time())
    if issued > current + _CLOCK_SKEW:
        return False
    if current - issued > COOKIE_MAX_AGE:
        return False
    try:
        expected = _sign(record, issued)
    except ValueError:
        return False
    return hmac.compare_digest(parts[2], expected)


def cookie_kwargs(secure: bool = False) -> dict[str, Any]:
    """Cookie settings. Bask is plain HTTP on a LAN, so Secure is off by default."""
    return {
        "httponly": True,
        "samesite": "lax",
        "max_age": COOKIE_MAX_AGE,
        "path": "/",
        "secure": secure or os.environ.get("BASK_COOKIE_SECURE", "").lower() == "true",
    }
