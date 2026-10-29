"""ADB calls for Shields and Fire OS devices, on the shared local ADB server.

No function here runs the adb disconnect command: it would drop the device for every other client of the shared ADB server.
"""

import asyncio
import io
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import appcache
from inventory import DeviceError, run
from PIL import Image as PILImage

ADB_TIMEOUT_S = 20
SCREENCAP_TIMEOUT_S = 90
SCREENSHOT_MAX_PX = 1280
BLACK_MAX_LEVEL = 16
SMARTTUBE_PKG = "org.smarttube.stable"
ADB_TYPES = frozenset({"shield", "firestick", "firetv"})
FIRE_TYPES = frozenset({"firestick", "firetv"})
UNDO_DIR = Path(os.environ.get("UNDO_DIR", "/config/undo"))

KEYS = frozenset(
    {
        "HOME",
        "BACK",
        "DPAD_UP",
        "DPAD_DOWN",
        "DPAD_LEFT",
        "DPAD_RIGHT",
        "DPAD_CENTER",
        "MEDIA_PLAY_PAUSE",
        "MEDIA_PLAY",
        "MEDIA_PAUSE",
        "MEDIA_STOP",
        "MEDIA_NEXT",
        "MEDIA_PREVIOUS",
        "MEDIA_FAST_FORWARD",
        "MEDIA_REWIND",
        "VOLUME_UP",
        "VOLUME_DOWN",
        "VOLUME_MUTE",
        "SLEEP",
        "WAKEUP",
    }
)
KEY_ALIASES = {
    "CENTER": "DPAD_CENTER",
    "OK": "DPAD_CENTER",
    "UP": "DPAD_UP",
    "DOWN": "DPAD_DOWN",
    "LEFT": "DPAD_LEFT",
    "RIGHT": "DPAD_RIGHT",
}
# Keys that reach the TV only through HDMI-CEC; tv_control: best-effort devices refuse them.
CEC_KEYS = frozenset({"VOLUME_UP", "VOLUME_DOWN", "VOLUME_MUTE"})
PKG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.]+/[A-Za-z0-9_.$]+$")

# Every key deploy-config.sh, lockdown.sh, remap-buttons.sh, and firetv-settings.sh (sourced tier) write.
# tests/test_static.py fails when a script writes a key missing here.
UNDO_KEYS = (
    ("secure", "enabled_accessibility_services"),
    ("secure", "accessibility_enabled"),
    ("secure", "key_app_map"),
    ("secure", "sleep_timeout"),
    ("global", "always_on_vpn_app"),
    ("global", "always_on_vpn_lockdown"),
    ("global", "private_dns_mode"),
)


def normalize_key(key: str) -> str:
    name = key.strip().upper().removeprefix("KEYCODE_")
    name = KEY_ALIASES.get(name, name)
    if name not in KEYS:
        raise DeviceError(f"key {key!r} not allowed; use one of {sorted(KEYS)}")
    return name


def check_pkg(pkg: str) -> str:
    if not PKG_RE.match(pkg):
        raise DeviceError(f"{pkg!r} is not a package name")
    return pkg


# Serial that last reached a device; health and lockdown reuse it instead of re-probing.
_active: dict[str, str] = {}


def candidates(dev: dict[str, Any]) -> list[str]:
    """ADB serials to try in order: the primary ip, then the MagicDNS name."""
    port = dev.get("port") or 5555
    hosts = dict.fromkeys(h for h in (dev.get("ip"), dev.get("tailnet_fqdn")) if h)
    return [f"{h}:{port}" for h in hosts]


def serial_of(dev: dict[str, Any]) -> str:
    return _active.get(dev["name"]) or candidates(dev)[0]


async def adb(
    serial: str, *args: str, timeout: float = ADB_TIMEOUT_S
) -> tuple[int, bytes, str]:
    return await run("adb", "-s", serial, *args, timeout=timeout)


async def adb_ready(dev: dict[str, Any]) -> tuple[str, str]:
    """Connect if needed and return (serial, state), trying each candidate address."""
    best: tuple[str, str] = (candidates(dev)[0], "offline")
    for serial in candidates(dev):
        serial, state = await _ready_one(dev, serial)
        if state == "device":
            _active[dev["name"]] = serial
            return serial, state
        if state == "unauthorized":
            best = (serial, state)
    return best


