"""ADB calls for Shields and Fire OS devices, on the shared local ADB server.

No function here runs the adb disconnect command: it would drop the device for androidtv-watcher and the scripts.
"""

import asyncio
import gzip
import html
import io
import json
import os
import re
import struct
import tempfile
import time
import zlib
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

from PIL import Image as PILImage

import appcache
import inputs
from inventory import DeviceError, run

ADB_TIMEOUT_S = 20
# A device that answered get-state "device" is trusted this long; the next command skips the probe.
READY_TTL_S = 15.0
SCREENCAP_TIMEOUT_S = 90
SCREENSHOT_MAX_PX = 1280
# screenrecord frames mode scales one RGB frame on the GPU, with no PNG encode. Its header is five
# little-endian uint32 values: packet length, width, height, stride, pixel format (3 = RGB_888).
FRAME_W, FRAME_H = 1280, 720
FRAME_PIXEL_BYTES = FRAME_W * FRAME_H * 3
FRAME_HEADER = struct.pack(
    "<5I", 16 + FRAME_PIXEL_BYTES, FRAME_W, FRAME_H, FRAME_W * 3, 3
)
# adbd forwards a pipe in small packets, one per network round trip (140 KB/s over the subnet route).
# `cat` of a file sends large packets (1 MB/s), so each capture goes to a file first.
FAST_CAPTURE = (
    "f=/data/local/tmp/tv-control-$$.gz;"
    f"(timeout -s INT 3 screenrecord --size {FRAME_W}x{FRAME_H} --output-format=frames - 2>/dev/null </dev/null &)"
    f"|head -c {len(FRAME_HEADER) + FRAME_PIXEL_BYTES}|gzip -1 >$f;cat $f;rm -f $f"
)
PNG_CAPTURE = "f=/data/local/tmp/tv-control-$$.png;screencap -p $f;cat $f;rm -f $f"
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
    ("secure", "always_on_vpn_app"),
    ("secure", "always_on_vpn_lockdown"),
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
# Connection pool: device name -> (serial, monotonic time of the last confirmed "device" state).
_ready: dict[str, tuple[str, float]] = {}
# When a serial last answered "device"; for PATH_MEMORY_S it is tried first, so a dead LAN
# address does not cost a timeout on every probe.
_path_at: dict[str, float] = {}
PATH_MEMORY_S = 600.0


def candidates(dev: dict[str, Any]) -> list[str]:
    """ADB serials to try: the path that last worked (if recent), then ip, tailnet_ip, MagicDNS name."""
    port = dev.get("port") or 5555
    keys = ("ip", "tailnet_ip", "tailnet_fqdn")
    hosts = dict.fromkeys(str(dev[k]) for k in keys if dev.get(k))
    serials = [f"{h}:{port}" for h in hosts]
    last = _active.get(dev["name"])
    fresh = time.monotonic() - _path_at.get(dev["name"], float("-inf")) < PATH_MEMORY_S
    if last in serials and fresh:
        serials.remove(last)
        serials.insert(0, last)
    return serials


def serial_of(dev: dict[str, Any]) -> str:
    return _active.get(dev["name"]) or candidates(dev)[0]


def forget_ready(serial: str) -> None:
    """Drop pooled readiness for a serial so the next command probes and reconnects."""
    for name in [n for n, (s, _) in _ready.items() if s == serial]:
        del _ready[name]


async def adb(
    serial: str, *args: str, timeout: float = ADB_TIMEOUT_S
) -> tuple[int, bytes, str]:
    try:
        reply = await run("adb", "-s", serial, *args, timeout=timeout)
    except DeviceError:
        forget_ready(serial)
        raise
    if reply[0] != 0:
        forget_ready(serial)
    return reply


