"""Web panel on PANEL_PORT, behind a reverse proxy and an Authentik proxy provider.

Every route except /healthz needs X-TV-Proxy-Key == TV_PROXY_KEY (set only by the reverse proxy, so
the bind address cannot be called with forged X-authentik-* headers) and X-authentik-username.
tv-admin may do everything; tv-site-<site> members see and drive only that site's devices, and
only the USER_*_VERBS. Every POST writes one audit line to stdout.
POST routes also need X-TV-Panel: 1, a custom header a cross-site form cannot send without a
CORS preflight this app never answers. Real lockdown and baseline need a typed device-name confirm.
"""

import asyncio
import hashlib
import hmac
import html
import json
import logging
import mimetypes
import os
import re
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import scan
import tools
from inventory import DeviceError, load_denied_apps, load_devices
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route

CSRF_HEADER = "x-tv-panel"
USER_HEADER = "x-authentik-username"
GROUPS_HEADER = "x-authentik-groups"
PROXY_KEY_HEADER = "x-tv-proxy-key"
PROXY_KEY = os.environ.get("TV_PROXY_KEY", "")
PANEL_URL = os.environ.get("TV_PANEL_URL", "the panel URL")
ADMIN_GROUP = "tv-admin"
SITE_GROUP_PREFIX = "tv-site-"
USER_POST_VERBS = frozenset(
    {"key", "launch", "sleeptimer", "power", "wake", "input", "toast"}
)
USER_GET_VERBS = frozenset(
    {"status", "health", "apps", "inputs", "quickapps", "sleeptimer"}
)
RATE_LIMIT_S = 5.0
POWER_KEYS = frozenset({"SLEEP", "WAKEUP", "POWER"})
_last_power: dict[str, float] = {}

audit = logging.getLogger("tv-control.audit")
audit.addHandler(logging.StreamHandler(sys.stdout))
audit.setLevel(logging.INFO)
audit.propagate = False
BEST_EFFORT_BADGE = "TV power/volume/input: best-effort (no CEC)"

SLEEP_PRESETS = (15, 30, 60, 90)


def _user(request: Request) -> str | None:
    return request.headers.get(USER_HEADER) or None


def _groups(request: Request) -> set[str]:
    raw = request.headers.get(GROUPS_HEADER, "")
    return {g.strip() for g in re.split(r"[|,]", raw) if g.strip()}


def _is_admin(request: Request) -> bool:
    return ADMIN_GROUP in _groups(request)


def _sites(request: Request) -> set[str]:
    return {
        g.removeprefix(SITE_GROUP_PREFIX)
        for g in _groups(request)
        if g.startswith(SITE_GROUP_PREFIX)
    }


def _forbidden(cause: str) -> Response:
    return JSONResponse(
        {
            "error": "Not allowed",
            "cause": cause,
            "next_step": "Ask an admin to add you to the tv-admin or matching tv-site-<site> group.",
        },
        403,
    )


def _deny(request: Request, post: bool) -> Response | None:
    if not PROXY_KEY:
        return JSONResponse({"error": "TV_PROXY_KEY not set"}, 503)
    got = request.headers.get(PROXY_KEY_HEADER, "").encode()
    if not hmac.compare_digest(got, PROXY_KEY.encode()):
        return JSONResponse(
            {
                "error": f"request did not come through the reverse proxy; open {PANEL_URL}"
            },
            403,
        )
    if not _user(request):
        return JSONResponse(
            {"error": f"no Authentik user header; open the panel via {PANEL_URL}"},
            401,
        )
    if post and request.headers.get(CSRF_HEADER) != "1":
        return JSONResponse({"error": f"missing {CSRF_HEADER} header"}, 403)
    return None


async def _visible_devices(request: Request) -> list[dict[str, Any]]:
    devices = await load_devices()
    if _is_admin(request):
        return devices
    sites = _sites(request)
    return [d for d in devices if d.get("site") and d["site"] in sites]


