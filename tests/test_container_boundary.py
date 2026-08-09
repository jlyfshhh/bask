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

    for name, service in (("web", web), ("scanner", scanner)):
        assert service.get("user") and not str(service["user"]).startswith("0:"), name
        assert service.get("read_only") is True, name
        assert "ALL" in service.get("cap_drop", []), name
        assert "no-new-privileges:true" in service.get("security_opt", []), name
        assert service.get("pids_limit"), name
        assert service.get("mem_limit"), name
        assert any(str(mount).startswith("/tmp:") for mount in service.get("tmpfs", [])), name

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
    for marker in ("BASK_UID", "BASK_GID", "BASK_BLUETOOTH_GID", "chmod 0700", "chmod 0600"):
        assert marker in installer, marker

    publish = (ROOT / ".github" / "workflows" / "publish-image.yml").read_text()
    assert "provenance: mode=max" in publish
    assert "sbom: true" in publish
    print("Container boundary and private-file tests passed.")


if __name__ == "__main__":
    main()
