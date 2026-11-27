"""Browser overflow test: long device names and alert text must not push content off screen.

Run with `pytest -m e2e --no-cov`. No device is contacted: every tools.do_* verb is replaced.
"""

import os
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import uvicorn
from playwright.sync_api import Browser, Page, sync_playwright
from starlette.applications import Starlette
from starlette.middleware import Middleware

import panel

pytestmark = pytest.mark.e2e

PROXY_KEY = "e2e-overflow-key"
HEADERS = {
    "x-authentik-username": "a-very-long-signed-in-user-name-for-the-header-check",
    "x-authentik-groups": "tv-admin",
    "x-tv-proxy-key": PROXY_KEY,
    "x-tv-panel": "1",
}
LONG = "Living-Room-Basement-Theater-Annex-Shield-TV-Pro-Extra-Long-Name"
DEVICES = [
    {
        "name": LONG,
        "type": "shield",
        "site": "home",
        "ip": "192.0.2.1",
        "capabilities": ["find_remote", "power", "sleep_timer", "inputs"],
    },
    {
        "name": "lg-a",
        "type": "webos",
        "site": "home",
        "ip": "192.0.2.2",
        "capabilities": ["wake", "power", "inputs"],
    },
]
SIZES = {"phone": (390, 844), "desktop": (1280, 800)}
FIND = """() => {
  const vw = document.documentElement.clientWidth, out = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height || el.closest('[hidden],dialog:not([open])')) continue;
    if (getComputedStyle(el).position === 'fixed') continue;
    const chip = el.closest('.device-choice');
    if (chip) {
      const c = chip.getBoundingClientRect();
      if (el !== chip && (r.right > c.right + 1 || r.left < c.left - 1 || el.scrollWidth > el.clientWidth + 1))
        out.push('chip content ' + el.tagName + ' ' + el.textContent.slice(0, 30));
      continue;
    }
    if (el.closest('#device-choices')) continue;
    if (r.right > vw + 1 || r.left < -1)
      out.push(el.tagName + '.' + el.className + ' ' + Math.round(r.left) + '..' + Math.round(r.right));
  }
  return out;
}"""


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    mp = pytest.MonkeyPatch()
    mp.setattr(panel, "PROXY_KEY", PROXY_KEY)
    mp.setattr(panel, "load_devices", AsyncMock(return_value=DEVICES))

    async def status(name: str) -> dict[str, Any]:
        return {
            "adb_state": "device",
            "foreground": "home",
            "ip_alert": "answered at 192.0.2.40-with-a-long-hostname-example-internal-name; devices.yml says 192.0.2.1",
        }

    mp.setattr(panel.tools, "do_status", status)
    ok: Any = AsyncMock(return_value={"adb_state": "device", "foreground": "home"})
    for verb in (
        "do_health",
        "do_list_apps",
        "do_quick_apps",
        "do_list_inputs",
        "do_sleep_timer",
    ):
        mp.setattr(panel.tools, verb, ok)
    mp.setattr(
        panel.tools,
        "do_favorites",
        AsyncMock(return_value={"device": LONG, "favorites": []}),
    )
    mp.setattr(
        panel.tools,
        "do_what_is_on",
        AsyncMock(
            return_value={
                "app": "com.example.verylongpackagename.with.many.segments.here",
                "app_title": "An Extremely Long Application Title Without Any Break",
                "awake": True,
                "state": "playing",
                "title": "A-Single-Unbroken-Media-Title-That-Keeps-Going-And-Going-Forever",
            }
        ),
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


def _open(browser: Browser, server: str, size: tuple[int, int]) -> Page:
    context = browser.new_context(
        viewport={"width": size[0], "height": size[1]}, extra_http_headers=HEADERS
    )
    page = context.new_page()
    page.goto(f"{server}/#device={LONG}")
    page.wait_for_selector(".device")
    page.locator(".ip-alert").first.wait_for(state="visible")
    page.wait_for_timeout(600)
    return page


@pytest.mark.parametrize("size", SIZES.values(), ids=SIZES.keys())
def test_long_names_do_not_overflow(
    server: str, browser: Browser, size: tuple[int, int]
) -> None:
    page = _open(browser, server, size)
    shots = os.environ.get("UI_SHOTS")
    if shots:
        page.screenshot(path=f"{shots}/{size[0]}.png", full_page=True)
    scroll = page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    offenders = page.evaluate(FIND)
    assert scroll <= 0, f"page scrolls sideways by {scroll}px: {offenders}"
    assert not offenders, offenders
    page.context.close()