async def _authorize(
    request: Request, what: str, name: str, post: bool
) -> Response | None:
    if _is_admin(request):
        return None
    if what not in (USER_POST_VERBS if post else USER_GET_VERBS):
        return _forbidden(f"{what} is limited to the {ADMIN_GROUP} group")
    if not any(d.get("name") == name for d in await _visible_devices(request)):
        return _forbidden(f"{name} is not at a site you can control")
    return None


def _rate_limited(what: str, name: str, data: dict[str, Any]) -> Response | None:
    key = str(data.get("key", "")).strip().upper().removeprefix("KEYCODE_")
    if what not in ("power", "wake", "input") and not (
        what == "key" and key in POWER_KEYS
    ):
        return None
    now = time.monotonic()
    wait = _last_power.get(name, -RATE_LIMIT_S) + RATE_LIMIT_S - now
    if wait > 0:
        return JSONResponse(
            {
                "error": "Too many power or input changes",
                "cause": f"{name} accepts one power or input change every {RATE_LIMIT_S:.0f} s",
                "next_step": "Wait a few seconds and try again.",
            },
            429,
            headers={"Retry-After": str(int(wait) + 1)},
        )
    _last_power[name] = now
    return None


async def _body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _friendly_name(dev: dict[str, Any]) -> str:
    if dev.get("label"):
        return str(dev["label"])
    words = dev["name"].replace("nvidia-", "").split("-")
    if "shield" in words:
        words.remove("shield")
        words.append("shield")
    special = {
        "lg": "LG",
        "tv": "TV",
        "firetv": "Fire TV",
        "firestick": "Fire TV",
    }
    return " ".join(special.get(word, word.capitalize()) for word in words)


def _card(dev: dict[str, Any], admin: bool = True) -> str:
    name = html.escape(dev["name"], quote=True)
    web = dev.get("type") == "webos"
    caps = dev.get("capabilities") or []
    keys = [
        ("DPAD_UP", "Up"),
        ("DPAD_LEFT", "Left"),
        ("DPAD_CENTER", "OK"),
        ("DPAD_RIGHT", "Right"),
        ("DPAD_DOWN", "Down"),
    ]
    dpad = "".join(
        f'<button data-key="{key}" aria-label="{label}" class="{label.lower()}">{label}</button>'
        for key, label in keys
    )
    keys = [
        ("BACK", "Back"),
        ("HOME", "Home"),
        ("MEDIA_PLAY_PAUSE", "Play / pause"),
        ("VOLUME_DOWN", "Volume −"),
        ("VOLUME_MUTE", "Mute"),
        ("VOLUME_UP", "Volume +"),
    ]
    controls = "".join(
        f'<button data-key="{key}">{label}</button>' for key, label in keys
    )
    presets = "".join(
        f'<button data-act="timer" data-min="{m}">{m} min</button>'
        for m in SLEEP_PRESETS
    )
    find = (
        '<button data-act="findremote">Find remote</button>'
        if "find_remote" in caps
        else ""
    )
    inputs = (
        '<div class="input-section"><p class="muted">TV input · <span class="current-input">Checking…</span></p><div class="inputs row" aria-label="TV inputs"></div><p class="inputnote muted"></p></div>'
        if "inputs" in caps or dev.get("inputs")
        else ""
    )
    screenshot = (
        ""
        if web or not admin
        else '<button data-act="shot">Take screenshot</button><figure class="shot" hidden><img alt="Current TV screen"><figcaption></figcaption></figure>'
    )
    preview = (
        '<div class="screen-info" aria-live="polite">Current screen · Checking…</div>'
        if web
        else '<figure class="preview"><button class="preview-open" aria-label="Open current screen full-size" disabled><img alt="Current TV screen" hidden></button><figcaption><span class="preview-age">Waiting for connection…</span><button class="preview-refresh">Refresh screen</button></figcaption></figure>'
        if admin
        else ""
    )
    setup = (
        '<button data-act="pair">Pair TV</button>'
        if web
        else '<button data-act="reboot">Reboot</button>'
    )
    if admin and dev.get("type") == "shield":
        setup += '<button data-act="remotepair">Pair fast keys</button>'
    note = (
        '<p class="muted">Volume needs HDMI-CEC on this TV; it may be unavailable.</p>'
        if dev.get("tv_control") == "best-effort"
        else ""
    )
    return f"""<section class="device" data-dev="{name}" data-type="{html.escape(dev.get("type", ""))}" hidden>
<div class="device-heading"><div><h1>{name}</h1><p class="connection">Checking connection…</p></div><span class="device-type">{html.escape(dev.get("type", ""))}</span></div>
{preview}
<p class="foreground">Looking for the current app…</p><p class="timer-status">Sleep timer · <span class="countdown" role="timer">Off</span></p>
<div class="row power"><button data-act="power-on">Wake</button><button data-act="power-off" class="danger">Power off</button>{find}</div>
{inputs}<div class="quick" aria-label="Quick apps"><p class="muted">Loading installed apps…</p></div>
<div class="remote" aria-label="Remote control"><div class="dpad">{dpad}</div><div class="controls">{controls}</div>{note}</div>
<details class="tools"><summary>Apps &amp; tools</summary>
<details><summary>All apps</summary><label>Search installed apps<input class="app-search" type="search" placeholder="App name or package"></label><div class="app-list"></div><p class="appnote muted"></p><button data-act="refresh">Refresh app list</button></details>
<details><summary>Sleep timer <span class="timer-detail">15 / 30 / 60 / 90 min</span></summary><div class="timer row">{presets}<button data-act="timer" data-min="0">Cancel timer</button></div></details>
<label>Message on TV<input class="toast" maxlength="200" placeholder="Write a short message"></label><button data-act="toast">Send message</button>
{screenshot}{setup}<details><summary>Connection details</summary><pre class="status">Checking…</pre></details><pre class="result" hidden></pre>
</details></section>"""


