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

from mcp.server import MCPServer
from mcp.server.mcpserver import Image
from PIL import Image as PillowImage

import adbops
import appcache
import apppolicy
import messages
import plexnow
import scan
import sleeptimer
import tvremote
import webos
from inventory import (
    DeviceError,
    device_lock,
    get_device,
    is_active,
    load_app_aliases,
    load_denied_apps,
    load_devices,
    load_message_policy,
    load_quick_apps,
    load_sites,
    require_capability,
    run,
)

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


FAST_KEY_LABELS = {
    "ready": "ready",
    "needs_pairing": "needs pairing",
    "unsupported": "unsupported",
}


def control_path(
    dev: dict[str, Any], fast_keys: str | None, relay: str | None
) -> list[str]:
    """Human-readable connection and control path from inventory plus known state; no I/O."""
    lines: list[str] = []
    if dev["type"] == "webos":
        lines.append(f"webOS over LAN (port {dev.get('port') or 3001})")
        if not dev.get("mac"):
            lines.append("Wake: no MAC set")
        else:
            lines.append(
                f"Wake: Wake-on-LAN via {relay}" if relay else "Wake: no relay set"
            )
    else:
        host = adbops.serial_of(dev).rsplit(":", 1)[0]
        if host == dev.get("ip"):
            lines.append("ADB over LAN (site subnet route)")
        elif host == str(dev.get("tailnet_ip")):
            lines.append("ADB via tailnet IP (LAN address not answering)")
        elif host == dev.get("tailnet_fqdn"):
            lines.append("ADB via tailnet name")
        else:
            lines.append("ADB over LAN (site subnet route)")
        lines.append("Wake: ADB WAKEUP key")
    if fast_keys is not None:
        lines.append(
            f"Fast keys (Remote v2): {FAST_KEY_LABELS.get(fast_keys, fast_keys)}"
        )
    lines.append(f"TV control: {dev.get('tv_control', 'full')}")
    return lines


LABEL_PULL_TIMEOUT_S = 30


def hdmi_title(
    dev: dict[str, Any],
    devices: list[dict[str, Any]],
    port: int | None,
    cec_name: str | None,
) -> str:
    """'HDMI <n> (<what is on it>)': the CEC name the TV reports, named from devices.yml when it is a Shield paired to this TV."""
    base = f"HDMI {port}" if port else "HDMI input"
    if not cec_name:
        return base
    shields = [
        d
        for d in devices
        if d.get("paired_tv") == dev["name"] and d.get("type") == "shield"
    ]
    if len(shields) == 1 and "shield" in cec_name.casefold():
        cec_name = str(shields[0].get("label") or shields[0]["name"])
    return f"{base} ({cec_name})"


async def device_label(
    dev: dict[str, Any], pkg: str, cached_title: str | None
) -> str | None:
    """The app's own label from the device (cached per device and package); None when it has none."""
    if cached_title and cached_title != pkg:
        return cached_title
    known, label = appcache.cached_label(dev["name"], pkg)
    if known:
        return label
    try:
        label = await adbops.read_label(
            adbops.serial_of(dev), pkg, pull_timeout=LABEL_PULL_TIMEOUT_S
        )
    except DeviceError:
        return None
    appcache.put_label(dev["name"], pkg, label)
    return label


async def plex_names(dev: dict[str, Any]) -> list[str | None]:
    names = [dev["name"], dev.get("label"), dev.get("plex_player")]
    try:
        raw = await adbops.read(
            adbops.serial_of(dev), "settings get global device_name"
        )
    except DeviceError:
        raw = ""
    return [*names, raw.strip() if raw.strip() != "null" else None]


async def app_fields(
    dev: dict[str, Any], app_id: str, cached_title: str | None = None
) -> dict[str, Any]:
    """app_title plus what the device and the Plex server say about the foreground app."""
    if not app_id:
        return {"app_title": None}
    title = appcache.app_title(app_id, cached_title)
    if dev["type"] not in adbops.ADB_TYPES:
        return {"app_title": title}
    extra: dict[str, Any] = {}
    try:
        if app_id == adbops.PASSTHROUGH_PKG:
            hdmi = await adbops.hdmi_input(adbops.serial_of(dev))
            title = hdmi_title(
                dev, await load_devices(), hdmi["hdmi_port"], hdmi["hdmi_device"]
            )
            extra |= hdmi
        elif app_id not in appcache.FALLBACK_LABELS:
            title = appcache.app_title(
                app_id, await device_label(dev, app_id, cached_title)
            )
        if plexnow.is_plex(app_id):
            extra |= await plexnow.now_playing(await plex_names(dev))
    except DeviceError:
        pass
    return {"app_title": title, **extra}


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
        if body.get("adb_state", "device") == "device":
            body |= await app_fields(dev, app_id, title)
        else:
            body["app_title"] = appcache.app_title(app_id, title) if app_id else None
        fast = {}
        if dev["type"] == "shield":
            if body.get("adb_state") == "device":
                tvremote.warm(dev)
            fast = {
                "fast_keys": tvremote.fast_key_state(dev),
                "remote_app": tvremote.remote_app(dev["name"]),
            }
        relay = None
        if dev["type"] == "webos":
            relay = ((await load_sites()).get(str(dev.get("site"))) or {}).get(
                "wol_relay"
            )
        return {
            "type": dev["type"],
            "tv_control": dev.get("tv_control", "full"),
            **body,
            **fast,
            "control_path": control_path(dev, fast.get("fast_keys"), relay),
        }

    # Read-only: the 10 s status poll must not queue a press behind its dumpsys calls.
    return await guarded(device, act, locked=False)