async def adb_ready(dev: dict[str, Any], fresh: bool = False) -> tuple[str, str]:
    """Connect if needed and return (serial, state), trying each candidate address.

    A pooled serial confirmed within READY_TTL_S is returned without a probe unless fresh=True.
    """
    pooled = _ready.get(dev["name"])
    if not fresh and pooled and time.monotonic() - pooled[1] < READY_TTL_S:
        return pooled[0], "device"
    best: tuple[str, str] = (candidates(dev)[0], "offline")
    for serial in candidates(dev):
        serial, state = await _ready_one(dev, serial)
        if state == "device":
            _active[dev["name"]] = serial
            now = time.monotonic()
            _ready[dev["name"]] = (serial, now)
            _path_at[dev["name"]] = now
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
        raise DeviceError(
            f"adb state is {state!r}, not device; tried {', '.join(candidates(dev))}"
        )
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
    serial, state = await adb_ready(dev, fresh=True)
    body: dict[str, Any] = {"adb_state": state}
    if state != "device":
        return body
    focus = await read(serial, "dumpsys window | grep mCurrentFocus")
    body["foreground"] = focus.strip().splitlines()[0] if focus.strip() else None
    pkg = await read(serial, f"dumpsys package {SMARTTUBE_PKG} | grep versionName")
    match = re.search(r"versionName=(\S+)", pkg)
    body["smarttube_version"] = match.group(1) if match else None
    body["ip_alert"] = await ip_alert(dev, serial)
    try:
        body["playback"] = playback_state(
            parse_sessions(await read(serial, SESSIONS_READ))
        )
    except DeviceError:
        body["playback"] = None
    return body


LAN_READ = "ip -4 -o addr show"
INET_RE = re.compile(r"\binet (\d{1,3}(?:\.\d{1,3}){3})/")
ADDR_TTL_S = 120.0
_addrs: dict[str, tuple[float, list[str]]] = {}


def lan_addresses(text: str) -> list[str]:
    """IPv4 addresses the device holds, less loopback and the tailnet range."""
    found = []
    for addr in INET_RE.findall(text):
        a, b = (int(x) for x in addr.split(".")[:2])
        if a == 127 or (a == 100 and 64 <= b <= 127):
            continue
        found.append(addr)
    return found


async def ip_alert(dev: dict[str, Any], serial: str) -> str | None:
    """Why devices.yml ip is stale: the device holds other LAN addresses, or only a fallback path answered."""
    configured = str(dev.get("ip") or "")
    if not configured:
        return None
    cached = _addrs.get(dev["name"])
    if cached is None or time.monotonic() - cached[0] > ADDR_TTL_S:
        try:
            held = lan_addresses(await read(serial, LAN_READ))
        except DeviceError:
            held = []
        cached = _addrs[dev["name"]] = (time.monotonic(), held)
    held = cached[1]
    if held and configured not in held:
        return f"answered at {', '.join(held)}; devices.yml says {configured}"
    host = serial.rsplit(":", 1)[0]
    if host != configured:
        return f"LAN address {configured} is not answering; reached via {host}"
    return None


PASSTHROUGH_PKG = "com.amazon.tv.inputpreference.service"
HDMI_READ = "dumpsys hdmi_control | grep -E 'mActiveRoutingPath|display_name'"
ROUTING_RE = re.compile(r"mActiveRoutingPath:\s*0x([0-9a-fA-F]{4})")
CEC_DEVICE_RE = re.compile(
    r"display_name:\s*(.*?)\s+power_status:.*?physical_address:\s*0x([0-9a-fA-F]{4})"
)


def parse_hdmi(text: str) -> dict[str, Any]:
    """HDMI port and the CEC device name on it, from `dumpsys hdmi_control` lines (Fire OS TVs)."""
    routing = ROUTING_RE.search(text)
    path = routing.group(1) if routing else "0000"
    port = int(path[0], 16) or None
    named = [
        (addr, name) for name, addr in CEC_DEVICE_RE.findall(text) if addr != "0000"
    ]
    exact = [n for a, n in named if a == path]
    same_port = [n for a, n in named if port and int(a[0], 16) == port]
    return {"hdmi_port": port, "hdmi_device": (exact or same_port or [None])[0]}


async def hdmi_input(serial: str) -> dict[str, Any]:
    return parse_hdmi(await read(serial, HDMI_READ))


