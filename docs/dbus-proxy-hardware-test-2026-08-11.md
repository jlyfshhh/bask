# D-Bus proxy scanner: hardware test result, 2026-08-11

**The proxy design does not produce readings on the live hardware.** Tested
twice against the real Raspberry Pi and its BlueZ, and the production scanner
config was restored after each run. Do not merge or deploy this until it
delivers readings.

The idea is sound and better than what is on `main`: the parser handles
attacker-controlled radio payloads, so keeping it unprivileged is worth real
effort. This is a note about one unresolved gap, not an argument against the
approach.

## What happens

The scanner starts cleanly and then receives nothing:

```
Bask BLE scanner starting
INFO Linux/BlueZ passive scanning enabled (advert dedup disabled)
INFO scan started
```

Then, over 150 seconds:

| | |
| --- | --- |
| flush lines | **0** |
| D-Bus auth failures | 0 |
| tracebacks | 0 |
| proxy denials logged | 0 |
| container health | healthy |

For comparison, the scanner on `main` emits a flush roughly every five
seconds against the same adapter.

**There is no error anywhere.** The container is healthy, the proxy is healthy,
the monitor registers, and no data arrives. That is the same signature as the
outage on 2026-08-10, which ran for seven hours precisely because nothing
looked wrong.

## What was tried

1. **As written** — `--filter` with three `--call` and three `--broadcast`
   rules. Zero readings. The theory was that `--call` only authorises outgoing
   calls, while registering an AdvertisementMonitor makes BlueZ invoke methods
   back on the client's exported object.
2. **`--filter --talk=org.bluez`** — permits that conversation in both
   directions while still reaching no service other than BlueZ. Also zero
   readings.

So this is not a missing rule in the allow-list. The proxy is not relaying
BlueZ's advertisement callbacks at all, and the next step is establishing
whether `xdg-dbus-proxy` can route method calls back to a client that exports
an object — on the host bus the client's identity is the *proxy's* unique
name, so BlueZ's callbacks are addressed to the proxy, not the scanner.

Worth checking before more filter tuning:

- Whether `bleak`'s passive scanning needs the monitor callbacks at all, or
  whether `InterfacesAdded` / `PropertiesChanged` alone should suffice — if the
  latter, the signals are not reaching the client either, which points at
  match-rule installation rather than callbacks.
- What `BLEAK_DBUS_AUTH_UID: "0"` actually achieves. SASL EXTERNAL is settled
  from kernel-verified peer credentials, so a client cannot simply assert a
  different uid; if the proxy terminates and re-originates the connection, this
  variable may be doing nothing.
- Whether `dbus-broker`/`dbus-daemon` on this host applies its own policy to
  the re-originated connection.

## Reproducing

The harness is at `/home/cc/bask-proxy-test` on the Pi: a checkout of this
branch with `compose.override.yaml` giving the containers distinct names, its
own `testdata` directory, and its own compose project name so it cannot touch
the live stack.

Two things that will waste time otherwise:

- `testdata` must be owned by the container uid (`chown 10001:10001`, mode
  0700). Owned by the login user, the entrypoint dies on
  `mktemp: ... /data/.config.example.XXXXXX: Permission denied` and crashloops
  before BlueZ is ever reached — an inconclusive run that looks like a failure.
- The live scanner must be stopped for the duration; two clients registering
  advertisement monitors on one adapter is not a fair test. Restart it
  afterwards, and confirm readings resume before walking away.

Copy `.env` in for the run and remove it afterwards; it holds secrets and
should not be left lying in a second directory.

## The test that did not catch this

`tests/test_container_boundary.py` passes on this configuration. It asserts the
scanner is non-root, that it depends on the proxy, and that its healthcheck
probes the proxy socket — all true here, and all true while no reading has
arrived in 150 seconds.

That is the same class of test as the one this repository already had, which
asserted the broken arrangement and could never have failed. A configuration
assertion cannot tell whether BlueZ is delivering adverts. What would have
caught it is a check that a reading timestamp advances — the container
healthcheck currently probes the socket, and probing data freshness instead
would turn this silent failure into a red container.
