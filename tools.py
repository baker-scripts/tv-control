"""Device actions shared by the MCP tools and the panel, and the MCP tool definitions.

Every action except a key press holds the device lock. lockdown and apply_baseline write to the device only when
the panel calls them with real=True; the MCP tools always pass real=False (dry-run).
"""

import asyncio
import io
import os
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import adbops
import appcache
import scan
import sleeptimer
import tvremote
import webos
from inventory import (
    DeviceError,
    check_app_allowed,
    device_lock,
    get_device,
    is_active,
    load_denied_apps,
    load_devices,
    load_quick_apps,
    require_capability,
    run,
)
from mcp.server import MCPServer
from mcp.server.mcpserver import Image
from PIL import Image as PillowImage

SCRIPTS_DIR = Path(os.environ.get("SCRIPTS_DIR", "/scripts"))
SCRIPT_TIMEOUT_S = 900
SCRIPT_TAIL_LINES = 200
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def unsupported_cec(dev: dict[str, Any], key: str) -> str | None:
    if dev.get("tv_control") == "best-effort" and key in adbops.CEC_KEYS:
        return (
            f"unsupported_on_device: {dev['name']} sits on a TV with no confirmed HDMI-CEC; "
            "TV power, volume, and input are best-effort"
        )
    return None


async def guarded(
    device: str,
    action: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    locked: bool = True,
) -> dict[str, Any]:
    """Look up the device, hold its lock (unless locked=False), run the action, and map errors to a reply."""
    try:
        dev = await get_device(device)
        if locked:
            async with device_lock(device):
                body = await action(dev)
        else:
            body = await action(dev)
    except DeviceError as exc:
        return {"device": device, **exc.body()}
    return {"device": device, **body}


def _by_type(
    dev: dict[str, Any], adb_fn: Any, webos_fn: Any
) -> Callable[..., Awaitable[Any]]:
    if dev["type"] == "webos":
        return cast(Callable[..., Awaitable[Any]], webos_fn)
    if dev["type"] in adbops.ADB_TYPES:
        return cast(Callable[..., Awaitable[Any]], adb_fn)
    raise DeviceError(f"unsupported type {dev['type']!r}")


async def do_status(device: str) -> dict[str, Any]:
    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        body = await _by_type(dev, adbops.status, webos.status)(dev)
        entry = appcache.get(device, "apps")
        app_id = body.get("current_app") or ""
        if not app_id and body.get("foreground"):
            match = re.search(
                r"([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)/", body["foreground"]
            )
            app_id = match.group(1) if match else ""
        title = next(
            (
                a.get("title")
                for a in (entry or {}).get("items", [])
                if a["id"] == app_id
            ),
            None,
        )
        body["app_id"] = app_id
        body["app_title"] = appcache.app_title(app_id, title) if app_id else None
        return {
            "type": dev["type"],
            "tv_control": dev.get("tv_control", "full"),
            **body,
        }

    return await guarded(device, act)


async def do_health(device: str) -> dict[str, Any]:
    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        body = await _by_type(dev, adbops.health, webos.status)(dev)
        return {"type": dev["type"], **body}

    result = await guarded(device, act)
    return await _paired_tv_status(device, result)


def _tv_unreachable(tv: dict[str, Any]) -> bool:
    return "error" in tv or tv.get("adb_state") == "offline"


async def _paired_tv_status(device: str, result: dict[str, Any]) -> dict[str, Any]:
    """Add status for a Shield paired with a TV: an unreachable TV is standby unless the Shield plays."""
    try:
        paired = (await get_device(device)).get("paired_tv")
    except DeviceError:
        return result
    if not paired or "error" in result or result.get("adb_state") != "device":
        return result
    result["paired_tv"] = paired
    if not _tv_unreachable(await do_status(paired)):
        result["status"] = "ok"
    elif result.get("awake") and result.get("playing"):
        result["status"] = "fault: paired TV unreachable while Shield plays"
    else:
        result["status"] = "standby"
    return result