def _require_adb(dev: dict[str, Any], verb: str) -> None:
    if dev["type"] not in adbops.ADB_TYPES:
        raise DeviceError(f"unsupported_on_device: {verb} is Android only")


async def do_current_screen(device: str, focus: bool = True) -> dict[str, Any]:
    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        _require_adb(dev, "current_screen")
        return await adbops.current_screen(dev, focus)

    return await guarded(device, act, locked=False)


async def do_what_is_on(device: str) -> dict[str, Any]:
    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        _require_adb(dev, "what_is_on")
        body = await adbops.what_is_on(dev)
        body |= await app_fields(dev, body["app"] or "")
        return body

    return await guarded(device, act, locked=False)


async def do_health(device: str) -> dict[str, Any]:
    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        body = await _by_type(dev, adbops.health, webos.status)(dev)
        return {"type": dev["type"], **body}

    result = await guarded(device, act)
    return await _paired_tv_status(device, result)


def _tv_unreachable(tv: dict[str, Any]) -> bool:
    return (
        "error" in tv
        or tv.get("adb_state") == "offline"
        or tv.get("webos_state") in ("unreachable", "standby")
    )


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
        # Own lock: a capture must not queue key, power, or launch presses.
        async with device_lock(f"shot:{device}"):
            return await adbops.screenshot(dev)
    except DeviceError as exc:
        return None, f"error: {exc}"


PREVIEW_INTERVAL_S = 4.0
_preview_cache: dict[str, tuple[float, bytes | None, str | None, float | None]] = {}


def _thumbnail(jpeg: bytes) -> bytes:
    with PillowImage.open(io.BytesIO(jpeg)) as source:
        image = source.convert("RGB")
    image.thumbnail((480, 480))
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=70)
    return output.getvalue()


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
                jpeg = await asyncio.to_thread(_thumbnail, jpeg)
                captured_at = time.time()
            _preview_cache[device] = (time.monotonic(), jpeg, note, captured_at)
            return jpeg, note, captured_at
    except DeviceError as exc:
        return None, f"error: {exc}", None


async def do_key(device: str, key: str) -> dict[str, Any]:
    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        name = adbops.normalize_key(key)
        target = dev.get("volume_via")
        if target and name in webos.VOLUME_KEYS:
            tv = await get_device(target)
            if tv["type"] != "webos":
                raise DeviceError(f"volume_via {target!r} is not a webOS TV")
            async with device_lock(target):
                result = await webos.send_key(tv, name)
            return {**result, "via": f"webos:{target}"}
        refusal = unsupported_cec(dev, name)
        if refusal:
            raise DeviceError(refusal)
        if dev["type"] == "webos":
            async with device_lock(device):
                return await webos.send_key(dev, name)
        _by_type(dev, adbops.send_key, None)
        if name == "HOME" and dev["type"] == "shield":
            return await adbops.launch(dev, SHIELD_HOME_APP)
        fast = await tvremote.send_key(dev, name)
        if fast:
            return fast
        sent = await adbops.send_key_fast(dev, name)
        if tvremote.should_use_remote(dev, name):
            return {**sent, "via": "adb-fallback"}
        return sent

    # Keys skip the device lock: a slow status or app list must never queue a press.
    return await guarded(device, act, locked=False)


async def do_text(device: str, text: str) -> dict[str, Any]:
    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        if dev["type"] == "webos":
            raise DeviceError("unsupported_on_device: text entry is Android only")
        if text and (typed := await tvremote.send_text(dev, text)):
            return typed
        return await adbops.send_text(dev, text)

    return await guarded(device, act, locked=False)


async def do_remote_pair(device: str, code: str = "") -> dict[str, Any]:
    """Start Android TV Remote pairing (no code), or finish it with the code shown on the TV."""

    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        if code:
            return await tvremote.finish_pair(dev, code)
        return await tvremote.start_pair(dev)

    return await guarded(device, act)


def _app_labels(dev: dict[str, Any], app_id: str) -> list[str | None]:
    """Every name tv-control knows for an app on this device, from cache only (no device call)."""
    entry = appcache.get(dev["name"], "apps") or {}
    titles = [a.get("title") for a in entry.get("items", []) if a.get("id") == app_id]
    return [
        *titles,
        appcache.FALLBACK_LABELS.get(app_id),
        appcache.cached_label(dev["name"], app_id)[1],
    ]


async def check_app(dev: dict[str, Any], app: str) -> str:
    """Resolve a spoken name or id to the device's package id, then apply the app policy.

    Called before any device call by every verb that opens an app, on every device type and
    path (agent or panel, forced or not). Raises DeviceError (AppRefusal) when refused.
    """
    entry = appcache.get(dev["name"], "apps") or {}
    installed = [
        {"id": str(a["id"]), "title": str(a.get("title") or a["id"])}
        for a in entry.get("items", [])
        if a.get("id")
    ]
    aliases = apppolicy.merged_aliases(await load_app_aliases(), dev)
    app_id = apppolicy.resolve(dev, app, installed, aliases)
    why = apppolicy.refusal(
        dev, app_id, _app_labels(dev, app_id), await load_denied_apps()
    )
    if why:
        raise apppolicy.AppRefusal(why)
    return app_id


async def launchable_apps(device: str, body: dict[str, Any]) -> dict[str, Any]:
    """body with its apps list cut to what the app policy lets automation open on the device."""
    try:
        dev = await get_device(device)
    except DeviceError:
        return {**body, "apps": []}
    apps = apppolicy.visible(dev, body.get("apps", []), await load_denied_apps())
    return {**body, "apps": apps}


