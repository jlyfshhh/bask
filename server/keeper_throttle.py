"""Bounded in-memory protection for the Head Keeper unlock endpoint.

The Head Keeper key has high entropy, so this is primarily a resource boundary:
PBKDF2 is intentionally expensive and an unauthenticated request must not be
able to make Bask perform it without limit.  Counts are split by source, exact
submitted key, and a loose global ceiling.  The two attacker-controlled maps
are bounded so the throttle itself cannot become a memory denial of service.

State is intentionally process-local.  Persisting each failed attempt would
turn the same unauthenticated endpoint into a disk-write amplifier.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Limits:
    max_failures: int
    window_seconds: float
    block_seconds: float


@dataclass
class _Bucket:
    failures: int
    window_started_at: float
    blocked_until: float = 0


SOURCE_LIMITS = Limits(30, 10 * 60, 60)
KEY_LIMITS = Limits(5, 10 * 60, 15 * 60)
GLOBAL_LIMITS = Limits(120, 10 * 60, 60)
MAX_BUCKETS = 512
_GLOBAL_KEY = "all"
_TRUSTED_PROXY_HEADERS = {"cf-connecting-ip", "x-real-ip", "x-forwarded-for"}
_fingerprint_salt = secrets.token_bytes(32)


class _Scope:
    def __init__(self, limits: Limits, capacity: int):
        self.limits = limits
        self.capacity = max(1, capacity)
        self.buckets: dict[str, _Bucket] = {}

    def retry_after(self, key: str, now: float) -> float:
        bucket = self._live(key, now)
        return max(0, bucket.blocked_until - now) if bucket else 0

    def fail(self, key: str, now: float) -> float:
        bucket = self._live(key, now)
        if bucket and bucket.blocked_until > now:
            return bucket.blocked_until - now
        if bucket is None:
            self._make_room(now)
            bucket = _Bucket(0, now)
            self.buckets[key] = bucket
        bucket.failures += 1
        if bucket.failures >= self.limits.max_failures:
            bucket.blocked_until = now + self.limits.block_seconds
            return self.limits.block_seconds
        return 0

    def forget(self, key: str) -> None:
        self.buckets.pop(key, None)

    def clear(self) -> None:
        self.buckets.clear()

    def _live(self, key: str, now: float) -> _Bucket | None:
        bucket = self.buckets.get(key)
        if bucket is None:
            return None
        if bucket.blocked_until > now:
            return bucket
        if bucket.blocked_until or now - bucket.window_started_at >= self.limits.window_seconds:
            self.buckets.pop(key, None)
            return None
        return bucket

    def _expires_at(self, bucket: _Bucket) -> float:
        return max(bucket.blocked_until, bucket.window_started_at + self.limits.window_seconds)

    def _make_room(self, now: float) -> None:
        if len(self.buckets) < self.capacity:
            return
        for key, bucket in list(self.buckets.items()):
            if self._expires_at(bucket) <= now:
                self.buckets.pop(key, None)
        while len(self.buckets) >= self.capacity:
            victim = min(self.buckets, key=lambda key: self._expires_at(self.buckets[key]))
            self.buckets.pop(victim, None)


class KeeperUnlockThrottle:
    def __init__(
        self,
        *,
        source_limits: Limits = SOURCE_LIMITS,
        key_limits: Limits = KEY_LIMITS,
        global_limits: Limits = GLOBAL_LIMITS,
        max_buckets: int = MAX_BUCKETS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._sources = _Scope(source_limits, max_buckets)
        self._keys = _Scope(key_limits, max_buckets)
        self._global = _Scope(global_limits, 1)
        self._clock = clock
        self._lock = threading.Lock()

    @property
    def bucket_counts(self) -> tuple[int, int]:
        with self._lock:
            return len(self._sources.buckets), len(self._keys.buckets)

    def check(self, source: str | None, key_fingerprint: str) -> int:
        with self._lock:
            now = self._clock()
            waits = [self._global.retry_after(_GLOBAL_KEY, now), self._keys.retry_after(key_fingerprint, now)]
            if source:
                waits.append(self._sources.retry_after(source, now))
            return _retry_seconds(max(waits))

    def fail(self, source: str | None, key_fingerprint: str) -> int:
        with self._lock:
            now = self._clock()
            waits = [self._global.fail(_GLOBAL_KEY, now), self._keys.fail(key_fingerprint, now)]
            if source:
                waits.append(self._sources.fail(source, now))
            return _retry_seconds(max(waits))

    def succeed(self, source: str | None, key_fingerprint: str) -> None:
        with self._lock:
            if source:
                self._sources.forget(source)
            self._keys.forget(key_fingerprint)

    def reset(self) -> None:
        with self._lock:
            self._sources.clear()
            self._keys.clear()
            self._global.clear()


def key_fingerprint(key: str) -> str:
    """Process-private identity for one exact submitted string."""
    return hmac.new(_fingerprint_salt, key.strip().encode(), hashlib.sha256).hexdigest()


def source_key(request) -> str | None:
    """Use the direct peer unless one explicitly trusted proxy header is set."""
    trusted = os.environ.get("BASK_TRUSTED_PROXY_IP_HEADER", "").strip().lower()
    if trusted in _TRUSTED_PROXY_HEADERS:
        raw = request.headers.get(trusted, "")
        candidate = raw.split(",", 1)[0] if trusted == "x-forwarded-for" else raw
    else:
        client = getattr(request, "client", None)
        candidate = getattr(client, "host", "") if client else ""
    cleaned = str(candidate).strip().lower()
    return cleaned[:128] or None


def _retry_seconds(wait: float) -> int:
    return max(0, int(wait + 0.999999))

