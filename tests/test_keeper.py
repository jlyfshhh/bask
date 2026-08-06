"""Head Keeper key: hashing, sessions, and the lockout cases that would hurt."""
import sys
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
    first = keeper.hash_key("first-key-value")
    token = keeper.session_token(first)
    assert keeper.session_is_valid(token, first)

    second = keeper.hash_key("second-key-value")
    # Changing or clearing the key must invalidate every cookie already issued.
    assert not keeper.session_is_valid(token, second)
    assert not keeper.session_is_valid(token, None)
    assert not keeper.session_is_valid(None, first)
    assert not keeper.session_is_valid("", first)


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
