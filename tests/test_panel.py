"""Panel contract tests. All device actions are replaced before requests run."""

import asyncio
import base64
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import app
import httpx2
import panel
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request

PROXY_KEY = "test-proxy-key"
DEVICES = [
    {"name": "mock-tv", "type": "firetv", "site": "home"},
    {"name": "far-tv", "type": "firetv", "site": "cabin"},
    {"name": "nosite-tv", "type": "firetv"},
]


@pytest.fixture(autouse=True)
def _panel_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(panel, "PROXY_KEY", PROXY_KEY)
    monkeypatch.setattr(panel, "load_devices", AsyncMock(return_value=DEVICES))
    monkeypatch.setattr(panel, "_last_power", {})


def headers(
    groups: str = "tv-admin", key: str | None = PROXY_KEY
) -> list[tuple[bytes, bytes]]:
    out = [
        (b"x-authentik-username", b"tester"),
        (b"x-tv-panel", b"1"),
        (b"x-authentik-groups", groups.encode()),
    ]
    return out + ([(b"x-tv-proxy-key", key.encode())] if key is not None else [])


def call(
    what: str,
    body: dict[str, Any] | None = None,
    *,
    method: str = "POST",
    name: str = "mock-tv",
    hdrs: list[tuple[bytes, bytes]] | None = None,
) -> Any:
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": json.dumps(body or {}).encode()}

    request = Request(
        {
            "type": "http",
            "method": method,
            "query_string": b"",
            "headers": headers() if hdrs is None else hdrs,
            "path_params": {"what": what, "name": name},
        },
        receive,
    )
    return asyncio.run(
        panel.api_get(request) if method == "GET" else panel.api_post(request)
    )


@pytest.mark.parametrize(
    "what,body",
    [
        ("power", {"state": "off"}),
        ("key", {"key": "SLEEP"}),
        ("key", {"key": " KEYCODE_SLEEP "}),
    ],
)
def test_power_off_requires_device_confirmation(
    monkeypatch: pytest.MonkeyPatch, what: str, body: dict[str, Any]
) -> None:
    action = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(panel.tools, "do_power", action)
    monkeypatch.setattr(panel.tools, "do_key", action)
    assert call(what, body).status_code == 400
    action.assert_not_called()
    assert call(what, {**body, "confirm": "mock-tv"}).status_code == 200
    action.assert_awaited_once()


def test_wake_route_calls_wol_verb(monkeypatch: pytest.MonkeyPatch) -> None:
    action = AsyncMock(
        return_value={"error": "Relay unavailable", "next_step": "Configure relay"}
    )
    monkeypatch.setattr(panel.tools, "do_wake", action)
    assert json.loads(call("wake").body)["next_step"] == "Configure relay"
    action.assert_awaited_once_with("mock-tv")


@pytest.mark.parametrize(
    "endpoint,verb", [("apps", "do_list_apps"), ("quickapps", "do_quick_apps")]
)
def test_netflix_never_reaches_button_data(
    monkeypatch: pytest.MonkeyPatch, endpoint: str, verb: str
) -> None:
    action = AsyncMock(
        return_value={
            "apps": [
                {"id": "safe.id", "title": "Plex"},
                {"id": "com.netflix.ninja", "title": "TV"},
                {"id": "other", "title": "NETFLIX"},
                {"id": "label", "label": "Netflix"},
            ]
        }
    )
    monkeypatch.setattr(panel.tools, verb, action)
    monkeypatch.setattr(panel, "load_denied_apps", AsyncMock(return_value=["netflix"]))
    apps = json.loads(call(endpoint, method="GET").body)["apps"]
    assert apps == [{"id": "safe.id", "title": "Plex"}]


def test_active_inventory_has_one_screen_per_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        panel,
        "load_devices",
        AsyncMock(
            return_value=[
                {"name": "fire", "type": "firetv"},
                {"name": "lg", "type": "webos"},
                {"name": "future", "status": "pending"},
            ]
        ),
    )
    request = Request({"type": "http", "headers": headers()})
    page = asyncio.run(panel.index(request)).body.decode()
    assert 'data-dev="fire"' in page and 'data-dev="lg"' in page
    assert 'data-dev="future"' not in page
    assert 'id="power-confirm"' in page
    assert "if(off&&!await confirmOff(card))return" in panel.JS
    assert '<button data-act="power-on">Wake</button>' in page
    assert 'class="app-search"' in page


