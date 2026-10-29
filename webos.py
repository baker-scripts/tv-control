"""LG webOS TVs through aiowebostv. Client keys live in WEBOS_KEYS_FILE, never in git."""

import asyncio
import contextlib
import json
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import aiohttp
from aiowebostv import WebOsClient
from inventory import DeviceError, load_sites, run

WEBOS_KEYS_FILE = Path(os.environ.get("WEBOS_KEYS_FILE", "/config/webos-keys.json"))
WOL_KEY = Path(os.environ.get("WOL_KEY_FILE", "/config/wol-ssh/id_ed25519"))
WEBOS_TIMEOUT_S = 15
WEBOS_PAIR_TIMEOUT_S = 60
WOL_TIMEOUT_S = 20
RETRY_DELAY_S = 1.0
# WSMessageTypeError is a TypeError: the TV sent a close frame mid-handshake (transient).
RETRYABLE = (TimeoutError, OSError, aiohttp.ClientError, aiohttp.WSMessageTypeError)
MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
TOAST_MAX_CHARS = 200

# Allowlisted key name -> pointer-socket button name (aiowebostv.buttons.BUTTONS).
BUTTONS = {
    "HOME": "HOME",
    "BACK": "BACK",
    "DPAD_UP": "UP",
    "DPAD_DOWN": "DOWN",
    "DPAD_LEFT": "LEFT",
    "DPAD_RIGHT": "RIGHT",
    "DPAD_CENTER": "ENTER",
    "MEDIA_PLAY": "PLAY",
    "MEDIA_PAUSE": "PAUSE",
    "MEDIA_STOP": "STOP",
    "MEDIA_FAST_FORWARD": "FASTFORWARD",
    "MEDIA_REWIND": "REWIND",
}


def read_keys() -> dict[str, str]:
    try:
        data: dict[str, str] = json.loads(WEBOS_KEYS_FILE.read_text())
        return data
    except FileNotFoundError:
        return {}


def is_paired(name: str) -> bool:
    return bool(read_keys().get(name))


def write_key(name: str, key: str) -> None:
    keys = {**read_keys(), name: key}
    tmp = WEBOS_KEYS_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(keys, fh, indent=2)
    tmp.replace(WEBOS_KEYS_FILE)


@contextlib.asynccontextmanager
async def client(
    dev: dict[str, Any], key: str | None, timeout: float = WEBOS_TIMEOUT_S
) -> AsyncIterator[WebOsClient]:
    tv = WebOsClient(dev["ip"], key)
    try:
        async with asyncio.timeout(timeout):
            await tv.connect()
        yield tv
    finally:
        await tv.disconnect()


async def call[T](dev: dict[str, Any], fn: Callable[[WebOsClient], Awaitable[T]]) -> T:
    """Connect with the stored client key, run fn, and retry once on a timeout or socket error.

    A fresh WebOsClient per attempt re-registers with the stored key, so a stale socket (TV
    standby, container recreate) cannot poison the retry. fn is bounded by WEBOS_TIMEOUT_S too.
    """
    key = read_keys().get(dev["name"])
    if not key:
        raise DeviceError("pairing pending: call webos_pair and press Allow on the TV")
    for attempt in (1, 2):
        try:
            async with client(dev, key) as tv:
                async with asyncio.timeout(WEBOS_TIMEOUT_S):
                    return await fn(tv)
        except DeviceError:
            raise
        except RETRYABLE as exc:
            if attempt == 2:
                raise DeviceError(
                    f"webOS call timed out after one reconnect ({type(exc).__name__})"
                ) from None
            await asyncio.sleep(RETRY_DELAY_S)
        except Exception as exc:  # noqa: BLE001 - aiowebostv raises aiohttp, pairing, and command errors
            raise DeviceError(f"webOS call failed: {type(exc).__name__}") from None
    raise AssertionError("unreachable")


async def status(dev: dict[str, Any]) -> dict[str, Any]:
    if not is_paired(dev["name"]):
        return {
            "webos_state": "not_paired",
            "hint": "pairing pending: call webos_pair once; press Allow on the TV",
        }

    async def read(tv: WebOsClient) -> dict[str, Any]:
        st = tv.tv_state
        inputs = cast(list[dict[str, Any]], await tv.get_inputs() or [])
        current_input = next(
            (i["id"] for i in inputs if i.get("appId") == st.current_app_id),
            None,
        )
        input_ids = {i["id"] for i in inputs}
        return {
            "webos_state": "connected",
            "is_on": st.is_on,
            "is_screen_on": st.is_screen_on,
            "power_state": st.power_state.get("state"),
            "current_app": st.current_app_id,
            "current_input": current_input,
            "input_errors": {
                key: f"Unknown configured input {key}"
                for key in dev.get("inputs", {})
                if key not in input_ids
            },
            "volume": st.volume,
            "muted": st.muted,
        }

    try:
        return await call(dev, read)
    except DeviceError as exc:
        return {"webos_state": "unreachable", "detail": str(exc)}


