#!/usr/bin/env bash
# Does the scanner's AppArmor profile actually permit what BlueZ needs?
#
# Ubuntu-family distributions mediate D-Bus through AppArmor; Debian and
# Raspberry Pi OS do not. An unmentioned mediation class is denied, and Docker's
# generated docker-default profile says nothing about D-Bus — so on Ubuntu the
# scanner's first call to the system bus is refused and it never sees a sensor.
#
# This is checked on a host with AppArmor actually enabled, because the failure
# only exists there. Everywhere else it skips: a profile that parses on a
# machine with no AppArmor proves nothing about whether it works.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
profile="$root/deploy/apparmor/bask-scanner"

[[ -f "$profile" ]] || { echo "Missing $profile" >&2; exit 1; }

if [[ ! -d /sys/kernel/security/apparmor ]] || ! command -v apparmor_parser >/dev/null 2>&1; then
  echo "AppArmor profile tests skipped — no AppArmor on this host." >&2
  exit 0
fi
if ! docker info >/dev/null 2>&1; then
  echo "AppArmor profile tests skipped — no usable Docker." >&2
  exit 0
fi

sudo apparmor_parser -r -W "$profile"
echo "  profile loads into the running kernel"

cleanup() { sudo apparmor_parser -R "$profile" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# The scanner reaches the bus through the host's socket, exactly as compose
# mounts it. Without a system bus there is nothing to be denied by, so the test
# would pass for the wrong reason.
if [[ ! -S /var/run/dbus/system_bus_socket ]]; then
  echo "AppArmor profile tests skipped — no system D-Bus socket to test against." >&2
  exit 0
fi

# Build the probe once, with network, so the probes themselves can run without
# one. D-Bus travels over the mounted socket, so network mode is irrelevant to
# what is being measured — but apt is not, and installing inside a --network
# none container is how the first version of this test failed for a reason that
# had nothing to do with AppArmor.
probe_image="bask-apparmor-probe:test"
if ! docker image inspect "$probe_image" >/dev/null 2>&1; then
  printf 'FROM debian:stable-slim\nRUN apt-get update -qq && apt-get install -y -qq dbus && rm -rf /var/lib/apt/lists/*\n' \
    | docker build -q -t "$probe_image" - >/dev/null
fi

probe() {
  docker run --rm --security-opt "apparmor=$1" --network none \
    -v /var/run/dbus/system_bus_socket:/var/run/dbus/system_bus_socket:ro \
    -e DBUS_SYSTEM_BUS_ADDRESS=unix:path=/var/run/dbus/system_bus_socket \
    "$probe_image" \
    dbus-send --system --dest=org.freedesktop.DBus --print-reply \
      /org/freedesktop/DBus org.freedesktop.DBus.ListNames 2>&1
}

# The call in the report a keeper sent was AddMatch against the bus driver, and
# ListNames exercises the same mediation: a confined sender talking to
# org.freedesktop.DBus on the system bus.
if probe bask-scanner | grep -q "org.freedesktop.DBus"; then
  echo "  a container under the profile can talk to the system bus"
else
  echo "The profile does not permit the system-bus calls the scanner needs." >&2
  probe bask-scanner | tail -5 >&2
  exit 1
fi

# And it has to still be a confinement. If docker-default already allowed this,
# the profile would be pointless and this suite would be proving nothing.
if probe docker-default | grep -q "org.freedesktop.DBus"; then
  echo "docker-default already permits this, so the custom profile is unnecessary." >&2
  echo "Either AppArmor is not mediating D-Bus here, or the profile can be dropped." >&2
  exit 1
fi
echo "  docker-default is still denied, so the profile is doing the work"

# ListNames only proves the bus *driver* is reachable. The scanner's real work
# is with BlueZ — org.bluez, a different destination AppArmor mediates
# separately — so a profile could pass every check above and still deny the one
# peer that matters. Address org.bluez directly.
#
# This stays hardware-independent: with no Bluetooth adapter org.bluez has no
# owner, and the bus answers ServiceUnknown/NameHasNoOwner. That still proves
# the send was *permitted* — the refusal came from the bus, not from AppArmor.
# Only an AppArmor denial (or a blocked Hello, which fails the whole connection)
# is a failure.
bluez_probe() {
  docker run --rm --security-opt "apparmor=$1" --network none \
    -v /var/run/dbus/system_bus_socket:/var/run/dbus/system_bus_socket:ro \
    -e DBUS_SYSTEM_BUS_ADDRESS=unix:path=/var/run/dbus/system_bus_socket \
    "$probe_image" \
    dbus-send --system --dest=org.bluez --print-reply \
      /org/bluez org.freedesktop.DBus.Introspectable.Introspect 2>&1 || true
}

# AppArmor's D-Bus refusals name AppArmor in the message, on the send itself
# ("An AppArmor policy prevents this sender...") and on the Hello that opens the
# connection ("Failed to open connection ... AppArmor"). A bus-level
# ServiceUnknown/NameHasNoOwner does not, and means the send was allowed. Match
# AppArmor specifically so an unrelated D-Bus policy quirk is not read as one.
denied='AppArmor'

if bluez_probe bask-scanner | grep -Eq "$denied"; then
  echo "The profile blocks org.bluez, the destination the scanner actually uses." >&2
  bluez_probe bask-scanner | tail -5 >&2
  exit 1
fi
echo "  the profile reaches org.bluez itself, not just the bus driver"

# docker-default cannot even greet the bus (asserted above), so a container
# under it certainly cannot reach org.bluez. If that send is somehow allowed,
# AppArmor is not mediating destinations here and the BlueZ check above proved
# nothing — fail rather than pass for the wrong reason.
if ! bluez_probe docker-default | grep -Eq "$denied"; then
  echo "docker-default was not denied for org.bluez; AppArmor is not mediating this path." >&2
  bluez_probe docker-default | tail -5 >&2
  exit 1
fi
echo "  docker-default is denied org.bluez, so the profile's BlueZ rule is what allows it"

echo "AppArmor profile tests passed."
