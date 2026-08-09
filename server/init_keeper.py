"""Create a Head Keeper record for a brand-new install, and print the key once.

Run inside the published image so it uses the same hashing and record shape as
the running application:

    docker run --rm -v <data>:/data --entrypoint python <image> \\
        -m server.init_keeper /data/config.json

The installer used to inline a copy of the hashing in a heredoc executed by the
host's python3 — which it neither installed nor required, and whose failure it
discarded. A host without python3 therefore ended up with no keeper record at
all, which reads as "no key set", which means every write is open; and the
installer printed success either way.

Exits non-zero on any failure so the caller can stop. Never overwrites an
existing record: an upgrade must not invalidate the key the keeper already has.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from server import keeper


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m server.init_keeper <config.json>", file=sys.stderr)
        return 2

    path = Path(argv[1])
    try:
        config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"could not read {path}: {exc}", file=sys.stderr)
        return 1
    if not isinstance(config, dict):
        print(f"{path} does not contain a settings object", file=sys.stderr)
        return 1

    state = keeper.keeper_state(config.get("keeper"))
    if state == "configured":
        # Already protected. Say so on stderr and print nothing on stdout, so a
        # caller capturing the key gets an empty string rather than a stale one.
        print("this install already has a Head Keeper key; leaving it alone", file=sys.stderr)
        return 0
    if state == "corrupt":
        print("the existing keeper record is unreadable; refusing to replace it silently", file=sys.stderr)
        return 1

    key = keeper.generate_key()
    config["keeper"] = keeper.ensure_session_secret(keeper.hash_key(key))

    try:
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"could not write {path}: {exc}", file=sys.stderr)
        return 1

    # Prove the record we just wrote actually verifies before telling anyone the
    # key works.
    written = json.loads(path.read_text(encoding="utf-8"))
    if not keeper.verify_key(key, written.get("keeper")):
        print("the keeper record was written but does not verify", file=sys.stderr)
        return 1

    print(key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