async def do_screenshot(device: str) -> tuple[bytes | None, str | None]:
    try:
        dev = await get_device(device)
        if dev["type"] == "webos":
            return None, "unsupported_on_device: aiowebostv has no screenshot call"
        async with device_lock(device):
            return await adbops.screenshot(dev)
    except DeviceError as exc:
        return None, f"error: {exc}"


PREVIEW_INTERVAL_S = 4.0
_preview_cache: dict[str, tuple[float, bytes | None, str | None, float | None]] = {}


async def do_preview(device: str) -> tuple[bytes | None, str | None, float | None]:
    """Share one small capture per device per four seconds, including failed attempts."""
    try:
        dev = await get_device(device)
        if dev["type"] not in adbops.ADB_TYPES:
            return None, "unsupported_on_device: no screenshot API", None
        async with device_lock(f"preview:{device}"):
            entry = _preview_cache.get(device)
            if entry and time.monotonic() - entry[0] < PREVIEW_INTERVAL_S:
                return entry[1:]
            jpeg, note = await do_screenshot(device)
            captured_at = None
            if jpeg:
                with PillowImage.open(io.BytesIO(jpeg)) as source:
                    image = source.convert("RGB")
                    image.thumbnail((480, 480))
                    output = io.BytesIO()
                    image.save(output, format="JPEG", quality=70)
                    jpeg = output.getvalue()
                captured_at = time.time()
            _preview_cache[device] = (time.monotonic(), jpeg, note, captured_at)
            return jpeg, note, captured_at
    except DeviceError as exc:
        return None, f"error: {exc}", None


async def do_key(device: str, key: str) -> dict[str, Any]:
    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        name = adbops.normalize_key(key)
        refusal = unsupported_cec(dev, name)
        if refusal:
            raise DeviceError(refusal)
        if dev["type"] == "webos":
            async with device_lock(device):
                return await webos.send_key(dev, name)
        _by_type(dev, adbops.send_key, None)
        fast = await tvremote.send_key(dev, name)
        return fast or await adbops.send_key_fast(dev, name)

    # Keys skip the device lock: a slow status or app list must never queue a press.
    return await guarded(device, act, locked=False)


async def do_remote_pair(device: str, code: str = "") -> dict[str, Any]:
    """Start Android TV Remote pairing (no code), or finish it with the code shown on the TV."""

    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        if code:
            return await tvremote.finish_pair(dev, code)
        return await tvremote.start_pair(dev)

    return await guarded(device, act)


async def do_launch(device: str, app: str) -> dict[str, Any]:
    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        # Checked before any device call, on every device type.
        check_app_allowed(app, await load_denied_apps())
        if dev["type"] == "webos":
            return await webos.launch(dev, app)
        return cast(
            dict[str, Any],
            await _by_type(dev, adbops.launch, None)(dev, adbops.check_pkg(app)),
        )

    return await guarded(device, act)


async def _live_lists(dev: dict[str, Any]) -> dict[str, list[Any]]:
    if dev["type"] == "webos":
        return {
            "apps": await webos.list_apps(dev),
            "inputs": await webos.list_inputs(dev),
        }
    return {"apps": await _by_type(dev, adbops.list_apps, None)(dev)}


_refreshing: set[str] = set()
_bg_tasks: set[asyncio.Task[None]] = set()


async def refresh_lists(device: str) -> dict[str, Any]:
    """Fetch the live app list (and LG inputs), store it, and return the guarded reply."""

    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        lists = await _live_lists(dev)
        for kind, items in lists.items():
            if kind == "apps":
                items = appcache.friendly_apps(items)
            appcache.put(device, kind, items)
        return {"refreshed": sorted(lists)}

    return await guarded(device, act)


