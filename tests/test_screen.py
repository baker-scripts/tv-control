"""Screen-state reads (current_screen, what_is_on) against FakeAdb: no device is contacted."""

import asyncio
from typing import Any

import adbops
import pytest
import tools
from test_adbops import SHIELD, ok
from test_adbops import install as install_adb

RESUMED = "    mResumedActivity: ActivityRecord{e56 u0 com.plexapp.android/tv.plex.app.MainActivity t47}\n"
WINDOW = "  mCurrentFocus=Window{85a u0 com.plexapp.android/tv.plex.app.MainActivity}\n"
SESSIONS = (
    "      package=org.siloserver.silo\n"
    "      active=true\n"
    "      state=PlaybackState {state=0, position=0, speed=0.0, error=null}\n"
    "      metadata: null\n"
    "      package=com.plexapp.android\n"
    "      active=true\n"
    "      state=PlaybackState {state=3, position=1, speed=1.0, error=null}\n"
    "      metadata: size=9, description=The Matrix, 1999, null\n"
)
UI = (
    "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation=\"0\">"
    '<node class="android.widget.FrameLayout" package="com.plexapp.android" text="" '
    'resource-id="" content-desc="" focused="false" bounds="[0,0][1920,1080]">'
    '<node class="android.view.View" package="com.plexapp.android" text="Continue Watching" '
    'resource-id="com.plexapp.android:id/card" content-desc="The Matrix" focused="true" '
    'bounds="[100,200][400,500]"/></node></hierarchy>'
)


def screen_script(**over: tuple[int, bytes, str]) -> dict[str, tuple[int, bytes, str]]:
    return {
        "get-state": ok("device"),
        "mWakefulness": ok("  mWakefulness=Awake\n"),
        "ResumedActivity": ok(RESUMED),
        "mCurrentFocus": ok(WINDOW),
        "media_session": ok(SESSIONS),
        "uiautomator": ok(UI),
        **over,
    }


def go(coro: Any) -> Any:
    return asyncio.run(coro)


def test_parse_resumed_expands_short_activity() -> None:
    assert adbops.parse_resumed(RESUMED) == (
        "com.plexapp.android",
        "tv.plex.app.MainActivity",
    )
    short = "topResumedActivity=ActivityRecord{1 u0 com.spocky.projengmenu/.ui.Home t1}"
    assert adbops.parse_resumed(short) == (
        "com.spocky.projengmenu",
        "com.spocky.projengmenu.ui.Home",
    )
    assert adbops.parse_resumed("") == (None, None)


def test_parse_sessions_reads_state_and_title() -> None:
    sessions = adbops.parse_sessions(SESSIONS)
    assert [s["package"] for s in sessions] == [
        "org.siloserver.silo",
        "com.plexapp.android",
    ]
    assert sessions[0]["state"] == "none" and sessions[0]["title"] is None
    assert sessions[1]["state"] == "playing" and sessions[1]["title"] == "The Matrix"
    assert sessions[1]["description"] == "The Matrix, 1999, null"


def test_parse_focus_returns_the_focused_node() -> None:
    focus = adbops.parse_focus(UI)
    assert focus == {
        "package": "com.plexapp.android",
        "class": "android.view.View",
        "text": "Continue Watching",
        "content_desc": "The Matrix",
        "resource_id": "com.plexapp.android:id/card",
        "bounds": [100, 200, 400, 500],
    }
    assert adbops.parse_focus("not xml") is None
    assert adbops.parse_focus(UI.replace('focused="true"', 'focused="false"')) is None


def test_current_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install_adb(monkeypatch, screen_script())
    screen = go(adbops.current_screen(SHIELD))
    assert screen["package"] == "com.plexapp.android"
    assert screen["activity"] == "tv.plex.app.MainActivity"
    assert screen["awake"] is True and screen["playing"] is True
    assert screen["focus"]["bounds"] == [100, 200, 400, 500]
    assert screen["sessions"][1]["title"] == "The Matrix"
    assert not any(" input " in c or "am start" in c for c in fake.calls)


def test_current_screen_without_focus_skips_uiautomator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_adb(monkeypatch, screen_script())
    screen = go(adbops.current_screen(SHIELD, focus=False))
    assert "focus" not in screen
    assert not any("uiautomator" in c for c in fake.calls)


def test_current_screen_reports_a_failed_ui_dump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_adb(
        monkeypatch, screen_script(uiautomator=ok("ERROR: could not get idle state"))
    )
    screen = go(adbops.current_screen(SHIELD))
    assert screen["focus"] is None and "uiautomator" in screen["focus_note"]


def test_what_is_on_prefers_the_foreground_app_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_adb(monkeypatch, screen_script())
    on = go(adbops.what_is_on(SHIELD))
    assert on["app"] == "com.plexapp.android"
    assert on["state"] == "playing" and on["playing"] is True
    assert on["title"] == "The Matrix"


def test_what_is_on_paused_and_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    paused = SESSIONS.replace("state=3", "state=2")
    install_adb(monkeypatch, screen_script(media_session=ok(paused)))
    on = go(adbops.what_is_on(SHIELD))
    assert on["state"] == "paused" and on["playing"] is False
    install_adb(monkeypatch, screen_script(media_session=ok("")))
    on = go(adbops.what_is_on(SHIELD))
    assert on["state"] == "none" and on["title"] is None


def test_tools_state_verbs_refuse_webos(monkeypatch: pytest.MonkeyPatch) -> None:
    async def get_device(name: str) -> dict[str, Any]:
        return {"name": name, "type": "webos", "ip": "192.0.2.2"}

    monkeypatch.setattr(tools, "get_device", get_device)
    assert "unsupported" in go(tools.do_current_screen("lg-a"))["error"]
    assert "unsupported" in go(tools.do_what_is_on("lg-a"))["error"]