async def do_launch(device: str, app: str) -> dict[str, Any]:
    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        # Checked before any device call, on every device type.
        app_id = await check_app(dev, app)
        if dev["type"] == "webos":
            return await webos.launch(dev, app_id)
        target = (
            SHIELD_HOME_APP
            if dev["type"] == "shield" and app_id in STOCK_LAUNCHERS
            else app_id
        )
        return cast(
            dict[str, Any],
            await _by_type(dev, adbops.launch, None)(dev, adbops.check_pkg(target)),
        )

    return await guarded(device, act)


CONFIRM_TIMEOUT_S = 10.0
# A launch sent while the screensaver runs comes to front only after the dream ends.
WOKE_CONFIRM_TIMEOUT_S = 25.0
WAKE_TIMEOUT_S = 5.0
PLAY_TIMEOUT_S = 25.0
CONFIRM_POLL_S = 1.0
PLEX_PKG = "com.plexapp.android"
PLEX_MACHINE_ID_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")
PLEX_RATING_KEY_RE = re.compile(r"^[0-9]{1,12}$")
MAX_VIEW_OFFSET_MS = 24 * 3600 * 1000


END_LINE = "All done."
NARRATE_GAP_S = 6.0
NARRATE_MAX_STEPS = 8
# 2019 NVIDIA Shield remote, wording checked against the physical remote.
# Every other button and every other remote has no approved wording, so narration names none.
SHIELD_BUTTONS = {
    "HOME": "the circle button, just under the big ring on the right side",
    "SELECT": "the round button in the middle of the ring",
}
NO_SPEECH = (
    "speech not available on this device: tv-control has no route to speak text on Android TV "
    "without Home Assistant (no shell text-to-speech command); the steps are shown on screen only"
)


class Dropped(DeviceError):
    """An unrequested message the policy withholds (asleep, quiet hours, unsupported); never shown."""

    def __init__(self, reason: str, why: str) -> None:
        super().__init__(
            f"not_shown: dropped ({reason}): {why}",
            cause="The message policy withholds an unrequested message here.",
            next_step="Nothing was shown. Send it again with requested=true only if the household asked for it.",
        )
        self.reason = reason


async def _deliver(
    dev: dict[str, Any],
    text: str,
    policy: dict[str, Any],
    requested: bool,
    image: str,
) -> dict[str, Any]:
    if dev["type"] == "webos":
        if not requested:
            raise Dropped(
                "unsupported",
                "a webOS TV has no clock or confirmed display, so only a requested message is sent",
            )
        return await webos.toast(dev, text)
    if requested:
        await _wake_first(dev)
    else:
        if (await adbops.current_screen(dev, focus=False)).get("awake") is not True:
            raise Dropped(
                "asleep", "the TV is asleep and the message was not requested"
            )
        minutes = await adbops.device_minutes(dev)
        if minutes is None or messages.in_quiet_hours(minutes, policy):
            raise Dropped(
                "quiet_hours",
                "unrequested messages wait outside quiet hours (or the device clock is unreadable)",
            )
    return await adbops.toast(
        dev,
        text,
        title=policy["title"],
        seconds=messages.seconds_for(text, policy),
        corner=policy["corner"],
        image=image,
    )


async def send_message(
    dev: dict[str, Any],
    text: str,
    *,
    policy: dict[str, Any],
    sender: str,
    requested: bool,
    kind: str,
    image: str = "",
    actor: str = "",
) -> dict[str, Any]:
    """Deliver one policy-checked message and record it. Returns shown=true only when the TV confirmed it."""
    row: dict[str, Any] = {
        "text": text.strip()[: adbops.TOAST_MAX_CHARS],
        "sender": actor or sender,
        "kind": kind,
        "requested": requested,
    }
    try:
        body = await _deliver(dev, text, policy, requested, image)
    except DeviceError as exc:
        messages.record(dev["name"], {**row, "shown": False, "result": str(exc)[:200]})
        raise
    shown = body.get("shown") is True
    messages.record(dev["name"], {**row, "shown": shown})
    return body


async def _show_lines(
    dev: dict[str, Any], lines: list[str], policy: dict[str, Any]
) -> list[str]:
    """Show narration lines one after another; a line the TV does not confirm raises."""
    shown: list[str] = []
    for i, line in enumerate(lines):
        if i:
            await asyncio.sleep(NARRATE_GAP_S)
        await send_message(
            dev,
            line,
            policy=policy,
            sender="narrate",
            requested=True,
            kind="narration",
        )
        shown.append(line)
    return shown


class Narration:
    """Narration depth for one verb: every_step (intro, step with button names, end), start_end, or off."""

    def __init__(self, dev: dict[str, Any], policy: dict[str, Any]) -> None:
        self.dev, self.policy = dev, policy
        self.mode = messages.narration_mode(dev, policy)
        self.shown: list[str] = []

    @property
    def teach(self) -> bool:
        return self.mode == "every_step"

    async def begin(self, words: str) -> None:
        if self.mode == "off":
            return
        lines = [messages.intro_line(self.policy), words] if self.teach else [words]
        self.shown = await _show_lines(self.dev, lines, self.policy)

    async def finish(self, result: dict[str, Any]) -> dict[str, Any]:
        """Show the end message after the action; a miss is reported, the action already happened."""
        if self.mode == "off" or not self.shown:
            return {**result, "narration_mode": self.mode}
        try:
            self.shown += await _show_lines(self.dev, [END_LINE], self.policy)
        except DeviceError as exc:
            result = {**result, "narration_error": str(exc)}
        return {**result, "narration": self.shown, "narration_mode": self.mode}


async def _narration(dev: dict[str, Any]) -> Narration:
    return Narration(dev, await load_message_policy())


def _button(dev: dict[str, Any], key: str, teach: bool = True) -> str | None:
    if not teach:
        return None
    return SHIELD_BUTTONS.get(key) if dev["type"] == "shield" else None