HEALTH_READS = {
    "private_dns_mode": "settings get global private_dns_mode",
    "private_dns_specifier": "settings get global private_dns_specifier",
    "always_on_vpn_app": "settings get secure always_on_vpn_app",
    "accessibility_services": "settings get secure enabled_accessibility_services",
    "resumed_activity": "dumpsys activity activities | grep -m1 -E 'mResumedActivity|topResumedActivity'",
}


PLAYBACK_STATE_RE = re.compile(r"PlaybackState \{state=(\d+)(?=[,\s}])")


def is_awake(power_dump: str) -> bool | None:
    states = re.findall(r"^\s*mWakefulness=(\S+)\s*$", power_dump, re.MULTILINE)
    if len(states) != 1:
        return None
    if states[0] == "Awake":
        return True
    if states[0] in {"Asleep", "Dreaming", "Dozing"}:
        return False
    return None


def is_playing(session_dump: str) -> bool | None:
    states = [int(value) for value in PLAYBACK_STATE_RE.findall(session_dump)]
    if not states or len(states) != session_dump.count("PlaybackState"):
        return None
    # Android treats buffering, connecting, seeking, and skipping as active playback.
    if any(state in {3, 4, 5, 6, 8, 9, 10, 11} for state in states):
        return True
    if all(state in {0, 1, 2} for state in states):
        return False
    return None


# android.media.session.PlaybackState constants.
PLAYBACK_STATE_NAMES = {
    0: "none",
    1: "stopped",
    2: "paused",
    3: "playing",
    4: "fast_forwarding",
    5: "rewinding",
    6: "buffering",
    7: "error",
    8: "connecting",
    9: "skipping_to_previous",
    10: "skipping_to_next",
    11: "skipping_to_queue_item",
}
RESUMED_RE = re.compile(
    r"(?:mResumedActivity|topResumedActivity)[=:]\s*ActivityRecord\{\S+ u\d+ ([A-Za-z0-9_.]+)/([A-Za-z0-9_.$]+)"
)
NODE_TAG_RE = re.compile(r"<node\s([^>]*)>")
NODE_ATTR_RE = re.compile(r'([A-Za-z-]+)="([^"]*)"')
BOUNDS_RE = re.compile(r"^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$")
RESUMED_READ = (
    "dumpsys activity activities | grep -m1 -E 'mResumedActivity|topResumedActivity'"
)
SESSIONS_READ = "dumpsys media_session | grep -E '^      (package=|active=|state=PlaybackState|metadata:)'"
UI_DUMP = (
    "f=/data/local/tmp/tv-control-ui-$$.xml;uiautomator dump $f >/dev/null 2>&1;"
    "cat $f 2>/dev/null;rm -f $f"
)


def parse_resumed(text: str) -> tuple[str | None, str | None]:
    match = RESUMED_RE.search(text)
    if not match:
        return None, None
    pkg, activity = match.groups()
    return pkg, pkg + activity if activity.startswith(".") else activity


def parse_sessions(dump: str) -> list[dict[str, Any]]:
    """Media sessions in dumpsys order: package, active, state name, and metadata title."""
    sessions: list[dict[str, Any]] = []
    for line in dump.splitlines():
        text = line.strip()
        if text.startswith("package="):
            sessions.append(
                {
                    "package": text.removeprefix("package="),
                    "active": None,
                    "state": None,
                    "title": None,
                    "description": None,
                }
            )
        elif not sessions:
            continue
        elif text.startswith("active="):
            sessions[-1]["active"] = text == "active=true"
        elif text.startswith("state=PlaybackState"):
            match = PLAYBACK_STATE_RE.search(text)
            if match:
                code = int(match.group(1))
                sessions[-1]["state"] = PLAYBACK_STATE_NAMES.get(code, str(code))
        elif text.startswith("metadata:"):
            match = re.search(r"description=(.*)$", text)
            if match:
                desc = match.group(1)
                sessions[-1]["description"] = desc
                first = desc.split(", ")[0]
                sessions[-1]["title"] = None if first == "null" else first
    return sessions