def test_input_chips_are_visible_and_setup_buttons_removed() -> None:
    card = panel._card(
        {"name": "fire", "type": "firetv", "inputs": {"hdmi1": "Shield"}}
    )
    assert card.index('class="inputs row"') < card.index('<details class="tools">')
    assert "<select" not in card
    assert "baseline" not in card and "lockdown" not in card
    assert "baseline-dry" not in panel.JS and "lockdown-real" not in panel.JS
    assert "b.title=a.id" in panel.JS
    assert "a.title+' '+a.id" in panel.JS
    assert "const app=j.app_title" in panel.JS
    assert "JSON.stringify(j,null,2)" not in panel.JS


@pytest.fixture
def actions(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    mocks = {}
    for verb in (
        "do_key",
        "do_power",
        "do_wake",
        "do_set_input",
        "do_reboot",
        "do_status",
    ):
        mocks[verb] = AsyncMock(return_value={"ok": True})
        monkeypatch.setattr(panel.tools, verb, mocks[verb])
    return mocks


def test_missing_or_wrong_proxy_key_is_refused(actions: dict[str, AsyncMock]) -> None:
    assert call("key", {"key": "HOME"}, hdrs=headers(key=None)).status_code == 403
    assert call("key", {"key": "HOME"}, hdrs=headers(key="forged")).status_code == 403
    assert call("status", method="GET", hdrs=headers(key="")).status_code == 403
    actions["do_key"].assert_not_called()
    actions["do_status"].assert_not_called()


def test_unset_proxy_key_fails_closed(
    monkeypatch: pytest.MonkeyPatch, actions: dict[str, AsyncMock]
) -> None:
    monkeypatch.setattr(panel, "PROXY_KEY", "")
    assert call("key", {"key": "HOME"}, hdrs=headers(key="")).status_code == 503
    actions["do_key"].assert_not_called()


@pytest.mark.parametrize(
    "groups", ["tv-site-home", "users|tv-site-home", "x, tv-site-home"]
)
def test_site_member_drives_own_site(
    actions: dict[str, AsyncMock], groups: str
) -> None:
    assert call("key", {"key": "HOME"}, hdrs=headers(groups)).status_code == 200
    assert call("status", method="GET", hdrs=headers(groups)).status_code == 200


@pytest.mark.parametrize("name", ["far-tv", "nosite-tv", "unknown-tv"])
@pytest.mark.parametrize("what,method", [("key", "POST"), ("status", "GET")])
def test_site_member_blocked_on_other_devices(
    actions: dict[str, AsyncMock], name: str, what: str, method: str
) -> None:
    resp = call(
        what, {"key": "HOME"}, method=method, name=name, hdrs=headers("tv-site-home")
    )
    assert resp.status_code == 403
    assert set(json.loads(resp.body)) == {"error", "cause", "next_step"}
    actions["do_key"].assert_not_called()
    actions["do_status"].assert_not_called()


@pytest.mark.parametrize(
    "what,method",
    [
        ("reboot", "POST"),
        ("pair", "POST"),
        ("remotepair", "POST"),
        ("baseline", "POST"),
        ("lockdown", "POST"),
        ("scan", "POST"),
        ("approve", "POST"),
        ("screenshot", "GET"),
    ],
)
def test_admin_only_verbs(
    actions: dict[str, AsyncMock], what: str, method: str
) -> None:
    resp = call(what, method=method, hdrs=headers("tv-site-home"))
    assert resp.status_code == 403
    assert json.loads(resp.body)["cause"]
    actions["do_reboot"].assert_not_called()


def test_admin_reaches_any_device(actions: dict[str, AsyncMock]) -> None:
    assert call("reboot", name="far-tv").status_code == 200
    assert (
        call("reboot", name="nosite-tv", hdrs=headers("a|tv-admin")).status_code == 200
    )


def test_index_lists_only_visible_devices() -> None:
    request = Request({"type": "http", "headers": headers("tv-site-home")})
    page = asyncio.run(panel.index(request)).body.decode()
    assert 'data-dev="mock-tv"' in page
    assert 'data-dev="far-tv"' not in page and 'data-dev="nosite-tv"' not in page


def test_post_writes_one_audit_line(
    actions: dict[str, AsyncMock], caplog: pytest.LogCaptureFixture
) -> None:
    panel.audit.addHandler(caplog.handler)
    try:
        call("key", {"key": "HOME"})
        call("reboot", hdrs=headers("tv-site-home"))
    finally:
        panel.audit.removeHandler(caplog.handler)
    assert [r.getMessage() for r in caplog.records] == [
        'audit user="tester" device="mock-tv" verb="key" result=200',
        'audit user="tester" device="mock-tv" verb="reboot" result=403',
    ]
    assert any(
        isinstance(h, logging.StreamHandler) and h.stream is sys.stdout
        for h in panel.audit.handlers
    )


@pytest.mark.parametrize(
    "what,body",
    [
        ("power", {"state": "on"}),
        ("wake", {}),
        ("input", {"input": "hdmi1"}),
        ("key", {"key": "WAKEUP"}),
    ],
)
def test_power_and_input_rate_limited_per_device(
    monkeypatch: pytest.MonkeyPatch,
    actions: dict[str, AsyncMock],
    what: str,
    body: dict[str, Any],
) -> None:
    now = [1000.0]
    monkeypatch.setattr(panel.time, "monotonic", lambda: now[0])
    assert call(what, body).status_code == 200
    limited = call(what, body)
    assert limited.status_code == 429 and limited.headers["retry-after"]
    assert call(what, body, name="far-tv").status_code == 200
    now[0] += 5
    assert call(what, body).status_code == 200


def test_plain_keys_not_rate_limited(actions: dict[str, AsyncMock]) -> None:
    assert all(call("key", {"key": "DPAD_UP"}).status_code == 200 for _ in range(3))


def _asgi_get(path: str, **kw: Any) -> Any:
    web = Starlette(routes=panel.ROUTES, middleware=[Middleware(panel.SecurityHeaders)])

    async def fetch() -> Any:
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=web), base_url="http://test"
        ) as client:
            return await client.get(path, **kw)

    return asyncio.run(fetch())