def _open_words(dev: dict[str, Any], pkg: str, teach: bool = True) -> str:
    name = appcache.app_title(pkg)
    name = "the app" if name == pkg else name
    home, select = _button(dev, "HOME", teach), _button(dev, "SELECT", teach)
    if home and select:
        return (
            f"I am opening {name}. To do it yourself: press {home}, "
            f"move to {name}, and press {select}."
        )
    return f"I am opening {name}."


def _home_words(dev: dict[str, Any], teach: bool = True) -> str:
    home = _button(dev, "HOME", teach)
    if home:
        return f"I am going to the home screen. To do it yourself: press {home}."
    return "I am going to the home screen."


async def do_narrate(device: str, steps: str, speak: bool = False) -> dict[str, Any]:
    """Show the assistant intro (messages.intro_line) and then each '|'-separated step on screen, one after another."""

    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        _require_adb(dev, "narrate")
        lines = [s.strip() for s in steps.split("|") if s.strip()]
        if not lines or len(lines) > NARRATE_MAX_STEPS:
            raise DeviceError(
                f"narrate needs 1-{NARRATE_MAX_STEPS} steps separated by |"
            )
        narration = await _narration(dev)
        if narration.mode == "off":
            show: list[str] = []
        elif narration.mode == "start_end":
            show = [messages.intro_line(narration.policy), *lines[-1:]]
        else:
            show = [messages.intro_line(narration.policy), *lines]
        body: dict[str, Any] = {
            "shown": await _show_lines(dev, show, narration.policy),
            "narration_mode": narration.mode,
        }
        if speak:
            body["speech"] = NO_SPEECH
        return body

    return await guarded(device, act)


async def _wait_screen(
    dev: dict[str, Any], ok: Callable[[dict[str, Any]], bool], timeout: float
) -> tuple[bool, dict[str, Any]]:
    """Poll current_screen (no UI dump) until ok(screen) or the timeout; return the last screen."""
    deadline = time.monotonic() + timeout
    while True:
        screen = await adbops.current_screen(dev, focus=False)
        if ok(screen):
            return True, screen
        if time.monotonic() >= deadline:
            return False, screen
        await asyncio.sleep(CONFIRM_POLL_S)


async def _wake_first(dev: dict[str, Any]) -> bool:
    """Send WAKEUP unless the device is awake (asleep, dozing, or screensaver); True when it woke."""
    screen = await adbops.current_screen(dev, focus=False)
    if screen.get("awake") is True:
        return False
    await adbops.send_key(dev, "WAKEUP")
    ok, screen = await _wait_screen(
        dev, lambda s: s.get("awake") is True, WAKE_TIMEOUT_S
    )
    if not ok:
        raise DeviceError(
            f"not_awake: the device is still {screen.get('wakefulness')} "
            f"{WAKE_TIMEOUT_S:g} s after WAKEUP; nothing launched"
        )
    return True


def _confirm_timeout(woke: bool) -> float:
    return WOKE_CONFIRM_TIMEOUT_S if woke else CONFIRM_TIMEOUT_S


async def _open_and_confirm(
    dev: dict[str, Any], pkg: str, woke: bool = False
) -> dict[str, Any]:
    launched = await adbops.launch(dev, adbops.check_pkg(pkg))
    timeout = _confirm_timeout(woke)
    ok, screen = await _wait_screen(dev, lambda s: s.get("package") == pkg, timeout)
    if not ok:
        raise DeviceError(
            f"not_confirmed: {pkg} is not in front after {timeout:g} s; "
            f"in front: {screen.get('package')}"
        )
    note_opened(dev["name"], pkg)
    return {
        **launched,
        "confirmed": True,
        "package": pkg,
        "activity": screen.get("activity"),
    }