def parse_focus(dump: str) -> dict[str, Any] | None:
    """The last node in document order with focused="true" in a uiautomator dump, or None.

    Tags are read with a regex, not an XML parser: the dump is one flat attribute format and
    device output stays untrusted.
    """
    nodes = [
        {k: html.unescape(v) for k, v in NODE_ATTR_RE.findall(tag)}
        for tag in NODE_TAG_RE.findall(dump)
    ]
    focused = [n for n in nodes if n.get("focused") == "true"]
    if not focused:
        return None
    node = focused[-1]
    match = BOUNDS_RE.match(node.get("bounds", ""))
    return {
        "package": node.get("package"),
        "class": node.get("class"),
        "text": node.get("text"),
        "content_desc": node.get("content-desc"),
        "resource_id": node.get("resource-id"),
        "bounds": [int(v) for v in match.groups()] if match else None,
    }


ACTIVE_STATES = frozenset(PLAYBACK_STATE_NAMES[c] for c in (3, 4, 5, 6, 8, 9, 10, 11))


async def current_screen(dev: dict[str, Any], focus: bool = True) -> dict[str, Any]:
    """Read-only screen state: wakefulness, resumed app, focus window, media sessions, focused view."""
    serial = await require_device(dev)
    power = await read(serial, "dumpsys power | grep mWakefulness=")
    match = re.search(r"mWakefulness=(\S+)", power)
    pkg, activity = parse_resumed(await read(serial, RESUMED_READ))
    window = (await read(serial, "dumpsys window | grep mCurrentFocus")).strip()
    sessions = parse_sessions(await read(serial, SESSIONS_READ))
    body: dict[str, Any] = {
        "awake": is_awake(power),
        "wakefulness": match.group(1) if match else None,
        "package": pkg,
        "activity": activity,
        "window": window.splitlines()[0] if window else None,
        "playing": any(s["state"] in ACTIVE_STATES for s in sessions),
        "sessions": sessions,
    }
    if focus:
        body["focus"] = parse_focus(await read(serial, UI_DUMP))
        if body["focus"] is None:
            body["focus_note"] = (
                "uiautomator found no focused view (dump failed, video surface, or nothing focused)"
            )
    return body


def session_for(
    sessions: list[dict[str, Any]], pkg: str | None
) -> dict[str, Any] | None:
    """The foreground app's session, else the first active one that plays."""
    own = [s for s in sessions if s["package"] == pkg]
    if own:
        return own[0]
    return next((s for s in sessions if s["state"] in ACTIVE_STATES), None)


def playback_state(sessions: list[dict[str, Any]]) -> str:
    """Panel chip: playing, buffering, paused, or idle across all media sessions."""
    names = {s.get("state") for s in sessions}
    if names & (ACTIVE_STATES - {"buffering", "connecting"}):
        return "playing"
    if names & {"buffering", "connecting"}:
        return "buffering"
    return "paused" if "paused" in names else "idle"


async def what_is_on(dev: dict[str, Any]) -> dict[str, Any]:
    screen = await current_screen(dev, focus=False)
    session = session_for(screen["sessions"], screen["package"])
    state = (session or {}).get("state") or "none"
    return {
        "app": screen["package"],
        "awake": screen["awake"],
        "state": state,
        "playing": state in ACTIVE_STATES,
        "title": (session or {}).get("title"),
        "description": (session or {}).get("description"),
        "session_app": (session or {}).get("package"),
    }


async def health(dev: dict[str, Any]) -> dict[str, Any]:
    body = await status(dev)
    if body["adb_state"] != "device":
        return body
    serial = serial_of(dev)
    body["via"] = serial
    for field, cmd in HEALTH_READS.items():
        body[field] = (await read(serial, cmd)).strip() or None
    if dev.get("paired_tv") or dev.get("type") == "shield":
        body["awake"] = is_awake(await shell(serial, "dumpsys power"))
        body["playing"] = is_playing(await shell(serial, "dumpsys media_session"))
    return body


async def screenshot(dev: dict[str, Any]) -> tuple[bytes, str | None]:
    serial = await require_device(dev)
    if dev["type"] == "shield":
        fast = await _fast_capture(serial)
        if fast:
            return fast, None
    rc, png, err = await adb(
        serial, "exec-out", PNG_CAPTURE, timeout=SCREENCAP_TIMEOUT_S
    )
    if rc != 0 or not png.startswith(b"\x89PNG"):
        raise DeviceError(f"screencap failed: {err or 'no PNG data'}")
    return await asyncio.to_thread(shrink, png, dev["type"] in FIRE_TYPES)