async def index(request: Request) -> Response:
    denied = _deny(request, post=False)
    if denied:
        return denied
    devices = [
        dev for dev in await _visible_devices(request) if dev.get("status") != "pending"
    ]
    choices = "".join(
        f'<button class="device-choice" data-online="unknown" data-target="{html.escape(d["name"], quote=True)}" aria-pressed="false"><strong>{html.escape(_friendly_name(d))}</strong><span>{html.escape(d.get("site") or "unassigned")} · Checking…</span></button>'
        for d in devices
    )
    body = (
        "".join(_card(d, _is_admin(request)) for d in devices)
        or '<p class="empty">No active devices in the inventory.</p>'
    )
    return HTMLResponse(
        PAGE.replace("{{BODY}}", body)
        .replace("{{CHOICES}}", choices)
        .replace("{{USER}}", html.escape(_user(request) or ""))
    )


async def _allowed_apps(body: dict[str, Any]) -> dict[str, Any]:
    denied = [d for d in await load_denied_apps() if d]
    return {
        **body,
        "apps": [
            app
            for app in body.get("apps", [])
            if not any(
                d
                in (
                    str(app.get("id", ""))
                    + str(app.get("title", ""))
                    + str(app.get("label", ""))
                ).lower()
                for d in denied
            )
        ],
    }