async def _ready_one(dev: dict[str, Any], serial: str) -> tuple[str, str]:
    # The adb server keeps the transport open between commands; connect only after a failed probe
    # (Fire OS drops idle sessions, which shows up here as a state other than "device").
    _, out, _ = await adb(serial, "get-state")
    if out.decode().strip() == "device":
        return serial, "device"
    await run("adb", "connect", serial)
    rc, out, err = await adb(serial, "get-state")
    if rc == 0:
        return serial, out.decode().strip()
    return serial, "unauthorized" if "unauthorized" in err else "offline"


async def require_device(dev: dict[str, Any]) -> str:
    serial, state = await adb_ready(dev)
    if state != "device":
        raise DeviceError(f"adb state is {state!r}, not device")
    return serial


async def shell(serial: str, command: str) -> str:
    rc, out, err = await adb(serial, "shell", command)
    if rc != 0:
        raise DeviceError(f"adb shell failed: {err}")
    return out.decode(errors="replace")


async def read(serial: str, command: str) -> str:
    """Run a read whose grep may match nothing; only an adb-level failure raises."""
    return await shell(serial, f"{command} || true")


async def status(dev: dict[str, Any]) -> dict[str, Any]:
    serial, state = await adb_ready(dev)
    body: dict[str, Any] = {"adb_state": state}
    if state != "device":
        return body
    focus = await read(serial, "dumpsys window | grep mCurrentFocus")
    body["foreground"] = focus.strip().splitlines()[0] if focus.strip() else None
    pkg = await read(serial, f"dumpsys package {SMARTTUBE_PKG} | grep versionName")
    match = re.search(r"versionName=(\S+)", pkg)
    body["smarttube_version"] = match.group(1) if match else None
    return body


HEALTH_READS = {
    "private_dns_mode": "settings get global private_dns_mode",
    "private_dns_specifier": "settings get global private_dns_specifier",
    "always_on_vpn_app": "settings get global always_on_vpn_app",
    "accessibility_services": "settings get secure enabled_accessibility_services",
    "resumed_activity": "dumpsys activity activities | grep -m1 -E 'mResumedActivity|topResumedActivity'",
}


PLAYBACK_PLAYING_RE = re.compile(r"PlaybackState \{state=3\b")


def is_awake(power_dump: str) -> bool:
    return "mWakefulness=Awake" in power_dump


def is_playing(session_dump: str) -> bool:
    return bool(PLAYBACK_PLAYING_RE.search(session_dump))


async def health(dev: dict[str, Any]) -> dict[str, Any]:
    body = await status(dev)
    if body["adb_state"] != "device":
        return body
    serial = serial_of(dev)
    body["via"] = serial
    for field, cmd in HEALTH_READS.items():
        body[field] = (await read(serial, cmd)).strip() or None
    if dev.get("paired_tv"):
        body["awake"] = is_awake(
            await read(serial, "dumpsys power | grep mWakefulness=")
        )
        body["playing"] = is_playing(
            await read(serial, "dumpsys media_session | grep 'state=PlaybackState'")
        )
    return body


async def screenshot(dev: dict[str, Any]) -> tuple[bytes, str | None]:
    serial = await require_device(dev)
    # A 4K PNG is about 4.5 MB and takes about 35 s over the subnet route.
    rc, png, err = await adb(
        serial, "exec-out", "screencap", "-p", timeout=SCREENCAP_TIMEOUT_S
    )
    if rc != 0 or not png.startswith(b"\x89PNG"):
        raise DeviceError(f"screencap failed: {err or 'no PNG data'}")
    return await asyncio.to_thread(shrink, png, dev["type"] in FIRE_TYPES)


def shrink(png: bytes, fire: bool) -> tuple[bytes, str | None]:
    """Downscale to a JPEG MCP clients accept, and flag an all-black frame."""
    with PILImage.open(io.BytesIO(png)) as img:
        rgb = img.convert("RGB")
    black = (
        max(hi for _, hi in cast(tuple[tuple[int, int], ...], rgb.getextrema()))
        < BLACK_MAX_LEVEL
    )
    rgb.thumbnail((SCREENSHOT_MAX_PX, SCREENSHOT_MAX_PX))
    buf = io.BytesIO()
    rgb.save(buf, format="JPEG", quality=80)
    note = None
    if black:
        note = "all-black frame" + (
            ": Fire OS blanks DRM content (Prime Video, Netflix) in screencap"
            if fire
            else ""
        )
    return buf.getvalue(), note


async def send_key(dev: dict[str, Any], key: str) -> dict[str, Any]:
    serial = await require_device(dev)
    rc, _, err = await adb(serial, "shell", "input", "keyevent", f"KEYCODE_{key}")
    if rc != 0:
        raise DeviceError(f"keyevent failed: {err}")
    return {"sent": key}