async def _narrated(
    dev: dict[str, Any],
    words: Callable[[bool], str] | None,
    body: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Narrate per the household's depth first (a failed message stops the action), run body, then say it is done."""
    if words is None:
        return await body()
    narration = await _narration(dev)
    await narration.begin(words(narration.teach))
    return await narration.finish(await body())


async def do_open_app(device: str, app: str, narrate: bool = False) -> dict[str, Any]:
    """Launch an Android app, then confirm through current_screen that it is in front."""

    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        _require_adb(dev, "open_app")
        app_id = await check_app(dev, app)
        target = (
            SHIELD_HOME_APP
            if dev["type"] == "shield" and app_id in STOCK_LAUNCHERS
            else app_id
        )
        adbops.check_pkg(target)
        woke = await _wake_first(dev)
        return await _narrated(
            dev,
            (lambda teach: _open_words(dev, target, teach)) if narrate else None,
            lambda: _open_and_confirm(dev, target, woke),
        )

    return await guarded(device, act)


async def do_go_home(device: str, narrate: bool = False) -> dict[str, Any]:
    """Shield: open Projectivy and confirm it is in front. Fire OS: press HOME and report the screen."""

    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        _require_adb(dev, "go_home")
        woke = await _wake_first(dev)

        async def home() -> dict[str, Any]:
            if dev["type"] == "shield":
                return await _open_and_confirm(dev, SHIELD_HOME_APP, woke)
            sent = await adbops.send_key(dev, "HOME")
            screen = await adbops.current_screen(dev, focus=False)
            return {**sent, "confirmed": None, "package": screen.get("package")}

        return await _narrated(
            dev,
            (lambda teach: _home_words(dev, teach)) if narrate else None,
            home,
        )

    return await guarded(device, act)


NAV_KEYS = frozenset(
    {
        "DPAD_UP",
        "DPAD_DOWN",
        "DPAD_LEFT",
        "DPAD_RIGHT",
        "DPAD_CENTER",
        "BACK",
        "MEDIA_PLAY_PAUSE",
        "MEDIA_PLAY",
        "MEDIA_PAUSE",
    }
)
NAV_MAX_KEYS = 10
NAV_WORDS = {
    "DPAD_UP": "move up",
    "DPAD_DOWN": "move down",
    "DPAD_LEFT": "move left",
    "DPAD_RIGHT": "move right",
    "BACK": "go back",
    "MEDIA_PLAY_PAUSE": "play or pause",
    "MEDIA_PLAY": "play",
    "MEDIA_PAUSE": "pause",
}


def _focus_matches(focus: dict[str, Any] | None, want: str) -> bool:
    if not focus:
        return False
    fields = (focus.get("text"), focus.get("content_desc"), focus.get("resource_id"))
    return any(want.casefold() in (f or "").casefold() for f in fields)


async def check_screen(
    dev: dict[str, Any], expect_app: str, expect_focus: str = ""
) -> dict[str, Any]:
    """Read current_screen and raise screen_mismatch unless expect_app is in front (and the focus matches)."""
    screen = await adbops.current_screen(dev, focus=bool(expect_focus))
    if screen.get("package") != expect_app:
        raise DeviceError(
            f"screen_mismatch: expected {expect_app} in front, found {screen.get('package')}; no key sent"
        )
    if expect_focus and not _focus_matches(screen.get("focus"), expect_focus):
        raise DeviceError(
            f"screen_mismatch: focus does not match {expect_focus!r} "
            f"(focused: {screen.get('focus')}); no key sent"
        )
    return screen


def _nav_words(dev: dict[str, Any], keys: list[str], teach: bool = True) -> str:
    runs: list[tuple[str, int]] = []
    for key in keys:
        if runs and runs[-1][0] == key:
            runs[-1] = (key, runs[-1][1] + 1)
        else:
            runs.append((key, 1))
    select = _button(dev, "SELECT", teach)
    parts = []
    for key, n in runs:
        if key == "DPAD_CENTER":
            part = f"press {select}" if select else "select it"
        else:
            part = NAV_WORDS[key]
        parts.append(part + (f" {n} times" if n > 1 else ""))
    return "I will " + ", then ".join(parts) + "."


async def do_navigate(
    device: str,
    keys: str,
    expect_app: str,
    expect_focus: str = "",
    narrate: bool = False,
) -> dict[str, Any]:
    """Send up to 10 navigation keys, reading current_screen before each one; stop on a mismatch."""

    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        if not expect_app:
            raise DeviceError(
                "navigate needs expect_app (the package that must be in front)"
            )
        adbops.check_pkg(expect_app)
        names = [adbops.normalize_key(k) for k in keys.split(",") if k.strip()]
        if not names or len(names) > NAV_MAX_KEYS:
            raise DeviceError(
                f"navigate needs 1-{NAV_MAX_KEYS} keys separated by commas"
            )
        if bad := sorted(set(names) - NAV_KEYS):
            raise DeviceError(f"navigate sends only {sorted(NAV_KEYS)}; refused {bad}")
        _require_adb(dev, "navigate")
        await _wake_first(dev)
        await check_screen(dev, expect_app, expect_focus)
        narration = await _narration(dev) if narrate else None
        if narration:
            await narration.begin(_nav_words(dev, names, narration.teach))
        sent: list[str] = []
        for i, name in enumerate(names):
            if i or (narration and narration.shown):
                await check_screen(dev, expect_app)
            await adbops.send_key(dev, name)
            sent.append(name)
        body: dict[str, Any] = {
            "sent": sent,
            "screen": await adbops.current_screen(dev, focus=True),
        }
        return await narration.finish(body) if narration else body

    return await guarded(device, act)


async def screen_guard(
    device: str, expect_app: str, expect_focus: str
) -> dict[str, Any] | None:
    """None when no expectation is set or the screen matches; otherwise the error reply."""
    if not expect_app and not expect_focus:
        return None

    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        _require_adb(dev, "expect_app")
        if not expect_app:
            raise DeviceError("expect_focus needs expect_app")
        await check_screen(dev, expect_app, expect_focus)
        return {}

    body = await guarded(device, act, locked=False)
    return body if "error" in body else None


def plex_deep_link(machine_id: str, rating_key: str, view_offset_ms: int = 0) -> str:
    if not PLEX_MACHINE_ID_RE.match(machine_id):
        raise DeviceError("machine_id must be the Plex server machineIdentifier")
    if not PLEX_RATING_KEY_RE.match(rating_key):
        raise DeviceError("rating_key must be the numeric Plex ratingKey")
    if type(view_offset_ms) is not int or not 0 <= view_offset_ms <= MAX_VIEW_OFFSET_MS:
        raise DeviceError(
            "view_offset_ms must be whole milliseconds from 0 to 24 hours"
        )
    offset = f"viewOffset={view_offset_ms}&" if view_offset_ms else ""
    return (
        f"plex://server://{machine_id}/com.plexapp.plugins.library/library/metadata/"
        f"{rating_key}?{offset}autoPlay=1"
    )


async def do_play_title(
    device: str,
    machine_id: str,
    rating_key: str,
    narrate: bool = False,
    view_offset_ms: int = 0,
) -> dict[str, Any]:
    """Open a Plex title by deep link with autoPlay, then wait for Plex's media session to play."""

    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        uri = plex_deep_link(machine_id, rating_key, view_offset_ms)
        _require_adb(dev, "play_title")
        await check_app(dev, PLEX_PKG)
        woke = await _wake_first(dev)
        return await _narrated(
            dev,
            (lambda teach: "I am starting your show in Plex.") if narrate else None,
            lambda: play(dev, uri, woke),
        )

    async def play(dev: dict[str, Any], uri: str, woke: bool) -> dict[str, Any]:
        await adbops.view_uri(dev, uri, PLEX_PKG)
        timeout = PLAY_TIMEOUT_S + (WOKE_CONFIRM_TIMEOUT_S if woke else 0)

        def plex_playing(screen: dict[str, Any]) -> bool:
            session = adbops.session_for(screen.get("sessions") or [], PLEX_PKG)
            return bool(
                session
                and session.get("package") == PLEX_PKG
                and session.get("state") in adbops.ACTIVE_STATES
            )

        ok, screen = await _wait_screen(dev, plex_playing, timeout)
        session = adbops.session_for(screen.get("sessions") or [], PLEX_PKG) or {}
        if not ok:
            raise DeviceError(
                f"not_playing: Plex did not report playback within {timeout:g} s; "
                f"in front: {screen.get('package')}, Plex state: {session.get('state')}"
            )
        return {
            "started": uri,
            "playing": True,
            "title": session.get("title"),
            "package": screen.get("package"),
        }

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
# The stock launcher is never wanted on a Shield.
SHIELD_HOME_APP = "com.spocky.projengmenu"
STOCK_LAUNCHERS = frozenset(
    {"com.google.android.tvlauncher", "com.google.android.apps.tv.launcherx"}
)


async def refresh_lists(device: str) -> dict[str, Any]:
    """Fetch the live app list (and LG inputs), store it, and return the guarded reply."""

    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        # Own lock: one refresh per device, and commands never queue behind its APK pulls.
        async with device_lock(f"refresh:{device}"):
            lists = await _live_lists(dev)
            for kind, items in lists.items():
                if kind == "apps":
                    items = appcache.friendly_apps(items)
                appcache.put(device, kind, items)
        return {"refreshed": sorted(lists)}

    return await guarded(device, act, locked=False)


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
        # A cache written before device labels held the package id as every title; an app with no
        # readable label also shows its id, so only an all-id list counts as legacy.
        legacy_labels = (
            kind == "apps"
            and bool(entry["items"])
            and all(a.get("title") == a.get("id") for a in entry["items"])
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
                            if (dev.get("input_keys") or {}).get(key)
                            or (dev.get("input_ids") or {}).get(key)
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
    denied = await load_denied_apps()
    apps = appcache.quick_apps(entry["items"], await load_quick_apps(), denied)
    try:
        apps = apppolicy.visible(await get_device(device), apps, denied)
    except DeviceError:
        apps = []
    return {
        "device": device,
        "apps": apps,
        "age_s": int(time.time() - entry["fetched_at"]),
    }


async def do_toast(
    device: str,
    message: str,
    sender: str = "operator",
    requested: bool = False,
    kind: str = "",
    household: str = "",
    image: str = "",
    actor: str = "",
) -> dict[str, Any]:
    """Send one message under the policy in devices.yml (messages:); see messages.check_sender and _deliver."""

    async def act(dev: dict[str, Any]) -> dict[str, Any]:
        policy = await load_message_policy()
        row = {
            "text": message.strip()[: adbops.TOAST_MAX_CHARS],
            "requested": requested,
        }
        try:
            kind_ = messages.check_sender(
                dev, policy, sender, requested, kind, household
            )
            image_ = messages.check_image(image)
        except DeviceError as exc:
            messages.record(
                dev["name"],
                {
                    **row,
                    "sender": actor or sender,
                    "kind": kind,
                    "shown": False,
                    "result": str(exc)[:200],
                },
            )
            raise
        try:
            return await send_message(
                dev,
                message,
                policy=policy,
                sender=sender,
                requested=requested,
                kind=kind_,
                image=image_,
                actor=actor,
            )
        except Dropped as exc:
            return {**exc.body(), "shown": False, "dropped": exc.reason}

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
        if dev["type"] == "shield":
            return await adbops.send_key(dev, "WAKEUP")
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
            input_key = (dev.get("input_keys") or {}).get(input_id)
            if input_key:
                return await adbops.set_input_key(dev, input_key)
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


# Multi-step flows (open_app, then play_title or navigate) would otherwise refuse their own
# next step: an app tv-control just opened, with nothing playing, is not someone watching.
OPENED_GRACE_S = 120.0
_opened: dict[str, tuple[str, float]] = {}


def note_opened(device: str, pkg: str) -> None:
    _opened[device] = (pkg, time.monotonic())


def _opened_recently(device: str, focus: str) -> bool:
    pkg, at = _opened.get(device, ("", 0.0))
    fresh = time.monotonic() - at < OPENED_GRACE_S
    return bool(pkg) and fresh and f" {pkg}/" in focus


# Fire OS home screens; in front of them a Fire OS device is idle unless something plays.
FIRE_LAUNCHER = "com.amazon.tv.launcher"
HOME_APPS = {
    "shield": frozenset({SHIELD_HOME_APP}),
    "firetv": frozenset({SHIELD_HOME_APP, FIRE_LAUNCHER}),
    "firestick": frozenset({SHIELD_HOME_APP, FIRE_LAUNCHER}),
}


async def _adb_busy(dev: dict[str, Any]) -> bool:
    serial = await adbops.require_device(dev)
    awake = adbops.is_awake(
        await adbops.read(serial, "dumpsys power | grep mWakefulness=")
    )
    if not awake:
        return False
    playing = adbops.is_playing(
        await adbops.read(serial, "dumpsys media_session | grep 'state=PlaybackState'")
    )
    focus = await adbops.read(serial, "dumpsys window | grep mCurrentFocus")
    # Pause reads as not playing, so a device in any app outside its home screen counts too.
    # TvOverlay's own screen (left open after setup) is not someone's app, and a Fire OS TV
    # showing an HDMI input is judged by the device on that input (paired_tv), not here.
    homes = HOME_APPS.get(dev["type"])
    in_app = (
        homes is not None
        and not any(home in focus for home in homes)
        and f" {adbops.OVERLAY_PKG}/" not in focus
        and f" {adbops.PASSTHROUGH_PKG}/" not in focus
    )
    if not playing and not in_app:
        return False
    return bool(playing) or not _opened_recently(dev["name"], focus)


async def device_busy(dev: dict[str, Any]) -> bool:
    """True when someone may be watching dev. A read failure counts as not busy."""
    try:
        if dev["type"] == "webos":
            # Playing or paused in the TV's media pipeline. An app with no media session
            # (a menu, a store page) is not visible to this read.
            return any(
                state in webos.BUSY_PLAY_STATES
                for state in await webos.play_states(dev)
            )
        if dev["type"] not in adbops.ADB_TYPES:
            return False
        return await _adb_busy(dev)
    except DeviceError:
        return False


async def _busy_feeder(tv: str) -> str | None:
    """The first active device whose paired_tv is tv and that someone may be watching."""
    try:
        devices = await load_devices()
    except DeviceError:
        return None
    for dev in devices:
        if dev.get("paired_tv") == tv and is_active(dev) and await device_busy(dev):
            return str(dev["name"])
    return None


IN_USE_NEXT_STEP = (
    "Do not test here. Pass force=true only when the operator asks for this command."
)


async def in_use_refusal(
    device: str, force: bool, paired: bool = False
) -> dict[str, Any] | None:
    """Stop an agent's command when someone may be watching. Panel presses never come here.

    paired=True (input switches and launches) also refuses while a device whose paired_tv is
    this TV is playing or open in an app. Read failures return None so the command reports
    its own device error.
    """
    if force:
        return None
    try:
        dev = await get_device(device)
    except DeviceError:
        return None
    if await device_busy(dev):
        return {
            "device": device,
            "error": "in_use: someone may be watching this device",
            "cause": "The device is awake and playing media or open in an app.",
            "next_step": IN_USE_NEXT_STEP,
        }
    feeder = await _busy_feeder(device) if paired else None
    if feeder:
        return {
            "device": device,
            "error": f"in_use: {feeder} on this TV may be in use",
            "cause": f"{feeder} feeds this TV (paired_tv) and is playing media or open in an app.",
            "next_step": IN_USE_NEXT_STEP,
        }
    return None


async def write_gate(
    device: str, force: bool, app: str | None = None, paired: bool = False
) -> dict[str, Any] | None:
    """The policy check before every agent write verb: the app policy (never skipped by force),
    then the in-use refusal. Returns the refusal reply, or None to go ahead."""
    if app is not None:
        try:
            await check_app(await get_device(device), app)
        except apppolicy.AppRefusal as exc:
            return {"device": device, **exc.body()}
        except DeviceError:
            pass  # The verb itself reports a lookup or inventory error.
    if paired:
        return await in_use_refusal(device, force, paired=True)
    return await in_use_refusal(device, force)


@mcp.tool()
async def list_devices() -> dict[str, list[dict[str, Any]]]:
    """List every TV in devices.yml, grouped by site."""
    return await inventory_by_site()


@mcp.tool()
async def device_status(device: str) -> dict[str, Any]:
    """Return live status: ADB state, foreground window, and SmartTube version; or webOS power, app, and volume."""
    return await do_status(device)


@mcp.tool()
async def current_screen(device: str, focus: bool = True) -> dict[str, Any]:
    """Read-only: foreground package and activity, focus window, wakefulness, media sessions, and the focused view (text, id, bounds) from uiautomator. focus=false skips the 2-3 s UI dump."""
    return await do_current_screen(device, focus)


@mcp.tool()
async def what_is_on(device: str) -> dict[str, Any]:
    """Read-only: the app in front, playback state (playing, paused, none), and the title the media session reports."""
    return await do_what_is_on(device)


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
async def send_key(
    device: str,
    key: str,
    expect_app: str = "",
    expect_focus: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """Press one remote key: HOME, BACK, DPAD_UP/DOWN/LEFT/RIGHT/CENTER, MEDIA_*, VOLUME_UP/DOWN/MUTE, SLEEP, WAKEUP.

    expect_app (and optional expect_focus, matched against the focused view's text, description, or id)
    reads current_screen first and refuses with screen_mismatch. For key sequences use navigate."""
    return (
        await write_gate(device, force)
        or await screen_guard(device, expect_app, expect_focus)
        or await do_key(device, key)
    )


@mcp.tool()
async def navigate(
    device: str,
    keys: str,
    expect_app: str,
    expect_focus: str = "",
    narrate: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Send up to 10 comma-separated navigation keys (DPAD_*, OK, BACK, MEDIA_PLAY/PAUSE). current_screen is read before every key: expect_app must stay in front, and expect_focus (if set) must match the focused view before the first key; otherwise screen_mismatch and no further key. Returns the final screen."""
    return await write_gate(device, force) or await do_navigate(
        device, keys, expect_app, expect_focus, narrate
    )


@mcp.tool()
async def send_text(device: str, text: str, force: bool = False) -> dict[str, Any]:
    """Type text into the focused field of an Android device: Remote v2 IME when paired, else adb input text."""
    return await write_gate(device, force) or await do_text(device, text)


@mcp.tool()
async def pair_start(device: str) -> dict[str, Any]:
    """Start Shield fast-key pairing. The TV shows a code; someone at the TV must read it. Pass it to pair_finish."""
    return await do_remote_pair(device)


@mcp.tool()
async def pair_finish(device: str, code: str) -> dict[str, Any]:
    """Finish Shield fast-key pairing with the code shown on the TV."""
    return await do_remote_pair(device, code)


@mcp.tool()
async def list_apps(device: str, refresh: bool = False) -> dict[str, Any]:
    """List launchable apps: Android package names, or webOS app ids with titles. Served from a 24 h cache; refresh=true goes live."""
    return await do_list_apps(device, refresh)


@mcp.tool()
async def list_inputs(device: str, refresh: bool = False) -> dict[str, Any]:
    """List the inputs of an LG webOS TV (id and label), from a 24 h cache; refresh=true goes live."""
    return await do_list_inputs(device, refresh)


@mcp.tool()
async def set_input(device: str, input_id: str, force: bool = False) -> dict[str, Any]:
    """Switch an LG webOS TV to an explicit input id from list_inputs. There is no default input."""
    return await write_gate(device, force, paired=True) or await do_set_input(
        device, input_id
    )


@mcp.tool()
async def wake(device: str, force: bool = False) -> dict[str, Any]:
    """Wake an LG webOS TV with Wake-on-LAN sent from the site's LAN relay (devices.yml mac and wol_relay)."""
    return await write_gate(device, force) or await do_wake(device)


@mcp.tool()
async def power(device: str, state: str, force: bool = False) -> dict[str, Any]:
    """Power a device on or off. Off is KEYCODE_SLEEP (standby) on ADB devices and power off on webOS."""
    return await write_gate(device, force) or await do_power(device, state)


@mcp.tool()
async def find_remote(device: str, force: bool = False) -> dict[str, Any]:
    """Make an NVIDIA Shield remote buzz (Remote Locator). Needs the find_remote capability in devices.yml."""
    return await write_gate(device, force) or await do_find_remote(device)


@mcp.tool()
async def sleep_timer(
    device: str, minutes: int = -1, force: bool = False
) -> dict[str, Any]:
    """Set a server-side sleep timer (15, 30, 60, or 90 min), cancel it (0), or read it (omit minutes)."""
    return await write_gate(device, force) or await do_sleep_timer(
        device, None if minutes < 0 else minutes
    )


@mcp.tool()
async def launch_app(device: str, app: str, force: bool = False) -> dict[str, Any]:
    """Launch an app by package id, webOS app id, or name (see list_apps). A name resolves by exact label, id, app_aliases, then unique prefix; ambiguous_app or unknown_app returns candidates. denied_apps, allowed_apps, and protected targets (stores, Settings, sign-in) are refused even with force=true."""
    return await write_gate(device, force, app=app, paired=True) or await do_launch(
        device, app
    )


@mcp.tool()
async def open_app(
    device: str, app: str, narrate: bool = False, force: bool = False
) -> dict[str, Any]:
    """Launch an Android app by package id or name (resolved as in launch_app, same app policy), then confirm it is in front (error not_confirmed otherwise). narrate=true shows the assistant intro and how a person does it first."""
    return await write_gate(device, force, app=app, paired=True) or await do_open_app(
        device, app, narrate
    )


@mcp.tool()
async def go_home(
    device: str, narrate: bool = False, force: bool = False
) -> dict[str, Any]:
    """Go to the home screen: Projectivy on a Shield (confirmed in front), the HOME key on Fire OS. narrate=true shows the steps first."""
    return await write_gate(device, force) or await do_go_home(device, narrate)


@mcp.tool()
async def play_title(
    device: str,
    machine_id: str,
    rating_key: str,
    narrate: bool = False,
    force: bool = False,
    view_offset_ms: int = 0,
) -> dict[str, Any]:
    """Play a Plex title by deep link (server machineIdentifier and ratingKey); confirms Plex's media session plays. view_offset_ms starts it at that point (0 keeps Plex's own choice). narrate=true shows the steps first."""
    return await write_gate(
        device, force, app=PLEX_PKG, paired=True
    ) or await do_play_title(device, machine_id, rating_key, narrate, view_offset_ms)


@mcp.tool()
async def narrate(
    device: str, steps: str, speak: bool = False, force: bool = False
) -> dict[str, Any]:
    """Show "Hi, this is <messages.assistant.name>. I will navigate now." and then each step (separated by |) on screen. speak=true reports that speech is not available."""
    return await write_gate(device, force) or await do_narrate(device, steps, speak)


@mcp.tool()
async def toast(
    device: str,
    message: str,
    sender: str = "operator",
    requested: bool = False,
    kind: str = "",
    household: str = "",
    image: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """Show a short on-screen message: a webOS toast on LG, a TvOverlay message (messages.title) on Android devices.

    Policy (devices.yml messages:): shows over playback at any time. sender=operator reaches any TV;
    sender=<messages.assistant.sender> needs household=<the TV's household> and either requested=true (the household asked)
    or kind in media_ready, reply_waiting (operator_push is the operator's). requested=false needs a kind
    (an operator message defaults to operator_push) and is dropped, returned shown=false, when the TV is
    asleep or inside quiet hours (10 pm to 7 am, device clock). requested=true wakes a sleeping TV.
    image: an http(s) poster URL or mdi:<name> (TvOverlay image field). sender is declared, not
    authenticated: the MCP bearer token is the trust boundary. force is accepted and ignored.
    Android replies shown=true only after HTTP 200 and a visible TvOverlay window; otherwise an error
    (overlay_app_missing, overlay_not_permitted, screen_not_on, not_shown, not_allowed, wrong_household)."""
    return await do_toast(device, message, sender, requested, kind, household, image)


@mcp.tool()
async def reboot(device: str, force: bool = False) -> dict[str, Any]:
    """Reboot an ADB device (plain reboot, never power-off). To turn a Shield off, send_key SLEEP."""
    return await write_gate(device, force) or await do_reboot(device)


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
