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
from typing import Any

COOKIE_NAME = "bask_keeper"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365
_ITERATIONS = 200_000
_MIN_KEY_LENGTH = 8


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


def is_configured(record: dict[str, Any] | None) -> bool:
    """
    True only for a record this module can actually verify against.

    The type check matters: a hand-edited or corrupted `keeper` block that
    looked configured but could never match would lock the Head Keeper out of
    their own dashboard with no way back in. Treating an unusable record as
    "no key set" falls back to the open behaviour instead, which is the same
    state every pre-existing install is already in.
    """
    if not isinstance(record, dict):
        return False
    return isinstance(record.get("salt"), str) and isinstance(record.get("hash"), str) \
        and bool(record["salt"]) and bool(record["hash"])


def validate_new_key(key: str) -> str:
    """Reject keys too short to be worth hashing. Returns the cleaned key."""
    key = (key or "").strip()
    if len(key) < _MIN_KEY_LENGTH:
        raise ValueError(f"The Head Keeper key must be at least {_MIN_KEY_LENGTH} characters.")
    if len(key) > 512:
        raise ValueError("That key is too long.")
    return key


def session_token(record: dict[str, Any]) -> str:
    """
    The cookie value for an unlocked session.

    It is derived from the stored hash, so changing or clearing the key
    invalidates every existing cookie without needing to track sessions.
    """
    return hashlib.sha256(f"{record.get('salt','')}:{record.get('hash','')}".encode()).hexdigest()


def session_is_valid(token: str | None, record: dict[str, Any] | None) -> bool:
    if not token or not is_configured(record):
        return False
    return hmac.compare_digest(token, session_token(record))


def cookie_kwargs(secure: bool = False) -> dict[str, Any]:
    """Cookie settings. Bask is plain HTTP on a LAN, so Secure is off by default."""
    return {
        "httponly": True,
        "samesite": "lax",
        "max_age": COOKIE_MAX_AGE,
        "path": "/",
        "secure": secure or os.environ.get("BASK_COOKIE_SECURE", "").lower() == "true",
    }