async def api_get(request: Request) -> Response:
    denied = _deny(request, post=False)
    if denied:
        return denied
    what, name = request.path_params["what"], request.path_params["name"]
    denied = await _authorize(request, what, name, post=False)
    if denied:
        return denied
    refresh = request.query_params.get("refresh") == "1"
    if what == "status":
        return JSONResponse(await tools.do_status(name))
    if what == "health":
        return JSONResponse(await tools.do_health(name))
    if what == "apps":
        return JSONResponse(
            await _allowed_apps(
                await tools.do_list_apps(name, refresh, background=True)
            )
        )
    if what == "inputs":
        return JSONResponse(await tools.do_list_inputs(name, refresh, background=True))
    if what == "quickapps":
        return JSONResponse(await _allowed_apps(await tools.do_quick_apps(name)))
    if what == "sleeptimer":
        return JSONResponse(await tools.do_sleep_timer(name))
    if what in {"preview", "screenshot"}:
        dev = next(
            (d for d in await _visible_devices(request) if d["name"] == name), None
        )
        if dev is None:
            return JSONResponse({"error": "Unknown device"}, 404)
        if dev.get("type") == "webos":
            return JSONResponse(
                {"error": "unsupported_on_device: no screenshot API"}, 400
            )
    if what == "preview":
        jpeg, note, captured_at = await tools.do_preview(name)
        if not jpeg:
            return JSONResponse(
                {"device": name, "error": note},
                502,
                headers={"Cache-Control": "no-store"},
            )
        return Response(
            jpeg,
            media_type="image/jpeg",
            headers={
                "X-Captured-At": str(captured_at),
                "Cache-Control": "no-store",
            },
        )
    if what == "screenshot":
        jpeg, note = await tools.do_screenshot(name)
        if not jpeg:
            return JSONResponse({"device": name, "error": note}, 502)
        return Response(
            jpeg,
            media_type="image/jpeg",
            headers={"X-Note": note or "", "Cache-Control": "no-store"},
        )
    return JSONResponse({"error": f"unknown endpoint {what}"}, 404)


async def api_post(request: Request) -> Response:
    what, name = request.path_params["what"], request.path_params["name"]
    response = await _api_post(request, what, name)
    result: int | str = response.status_code
    if response.status_code == 200 and b'"error"' in bytes(response.body):
        result = "200-error"
    audit.info(
        "audit user=%s device=%s verb=%s result=%s",
        json.dumps(_user(request)),
        json.dumps(name),
        json.dumps(what),
        result,
    )
    return response


async def _api_post(request: Request, what: str, name: str) -> Response:
    denied = _deny(request, post=True) or await _authorize(
        request, what, name, post=True
    )
    if denied:
        return denied
    data = await _body(request)
    if (what == "power" and data.get("state") == "off") or (
        what == "key"
        and str(data.get("key", "")).strip().upper().removeprefix("KEYCODE_") == "SLEEP"
    ):
        if data.get("confirm") != name:
            return JSONResponse(
                {
                    "error": "Power-off confirmation required",
                    "next_step": "Confirm power off for this device.",
                },
                400,
            )
    limited = _rate_limited(what, name, data)
    if limited:
        return limited
    if what == "wake":
        return JSONResponse(await tools.do_wake(name))
    if what == "key":
        return JSONResponse(await tools.do_key(name, str(data.get("key", ""))))
    if what == "launch":
        return JSONResponse(await tools.do_launch(name, str(data.get("app", ""))))
    if what == "toast":
        return JSONResponse(await tools.do_toast(name, str(data.get("message", ""))))
    if what == "sleeptimer":
        try:
            minutes = int(data.get("minutes", -1))
        except (TypeError, ValueError):
            minutes = -1
        return JSONResponse(await tools.do_sleep_timer(name, minutes))
    if what == "power":
        return JSONResponse(await tools.do_power(name, str(data.get("state", ""))))
    if what == "findremote":
        return JSONResponse(await tools.do_find_remote(name))
    if what == "input":
        return JSONResponse(await tools.do_set_input(name, str(data.get("input", ""))))
    if what == "remotepair":
        code = str(data.get("code", ""))
        return JSONResponse(await tools.do_remote_pair(name, code))
    if what == "reboot":
        return JSONResponse(await tools.do_reboot(name))
    if what == "pair":
        return JSONResponse(await tools.do_pair(name))
    if what in ("baseline", "lockdown"):
        real = bool(data.get("real"))
        if real and data.get("confirm") != name:
            return JSONResponse(
                {"device": name, "error": "confirm must equal the device name"}, 400
            )
        run = tools.do_baseline if what == "baseline" else tools.do_lockdown
        return JSONResponse(
            {**(await run(name, real)), "approved_by": _user(request) if real else None}
        )
    if what == "scan":
        return JSONResponse(await tools.do_scan(name))
    if what == "approve":
        try:
            return JSONResponse(await scan.stage_enroll({**data, "site": name}))
        except DeviceError as exc:
            return JSONResponse({"site": name, "error": str(exc)}, 400)
    return JSONResponse({"error": f"unknown endpoint {what}"}, 404)


