"""Lint a devices.yml (default: devices.example.yml; set DEVICES_FILE to lint a real one)."""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEVICES = Path(os.environ.get("DEVICES_FILE", ROOT / "devices.example.yml"))
CAPS = {"find_remote", "wake", "power", "inputs", "sleep_timer"}


def inventory() -> dict[str, Any]:
    out = subprocess.run(
        ["yq", "-o=json", ".", str(DEVICES)], capture_output=True, check=True
    )
    return json.loads(out.stdout)


def test_pending_devices_have_no_ip_and_capabilities_are_known() -> None:
    for dev in inventory()["devices"]:
        if dev.get("status") == "pending":
            assert not dev.get("ip"), dev["name"]
        assert set(dev.get("capabilities") or []) <= CAPS, dev["name"]
        if "find_remote" in (dev.get("capabilities") or []):
            assert dev["type"] == "shield", dev["name"]


def test_device_names_are_unique() -> None:
    names = [d["name"] for d in inventory()["devices"]]
    assert len(names) == len(set(names))


def test_every_site_with_devices_is_declared() -> None:
    data = inventory()
    used = {d["site"] for d in data["devices"] if d.get("site")}
    assert used <= set(data.get("sites") or {}), sorted(used - set(data["sites"]))


def test_webos_mac_format() -> None:
    mac = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
    for dev in inventory()["devices"]:
        if dev.get("mac"):
            assert mac.match(str(dev["mac"])), dev["name"]


def test_tailnet_fqdn_shares_one_suffix() -> None:
    suffixes = {
        d["tailnet_fqdn"].split(".", 1)[1]
        for d in inventory()["devices"]
        if d.get("tailnet_fqdn")
    }
    assert len(suffixes) <= 1
