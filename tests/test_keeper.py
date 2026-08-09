"""Head Keeper key: hashing, sessions, and the lockout cases that would hurt."""
import hmac
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from server import keeper


def test_key_is_never_stored_in_the_clear():
    key = "correct horse battery staple"
    record = keeper.hash_key(key)
    assert key not in str(record)
    assert record["hash"] != key
    assert len(record["salt"]) == 32
    assert record["iterations"] >= 200_000


def test_verify_accepts_the_key_and_rejects_everything_else():
    record = keeper.hash_key("bask_secret_one")
    assert keeper.verify_key("bask_secret_one", record)
    assert keeper.verify_key("  bask_secret_one  ", record)   # trimmed like the input is
    assert not keeper.verify_key("bask_secret_two", record)
    assert not keeper.verify_key("", record)
    assert not keeper.verify_key("bask_secret_one", None)
    assert not keeper.verify_key("bask_secret_one", {})


def test_same_key_twice_gets_different_salts():
    a, b = keeper.hash_key("same-key-both-times"), keeper.hash_key("same-key-both-times")
    assert a["salt"] != b["salt"]
    assert a["hash"] != b["hash"]
    assert keeper.verify_key("same-key-both-times", a)
    assert keeper.verify_key("same-key-both-times", b)


def test_malformed_records_never_authenticate():
    for bad in (None, {}, {"salt": "x"}, {"hash": "y"}, {"salt": 1, "hash": 2},
                {"salt": "", "hash": ""}, "nope"):
        assert not keeper.verify_key("anything", bad)  # type: ignore[arg-type]
        # And they must read as "no key set" rather than "locked with a key
        # nothing can match", which would be an unrecoverable lockout.
        assert not keeper.is_configured(bad)  # type: ignore[arg-type]


def test_absurdly_low_iterations_are_ignored():
    # A hand-edited config must not be able to weaken the work factor. A bogus
    # value is discarded in favour of the real one, so the correct key still
    # verifies and a wrong key still does not.
    record = keeper.hash_key("a-real-key")
    for bogus in (1, 0, -5, "lots", None):
        tampered = {**record, "iterations": bogus}
        assert keeper.verify_key("a-real-key", tampered)
        assert not keeper.verify_key("not-the-key", tampered)


def test_session_token_dies_when_the_key_changes():
    first = keeper.ensure_session_secret(keeper.hash_key("first-key-value"))
    token = keeper.issue_session(first)
    assert keeper.session_is_valid(token, first)

    second = keeper.ensure_session_secret(keeper.hash_key("second-key-value"))
    # Changing or clearing the key must invalidate every cookie already issued.
    assert not keeper.session_is_valid(token, second)
    assert not keeper.session_is_valid(token, None)
    assert not keeper.session_is_valid(None, first)
    assert not keeper.session_is_valid("", first)


def test_a_settings_export_cannot_be_turned_into_a_session():
    """QC-01. The cookie used to be sha256(salt:hash), and both were exported."""
    import hashlib

    record = keeper.ensure_session_secret(keeper.hash_key("a-real-keeper-key"))
    # Everything a portable export is allowed to contain about the keeper: nothing.
    exported = {k: v for k, v in record.items() if k not in ("session_secret",)}

    old_style = hashlib.sha256(
        f"{exported.get('salt', '')}:{exported.get('hash', '')}".encode()
    ).hexdigest()
    assert not keeper.session_is_valid(old_style, record)

    # Nor any token derivable from the exported values by the current scheme.
    for issued in (0, 1, int(time.time())):
        for secret_guess in (exported.get("salt", ""), exported.get("hash", ""), ""):
            forged = hmac.new(
                secret_guess.encode(),
                f"{issued}:{exported.get('salt','')}:{exported.get('hash','')}".encode(),
                hashlib.sha256,
            ).hexdigest()
            assert not keeper.session_is_valid(f"v2.{issued}.{forged}", record)


def test_sessions_expire():
    record = keeper.ensure_session_secret(keeper.hash_key("a-real-keeper-key"))
    now = time.time()
    fresh = keeper.issue_session(record, now=now)
    assert keeper.session_is_valid(fresh, record, now=now)
    assert keeper.session_is_valid(fresh, record, now=now + keeper.COOKIE_MAX_AGE - 10)
    assert not keeper.session_is_valid(fresh, record, now=now + keeper.COOKIE_MAX_AGE + 10)
    # A cookie stamped in the future must not outlive its window either.
    assert not keeper.session_is_valid(keeper.issue_session(record, now=now + 86_400), record, now=now)


def test_rotating_the_key_rejects_every_earlier_session():
    record = keeper.ensure_session_secret(keeper.hash_key("first-key-value"))
    token = keeper.issue_session(record)
    rotated = keeper.ensure_session_secret(keeper.hash_key("second-key-value"))
    assert record["session_secret"] != rotated["session_secret"]
    assert not keeper.session_is_valid(token, rotated)


def test_a_corrupt_record_is_not_treated_as_open():
    """QC-02. Three states, not two: absent means open, broken means closed."""
    assert keeper.keeper_state(None) == "unconfigured"
    assert keeper.keeper_state({}) == "unconfigured"
    assert keeper.keeper_state(keeper.hash_key("a-real-keeper-key")) == "configured"
    for broken in ({"salt": "abc"}, {"hash": "abc"}, {"salt": 1, "hash": 2},
                   {"salt": "", "hash": ""}, "not-a-dict", []):
        assert keeper.keeper_state(broken) == "corrupt", broken
        assert not keeper.is_configured(broken)


def test_new_keys_must_be_long_enough():
    for bad in ("", "   ", "short", "1234567"):
        try:
            keeper.validate_new_key(bad)
            raise AssertionError(f"{bad!r} should have been rejected")
        except ValueError:
            pass
    assert keeper.validate_new_key("  long-enough-key  ") == "long-enough-key"


def test_generated_keys_are_unique_and_prefixed():
    keys = {keeper.generate_key() for _ in range(50)}
    assert len(keys) == 50
    assert all(k.startswith("bask_") and len(k) > 20 for k in keys)


def test_unconfigured_means_open():
    # The upgrade path: an install with no key behaves exactly as it did before.
    assert not keeper.is_configured({})
    assert not keeper.is_configured(None)


def test_cookie_is_httponly_and_lax_by_default():
    kwargs = keeper.cookie_kwargs()
    assert kwargs["httponly"] is True
    assert kwargs["samesite"] == "lax"
    # Bask is plain HTTP on a LAN; a Secure cookie would never be sent back.
    assert kwargs["secure"] is False
    assert keeper.cookie_kwargs(secure=True)["secure"] is True


if __name__ == "__main__":
    # Run standalone the same way the other Bask tests do, so CI executes these
    # rather than importing the module and silently passing.
    import sys
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - report and keep going
            failures += 1
            print(f"FAIL {name}: {exc}")
    if failures:
        sys.exit(f"{failures} Head Keeper test(s) failed")
    print("Head Keeper key tests passed")