STATIC = Path(__file__).parent / "static"
IMMUTABLE = "public, max-age=31536000, immutable"
EVENT_INTERVAL_S = 10


def _load_assets() -> tuple[dict[str, tuple[bytes, str]], dict[str, str]]:
    """Hash each asset into its file name so a year-long cache never serves stale code."""
    assets: dict[str, tuple[bytes, str]] = {}
    urls: dict[str, str] = {}
    for path in sorted(STATIC.glob("panel.*")) + sorted(STATIC.glob("icon-*.png")):
        if path.name == "panel.html":
            continue
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()[:10]
        hashed = f"{path.stem}.{digest}{path.suffix}"
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        assets[hashed] = (data, kind)
        urls[path.name] = f"/static/{hashed}"
    return assets, urls


ASSETS, ASSET_URLS = _load_assets()


def _fill(template: str) -> str:
    return (
        template.replace("{{CSS}}", ASSET_URLS["panel.css"])
        .replace("{{JS}}", ASSET_URLS["panel.js"])
        .replace("{{ICON192}}", ASSET_URLS["icon-192.png"])
        .replace("{{ICON512}}", ASSET_URLS["icon-512.png"])
    )


PAGE = _fill((STATIC / "panel.html").read_text())
MANIFEST = _fill((STATIC / "manifest.webmanifest").read_text())
JS = (STATIC / "panel.js").read_text()


async def static_asset(request: Request) -> Response:
    denied = _deny(request, post=False)
    if denied:
        return denied
    asset = ASSETS.get(request.path_params["asset"])
    if asset is None:
        return JSONResponse({"error": "unknown asset"}, 404)
    return Response(asset[0], media_type=asset[1], headers={"Cache-Control": IMMUTABLE})


async def manifest(request: Request) -> Response:
    denied = _deny(request, post=False)
    if denied:
        return denied
    return Response(
        MANIFEST,
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


async def _device_status(name: str) -> dict[str, Any]:
    try:
        return {"device": name, "status": await tools.do_status(name)}
    except Exception as exc:  # noqa: BLE001 - one failing device must not end the stream
        return {"device": name, "status": {"error": str(exc)[:200]}}


async def _status_stream(
    names: list[str], interval: float = EVENT_INTERVAL_S
) -> AsyncIterator[str]:
    last: dict[str, str] = {}
    while True:
        tasks = [asyncio.create_task(_device_status(n)) for n in names]
        try:
            for done in asyncio.as_completed(tasks):
                event = await done
                payload = json.dumps(event, sort_keys=True)
                if last.get(event["device"]) != payload:
                    last[event["device"]] = payload
                    yield f"event: status\ndata: {payload}\n\n"
        finally:
            for task in tasks:
                task.cancel()
        yield ": keepalive\n\n"
        await asyncio.sleep(interval)


async def events(request: Request) -> Response:
    denied = _deny(request, post=False)
    if denied:
        return denied
    devices = await _visible_devices(request)
    names = [d["name"] for d in devices if d.get("status") != "pending"]
    return StreamingResponse(
        _status_stream(names),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


ROUTES = [
    Route("/", index),
    Route("/events", events),
    Route("/manifest.webmanifest", manifest),
    Route("/static/{asset}", static_asset),
    Route("/api/{what}/{name}", api_get, methods=["GET"]),
    Route("/api/{what}/{name}", api_post, methods=["POST"]),
]


SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; "
        "script-src 'self'; style-src 'self'; manifest-src 'self'; "
        "img-src 'self' blob:; connect-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class SecurityHeaders:
    """Pure ASGI middleware: add SECURITY_HEADERS to every panel response."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).update(SECURITY_HEADERS)
            await send(message)

        await self.app(scope, receive, send_with_headers)