async def _fast_capture(serial: str) -> bytes | None:
    """One scaled frame as a JPEG. None means the caller must use screencap."""
    try:
        rc, data, _ = await adb(serial, "exec-out", FAST_CAPTURE)
    except DeviceError:
        return None
    return await asyncio.to_thread(frame_to_jpeg, data) if rc == 0 else None


def frame_to_jpeg(data: bytes) -> bytes | None:
    """None for a bad frame or a black one, which screencap must confirm."""
    try:
        raw = gzip.decompress(data)
    except (OSError, EOFError, zlib.error):
        return None
    if len(raw) != len(FRAME_HEADER) + FRAME_PIXEL_BYTES or not raw.startswith(
        FRAME_HEADER
    ):
        return None
    rgb = PILImage.frombytes("RGB", (FRAME_W, FRAME_H), raw[len(FRAME_HEADER) :])
    return None if _is_black(rgb) else _jpeg(rgb)


def _is_black(rgb: PILImage.Image) -> bool:
    return (
        max(hi for _, hi in cast(tuple[tuple[int, int], ...], rgb.getextrema()))
        < BLACK_MAX_LEVEL
    )


def _jpeg(rgb: PILImage.Image) -> bytes:
    rgb.thumbnail((SCREENSHOT_MAX_PX, SCREENSHOT_MAX_PX))
    buf = io.BytesIO()
    rgb.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def shrink(png: bytes, fire: bool) -> tuple[bytes, str | None]:
    """Downscale to a JPEG MCP clients accept, and flag an all-black frame."""
    with PILImage.open(io.BytesIO(png)) as img:
        rgb = img.convert("RGB")
    black = _is_black(rgb)
    jpeg = _jpeg(rgb)
    note = None
    if black:
        note = "all-black frame" + (
            ": Fire OS blanks DRM content (Prime Video, Netflix) in screencap"
            if fire
            else ""
        )
    return jpeg, note


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


TEXT_SAFE_RE = re.compile(r"[A-Za-z0-9 .,_@+=/:-]{1,200}")


async def send_text(dev: dict[str, Any], text: str) -> dict[str, Any]:
    """Type into the focused field with `input text`; the safe set avoids shell metacharacters."""
    if not TEXT_SAFE_RE.fullmatch(text):
        raise DeviceError(
            "text must be 1-200 characters of letters, digits, spaces, and . , _ @ + = / : -"
        )
    serial = await require_device(dev)
    await shell(serial, f"input text '{text.replace(' ', '%s')}'")
    return {"typed": len(text), "via": "adb input text"}


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


VIEW_URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[A-Za-z0-9/:.?=&_%-]+$")


async def view_uri(dev: dict[str, Any], uri: str, pkg: str) -> dict[str, Any]:
    """Start a VIEW intent for a deep link, limited to one package. The URI is quoted for the device shell."""
    if not VIEW_URI_RE.match(uri):
        raise DeviceError(f"invalid deep link {uri!r}")
    serial = await require_device(dev)
    rc, out, err = await adb(
        serial,
        "shell",
        f"am start -a android.intent.action.VIEW -d '{uri}' -p {check_pkg(pkg)}",
    )
    text = out.decode(errors="replace").strip()
    if rc != 0 or "Error" in text:
        raise DeviceError(f"am start failed: {err or text}"[:300])
    return {"started": uri, "package": pkg}


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


async def list_inputs(dev: dict[str, Any]) -> list[dict[str, Any]]:
    """Read-only: the HDMI inputs the device's TV input service lists (none on a streaming device)."""
    serial = await require_device(dev)
    return inputs.parse_android(await read(serial, inputs.ANDROID_READ))


INPUT_KEY_RE = re.compile(r"^TV_INPUT_[A-Z0-9_]+$")


