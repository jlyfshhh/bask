#!/bin/sh
set -eu

# The host bus sees this proxy's uid (root), which is required by BlueZ's
# AdvertisementMonitor1 policy. Bask's scanner connects to the new socket as an
# ordinary uid and never sees the host socket itself.
proxy_dir=/run/bask-dbus
proxy_socket="$proxy_dir/system_bus_socket"
mkdir -p "$proxy_dir"
rm -f "$proxy_socket"

# xdg-dbus-proxy creates its socket using the process umask. Compose starts
# this process as root:BASK_GID and the scanner uses that same group, so 007
# permits only the proxy and scanner to connect. A 077 umask would silently
# create a root-only socket and strand the non-root scanner.
umask 007

exec xdg-dbus-proxy \
  unix:path=/host/run/dbus/system_bus_socket \
  "$proxy_socket" \
  --filter \
  --call=org.bluez=org.freedesktop.DBus.ObjectManager.GetManagedObjects@/ \
  --call=org.bluez=org.bluez.AdvertisementMonitorManager1.RegisterMonitor@/org/bluez/* \
  --call=org.bluez=org.bluez.AdvertisementMonitorManager1.UnregisterMonitor@/org/bluez/* \
  --broadcast=org.bluez=org.freedesktop.DBus.ObjectManager.InterfacesAdded@/ \
  --broadcast=org.bluez=org.freedesktop.DBus.ObjectManager.InterfacesRemoved@/ \
  --broadcast=org.bluez=org.freedesktop.DBus.Properties.PropertiesChanged@/org/bluez/*