def _refresh_in_background(device: str) -> None:
    if device in _refreshing:
        return
    _refreshing.add(device)

    async def run_refresh() -> None:
        try:
            await refresh_lists(device)
        finally:
            _refreshing.discard(device)

    task = asyncio.get_running_loop().create_task(run_refresh())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def _is_offline(err: dict[str, Any]) -> bool:
    low = str(err.get("error", "")).lower()
    return "offline" in low or "timed out" in low or "unreachable" in low


async def cached_list(
    device: str, kind: str, refresh: bool = False, background: bool = False
) -> dict[str, Any]:
    """Serve the cache first. refresh=True goes live; background=True refreshes a stale entry after replying."""
    body: dict[str, Any]
    entry = None if refresh else appcache.get(device, kind)
    if entry is None or (not background and not appcache.is_fresh(entry)):
        err = await refresh_lists(device)
        if "error" in err:
            stale = appcache.get(device, kind)
            offline = _is_offline(err)
            if stale is None:
                return {**err, "offline": True} if offline else err
            entry = stale
            body = {"warning": f"live refresh failed, serving cache: {err['error']}"}
            if offline:
                body["offline"] = True
        else:
            entry, body = appcache.get(device, kind), {}
        if entry is None:
            return {"device": device, kind: [], "note": f"{kind} not available"}
    else:
        body = {}
        legacy_labels = kind == "apps" and any(
            a.get("title") == a.get("id") for a in entry["items"]
        )
        if not appcache.is_fresh(entry) or (background and legacy_labels):
            _refresh_in_background(device)
            body = {"refreshing": True}
    age = int(time.time() - entry["fetched_at"])
    return {
        "device": device,
        kind: appcache.friendly_apps(entry["items"])
        if kind == "apps"
        else entry["items"],
        "fetched_at": entry["fetched_at"],
        "age_s": age,
        "stale": not appcache.is_fresh(entry),
        **body,
    }


async def do_list_apps(
    device: str, refresh: bool = False, background: bool = False
) -> dict[str, Any]:
    return await cached_list(device, "apps", refresh, background)


async def do_list_inputs(
    device: str, refresh: bool = False, background: bool = False
) -> dict[str, Any]:
    try:
        dev = await get_device(device)
        if not dev.get("inputs"):
            require_capability(dev, "inputs")
        if dev["type"] != "webos":
            return {
                "device": device,
                "inputs": [
                    {
                        "id": key,
                        "label": label,
                        **(
                            {}
                            if (dev.get("input_ids") or {}).get(key)
                            else {
                                "error": "Input switching is not verified for this Fire TV; unavailable."
                            }
                        ),
                    }
                    for key, label in (dev.get("inputs") or {}).items()
                ],
                "current_input": None,
            }
    except DeviceError as exc:
        return {"device": device, **exc.body()}
    body = await cached_list(device, "inputs", refresh, background)
    if "error" in body:
        if dev.get("inputs"):
            body["inputs"] = [
                {
                    "id": key,
                    "label": label,
                    "error": "Input list unavailable; retry when the TV is online.",
                }
                for key, label in dev["inputs"].items()
            ]
        return body
    configured = dev.get("inputs")
    if configured:
        reported = {i["id"]: i for i in body.get("inputs", [])}
        body["inputs"] = [
            {
                **reported.get(key, {}),
                "id": key,
                "label": label,
                **(
                    {}
                    if key in reported
                    else {
                        "error": f"Unknown configured input {key}; refresh the input list."
                    }
                ),
            }
            for key, label in configured.items()
        ]
    return body


async def do_quick_apps(device: str) -> dict[str, Any]:
    """Quick-launch buttons for installed apps, from the cache only (no device call)."""
    entry = appcache.get(device, "apps")
    if entry is None:
        _refresh_in_background(device)
        return {"device": device, "apps": [], "refreshing": True}
    if not appcache.is_fresh(entry):
        _refresh_in_background(device)
    apps = appcache.quick_apps(
        entry["items"], await load_quick_apps(), await load_denied_apps()
    )
    return {
        "device": device,
        "apps": apps,
        "age_s": int(time.time() - entry["fetched_at"]),
    }