async def set_input_key(dev: dict[str, Any], key: str) -> dict[str, Any]:
    """Switch a Fire TV input with a TV_INPUT_* keyevent. Never wakes the TV."""
    key = key.removeprefix("KEYCODE_")
    if not INPUT_KEY_RE.match(key):
        raise DeviceError(f"invalid input key {key!r}")
    serial = await require_device(dev)
    if not is_awake(await read(serial, "dumpsys power")):
        raise DeviceError("TV is asleep; input not switched (set_input does not wake)")
    rc, _, err = await adb(serial, "shell", "input", "keyevent", f"KEYCODE_{key}")
    if rc != 0:
        raise DeviceError(f"keyevent failed: {err}")
    return {"input_set": key, "via": "keyevent"}


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


# Android TV shows no heads-up notifications, so `cmd notification post` lands only in the
# notification list (seen 2026-11-22 on a Shield). TvOverlay draws over the current app.
# Source: https://github.com/gugutab/TvOverlay (REST API: POST /notify on port 5001).
OVERLAY_PKG = "com.tabdeveloper.tvoverlay"
OVERLAY_PORT = 5001
OVERLAY_SECONDS = 8
OVERLAY_SETTLE_S = 1.0
OVERLAY_INSTALL = (
    f"Install TvOverlay ({OVERLAY_PKG}) from the Play Store, run "
    f"`appops set {OVERLAY_PKG} SYSTEM_ALERT_WINDOW allow` and "
    f"`dumpsys deviceidle whitelist +{OVERLAY_PKG}`, then open the app once"
)
WINDOW_READ = "dumpsys window windows | grep -E '^  Window #|package=|mViewVisibility=|mHasSurface='"


def visible_windows(dump: str, pkg: str) -> int:
    """Count windows owned by pkg that are visible and have a surface."""
    count = 0
    for block in re.split(r"^  Window #", dump, flags=re.MULTILINE)[1:]:
        if (
            re.search(rf"\bpackage={re.escape(pkg)}(\s|$)", block)
            and "mViewVisibility=0x0 " in block
            and "mHasSurface=true " in block
        ):
            count += 1
    return count


def overlay_payload(
    text: str, title: str, seconds: int, corner: str = "", image: str = ""
) -> dict[str, Any]:
    """TvOverlay /notify body; title, message, duration, corner, and image are fields its README documents."""
    body: dict[str, Any] = (
        {"title": title, "message": text, "duration": seconds}
        if title
        else {"title": text, "duration": seconds}
    )
    if corner:
        body["corner"] = corner
    if image:
        body["image"] = image
    return body


async def device_minutes(dev: dict[str, Any]) -> int | None:
    """Minutes since midnight on the device's own clock, or None when it cannot be read."""
    serial = await require_device(dev)
    match = re.search(r"(\d{1,2}):(\d{2})", await read(serial, "date +%H:%M"))
    return int(match.group(1)) * 60 + int(match.group(2)) if match else None


def overlay_http_post(port: int, path: str, body: dict[str, Any]) -> int:
    """POST JSON to the adb-forwarded TvOverlay port on the adb server host; return the status."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except OSError as exc:
        raise DeviceError(f"not_shown: TvOverlay did not answer: {exc}") from None


async def _overlay_forward(serial: str, remote: int) -> int:
    """Reuse this serial's adb forward to the TvOverlay port, or open one (no ACL change needed)."""
    rc, out, err = await adb(serial, "forward", "--list")
    if rc == 0:
        for line in out.decode(errors="replace").splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[0] == serial and parts[2] == f"tcp:{remote}":
                return int(parts[1].removeprefix("tcp:"))
    rc, out, err = await adb(serial, "forward", "tcp:0", f"tcp:{remote}")
    text = out.decode(errors="replace").strip()
    if rc != 0 or not text.isdigit():
        raise DeviceError(f"not_shown: adb forward failed: {err or text}"[:300])
    return int(text)


