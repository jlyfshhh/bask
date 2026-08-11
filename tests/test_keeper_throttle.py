"""QC-24: Head Keeper PBKDF work is bounded without an unbounded map."""
from __future__ import annotations

import os
from types import SimpleNamespace

from server.keeper_throttle import KeeperUnlockThrottle, Limits, key_fingerprint, source_key


def harness(*, max_buckets: int = 8):
    now = [0.0]
    throttle = KeeperUnlockThrottle(
        source_limits=Limits(3, 100, 50),
        key_limits=Limits(2, 100, 50),
        global_limits=Limits(50, 1000, 10),
        max_buckets=max_buckets,
        clock=lambda: now[0],
    )
    return throttle, now


def test_source_key_and_exact_key_scopes() -> None:
    throttle, now = harness()
    first = key_fingerprint("wrong-one")
    repeated = key_fingerprint("wrong-two")
    assert first != repeated
    assert repeated == key_fingerprint("  wrong-two  ")

    assert throttle.fail("phone-a", first) == 0
    now[0] = 1
    assert throttle.fail("phone-a", repeated) == 0
    now[0] = 2
    assert throttle.fail("phone-b", repeated) == 50
    assert throttle.check("phone-c", repeated) == 50
    # A different phone and different candidate are not locked out.
    assert throttle.check("phone-c", key_fingerprint("real-key")) == 0


def test_source_block_success_and_expiry() -> None:
    throttle, now = harness()
    for index in range(3):
        now[0] = float(index)
        wait = throttle.fail("attacker", key_fingerprint(f"guess-{index}"))
    assert wait == 50
    assert throttle.check("attacker", key_fingerprint("another")) == 50
    assert throttle.check("keeper-phone", key_fingerprint("real")) == 0

    throttle.succeed("keeper-phone", key_fingerprint("real"))
    now[0] = 52
    assert throttle.check("attacker", key_fingerprint("another")) == 0


def test_attacker_controlled_maps_stay_bounded() -> None:
    throttle, now = harness(max_buckets=8)
    for index in range(2_000):
        now[0] = index / 100
        throttle.fail(f"source-{index}", key_fingerprint(f"key-{index}"))
    sources, keys = throttle.bucket_counts
    assert sources <= 8 and keys <= 8


def test_proxy_headers_are_opt_in() -> None:
    request = SimpleNamespace(
        client=SimpleNamespace(host="direct-peer"),
        headers={"x-real-ip": "forwarded-peer", "x-forwarded-for": "first, second"},
    )
    previous = os.environ.pop("BASK_TRUSTED_PROXY_IP_HEADER", None)
    try:
        assert source_key(request) == "direct-peer"
        os.environ["BASK_TRUSTED_PROXY_IP_HEADER"] = "x-real-ip"
        assert source_key(request) == "forwarded-peer"
        os.environ["BASK_TRUSTED_PROXY_IP_HEADER"] = "x-forwarded-for"
        assert source_key(request) == "first"
        os.environ["BASK_TRUSTED_PROXY_IP_HEADER"] = "x-client-ip"
        assert source_key(request) == "direct-peer"
    finally:
        if previous is None:
            os.environ.pop("BASK_TRUSTED_PROXY_IP_HEADER", None)
        else:
            os.environ["BASK_TRUSTED_PROXY_IP_HEADER"] = previous


def main() -> None:
    test_source_key_and_exact_key_scopes()
    test_source_block_success_and_expiry()
    test_attacker_controlled_maps_stay_bounded()
    test_proxy_headers_are_opt_in()
    print("Head Keeper unlock throttle tests passed.")


if __name__ == "__main__":
    main()
