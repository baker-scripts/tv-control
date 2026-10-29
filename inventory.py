"""devices.yml inventory, the subprocess runner, and per-device locks."""

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

DEVICES_FILE = Path(os.environ.get("DEVICES_FILE", "/scripts/devices.yml"))
DEFAULT_TIMEOUT_S = 20
# A caller waits this long for a device another action holds, then gets "busy".
LOCK_WAIT_S = 30

_locks: dict[str, asyncio.Lock] = {}


# (substring of the lowercased message, cause, next step); first match wins.
ERROR_HINTS = (
    (
        "unauthorized",
        "The device has not trusted this ADB key.",
        "On the TV, tick 'Always allow from this computer' and press Allow, then retry.",
    ),
    (
        "denylist",
        "The app is on the tv-control denylist (denied_apps in devices.yml).",
        "Pick a different app; the denylist is edited in devices.yml.",
    ),
    (
        "pending",
        "The device is marked status: pending in devices.yml or its webOS pairing is unfinished.",
        "Finish enrolment (set its ip and drop status: pending), or for webOS call webos_pair and press Allow on the TV.",
    ),
    (
        "unsupported_on_device",
        "The device type or capabilities do not support this verb.",
        "Check capabilities in devices.yml, or use a verb the device supports.",
    ),
    (
        "busy",
        "Another action holds this device.",
        "Wait about 30 s and retry.",
    ),
    (
        "unknown device",
        "No device with that name is in devices.yml.",
        "Call list_devices for valid names.",
    ),
    (
        "timed out",
        "The device did not answer in time.",
        "Check the TV is on and on the network, retry once, then run health for it.",
    ),
    (
        "timeout",
        "The device did not answer in time.",
        "Check the TV is on and on the network, retry once, then run health for it.",
    ),
    (
        "offline",
        "The device did not answer over ADB on its LAN address or its Tailscale name.",
        "Check it is powered and on the network. The LAN address and the tailnet name are both tried; if both fail, check the site's router or subnet route and that Tailscale is connected.",
    ),
)
DEFAULT_HINT = (
    "The command failed on the device.",
    "Run device_status for the device, then retry.",
)


class DeviceError(Exception):
    """A device lookup or command failed; the message is safe to return.

    cause and next_step default from ERROR_HINTS by message text.
    """

    def __init__(
        self, message: str, cause: str | None = None, next_step: str | None = None
    ) -> None:
        super().__init__(message)
        low = message.lower()
        hint = next((h for h in ERROR_HINTS if h[0] in low), (None, *DEFAULT_HINT))
        self.cause = cause or hint[1]
        self.next_step = next_step or hint[2]

    def body(self) -> dict[str, str]:
        return {"error": str(self), "cause": self.cause, "next_step": self.next_step}


async def run(
    *argv: str, timeout: float = DEFAULT_TIMEOUT_S, merge_stderr: bool = False
) -> tuple[int, bytes, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, b"", f"timeout after {timeout:.0f} s"
    return proc.returncode or 0, out, (err or b"").decode(errors="replace").strip()


async def _yq(expr: str) -> Any:
    rc, out, err = await run("yq", "-o=json", expr, str(DEVICES_FILE), timeout=10)
    if rc != 0:
        raise DeviceError(f"cannot parse {DEVICES_FILE}: {err}")
    return json.loads(out)


async def load_devices() -> list[dict[str, Any]]:
    return await _yq(".devices") or []


async def load_sites() -> dict[str, dict[str, Any]]:
    return await _yq(".sites // {}") or {}


def is_active(dev: dict[str, Any]) -> bool:
    return bool(dev.get("ip")) and dev.get("status") != "pending"


async def load_denied_apps() -> list[str]:
    return [str(x).lower() for x in await _yq(".denied_apps // []") or []]


async def load_quick_apps() -> list[dict[str, Any]]:
    return await _yq(".quick_apps // []") or []


def check_app_allowed(app: str, denied: list[str]) -> None:
    low = app.lower()
    for word in denied:
        if word and word in low:
            raise DeviceError(f"{app!r} refused: matches denylist entry {word!r}")


def require_capability(dev: dict[str, Any], cap: str) -> None:
    if cap not in (dev.get("capabilities") or []):
        raise DeviceError(
            f"unsupported_on_device: {dev['name']} has no {cap!r} capability in devices.yml"
        )


async def get_device(name: str) -> dict[str, Any]:
    for dev in await load_devices():
        if dev.get("name") == name:
            if dev.get("status") == "pending":
                raise DeviceError(f"{name} is status: pending in devices.yml")
            if not dev.get("ip"):
                raise DeviceError(f"{name} has no ip in devices.yml yet (pending)")
            return dev
    raise DeviceError(f"unknown device {name!r}; call list_devices")


@contextlib.asynccontextmanager
async def device_lock(name: str) -> AsyncIterator[None]:
    """Serialize every action on one device (or one site scan)."""
    lock = _locks.setdefault(name, asyncio.Lock())
    try:
        async with asyncio.timeout(LOCK_WAIT_S):
            await lock.acquire()
    except TimeoutError:
        raise DeviceError(f"{name} is busy with another action; retry later") from None
    try:
        yield
    finally:
        lock.release()