def test_security_headers_csp_and_hashed_assets() -> None:
    hdrs = {k.decode(): v.decode() for k, v in headers()}
    resp = _asgi_get("/", headers=hdrs)
    assert resp.status_code == 200
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"
    csp = resp.headers["content-security-policy"]
    assert "script-src 'self'; style-src 'self'" in csp
    assert "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
    assert "<script>" not in resp.text and "<style>" not in resp.text
    js_url = panel.ASSET_URLS["panel.js"]
    assert js_url in resp.text
    assert re.fullmatch(r"/static/panel\.[0-9a-f]{10}\.js", js_url)
    asset = _asgi_get(js_url, headers=hdrs)
    assert asset.status_code == 200 and asset.text == panel.JS
    assert "immutable" in asset.headers["cache-control"]
    assert "max-age=31536000" in asset.headers["cache-control"]
    assert _asgi_get("/static/panel.js", headers=hdrs).status_code == 404
    assert _asgi_get(js_url).status_code == 403
    icon = _asgi_get(panel.ASSET_URLS["icon-512.png"], headers=hdrs)
    assert icon.content.startswith(b"\x89PNG")
    manifest = _asgi_get("/manifest.webmanifest", headers=hdrs)
    assert manifest.headers["cache-control"] == "no-cache"
    body = manifest.json()
    assert body["display"] == "standalone"
    assert {i["src"] for i in body["icons"]} <= set(panel.ASSET_URLS.values())


def test_event_stream_pushes_changes_only(monkeypatch: pytest.MonkeyPatch) -> None:
    states = iter(
        [{"adb_state": "device"}, {"adb_state": "device"}, {"adb_state": "offline"}]
    )
    monkeypatch.setattr(
        panel.tools, "do_status", AsyncMock(side_effect=lambda n: next(states))
    )

    async def collect() -> list[str]:
        out: list[str] = []
        stream = panel._status_stream(["tv"], interval=0)
        async for chunk in stream:
            out.append(chunk)
            if len(out) == 5:
                break
        await stream.aclose()
        return out

    chunks = asyncio.run(collect())
    events = [c for c in chunks if c.startswith("event: status")]
    assert len(events) == 2
    assert '"adb_state": "device"' in events[0]
    assert '"adb_state": "offline"' in events[1]
    assert chunks[1] == ": keepalive\n\n"


def test_event_endpoint_needs_proxy_key_and_filters_sites() -> None:
    assert _asgi_get("/events").status_code == 403

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b""}

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "query_string": b"",
            "headers": headers(groups="tv-site-home"),
        },
        receive,
    )
    resp = asyncio.run(panel.events(request))
    assert resp.media_type == "text/event-stream"
    assert resp.headers["x-accel-buffering"] == "no"
    assert panel._status_stream.__name__ == "_status_stream"
    assert asyncio.run(panel._visible_devices(request)) == [DEVICES[0]]


@pytest.mark.parametrize("device", ["../../etc/x", "a/b", ".hidden", "", "ok-tv_1"])
def test_save_image_stays_in_screenshot_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, device: str
) -> None:
    monkeypatch.setattr(app, "SCREENSHOT_DIR", tmp_path)
    app._save_image(device, base64.b64encode(b"jpeg").decode())
    files = list(tmp_path.iterdir())
    assert len(files) == 1 and files[0].parent == tmp_path
    assert not files[0].name.startswith(".")


