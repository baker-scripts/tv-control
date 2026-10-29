"""Enroll: probe a routed site LAN for TV ports and stage operator-approved entries.

The container cannot edit devices.yml (/scripts is read-only, no git). An approved hit is
written to ENROLL_DIR as a devices.yml snippet that Claude lands with a CI-gated commit.
"""

import asyncio
import contextlib
import ipaddress
import os
import re
import time
from pathlib import Path
from typing import Any

from inventory import DeviceError, load_devices, load_sites

SCAN_PORTS = {5555: "adb", 3000: "webos", 3001: "webos"}
PROBE_TIMEOUT_S = 1
SCAN_CONCURRENCY = 64
MIN_PREFIX = 24
ENROLL_DIR = Path(os.environ.get("ENROLL_DIR", "/config/enroll"))
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,62}$")
ENROLL_TYPES = frozenset({"shield", "firestick", "firetv", "webos"})
CONFIGS = frozenset({"simple", "advanced"})


async def site_network(site: str) -> ipaddress.IPv4Network:
    cidr = ((await load_sites()).get(site) or {}).get("cidr")
    if not cidr:
        raise DeviceError(
            f"site {site!r} has no routed LAN (sites.{site}.cidr in devices.yml)"
        )
    net = ipaddress.ip_network(cidr, strict=True)
    if (
        not isinstance(net, ipaddress.IPv4Network)
        or not net.is_private
        or net.prefixlen < MIN_PREFIX
    ):
        raise DeviceError(f"{cidr} must be a private IPv4 /{MIN_PREFIX} or smaller")
    return net


async def _open(ip: str, port: int, sem: asyncio.Semaphore) -> bool:
    async with sem:
        try:
            async with asyncio.timeout(PROBE_TIMEOUT_S):
                _, writer = await asyncio.open_connection(ip, port)
        except (OSError, TimeoutError):
            return False
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        return True


async def scan_site(site: str) -> dict[str, Any]:
    net = await site_network(site)
    known: set[str] = set()
    for dev in await load_devices():
        known |= {str(dev.get(k)) for k in ("ip", "tailnet_ip") if dev.get(k)}
    sem = asyncio.Semaphore(SCAN_CONCURRENCY)
    targets = [(str(ip), port) for ip in net.hosts() for port in SCAN_PORTS]
    started = time.monotonic()
    found = await asyncio.gather(*(_open(ip, port, sem) for ip, port in targets))
    hits: dict[str, list[int]] = {}
    for (ip, port), is_open in zip(targets, found, strict=True):
        if is_open:
            hits.setdefault(ip, []).append(port)
    unknown = [
        {
            "ip": ip,
            "ports": ports,
            "guessed_type": "webos"
            if {3000, 3001} & set(ports)
            else "adb (shield, firestick, or firetv)",
        }
        for ip, ports in sorted(
            hits.items(), key=lambda kv: ipaddress.ip_address(kv[0])
        )
        if ip not in known
    ]
    return {
        "site": site,
        "cidr": str(net),
        "probed": len(targets),
        "seconds": round(time.monotonic() - started, 1),
        "known_hits": sorted(set(hits) & known, key=ipaddress.ip_address),
        "unknown": unknown,
    }


async def stage_enroll(req: dict[str, Any]) -> dict[str, Any]:
    """Validate an approved hit and write its devices.yml entry for Claude to land."""
    site, name, dev_type = (
        str(req.get("site", "")),
        str(req.get("name", "")),
        str(req.get("type", "")),
    )
    net = await site_network(site)
    try:
        ip = ipaddress.ip_address(str(req.get("ip", "")))
        port = int(req.get("port", 0))
    except ValueError:
        raise DeviceError("ip or port is not valid") from None
    if ip not in net:
        raise DeviceError(f"{ip} is outside {net}")
    if port not in SCAN_PORTS:
        raise DeviceError(f"port must be one of {sorted(SCAN_PORTS)}")
    if not NAME_RE.match(name):
        raise DeviceError("name must be 3-63 chars of a-z, 0-9, and -")
    if dev_type not in ENROLL_TYPES:
        raise DeviceError(f"type must be one of {sorted(ENROLL_TYPES)}")
    if any(d.get("name") == name for d in await load_devices()):
        raise DeviceError(f"{name} already exists in devices.yml")
    lines = [
        f"  - name: {name}",
        f"    site: {site}",
        f'    ip: "{ip}"',
        f"    port: {port}",
        f"    type: {dev_type}",
    ]
    if dev_type != "webos":
        config = str(req.get("config", ""))
        if config not in CONFIGS:
            raise DeviceError(f"config must be one of {sorted(CONFIGS)}")
        lines.append(f"    config: {config}")
    snippet = "\n".join(lines) + "\n"
    ENROLL_DIR.mkdir(parents=True, exist_ok=True)
    utc = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = ENROLL_DIR / f"{utc}-{name}.yml"
    path.write_text(snippet)
    return {
        "staged": str(path),
        "snippet": snippet,
        "next": "tell Claude to land the staged enroll entry",
    }
