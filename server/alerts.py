"""Durable, bounded state machine for Bask's optional phone alerts.

This module deliberately knows nothing about ntfy, HTTP, FastAPI, or Bask's
configuration file.  The transition functions are pure: callers provide an
observed enclosure snapshot and a clock value, and receive a new state value.
``AlertStateStore`` is the small persistence/concurrency boundary around them.

Delivery is at-least-once.  A pending event is written before it is published
and removed only after the publisher confirms success.  A process failure in
that narrow interval can therefore duplicate an alert, but cannot lose it.
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable


STATE_VERSION = 1
MAX_ENCLOSURES = 500
MAX_TEXT = 500
DEBOUNCE_SECONDS = 120
RETRY_BASE_SECONDS = 60
RETRY_MAX_SECONDS = 3600
MAX_STATE_BYTES = 2_000_000

BAD_STATES = frozenset({"warning", "danger", "stale"})


def initial_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "active": False,
        "sequence": 0,
        "enclosures": {},
        "last_success_at": None,
        "last_error_at": None,
        "last_error": None,
    }


def _group(status: Any) -> str:
    if status in BAD_STATES:
        return "bad"
    if status == "ok":
        return "ok"
    # no_data and no_ranges are display/setup states, not alarm conditions.
    return "neutral"


def _text(value: Any, fallback: str = "") -> str:
    return value[:MAX_TEXT] if isinstance(value, str) else fallback


def _observation(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    enclosure_id = value.get("id")
    if not isinstance(enclosure_id, str) or not enclosure_id or len(enclosure_id) > 128:
        return None
    return {
        "id": enclosure_id,
        "group": _group(value.get("status")),
        "alert": {
            "title": _text(value.get("alert_title"), "Bask alert"),
            "body": _text(value.get("alert_body"), "Enclosure needs attention"),
            "tags": _text(value.get("alert_tags"), "warning"),
            "priority": _text(value.get("alert_priority"), "high"),
        },
        "recovery": {
            "title": _text(value.get("recovery_title"), "Bask"),
            "body": _text(value.get("recovery_body"), "Enclosure is back to normal"),
            "tags": _text(value.get("recovery_tags"), "white_check_mark"),
            "priority": _text(value.get("recovery_priority"), ""),
        },
    }


def _new_entry(group: str, now: float) -> dict[str, Any]:
    return {
        "baseline": group,
        "last_seen": group,
        "last_seen_since": now,
        "pending": None,
    }


def _new_pending(state: dict[str, Any], enclosure_id: str, target: str,
                 message: dict[str, str], now: float) -> dict[str, Any]:
    state["sequence"] = int(state.get("sequence", 0)) + 1
    return {
        "id": f"{state['sequence']}:{enclosure_id}",
        "enclosure_id": enclosure_id,
        "target": target,
        "kind": "alert" if target == "bad" else "recovery",
        "title": message["title"],
        "body": message["body"],
        "tags": message["tags"],
        "priority": message["priority"],
        "created_at": now,
        "attempts": 0,
        "next_attempt_at": now,
    }


def observe(state: dict[str, Any], observations: Iterable[dict[str, Any]], *,
            enabled: bool, now: float,
            debounce_seconds: int = DEBOUNCE_SECONDS) -> dict[str, Any]:
    """Return state after observing one complete dashboard snapshot.

    One pending transition per enclosure bounds the durable outbox to the
    configured enclosure count.  If a condition reverses before delivery, its
    debounced transition is cancelled; otherwise it remains pending until a
    confirmed success, with retries handled separately.
    """
    out = copy.deepcopy(state)
    if not enabled:
        # Turning alerts off cancels every pending send.  A later opt-in seeds a
        # fresh baseline, preserving the historical no-burst behaviour.
        out["active"] = False
        out["enclosures"] = {}
        return out

    current: dict[str, dict[str, Any]] = {}
    for raw in observations:
        item = _observation(raw)
        if item is not None and item["id"] not in current:
            current[item["id"]] = item
        if len(current) >= MAX_ENCLOSURES:
            break

    if not out.get("active"):
        out["active"] = True
        out["enclosures"] = {
            enclosure_id: _new_entry(item["group"], now)
            for enclosure_id, item in current.items()
        }
        return out

    entries = out.setdefault("enclosures", {})
    for enclosure_id in list(entries):
        if enclosure_id not in current:
            del entries[enclosure_id]

    debounce = max(0, int(debounce_seconds))
    for enclosure_id, item in current.items():
        group = item["group"]
        entry = entries.get(enclosure_id)
        if not isinstance(entry, dict):
            entries[enclosure_id] = _new_entry(group, now)
            continue

        if entry.get("last_seen") != group:
            entry["last_seen"] = group
            entry["last_seen_since"] = now
            continue

        since = entry.get("last_seen_since")
        if not isinstance(since, (int, float)) or isinstance(since, bool) or since > now:
            entry["last_seen_since"] = now
            continue
        if now - since < debounce:
            continue

        baseline = entry.get("baseline")
        pending = entry.get("pending") if isinstance(entry.get("pending"), dict) else None

        # Neutral/setup states are not alerts.  Once stable they become the new
        # silent baseline and invalidate any obsolete pending transition.
        if group == "neutral":
            entry["baseline"] = "neutral"
            entry["pending"] = None
            continue

        if group == baseline:
            # A transition reversed before it could be delivered.
            entry["pending"] = None
            continue

        # Establishing a healthy reading after a no-data/setup baseline should
        # also be silent; only recovery from a delivered problem is announced.
        if baseline == "neutral" and group == "ok":
            entry["baseline"] = "ok"
            entry["pending"] = None
            continue

        message = item["alert"] if group == "bad" else item["recovery"]
        if pending and pending.get("target") == group:
            # Keep retry timing/identity stable while refreshing the human text
            # to the latest debounced enclosure details.
            for key in ("title", "body", "tags", "priority"):
                pending[key] = message[key]
            entry["pending"] = pending
        else:
            entry["pending"] = _new_pending(out, enclosure_id, group, message, now)

    return out


def next_due(state: dict[str, Any], *, now: float) -> dict[str, Any] | None:
    due = []
    for entry in state.get("enclosures", {}).values():
        if not isinstance(entry, dict) or not isinstance(entry.get("pending"), dict):
            continue
        pending = entry["pending"]
        at = pending.get("next_attempt_at")
        if isinstance(at, (int, float)) and not isinstance(at, bool) and at <= now:
            due.append(pending)
    if not due:
        return None
    return copy.deepcopy(min(due, key=lambda item: (
        item.get("next_attempt_at", 0), item.get("created_at", 0), item.get("id", ""))))


def delivery_succeeded(state: dict[str, Any], event_id: str, *, now: float) -> dict[str, Any]:
    out = copy.deepcopy(state)
    for entry in out.get("enclosures", {}).values():
        pending = entry.get("pending") if isinstance(entry, dict) else None
        if isinstance(pending, dict) and pending.get("id") == event_id:
            entry["baseline"] = pending.get("target", entry.get("baseline"))
            entry["pending"] = None
            out["last_success_at"] = now
            return out
    return out


def delivery_failed(state: dict[str, Any], event_id: str, *, now: float) -> dict[str, Any]:
    out = copy.deepcopy(state)
    for entry in out.get("enclosures", {}).values():
        pending = entry.get("pending") if isinstance(entry, dict) else None
        if not isinstance(pending, dict) or pending.get("id") != event_id:
            continue
        attempts = max(0, int(pending.get("attempts", 0))) + 1
        # Cap the exponent as well as the resulting delay so hostile/corrupt
        # state cannot ask Python to construct an enormous integer.
        exponent = min(attempts - 1, 16)
        delay = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (2 ** exponent))
        pending["attempts"] = attempts
        pending["next_attempt_at"] = now + delay
        entry["pending"] = pending
        out["last_error_at"] = now
        # Never persist exception text: urllib errors can contain the private
        # topic URL.  The keeper-facing status needs only a safe diagnosis.
        out["last_error"] = "Notification service unavailable; Bask will retry automatically."
        return out
    return out


def delivery_status(state: dict[str, Any], *, enabled: bool) -> dict[str, Any]:
    pending = [entry["pending"] for entry in state.get("enclosures", {}).values()
               if isinstance(entry, dict) and isinstance(entry.get("pending"), dict)]
    retries = [max(0, int(item.get("attempts", 0))) for item in pending]
    retry_times = [item.get("next_attempt_at") for item in pending
                   if isinstance(item.get("next_attempt_at"), (int, float))
                   and not isinstance(item.get("next_attempt_at"), bool)]
    return {
        "enabled": bool(enabled),
        "pending": len(pending),
        "retrying": any(value > 0 for value in retries),
        "next_retry_at": min(retry_times) if retry_times else None,
        "last_success_at": state.get("last_success_at"),
        "last_error_at": state.get("last_error_at"),
        "last_error": _text(state.get("last_error")) or None,
    }


def _normalise_loaded(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
        raise ValueError("unsupported alert state")
    out = initial_state()
    out["active"] = raw.get("active") is True
    sequence = raw.get("sequence")
    if isinstance(sequence, int) and not isinstance(sequence, bool) and sequence >= 0:
        out["sequence"] = min(sequence, 2 ** 63 - 1)
    for key in ("last_success_at", "last_error_at"):
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            out[key] = value
    out["last_error"] = _text(raw.get("last_error")) or None

    entries = raw.get("enclosures")
    if not isinstance(entries, dict):
        return out
    for enclosure_id, source in entries.items():
        if len(out["enclosures"]) >= MAX_ENCLOSURES:
            break
        if not isinstance(enclosure_id, str) or not enclosure_id or len(enclosure_id) > 128:
            continue
        if not isinstance(source, dict):
            continue
        baseline = source.get("baseline")
        last_seen = source.get("last_seen")
        since = source.get("last_seen_since")
        if baseline not in {"ok", "bad", "neutral"} or last_seen not in {"ok", "bad", "neutral"}:
            continue
        if not isinstance(since, (int, float)) or isinstance(since, bool) or since < 0:
            continue
        entry = _new_entry(baseline, since)
        entry["last_seen"] = last_seen
        pending = source.get("pending")
        if isinstance(pending, dict):
            event_id = pending.get("id")
            target = pending.get("target")
            attempts = pending.get("attempts")
            created = pending.get("created_at")
            next_at = pending.get("next_attempt_at")
            if (isinstance(event_id, str) and len(event_id) <= 256
                    and target in {"ok", "bad"}
                    and isinstance(attempts, int) and not isinstance(attempts, bool)
                    and 0 <= attempts <= 1_000_000
                    and isinstance(created, (int, float)) and not isinstance(created, bool)
                    and created >= 0
                    and isinstance(next_at, (int, float)) and not isinstance(next_at, bool)
                    and next_at >= 0):
                entry["pending"] = {
                    "id": event_id,
                    "enclosure_id": enclosure_id,
                    "target": target,
                    "kind": "alert" if target == "bad" else "recovery",
                    "title": _text(pending.get("title"), "Bask"),
                    "body": _text(pending.get("body"), "Enclosure status changed"),
                    "tags": _text(pending.get("tags")),
                    "priority": _text(pending.get("priority")),
                    "created_at": created,
                    "attempts": attempts,
                    "next_attempt_at": next_at,
                }
        out["enclosures"][enclosure_id] = entry
    return out


class AlertStateStore:
    """Thread-safe, owner-only durable storage for the alert state machine."""

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time):
        self.path = path
        self._clock = clock
        self._lock = threading.RLock()
        self._state = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            if not self.path.exists():
                return initial_state()
            if self.path.is_symlink():
                raise ValueError("alert state must not be a symlink")
            if self.path.stat().st_size > MAX_STATE_BYTES:
                raise ValueError("alert state is too large")
            # Restores from older/manual tooling can preserve permissive mode
            # bits. Harden the private history as soon as Bask sees it.
            os.chmod(self.path, 0o600)
            return _normalise_loaded(json.loads(self.path.read_text(encoding="utf-8")))
        except Exception:
            # Failing closed here means seeding a new baseline (no startup
            # burst), never guessing that an old condition is a new alert.
            state = initial_state()
            state["last_error"] = "Alert delivery state could not be read and was safely reset."
            state["last_error_at"] = self._clock()
            return state

    def _save_unlocked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
        fd = None
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = None
                json.dump(self._state, handle, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if fd is not None:
                os.close(fd)
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def _replace(self, updated: dict[str, Any]) -> None:
        if updated != self._state:
            self._state = updated
            self._save_unlocked()

    def observe(self, observations: Iterable[dict[str, Any]], *, enabled: bool,
                now: float, debounce_seconds: int = DEBOUNCE_SECONDS) -> None:
        with self._lock:
            self._replace(observe(self._state, observations, enabled=enabled, now=now,
                                  debounce_seconds=debounce_seconds))

    def next_due(self, *, now: float) -> dict[str, Any] | None:
        with self._lock:
            return next_due(self._state, now=now)

    def succeeded(self, event_id: str, *, now: float) -> None:
        with self._lock:
            self._replace(delivery_succeeded(self._state, event_id, now=now))

    def failed(self, event_id: str, *, now: float) -> None:
        with self._lock:
            self._replace(delivery_failed(self._state, event_id, now=now))

    def disable(self) -> None:
        with self._lock:
            self._replace(observe(self._state, (), enabled=False, now=0))

    def status(self, *, enabled: bool) -> dict[str, Any]:
        with self._lock:
            return delivery_status(self._state, enabled=enabled)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state)
