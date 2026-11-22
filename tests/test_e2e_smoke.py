"""Browser smoke test: the panel at phone and desktop widths against a mocked backend.

Run with `pytest -m e2e --no-cov` (needs `playwright install chromium`). No device is contacted:
every tools.do_* verb the page calls is replaced.
"""

import socket
import threading
import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import uvicorn
from playwright.sync_api import Browser, sync_playwright
from starlette.applications import Starlette
from starlette.middleware import Middleware

import panel

pytestmark = pytest.mark.e2e

PROXY_KEY = "e2e-proxy-key"
HEADERS = {
    "x-authentik-username": "tester",
    "x-authentik-groups": "tv-admin",
    "x-tv-proxy-key": PROXY_KEY,
    "x-tv-panel": "1",
}
DEVICES = [
    {
        "name": "shield-a",
        "type": "shield",
        "site": "home",
        "ip": "192.0.2.1",
        "capabilities": ["find_remote", "power", "sleep_timer"],
    },
    {
        "name": "lg-a",
        "type": "webos",
        "site": "home",
        "ip": "192.0.2.2",
        "capabilities": ["wake", "power", "inputs"],
    },
]
VIEWPORTS = {"phone": (375, 812), "desktop": (1280, 800)}


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    mp = pytest.MonkeyPatch()
    mp.setattr(panel, "PROXY_KEY", PROXY_KEY)
    mp.setattr(panel, "load_devices", AsyncMock(return_value=DEVICES))
    ok: Any = AsyncMock(return_value={"adb_state": "device", "foreground": "home"})
    for verb in (
        "do_status",
        "do_health",
        "do_list_apps",
        "do_quick_apps",
        "do_list_inputs",
        "do_sleep_timer",
    ):
        mp.setattr(panel.tools, verb, ok)
    now = {"app": "com.plexapp.android", "app_title": "Plex", "awake": True}
    mp.setattr(
        panel.tools,
        "do_what_is_on",
        AsyncMock(return_value={**now, "state": "playing", "title": "The Matrix"}),
    )
    mp.setattr(
        panel.tools,
        "in_use_refusal",
        AsyncMock(return_value={"error": "in_use: someone may be watching"}),
    )
    mp.setattr(
        panel.tools,
        "do_go_home",
        AsyncMock(return_value={"confirmed": True, "package": "home"}),
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    web = Starlette(routes=panel.ROUTES, middleware=[Middleware(panel.SecurityHeaders)])
    srv = uvicorn.Server(
        uvicorn.Config(web, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    while not srv.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(5)
    mp.undo()


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as pw:
        chromium = pw.chromium.launch()
        yield chromium
        chromium.close()


@pytest.mark.parametrize("size", VIEWPORTS.values(), ids=VIEWPORTS.keys())
def test_panel_has_no_netflix_and_no_horizontal_overflow(
    server: str, browser: Browser, size: tuple[int, int]
) -> None:
    context = browser.new_context(
        viewport={"width": size[0], "height": size[1]}, extra_http_headers=HEADERS
    )
    page = context.new_page()
    page.goto(server + "/")
    page.wait_for_selector(".device")
    page.wait_for_timeout(500)
    assert page.locator("button, a, summary", has_text="netflix").count() == 0
    assert "netflix" not in page.inner_text("body").lower()
    overflow = page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 0, f"horizontal overflow of {overflow}px at {size[0]}px"
    context.close()


def test_now_on_in_use_notice_and_build_footer(server: str, browser: Browser) -> None:
    context = browser.new_context(
        viewport={"width": 375, "height": 812}, extra_http_headers=HEADERS
    )
    page = context.new_page()
    page.goto(server + "/#device=shield-a")
    card = page.locator('.device[data-dev="shield-a"]')
    card.locator(".foreground", has_text="Plex · Playing · The Matrix").wait_for()
    card.locator('[data-act="home"]').click()
    notice = card.locator(".notice")
    notice.wait_for(state="visible")
    assert "Someone may be watching this TV." in notice.inner_text()
    notice.locator(".anyway").click()
    page.locator("#snackbar", has_text="The home screen is showing.").wait_for()
    assert "built" in page.inner_text("#build")
    context.close()
