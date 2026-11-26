"""Volume through a Home Assistant remote entity (a Broadlink IR blaster for a soundbar).

devices.yml opts a device in with `volume_via: ha_remote` and an `ha_remote` block:
entity (remote.<name>), commands (up, down, mute: the learned command names), and an
optional device (the learned-device name). HA_URL and HA_TOKEN come from the environment.
Every failure is a DeviceError; the key never falls back to another volume route.
"""

import asyncio
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

from inventory import DeviceError

VIA = "ha_remote"
TIMEOUT_S = 5
ENTITY_RE = re.compile(r"remote\.[a-z0-9_]+")
COMMAND_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
KEY_ACTION = {"VOLUME_UP": "up", "VOLUME_DOWN": "down", "VOLUME_MUTE": "mute"}
UNAVAILABLE = ("unavailable", "unknown")


def _get(url: str, token: str) -> tuple[int, Any]:
    return _call(urllib.request.Request(url, headers=_headers(token)))


def _post(url: str, token: str, payload: dict[str, Any]) -> tuple[int, Any]:
    req = urllib.request.Request(
        url, json.dumps(payload).encode(), _headers(token), method="POST"
    )
    return _call(req)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _call(req: urllib.request.Request) -> tuple[int, Any]:
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            body = resp.read()
            return resp.status, json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise DeviceError(
            "ha_remote: Home Assistant did not answer",
            cause=type(exc).__name__,
            next_step="Check HA_URL and that Home Assistant is up.",
        ) from None


def config_error(dev: dict[str, Any]) -> str | None:
    """Why dev's ha_remote block cannot work, from devices.yml alone; None when it is well formed."""
    block = dev.get("ha_remote")
    if not isinstance(block, dict):
        return (
            f"ha_remote: {dev['name']} has volume_via: ha_remote but no ha_remote block"
        )
    if not ENTITY_RE.fullmatch(str(block.get("entity") or "")):
        return "ha_remote: entity must look like remote.<name>"
    commands = block.get("commands")
    if not isinstance(commands, dict) or not all(
        COMMAND_RE.fullmatch(str(commands.get(a) or "")) for a in KEY_ACTION.values()
    ):
        return "ha_remote: commands needs up, down, and mute names"
    return None


async def send_key(dev: dict[str, Any], key: str) -> dict[str, Any]:
    """Send the learned command for a volume key; raise DeviceError when it cannot be delivered."""
    bad = config_error(dev)
    if bad:
        raise DeviceError(bad)
    url, token = (
        os.environ.get("HA_URL", "").rstrip("/"),
        os.environ.get("HA_TOKEN", ""),
    )
    if not url or not token:
        raise DeviceError(
            "ha_remote: Home Assistant is not configured",
            cause="HA_URL or HA_TOKEN is not set for this app.",
            next_step="Set both in the container environment, or remove volume_via: ha_remote.",
        )
    block = dev["ha_remote"]
    entity, action = block["entity"], KEY_ACTION[key]
    code, state = await asyncio.to_thread(_get, f"{url}/api/states/{entity}", token)
    if code == 404:
        raise DeviceError(
            f"ha_remote: entity {entity} does not exist in Home Assistant",
            cause="The remote entity is not created yet, or was renamed.",
            next_step="Add the Broadlink remote to Home Assistant and fix ha_remote.entity.",
        )
    if code != 200 or str((state or {}).get("state")) in UNAVAILABLE:
        raise DeviceError(
            f"ha_remote: entity {entity} is not available",
            cause=f"Home Assistant answered {code}, state {(state or {}).get('state')}.",
            next_step="Check the Broadlink remote is online.",
        )
    payload: dict[str, Any] = {
        "entity_id": entity,
        "command": [block["commands"][action]],
    }
    if block.get("device"):
        payload["device"] = str(block["device"])
    code, _ = await asyncio.to_thread(
        _post, f"{url}/api/services/remote/send_command", token, payload
    )
    if code != 200:
        raise DeviceError(f"ha_remote: remote.send_command answered {code}")
    return {"sent": key, "via": f"{VIA}:{entity}"}