async def do_toast(device: str, message: str) -> dict[str, Any]:
    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        if dev["type"] == "webos":
            return await webos.toast(dev, message)
        return cast(
            dict[str, Any], await _by_type(dev, adbops.toast, None)(dev, message)
        )

    return await guarded(device, act)


async def do_find_remote(device: str) -> dict[str, Any]:
    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        require_capability(dev, "find_remote")
        if dev["type"] != "shield":
            raise DeviceError("unsupported_on_device: find_remote is Shield only")
        return await adbops.find_remote(dev)

    return await guarded(device, act)


async def do_power(device: str, state: str) -> dict[str, Any]:
    """Power on/off. Off is KEYCODE_SLEEP on ADB devices and power off on webOS; never reboot."""

    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        require_capability(dev, "power")
        if state not in ("on", "off"):
            raise DeviceError("power state must be 'on' or 'off'")
        if dev["type"] == "webos":
            return await webos.wake(dev) if state == "on" else await _webos_off(dev)
        key = "WAKEUP" if state == "on" else "SLEEP"
        return await adbops.send_key(dev, key)

    return await guarded(device, act)


async def _webos_off(dev: dict[str, Any]) -> dict[str, Any]:
    return await webos.send_key(dev, "SLEEP")


async def do_wake(device: str) -> dict[str, Any]:
    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        require_capability(dev, "wake")
        return await webos.wake(dev)

    return await guarded(device, act)


async def do_set_input(device: str, input_id: str) -> dict[str, Any]:
    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        if not dev.get("inputs"):
            require_capability(dev, "inputs")
        configured = dev.get("inputs") or {}
        if configured and input_id not in configured:
            raise DeviceError(f"Unknown configured input {input_id!r}")
        if dev["type"] == "firetv":
            tv_input_id = (dev.get("input_ids") or {}).get(input_id)
            if not tv_input_id:
                raise DeviceError(
                    "unsupported_on_device: no TV input id configured for "
                    f"{input_id!r} (input_ids in devices.yml; read it from dumpsys tv_input)"
                )
            return await adbops.set_input(dev, tv_input_id)
        if dev["type"] != "webos":
            raise DeviceError(
                "unsupported_on_device: input switching is not verified for this device type"
            )
        return await webos.set_input(dev, input_id)

    return await guarded(device, act)


async def power_off_for_timer(device: str) -> dict[str, Any]:
    """Fire a sleep timer: SLEEP on Shields, power off on webOS."""

    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        if dev["type"] not in adbops.ADB_TYPES:
            require_capability(dev, "sleep_timer")
        if dev["type"] == "webos":
            return await _webos_off(dev)
        return await adbops.send_key(dev, "SLEEP")

    return await guarded(device, act)


async def do_sleep_timer(
    device: str, minutes: int | None = None, now: float | None = None
) -> dict[str, Any]:
    """Set (15/30/60/90), cancel (0), or read (None) the server-side sleep timer."""
    t = time.time() if now is None else now
    try:
        dev = await get_device(device)
        if dev["type"] not in adbops.ADB_TYPES:
            require_capability(dev, "sleep_timer")
        if minutes is None:
            pass
        elif minutes == 0:
            sleeptimer.cancel(device)
        else:
            try:
                sleeptimer.set_timer(device, minutes, t)
            except ValueError as exc:
                raise DeviceError(f"unsupported_on_device: {exc}") from None
    except DeviceError as exc:
        return {"device": device, **exc.body()}
    return {"device": device, "remaining_s": sleeptimer.remaining(device, t)}


async def fire_sleep_timers() -> list[str]:
    return await sleeptimer.fire_due(time.time(), power_off_for_timer)


async def do_reboot(device: str) -> dict[str, Any]:
    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        if dev["type"] == "webos":
            raise DeviceError("unsupported_on_device: aiowebostv has no reboot call")
        return await adbops.reboot(dev)

    return await guarded(device, act)