async def toast(
    dev: dict[str, Any],
    message: str,
    title: str = "",
    seconds: int = OVERLAY_SECONDS,
    corner: str = "",
    image: str = "",
) -> dict[str, Any]:
    """Draw a message over the current app with TvOverlay; raise unless a visible overlay window is confirmed."""
    text = clean_toast(message)
    head = clean_toast(title) if title else ""
    serial = await require_device(dev)
    if "package:" not in await read(serial, f"pm path {OVERLAY_PKG}"):
        raise DeviceError(f"overlay_app_missing: {OVERLAY_INSTALL}")
    appop = await read(serial, f"appops get {OVERLAY_PKG} SYSTEM_ALERT_WINDOW")
    if "allow" not in appop:
        raise DeviceError(
            f"overlay_not_permitted: TvOverlay may not draw over apps; {OVERLAY_INSTALL}"
        )
    if not is_awake(await read(serial, "dumpsys power | grep mWakefulness=")):
        raise DeviceError(
            "screen_not_on: the device is asleep or showing the screen saver; message not shown"
        )
    port = await _overlay_forward(serial, int(dev.get("overlay_port") or OVERLAY_PORT))
    status = await asyncio.to_thread(
        overlay_http_post,
        port,
        "/notify",
        overlay_payload(text, head, seconds, corner, image),
    )
    if status != 200:
        raise DeviceError(f"not_shown: TvOverlay answered HTTP {status}")
    await asyncio.sleep(OVERLAY_SETTLE_S)
    if not visible_windows(await read(serial, WINDOW_READ), OVERLAY_PKG):
        raise DeviceError(
            "not_shown: TvOverlay accepted the message but no visible overlay window followed"
        )
    return {
        "toast": text,
        "shown": True,
        "via": "tvoverlay",
        "seconds": seconds,
        "check": "HTTP 200 and a visible TvOverlay window",
    }


def apk_label(path: str) -> str | None:
    from apkutils import APK

    with APK.from_file(path) as apk:
        label: str | None = apk.parse_resource().app_name
        return label


async def read_label(serial: str, pkg: str, pull_timeout: float = 90) -> str | None:
    """The installed APK's own label, or None. Android 11 shell has no label read: query-activities and dumpsys give only a resource id, so the base APK is pulled (read-only; nothing is installed or sent to the TV)."""
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
            return None
        with tempfile.TemporaryDirectory(prefix="tv-app-label-") as directory:
            local = str(Path(directory) / "base.apk")
            rc, _, _ = await adb(serial, "pull", path, local, timeout=pull_timeout)
            if rc == 0:
                return await asyncio.to_thread(apk_label, local)
    except Exception:  # noqa: BLE001 - unreadable APKs or unsupported resource tables mean no label
        return None
    return None


# A Fire OS TV pulls one launcher APK in about 20 s over Wi-Fi ADB (some take over 60 s), so labels
# are cached per device and no new pull starts after the budget; the next call finishes the rest.
LIST_APPS_BUDGET_S = 20
LABEL_JOB_PULL_S = 45


async def list_apps(
    dev: dict[str, Any], budget_s: float = LIST_APPS_BUDGET_S
) -> tuple[list[dict[str, str]], bool]:
    """(apps, partial). partial is True when the budget ran out before every label was read; the rest show their curated name or package id until a later call."""
    serial = await require_device(dev)
    pkgs: set[str] = set()
    for category in ("LEANBACK_LAUNCHER", "LAUNCHER"):
        text = await shell(
            serial,
            "cmd package query-activities --components -a android.intent.action.MAIN "
            f"-c android.intent.category.{category}",
        )
        pkgs |= {ln.split("/")[0].strip() for ln in text.splitlines() if "/" in ln}
    titles: dict[str, str | None] = {}
    partial = False
    deadline = time.monotonic() + budget_s
    for pkg in sorted(p for p in pkgs if PKG_RE.match(p)):
        known, label = appcache.cached_label(dev["name"], pkg)
        if not known and time.monotonic() < deadline:
            label = await read_label(serial, pkg, pull_timeout=LABEL_JOB_PULL_S)
            appcache.put_label(dev["name"], pkg, label)
        elif not known:
            partial = True
        titles[pkg] = label
    apps = [{"id": p, "title": appcache.app_title(p, t)} for p, t in titles.items()]
    return appcache.friendly_apps(apps), partial


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