@pytest.mark.parametrize("what", ["preview", "screenshot"])
def test_screen_endpoints_admin_only_and_never_call_webos(
    monkeypatch: pytest.MonkeyPatch, what: str
) -> None:
    action = AsyncMock()
    monkeypatch.setattr(panel.tools, "do_preview", action)
    monkeypatch.setattr(panel.tools, "do_screenshot", action)
    assert call(what, method="GET", hdrs=headers("tv-site-home")).status_code == 403
    monkeypatch.setattr(
        panel,
        "load_devices",
        AsyncMock(return_value=[{"name": "mock-tv", "type": "webos"}]),
    )
    assert call(what, method="GET").status_code == 400
    action.assert_not_called()


def test_preview_response_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    action = AsyncMock(return_value=(b"jpeg", None, 1234.5))
    monkeypatch.setattr(panel.tools, "do_preview", action)
    response = call("preview", method="GET")
    assert response.status_code == 200 and response.body == b"jpeg"
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-captured-at"] == "1234.5"
    action.return_value = (None, "offline", None)
    assert call("preview", method="GET").status_code == 502


def test_preview_markup_and_friendly_labels() -> None:
    dev = {"name": "lg-nvidia-shield-living-room", "type": "shield"}
    assert panel._friendly_name(dev) == "LG Living Room Shield"
    assert panel._friendly_name({"name": "cabin-tv"}) == "Cabin TV"
    assert panel._friendly_name({"name": "x", "label": "Living room"}) == "Living room"
    admin = panel._card(dev, True)
    assert admin.index('class="preview"') < admin.index('class="remote"')
    assert "Open live view" not in admin and "live view" not in admin.lower()
    assert 'class="preview"' not in panel._card(dev, False)
    assert 'data-act="shot"' not in panel._card(dev, False)
    webos = panel._card({"name": "lg", "type": "webos"}, False)
    assert 'class="screen-info"' in webos and 'class="preview"' not in webos
    assert 'id="device-choices" hidden' not in panel.PAGE


def test_initial_device_selection_runs_javascript() -> None:
    import subprocess

    function = panel.JS.split("function initialDevice", 1)[1].split(
        "\nfunction choose", 1
    )[0]
    code = (
        "const assert=require('node:assert/strict'); function initialDevice"
        + function
        + """
const names=['first','living room','third'];
assert.equal(initialDevice(names,'#device=living%20room','third'),'living room');
assert.equal(initialDevice(names,'#device=missing','third'),'third');
assert.equal(initialDevice(names,'#device=%zz','third'),'third');
assert.equal(initialDevice(names,'','missing'),'first');
assert.equal(initialDevice([], '', null),undefined);
"""
    )
    subprocess.run(["node", "-e", code], check=True, capture_output=True)


def test_preview_visibility_and_refresh_schedule_runs_javascript() -> None:
    import subprocess

    functions = panel.JS.split("function previewVisible", 1)[1].split(
        "\nfunction previewAges", 1
    )[0]
    code = (
        "const assert=require('node:assert/strict'); function previewVisible"
        + functions
        + """
const button={},age={},box={getBoundingClientRect:()=>({top:10,bottom:200}),querySelector:s=>s==='.preview-refresh'?button:age};
const card={hidden:false,dataset:{dev:'mock-tv'},querySelector:()=>box};
let selected=card,document={hidden:false},navigator={onLine:true},window={innerHeight:800},calls=0,now=10000;
const st={online:true,previewBusy:false,previewAttempt:0},state=new Map([[card,st]]);
Date.now=()=>now;
const fetch=async()=>{calls++;return {ok:false,json:async()=>({error:'offline'})};};
(async()=>{
assert.equal(previewVisible(card),true);
for(const [target,key,value] of [[card,'hidden',true],[document,'hidden',true],[navigator,'onLine',false],[st,'online',false]]){
const old=target[key];target[key]=value;assert.equal(previewVisible(card),false);await refreshPreview(card);target[key]=old;
}
selected=null;assert.equal(previewVisible(card),false);await refreshPreview(card);selected=card;
box.getBoundingClientRect=()=>({top:900,bottom:1100});assert.equal(previewVisible(card),false);await refreshPreview(card);
box.getBoundingClientRect=()=>({top:10,bottom:200});
assert.equal(calls,0);
await refreshPreview(card);assert.equal(calls,1);
now+=4999;await refreshPreview(card);assert.equal(calls,1);
now+=1;await refreshPreview(card);assert.equal(calls,2);
st.previewBusy=true;now+=5000;await refreshPreview(card);assert.equal(calls,2);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    )
    subprocess.run(["node", "-e", code], check=True, capture_output=True)
