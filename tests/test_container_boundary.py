"""QC-12/QC-23: pin the separation between web, Bluetooth, and host data."""
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent


def main():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    services = compose["services"]
    assert {"bask", "bask-scanner", "bask-dbus-proxy"}.issubset(services)
    web = services["bask"]
    scanner = services["bask-scanner"]
    proxy = services["bask-dbus-proxy"]

    host_dbus = "/var/run/dbus/system_bus_socket"
    assert all(host_dbus not in str(volume) for volume in web.get("volumes", [])), \
        "the network-facing web service can reach host D-Bus"
    assert all(host_dbus not in str(volume) for volume in scanner.get("volumes", [])), \
        "the radio parser must never receive the host system bus"
    assert any(host_dbus in str(volume) for volume in proxy.get("volumes", [])), \
        "the filtering proxy needs the host system bus"
    assert not scanner.get("ports"), "the scanner must not publish a network port"
    assert scanner.get("network_mode") == "none"

    # Shared floor for all three trust domains.
    for name, service in (("web", web), ("scanner", scanner), ("proxy", proxy)):
        assert "ALL" in service.get("cap_drop", []), name
        assert "no-new-privileges:true" in service.get("security_opt", []), name
        assert service.get("pids_limit"), name
        assert service.get("mem_limit"), name
        assert service.get("read_only") is True, name

    # The web service is the network-facing one, so it carries full hardening.
    assert web.get("user") and not str(web["user"]).startswith("0:")
    assert web.get("read_only") is True
    assert any(str(mount).startswith("/tmp:") for mount in web.get("tmpfs", []))
    assert "DAC_OVERRIDE" not in web.get("cap_add", []), \
        "the network-facing service must not regain write override"

    # The scanner parses attacker-controlled radio packets, so keep it
    # unprivileged and give it only a filtered D-Bus socket plus app data.
    assert scanner.get("user") and not str(scanner["user"]).startswith("0:")
    assert not scanner.get("cap_add")
    assert scanner.get("network_mode") == "none"
    assert not scanner.get("ports")
    scanner_env = scanner.get("environment", {})
    assert scanner_env.get("DBUS_SYSTEM_BUS_ADDRESS") == \
        "unix:path=/run/bask-dbus/system_bus_socket"
    assert str(scanner_env.get("BLEAK_DBUS_AUTH_UID")) == "0"
    assert any(str(v) == "bask-dbus:/run/bask-dbus:ro"
               for v in scanner.get("volumes", []))
    assert scanner.get("depends_on", {}).get("bask-dbus-proxy", {}).get("condition") \
        == "service_healthy"
    assert "system_bus_socket" in " ".join(
        str(part) for part in scanner.get("healthcheck", {}).get("test", [])
    ), "scanner health must test the proxy socket as its non-root uid"

    # Only the small audited proxy authenticates as root. It cannot reach the
    # network or application data and its host-bus mount is protocol-filtered.
    assert str(proxy.get("user", "")).startswith("0:")
    assert proxy.get("network_mode") == "none"
    assert not proxy.get("ports")
    assert not proxy.get("cap_add")
    assert any(
        str(v) == f"{host_dbus}:/host/run/dbus/system_bus_socket:ro"
        for v in proxy.get("volumes", [])
    )
    assert all("/data" not in str(v) for v in proxy.get("volumes", [])), \
        "the D-Bus proxy must not receive Bask data"

    proxy_script = (ROOT / "dbus-proxy-entrypoint.sh").read_text()
    for rule in (
        "ObjectManager.GetManagedObjects@/",
        "AdvertisementMonitorManager1.RegisterMonitor@/org/bluez/*",
        "AdvertisementMonitorManager1.UnregisterMonitor@/org/bluez/*",
        "ObjectManager.InterfacesAdded@/",
        "ObjectManager.InterfacesRemoved@/",
        "Properties.PropertiesChanged@/org/bluez/*",
        "umask 007",
    ):
        assert rule in proxy_script, rule
    assert "--talk=org.bluez" not in proxy_script, \
        "broad BlueZ access would allow pairing and device control"
    assert "--see=org.bluez" not in proxy_script, \
        "the exact call/signal rules already provide required name visibility"

    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "USER bask:bask" in dockerfile
    assert "xdg-dbus-proxy" in dockerfile
    assert "dbus-proxy-entrypoint.sh" in dockerfile
    for label in (
        "org.opencontainers.image.source",
        "org.opencontainers.image.revision",
        "org.opencontainers.image.version",
        "org.opencontainers.image.licenses",
    ):
        assert label in dockerfile, label
    assert "BASK_ROLE: web" in (ROOT / "compose.yaml").read_text()
    assert "BASK_ROLE: scanner" in (ROOT / "compose.yaml").read_text()

    installer = (ROOT / "deploy" / "install.sh").read_text()
    for marker in ("BASK_UID", "BASK_GID", "chmod 0700", "chmod 0600"):
        assert marker in installer, marker

    publish = (ROOT / ".github" / "workflows" / "publish-image.yml").read_text()
    assert "provenance: mode=max" in publish
    assert "sbom: true" in publish
    print("Container boundary and private-file tests passed.")


if __name__ == "__main__":
    main()