async def do_pair(device: str) -> dict[str, Any]:
    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        if dev["type"] != "webos":
            raise DeviceError(f"{device} is type {dev['type']!r}, not webos")
        return await webos.pair(dev)

    return await guarded(device, act)


async def run_script(name: str, *args: str) -> dict[str, Any]:
    rc, out, err = await run(
        "bash",
        str(SCRIPTS_DIR / name),
        *args,
        timeout=SCRIPT_TIMEOUT_S,
        merge_stderr=True,
    )
    text = ANSI_RE.sub("", out.decode(errors="replace")) + (err if rc == 124 else "")
    lines = text.strip().splitlines()
    return {
        "script": name,
        "args": list(args),
        "rc": rc,
        "output": "\n".join(lines[-SCRIPT_TAIL_LINES:]),
    }


async def do_baseline(device: str, real: bool) -> dict[str, Any]:
    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        if dev["type"] not in adbops.ADB_TYPES:
            raise DeviceError("unsupported_on_device: baseline is ADB only")
        await adbops.require_device(dev)
        body: dict[str, Any] = {"mode": "real" if real else "dry-run"}
        if real:
            body["undo_file"] = await adbops.save_undo(dev, "baseline")
        # deploy-config.sh also runs remap-buttons.sh for Shields, so it is not called again here.
        steps = [
            await run_script(
                "deploy-config.sh", dev["name"], *([] if real else ["--dry-run"])
            )
        ]
        if dev["type"] in adbops.FIRE_TYPES:
            # Sourced keys only; the unverified tier stays off until a before/after diff proves each key.
            steps.append(
                await run_script(
                    "firetv-settings.sh", dev["name"], *(["--apply"] if real else [])
                )
            )
        body["steps"] = steps
        return body

    return await guarded(device, act)


async def do_lockdown(device: str, real: bool) -> dict[str, Any]:
    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        if dev["type"] not in adbops.ADB_TYPES:
            raise DeviceError("unsupported_on_device: lockdown is ADB only")
        serial = await adbops.require_device(dev)
        body: dict[str, Any] = {"mode": "real" if real else "dry-run"}
        if real:
            body["undo_file"] = await adbops.save_undo(dev, "lockdown")
        body["steps"] = [
            await run_script("lockdown.sh", serial, *([] if real else ["--dry-run"]))
        ]
        return body

    return await guarded(device, act)


async def do_scan(site: str) -> dict[str, Any]:
    try:
        async with device_lock(f"scan:{site}"):
            return await scan.scan_site(site)
    except DeviceError as exc:
        return {"site": site, "error": str(exc)}


async def inventory_by_site() -> dict[str, list[dict[str, Any]]]:
    sites: dict[str, list[dict[str, Any]]] = {}
    for dev in await load_devices():
        entry = {
            k: dev.get(k)
            for k in (
                "name",
                "type",
                "ip",
                "port",
                "config",
                "tv_control",
                "status",
                "capabilities",
            )
        }
        sites.setdefault(dev.get("site") or "unassigned", []).append(entry)
    return sites


# ---------- MCP ----------

mcp = MCPServer("tv-control")


@mcp.tool()
async def list_devices() -> dict[str, list[dict[str, Any]]]:
    """List every TV in devices.yml, grouped by site."""
    return await inventory_by_site()


@mcp.tool()
async def device_status(device: str) -> dict[str, Any]:
    """Return live status: ADB state, foreground window, and SmartTube version; or webOS power, app, and volume."""
    return await do_status(device)


@mcp.tool(structured_output=False)
async def screenshot(device: str) -> list[Any]:
    """Return a JPEG screenshot (max 1280 px) of an ADB device. webOS has no screenshot API."""
    jpeg, note = await do_screenshot(device)
    return ([Image(data=jpeg, format="jpeg")] if jpeg else []) + (
        [note] if note else []
    )


