"""QC-12/QC-23: pin the separation between web, Bluetooth, and host data."""
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent


def main():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    services = compose["services"]
    assert {"bask", "bask-scanner"}.issubset(services)
    web = services["bask"]
    scanner = services["bask-scanner"]

    dbus = "/var/run/dbus/system_bus_socket"
    assert all(dbus not in str(volume) for volume in web.get("volumes", [])), \
        "the network-facing web service can reach host D-Bus"
    assert any(dbus in str(volume) for volume in scanner.get("volumes", []))
    assert not scanner.get("ports"), "the scanner must not publish a network port"
    assert scanner.get("network_mode") == "none"

    # Shared floor for both services.
    for name, service in (("web", web), ("scanner", scanner)):
        assert "ALL" in service.get("cap_drop", []), name
        assert "no-new-privileges:true" in service.get("security_opt", []), name
        assert service.get("pids_limit"), name
        assert service.get("mem_limit"), name

    # The web service is the network-facing one, so it carries full hardening.
    assert web.get("user") and not str(web["user"]).startswith("0:")
    assert web.get("read_only") is True
    assert any(str(mount).startswith("/tmp:") for mount in web.get("tmpfs", []))
    assert "DAC_OVERRIDE" not in web.get("cap_add", []), \
        "the network-facing service must not regain write override"

    # The scanner is root on purpose. BlueZ grants AdvertisementMonitor1 — the
    # interface passive scanning needs — to root only, and refuses anyone else
    # at D-Bus authentication. An earlier attempt to run this as uid 10001
    # stopped every sensor reading for seven hours, and the previous version of
    # this test asserted the broken configuration, so it could never have
    # caught it. These asserts now pin the requirement in place.
    assert str(scanner.get("user")) == "0:0", \
        "the scanner must stay root or BlueZ refuses passive scanning"
    assert "DAC_OVERRIDE" in scanner.get("cap_add", []), \
        "cap_drop ALL removes CAP_DAC_OVERRIDE, which root needs to write /data"
    # What compensates for that root: the scanner is unreachable from anywhere.
    assert scanner.get("network_mode") == "none"
    assert not scanner.get("ports")
    assert all(
        str(volume).endswith(":ro")
        for volume in scanner.get("volumes", [])
        if dbus in str(volume)
    ), "the D-Bus socket must be mounted read-only"

    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "USER bask:bask" in dockerfile
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