_key_shells: dict[str, asyncio.subprocess.Process] = {}


def _shell_alive(proc: asyncio.subprocess.Process | None) -> bool:
    return (
        proc is not None
        and proc.returncode is None
        and proc.stdin is not None
        and not proc.stdin.is_closing()
    )


async def send_key_fast(dev: dict[str, Any], key: str) -> dict[str, Any]:
    """Feed one `input keyevent` line to a per-device adb shell that stays open between presses.

    Skips the get-state and connect probes and the adb process start; it does not wait for the
    key to run on the device. A dead shell is replaced once, then the press fails.
    """
    if key not in KEYS:
        raise DeviceError(f"key {key!r} not allowed")
    name = dev["name"]
    for _ in range(2):
        proc = _key_shells.get(name)
        if not _shell_alive(proc):
            serial = await require_device(dev)
            proc = await asyncio.create_subprocess_exec(
                "adb",
                "-s",
                serial,
                "shell",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            _key_shells[name] = proc
        try:
            stdin = proc.stdin if proc else None
            if stdin is None:
                raise DeviceError("adb shell has no stdin")
            stdin.write(f"input keyevent KEYCODE_{key}\n".encode())
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            _key_shells.pop(name, None)
            continue
        return {"sent": key, "via": "adb shell"}
    raise DeviceError(f"adb key shell for {name} closed; press again")


async def launch(dev: dict[str, Any], pkg: str) -> dict[str, Any]:
    serial = await require_device(dev)
    rc, out, _ = await adb(
        serial,
        "shell",
        "monkey",
        "-p",
        pkg,
        "-c",
        "android.intent.category.LEANBACK_LAUNCHER",
        "1",
    )
    if rc == 0 and b"Events injected: 1" in out:
        return {"launched": pkg, "via": "monkey"}
    # Fire OS Amazon-category apps have no LEANBACK_LAUNCHER intent.
    words = (await read(serial, f"cmd package resolve-activity --brief {pkg}")).split()
    component = words[-1] if words else ""
    if not COMPONENT_RE.match(component):
        raise DeviceError(f"no launchable activity for {pkg}")
    rc, out, err = await adb(serial, "shell", "am", "start", "-n", component)
    if rc != 0 or b"Error" in out:
        detail = err or out.decode(errors="replace").strip()
        raise DeviceError(f"am start failed: {detail}")
    return {"launched": pkg, "via": "am start", "component": component}


TV_INPUT_ID_RE = re.compile(r"^[A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+/[A-Za-z0-9_]+$")


def passthrough_uri(tv_input_id: str) -> str:
    """TvContract.buildChannelUriForPassthroughInput: content://android.media.tv/passthrough/<id>, id URL-encoded."""
    if not TV_INPUT_ID_RE.match(tv_input_id):
        raise DeviceError(f"invalid TV input id {tv_input_id!r}")
    return "content://android.media.tv/passthrough/" + quote(tv_input_id, safe="")


async def set_input(dev: dict[str, Any], tv_input_id: str) -> dict[str, Any]:
    """Switch a Fire TV to a passthrough input with a VIEW intent. Never wakes the TV."""
    uri = passthrough_uri(tv_input_id)
    serial = await require_device(dev)
    if not is_awake(await read(serial, "dumpsys power")):
        raise DeviceError("TV is asleep; input not switched (set_input does not wake)")
    rc, out, err = await adb(
        serial, "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", uri
    )
    if rc != 0 or b"Error" in out:
        raise DeviceError(
            f"am start failed: {err or out.decode(errors='replace').strip()}"
        )
    top = await read(serial, "dumpsys activity activities")
    resumed = next(
        (ln.strip() for ln in top.splitlines() if "mResumedActivity" in ln), ""
    )
    return {"input_set": tv_input_id, "via": "am start passthrough", "resumed": resumed}


REMOTE_LOCATOR = "com.nvidia.remotelocator/.ShieldRemoteLocatorActivity"
TOAST_MAX_CHARS = 200
TOAST_SAFE_RE = re.compile(r"[^A-Za-z0-9 .,!?:;()_@#%&+=/-]")


async def find_remote(dev: dict[str, Any]) -> dict[str, Any]:
    """Wake the Shield and open Remote Locator, which makes the paired remote buzz."""
    serial = await require_device(dev)
    await adb(serial, "shell", "input", "keyevent", "KEYCODE_WAKEUP")
    rc, out, err = await adb(serial, "shell", "am", "start", "-n", REMOTE_LOCATOR)
    text = out.decode(errors="replace")
    if rc != 0 or "Error" in text:
        raise DeviceError(f"am start failed: {err or text.strip()}")
    return {"launched": REMOTE_LOCATOR}


def clean_toast(message: str) -> str:
    text = TOAST_SAFE_RE.sub("", message).strip()
    if not text or len(message) > TOAST_MAX_CHARS:
        raise DeviceError(f"toast needs 1-{TOAST_MAX_CHARS} characters")
    return text


async def toast(dev: dict[str, Any], message: str) -> dict[str, Any]:
    """Post a heads-up notification with cmd notification (no root, Android 11+)."""
    text = clean_toast(message)
    serial = await require_device(dev)
    rc, out, err = await adb(
        serial,
        "shell",
        f"cmd notification post -S bigtext -t 'tv-control' tvcontrol '{text}'",
    )
    body = out.decode(errors="replace").strip()
    if rc != 0 or "error" in body.lower() or "unknown command" in body.lower():
        raise DeviceError(
            f"unsupported_on_device: toast via cmd notification failed: {err or body}"[
                :300
            ]
        )
    return {"toast": text, "via": "cmd notification"}


def apk_label(path: str) -> str | None:
    from apkutils import APK

    with APK.from_file(path) as apk:
        label: str | None = apk.parse_resource().app_name
        return label


async def app_label(serial: str, pkg: str) -> str:
    """Read the installed APK's manifest/resources; never install a helper on the TV."""
    try:
        raw = await shell(serial, f"pm path {check_pkg(pkg)}")
        paths = [
            ln.removeprefix("package:").strip()
            for ln in raw.splitlines()
            if ln.startswith("package:")
        ]
        path = next(
            (p for p in paths if p.endswith("/base.apk")), paths[0] if paths else ""
        )
        if not path.startswith("/") or not path.endswith(".apk"):
            return appcache.app_title(pkg)
        with tempfile.TemporaryDirectory(prefix="tv-app-label-") as directory:
            local = str(Path(directory) / "base.apk")
            rc, _, _ = await adb(serial, "pull", path, local, timeout=90)
            if rc == 0:
                return appcache.app_title(
                    pkg, await asyncio.to_thread(apk_label, local)
                )
    except Exception:  # noqa: BLE001 - unreadable APKs or unsupported resource tables use the fallback
        return appcache.app_title(pkg)
    return appcache.app_title(pkg)


async def list_apps(dev: dict[str, Any]) -> list[dict[str, str]]:
    serial = await require_device(dev)
    pkgs: set[str] = set()
    for category in ("LEANBACK_LAUNCHER", "LAUNCHER"):
        text = await shell(
            serial,
            "cmd package query-activities --components -a android.intent.action.MAIN "
            f"-c android.intent.category.{category}",
        )
        pkgs |= {ln.split("/")[0].strip() for ln in text.splitlines() if "/" in ln}
    apps = [
        {"id": p, "title": await app_label(serial, p)}
        for p in sorted(pkgs)
        if PKG_RE.match(p)
    ]
    return appcache.friendly_apps(apps)


async def reboot(dev: dict[str, Any]) -> dict[str, Any]:
    serial = await require_device(dev)
    # Plain reboot only: a power-off reboot leaves a Shield with no remote way back.
    rc, _, err = await adb(serial, "reboot")
    if rc != 0:
        raise DeviceError(f"reboot failed: {err}")
    return {"rebooting": True}


async def save_undo(dev: dict[str, Any], action: str) -> str:
    """Write the state a real lockdown or baseline can change, before it runs."""
    serial = await require_device(dev)
    disabled = await shell(serial, "pm list packages -d")
    home = await shell(
        serial,
        "cmd package resolve-activity --brief -a android.intent.action.MAIN "
        "-c android.intent.category.HOME",
    )
    settings = {}
    for ns, key in UNDO_KEYS:
        settings[f"{ns}/{key}"] = (
            await shell(serial, f"settings get {ns} {key}")
        ).strip()
    utc = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    state = {
        "device": dev["name"],
        "serial": serial,
        "action": action,
        "utc": utc,
        "disabled_packages": sorted(
            ln.removeprefix("package:").strip()
            for ln in disabled.splitlines()
            if ln.startswith("package:")
        ),
        "home_activity": home.strip(),
        "settings": settings,
    }
    UNDO_DIR.mkdir(parents=True, exist_ok=True)
    path = UNDO_DIR / f"{dev['name']}-{utc}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(state, fh, indent=2)
    return str(path)