@mcp.tool()
async def webos_pair(device: str) -> dict[str, Any]:
    """Pair with an LG webOS TV. Someone at the TV must press Allow within 60 s."""
    return await do_pair(device)


@mcp.tool()
async def send_key(device: str, key: str) -> dict[str, Any]:
    """Press one remote key: HOME, BACK, DPAD_UP/DOWN/LEFT/RIGHT/CENTER, MEDIA_*, VOLUME_UP/DOWN/MUTE, SLEEP, WAKEUP."""
    return await do_key(device, key)


@mcp.tool()
async def list_apps(device: str, refresh: bool = False) -> dict[str, Any]:
    """List launchable apps: Android package names, or webOS app ids with titles. Served from a 24 h cache; refresh=true goes live."""
    return await do_list_apps(device, refresh)


@mcp.tool()
async def list_inputs(device: str, refresh: bool = False) -> dict[str, Any]:
    """List the inputs of an LG webOS TV (id and label), from a 24 h cache; refresh=true goes live."""
    return await do_list_inputs(device, refresh)


@mcp.tool()
async def set_input(device: str, input_id: str) -> dict[str, Any]:
    """Switch an LG webOS TV to an explicit input id from list_inputs. There is no default input."""
    return await do_set_input(device, input_id)


@mcp.tool()
async def wake(device: str) -> dict[str, Any]:
    """Wake an LG webOS TV with Wake-on-LAN sent from the site's LAN relay (devices.yml mac and wol_relay)."""
    return await do_wake(device)


@mcp.tool()
async def power(device: str, state: str) -> dict[str, Any]:
    """Power a device on or off. Off is KEYCODE_SLEEP (standby) on ADB devices and power off on webOS."""
    return await do_power(device, state)


@mcp.tool()
async def find_remote(device: str) -> dict[str, Any]:
    """Make an NVIDIA Shield remote buzz (Remote Locator). Needs the find_remote capability in devices.yml."""
    return await do_find_remote(device)


@mcp.tool()
async def sleep_timer(device: str, minutes: int = -1) -> dict[str, Any]:
    """Set a server-side sleep timer (15, 30, 60, or 90 min), cancel it (0), or read it (omit minutes)."""
    return await do_sleep_timer(device, None if minutes < 0 else minutes)


@mcp.tool()
async def launch_app(device: str, app: str) -> dict[str, Any]:
    """Launch an app by Android package name or webOS app id (see list_apps)."""
    return await do_launch(device, app)


@mcp.tool()
async def toast(device: str, message: str) -> dict[str, Any]:
    """Show a short on-screen message: a webOS toast on LG, a heads-up notification on Android devices."""
    return await do_toast(device, message)


@mcp.tool()
async def reboot(device: str) -> dict[str, Any]:
    """Reboot an ADB device (plain reboot, never power-off). To turn a Shield off, send_key SLEEP."""
    return await do_reboot(device)


@mcp.tool()
async def scan_site(site: str) -> dict[str, Any]:
    """Probe a site LAN for ADB (5555) and webOS (3000/3001) ports; return hits not in devices.yml. Writes nothing."""
    return await do_scan(site)


@mcp.tool()
async def apply_baseline(device: str) -> dict[str, Any]:
    """Dry-run the setup baseline (deploy-config.sh, plus firetv-settings.sh on Fire OS). Real runs are panel-only."""
    return await do_baseline(device, real=False)


@mcp.tool()
async def lockdown(device: str) -> dict[str, Any]:
    """Dry-run lockdown.sh for a device. Real runs are panel-only, after operator approval."""
    return await do_lockdown(device, real=False)


@mcp.tool()
async def health(device: str) -> dict[str, Any]:
    """Health readout for one device, or "all": status plus Private DNS, always-on VPN, accessibility, resumed activity."""
    if device != "all":
        return await do_health(device)
    names = [d["name"] for d in await load_devices() if is_active(d)]
    results = await asyncio.gather(*(do_health(n) for n in names))
    return {"devices": list(results)}
