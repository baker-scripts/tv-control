"""Fast key path for Shields: Android TV Remote v2 over one persistent connection per device.

`adb shell input keyevent` starts a JVM process per press (about 1 s); the Remote v2 protocol
(androidtvremote2, as in Home Assistant) sends a key in milliseconds on an open socket.
A Shield uses it only after pairing (on-screen code, admin-only `pair` verb); until then, and on
any failure, send_key returns None and the caller falls back to ADB. Certs live under REMOTE_DIR.
"""

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from androidtvremote2 import (
    AndroidTVRemote,
    CannotConnect,
    ConnectionClosed,
    InvalidAuth,
)
from inventory import DeviceError

REMOTE_DIR = Path(os.environ.get("REMOTE_DIR", "/config/remote"))
CLIENT_NAME = "tv-control"
CONNECT_TIMEOUT_S = 3.0
RETRY_AFTER_S = 30.0
# Wake and sleep must work while the Shield's remote service is down, so they stay on ADB.
ADB_ONLY_KEYS = frozenset({"SLEEP", "WAKEUP", "POWER"})
FAILURES = (
    CannotConnect,
    ConnectionClosed,
    InvalidAuth,
    TimeoutError,
    OSError,
    ValueError,
)

_remotes: dict[str, AndroidTVRemote] = {}
_pairing: dict[str, AndroidTVRemote] = {}
_retry_at: dict[str, float] = {}
_locks: dict[str, asyncio.Lock] = {}


def _paths(name: str) -> tuple[str, str, Path]:
    return (
        str(REMOTE_DIR / f"{name}-cert.pem"),
        str(REMOTE_DIR / f"{name}-key.pem"),
        REMOTE_DIR / f"{name}.paired",
    )


def eligible(dev: dict[str, Any]) -> bool:
    return dev.get("type") == "shield" and bool(dev.get("ip"))


def is_paired(dev: dict[str, Any]) -> bool:
    return eligible(dev) and _paths(dev["name"])[2].exists()


def _new(dev: dict[str, Any]) -> AndroidTVRemote:
    REMOTE_DIR.mkdir(parents=True, exist_ok=True)
    cert, key, _ = _paths(dev["name"])
    return AndroidTVRemote(CLIENT_NAME, cert, key, dev["ip"], enable_ime=False)


def _drop(name: str) -> None:
    remote = _remotes.pop(name, None)
    if remote is not None:
        remote.disconnect()
    _retry_at[name] = time.monotonic() + RETRY_AFTER_S


async def _connected(dev: dict[str, Any]) -> AndroidTVRemote:
    name = dev["name"]
    async with _locks.setdefault(name, asyncio.Lock()):
        remote = _remotes.get(name)
        if remote is None:
            remote = _new(dev)
            await asyncio.wait_for(remote.async_connect(), CONNECT_TIMEOUT_S)
            remote.keep_reconnecting()
            _remotes[name] = remote
        return remote


async def send_key(dev: dict[str, Any], key: str) -> dict[str, Any] | None:
    """Send one key over the open connection; None means the caller must use ADB."""
    name = dev["name"]
    if key in ADB_ONLY_KEYS or not is_paired(dev):
        return None
    if time.monotonic() < _retry_at.get(name, 0.0):
        return None
    try:
        (await _connected(dev)).send_key_command(key)
    except FAILURES:
        _drop(name)
        return None
    return {"sent": key, "via": "remote"}


async def start_pair(dev: dict[str, Any]) -> dict[str, Any]:
    if not eligible(dev):
        raise DeviceError("unsupported_on_device: only Shields pair for fast keys")
    remote = _new(dev)
    try:
        await remote.async_generate_cert_if_missing()
        await remote.async_start_pairing()
    except (CannotConnect, ConnectionClosed) as exc:
        raise DeviceError(f"{dev['name']} did not answer the pairing request") from exc
    old = _pairing.pop(dev["name"], None)
    if old is not None:
        old.disconnect()
    _pairing[dev["name"]] = remote
    return {"pairing": "started", "next_step": "Enter the code shown on the TV."}


async def finish_pair(dev: dict[str, Any], code: str) -> dict[str, Any]:
    remote = _pairing.pop(dev["name"], None)
    if remote is None:
        raise DeviceError("no pairing in progress; start pairing first")
    try:
        await remote.async_finish_pairing(code.strip())
    except (InvalidAuth, ConnectionClosed) as exc:
        raise DeviceError("pairing code rejected; start pairing again") from exc
    _paths(dev["name"])[2].touch()
    _retry_at.pop(dev["name"], None)
    return {"paired": True}