async def pair(dev: dict[str, Any]) -> dict[str, Any]:
    try:
        async with client(dev, None, WEBOS_PAIR_TIMEOUT_S) as tv:
            write_key(dev["name"], str(tv.client_key))
    except Exception as exc:  # noqa: BLE001 - aiowebostv raises aiohttp, pairing, and command errors
        raise DeviceError(f"pairing failed: {type(exc).__name__}") from None
    return {"paired": True}


async def wake(dev: dict[str, Any]) -> dict[str, Any]:
    """Send a magic packet from the site's WoL relay; a packet from the server does not cross a routed link."""
    mac = str(dev.get("mac") or "")
    if not MAC_RE.match(mac):
        raise DeviceError(
            f"{dev['name']} has no valid mac in devices.yml",
            cause="WoL needs the TV's MAC address.",
            next_step="Add mac: to the device in devices.yml.",
        )
    relay = ((await load_sites()).get(str(dev.get("site"))) or {}).get("wol_relay")
    if not relay:
        raise DeviceError(
            f"no wol_relay for site {dev.get('site')!r} in devices.yml",
            cause="No LAN host is configured to send WoL for this site.",
            next_step="Set sites.<site>.wol_relay (user@host) in devices.yml.",
        )
    if not WOL_KEY.exists():
        raise DeviceError(
            f"WoL relay key missing at {WOL_KEY}",
            cause="The container has no SSH key for the WoL relay yet.",
            next_step=f"Install the forced-command key at {WOL_KEY} and its authorized_keys entry on {relay}.",
        )
    rc, _, err = await run(
        "ssh",
        "-i",
        str(WOL_KEY),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        f"UserKnownHostsFile={WOL_KEY.parent / 'known_hosts'}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=8",
        relay,
        "wol",
        mac,
        timeout=WOL_TIMEOUT_S,
    )
    if rc != 0:
        raise DeviceError(
            f"WoL relay {relay} failed: {err[:200]}",
            cause="The WoL relay host rejected or did not answer the SSH request.",
            next_step="Check the relay host is up and the key's authorized_keys entry exists.",
        )
    return {"wol_sent": mac, "via": relay}


async def send_key(dev: dict[str, Any], key: str) -> dict[str, Any]:
    if key == "WAKEUP":
        return await wake(dev)

    async def press(tv: WebOsClient) -> None:
        if key in BUTTONS:
            await tv.button(BUTTONS[key])
        elif key == "MEDIA_PLAY_PAUSE":
            await tv.button("PAUSE" if tv.tv_state.media_state else "PLAY")
        elif key == "VOLUME_UP":
            await tv.volume_up()
        elif key == "VOLUME_DOWN":
            await tv.volume_down()
        elif key == "VOLUME_MUTE":
            await tv.set_mute(not tv.tv_state.muted)
        elif key == "SLEEP":
            await tv.power_off()
        else:
            raise DeviceError(f"unsupported_on_device: {key} has no webOS mapping")

    await call(dev, press)
    return {"sent": key}


async def list_apps(dev: dict[str, Any]) -> list[dict[str, str]]:
    async def read(tv: WebOsClient) -> dict[str, Any]:
        return dict(tv.tv_state.apps or {})

    apps = await call(dev, read)
    return sorted(
        (
            {"id": app_id, "title": str(app.get("title", app_id))}
            for app_id, app in apps.items()
        ),
        key=lambda a: a["title"].lower(),
    )


async def list_inputs(dev: dict[str, Any]) -> list[dict[str, str]]:
    async def read(tv: WebOsClient) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], await tv.get_inputs() or [])

    inputs = await call(dev, read)
    return [
        {
            "id": i["id"],
            "label": str(i.get("label", i["id"])),
            "appId": str(i.get("appId", "")),
        }
        for i in inputs
    ]


async def set_input(dev: dict[str, Any], input_id: str) -> dict[str, Any]:
    """Switch to an explicit input. There is no default: the id must be in list_inputs."""
    if not input_id:
        raise DeviceError("set_input needs an explicit input id; call list_inputs")
    valid = {i["id"] for i in await list_inputs(dev)}
    if input_id not in valid:
        raise DeviceError(f"{input_id!r} is not an input on this TV; call list_inputs")

    async def switch(tv: WebOsClient) -> None:
        await tv.set_input(input_id)

    await call(dev, switch)
    return {"input": input_id}


async def launch(dev: dict[str, Any], app_id: str) -> dict[str, Any]:
    async def go(tv: WebOsClient) -> None:
        if app_id not in (tv.tv_state.apps or {}):
            raise DeviceError(f"{app_id!r} is not in the TV's app list; call list_apps")
        await tv.launch_app(app_id)

    await call(dev, go)
    return {"launched": app_id}


async def toast(dev: dict[str, Any], message: str) -> dict[str, Any]:
    text = message.strip()
    if not text or len(text) > TOAST_MAX_CHARS:
        raise DeviceError(f"toast needs 1-{TOAST_MAX_CHARS} characters")

    async def show(tv: WebOsClient) -> None:
        await tv.send_message(text)

    await call(dev, show)
    return {"toast": text}
