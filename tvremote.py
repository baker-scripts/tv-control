"""Fast key path for Shields: Android TV Remote v2 over one persistent connection per device.

`adb shell input keyevent` starts a JVM process per press (about 1 s); the Remote v2 protocol
(androidtvremote2, as in Home Assistant) sends a key in milliseconds on an open socket.
A Shield uses it only after pairing (on-screen code, admin-only `pair` verb); until then, and on
any failure, send_key returns None and the caller falls back to ADB. Certs live under REMOTE_DIR.
"""

import asyncio
import os
import time
from collections.abc import Callable
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
# The library notices a dead socket itself (server pings every 5 s, idle close at 16 s) and
# reconnects with 0.1 s..30 s backoff; a press waits this long for that before using ADB.
RECONNECT_WAIT_S = 1.5
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
_available: dict[str, bool] = {}
_auth_failed: set[str] = set()
_locks: dict[str, asyncio.Lock] = {}
_warming: set[asyncio.Task[Any]] = set()


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
    return AndroidTVRemote(CLIENT_NAME, cert, key, dev["ip"], enable_ime=True)


def should_use_remote(dev: dict[str, Any], key: str) -> bool:
    return key not in ADB_ONLY_KEYS and is_paired(dev)


def _drop(name: str) -> None:
    _available.pop(name, None)
    remote = _remotes.pop(name, None)
    if remote is not None:
        remote.disconnect()
    _retry_at[name] = time.monotonic() + RETRY_AFTER_S


def _availability_setter(name: str) -> Callable[[bool], None]:
    def set_available(ok: bool) -> None:
        _available[name] = ok

    return set_available


async def _connected(dev: dict[str, Any]) -> AndroidTVRemote:
    name = dev["name"]
    async with _locks.setdefault(name, asyncio.Lock()):
        remote = _remotes.get(name)
        if remote is None:
            remote = _new(dev)
            await asyncio.wait_for(remote.async_connect(), CONNECT_TIMEOUT_S)
            _available[name] = True
            # send_key_command silently drops data on a closed transport, so the library's
            # availability callback (False on loss, True after its own reconnect) gates presses.
            remote.add_is_available_updated_callback(_availability_setter(name))
            remote.keep_reconnecting()
            _remotes[name] = remote
        return remote


async def _wait_available(name: str) -> bool:
    deadline = time.monotonic() + RECONNECT_WAIT_S
    while not _available.get(name, False):
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


async def _ready(dev: dict[str, Any]) -> AndroidTVRemote | None:
    """The open, available connection, or None when the caller must use ADB."""
    name = dev["name"]
    if not is_paired(dev) or name in _auth_failed:
        return None
    if time.monotonic() < _retry_at.get(name, 0.0):
        return None
    try:
        remote = await _connected(dev)
        if not await _wait_available(name):
            return None
    except InvalidAuth:
        _auth_failed.add(name)
        _drop(name)
        return None
    except FAILURES:
        _drop(name)
        return None
    return remote


def warm(dev: dict[str, Any]) -> None:
    """Open the connection in the background: a first press otherwise waits about 0.8 s for it."""
    if dev["name"] in _remotes or not is_paired(dev):
        return
    task = asyncio.create_task(_ready(dev))
    _warming.add(task)
    task.add_done_callback(_warming.discard)


def fast_key_state(dev: dict[str, Any]) -> str:
    """unsupported, needs_pairing (unpaired or the cert was rejected), or ready."""
    if not eligible(dev):
        return "unsupported"
    if not is_paired(dev) or dev["name"] in _auth_failed:
        return "needs_pairing"
    return "ready"


def remote_app(name: str) -> str | None:
    remote = _remotes.get(name)
    return remote.current_app if remote is not None else None


async def send_key(dev: dict[str, Any], key: str) -> dict[str, Any] | None:
    """Send one key over the open connection; None means the caller must use ADB."""
    if key in ADB_ONLY_KEYS:
        return None
    remote = await _ready(dev)
    if remote is None:
        return None
    try:
        remote.send_key_command(key)
    except FAILURES:
        _drop(dev["name"])
        return None
    return {"sent": key, "via": "remote"}


async def send_text(dev: dict[str, Any], text: str) -> dict[str, Any] | None:
    """Type into the focused field through the IME; None means the caller must use ADB."""
    remote = await _ready(dev)
    if remote is None:
        return None
    try:
        remote.send_text(text)
    except FAILURES:
        _drop(dev["name"])
        return None
    return {"typed": len(text), "via": "remote"}


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
    _auth_failed.discard(dev["name"])
    stale = _remotes.pop(dev["name"], None)
    if stale is not None:
        stale.disconnect()
    _available.pop(dev["name"], None)
    return {"paired": True}
