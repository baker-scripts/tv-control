"""On-screen messages through TvOverlay, against FakeAdb and a fake HTTP post: no device is contacted."""

import asyncio
import json
from typing import Any

import adbops
import pytest
from inventory import DeviceError
from test_adbops import SHIELD, FakeAdb, ok

PKG = adbops.OVERLAY_PKG


def window(pkg: str, visible: bool = True) -> str:
    return (
        f"  Window #1 Window{{abc u0 {pkg}}}:\n"
        f"    mOwnerUid=10100 showForAllUsers=false package={pkg} appop=SYSTEM_ALERT_WINDOW\n"
        f"    mViewVisibility={'0x0' if visible else '0x8'} mHaveFrame=true mObscured=false\n"
        f"    mHasSurface={'true' if visible else 'false'} isReadyForDisplay()=true mWindowRemovalAllowed=false\n"
    )


def install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    installed: bool = True,
    appop: str = "SYSTEM_ALERT_WINDOW: allow; time=+1d",
    awake: str = "mWakefulness=Awake",
    after: str = "",
    status: int = 200,
) -> tuple[FakeAdb, list[tuple[int, str, dict[str, Any]]]]:
    fake = FakeAdb(
        {
            "get-state": ok("device"),
            "pm path": ok(f"package:/data/app/{PKG}/base.apk\n" if installed else ""),
            "appops get": ok(appop),
            "mWakefulness": ok(awake),
            "dumpsys window windows": ok(after or window(PKG)),
            "forward --list": ok(""),
            "forward tcp:0": ok("41234\n"),
        }
    )
    posts: list[tuple[int, str, dict[str, Any]]] = []

    def post(port: int, path: str, body: dict[str, Any]) -> int:
        posts.append((port, path, body))
        return status

    monkeypatch.setattr(adbops, "run", fake)
    monkeypatch.setattr(adbops, "_active", {})
    monkeypatch.setattr(adbops, "_ready", {})
    monkeypatch.setattr(adbops, "overlay_http_post", post)
    monkeypatch.setattr(adbops, "OVERLAY_SETTLE_S", 0)
    return fake, posts


def go(coro: Any) -> Any:
    return asyncio.run(coro)


def test_toast_posts_to_tvoverlay_and_confirms_a_visible_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, posts = install(monkeypatch)
    body = go(adbops.toast(SHIELD, "Hi, this is TV Helper."))
    assert body["shown"] is True and body["via"] == "tvoverlay"
    assert body["toast"] == "Hi, this is TV Helper."
    port, path, sent = posts[0]
    assert (port, path) == (41234, "/notify")
    assert sent["title"] == "Hi, this is TV Helper." and sent["duration"] > 0
    assert not any("cmd notification" in c for c in fake.calls)
    assert any(f"forward tcp:0 tcp:{adbops.OVERLAY_PORT}" in c for c in fake.calls)


def test_toast_reuses_an_existing_forward(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, posts = install(monkeypatch)
    fake.script["forward --list"] = ok(
        f"192.0.2.1:5555 tcp:39000 tcp:{adbops.OVERLAY_PORT}\nother:5555 tcp:1 tcp:5001\n"
    )
    go(adbops.toast(SHIELD, "hello"))
    assert posts[0][0] == 39000
    assert not any("forward tcp:0" in c for c in fake.calls)


def test_toast_title_and_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _, posts = install(monkeypatch)
    go(adbops.toast(SHIELD, "Opening Plex", title="TV Helper"))
    assert (
        posts[0][2]["title"] == "TV Helper" and posts[0][2]["message"] == "Opening Plex"
    )


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"installed": False}, "overlay_app_missing"),
        ({"appop": "SYSTEM_ALERT_WINDOW: default"}, "overlay_not_permitted"),
        ({"awake": "mWakefulness=Dreaming"}, "screen_not_on"),
        ({"status": 500}, "not_shown"),
        ({"after": window(PKG, visible=False)}, "not_shown"),
        ({"after": window("other.pkg")}, "not_shown"),
    ],
)
def test_toast_errors_when_display_is_not_confirmed(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any], error: str
) -> None:
    install(monkeypatch, **kwargs)
    with pytest.raises(DeviceError, match=error):
        go(adbops.toast(SHIELD, "hello"))


def test_toast_never_falls_back_to_cmd_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, _ = install(monkeypatch, installed=False)
    with pytest.raises(DeviceError):
        go(adbops.toast(SHIELD, "hello"))
    assert not any("cmd notification" in c for c in fake.calls)


def test_visible_overlay_windows_parser() -> None:
    dump = window(PKG) + window(PKG, visible=False) + window("com.plexapp.android")
    assert adbops.visible_windows(dump, PKG) == 1
    assert adbops.visible_windows("", PKG) == 0


def test_toast_sends_the_documented_corner_and_image_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, posts = install(monkeypatch)
    go(
        adbops.toast(
            SHIELD,
            "Your movie is ready.",
            title="TV Helper",
            seconds=7,
            corner="top_end",
            image="https://img.example/poster.jpg",
        )
    )
    assert posts[0][2] == {
        "title": "TV Helper",
        "message": "Your movie is ready.",
        "duration": 7,
        "corner": "top_end",
        "image": "https://img.example/poster.jpg",
    }


def test_device_clock_is_read_from_the_device(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _ = install(monkeypatch)
    fake.script["date +%H:%M"] = ok("23:15\n")
    assert go(adbops.device_minutes(SHIELD)) == 23 * 60 + 15
    fake.script["date +%H:%M"] = ok("")
    assert go(adbops.device_minutes(SHIELD)) is None


def test_overlay_payload_is_json_safe() -> None:
    body = adbops.overlay_payload("a, b", "", 5)
    assert json.loads(json.dumps(body)) == body
    assert body == {"title": "a, b", "duration": 5}
