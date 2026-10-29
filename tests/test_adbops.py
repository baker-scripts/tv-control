"""adbops against a scripted `adb`: every subprocess call is answered by FakeAdb, no device is contacted."""

import asyncio
import io
import json
from pathlib import Path
from typing import Any

import adbops
import appcache
import pytest
from inventory import DeviceError
from PIL import Image

SHIELD = {"name": "shield-a", "type": "shield", "ip": "192.0.2.1", "port": 5555}
FIRE = {
    "name": "fire-a",
    "type": "firetv",
    "ip": "192.0.2.3",
    "tailnet_fqdn": "fire-a.example.ts.net",
    "paired_tv": "lg-a",
}


class FakeAdb:
    """Answer by first matching substring of the joined argv; log every call."""

    def __init__(self, script: dict[str, tuple[int, bytes, str]]) -> None:
        self.script = script
        self.calls: list[str] = []

    async def __call__(self, *argv: str, **_: Any) -> tuple[int, bytes, str]:
        line = " ".join(argv)
        self.calls.append(line)
        for needle, reply in self.script.items():
            if needle in line:
                return reply
        return 0, b"", ""


def install(
    monkeypatch: pytest.MonkeyPatch, script: dict[str, tuple[int, bytes, str]]
) -> FakeAdb:
    fake = FakeAdb(script)
    monkeypatch.setattr(adbops, "run", fake)
    monkeypatch.setattr(adbops, "_active", {})
    return fake


def go(coro: Any) -> Any:
    return asyncio.run(coro)


def ok(text: str = "") -> tuple[int, bytes, str]:
    return 0, text.encode(), ""


def test_candidates_and_serial() -> None:
    assert adbops.candidates(FIRE) == ["192.0.2.3:5555", "fire-a.example.ts.net:5555"]
    assert adbops.serial_of(SHIELD) == "192.0.2.1:5555"


def test_adb_ready_shield_device(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(monkeypatch, {"get-state": ok("device\n")})
    assert go(adbops.adb_ready(SHIELD)) == ("192.0.2.1:5555", "device")
    assert not any("connect" in c for c in fake.calls)
    assert adbops.serial_of(SHIELD) == "192.0.2.1:5555"


def test_adb_ready_fire_reconnects_then_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(
        monkeypatch,
        {
            "-s 192.0.2.3:5555 get-state": (1, b"", "device unauthorized"),
            "-s fire-a": ok("device\n"),
        },
    )
    assert go(adbops.adb_ready(FIRE)) == ("fire-a.example.ts.net:5555", "device")


@pytest.mark.parametrize(
    "reply,state",
    [
        ((1, b"", "device unauthorized"), "unauthorized"),
        ((1, b"", "no devices"), "offline"),
    ],
)
def test_adb_ready_bad_states(
    monkeypatch: pytest.MonkeyPatch, reply: tuple[int, bytes, str], state: str
) -> None:
    install(monkeypatch, {"get-state": reply})
    assert go(adbops.adb_ready(SHIELD))[1] == state
    with pytest.raises(DeviceError, match=state):
        go(adbops.require_device(SHIELD))


def test_shell_and_read_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(monkeypatch, {"shell boom": (1, b"", "closed")})
    with pytest.raises(DeviceError, match="closed"):
        go(adbops.shell("s", "boom"))
    install(monkeypatch, {"shell grep": ok("x")})
    assert go(adbops.read("s", "grep a")) == "x"
    assert fake.calls


def test_status_and_health(monkeypatch: pytest.MonkeyPatch) -> None:
    install(
        monkeypatch,
        {
            "get-state": ok("device"),
            "mCurrentFocus": ok("mCurrentFocus=Window{x u0 pkg/act}\n"),
            "versionName": ok("    versionName=30.1\n"),
            "mWakefulness": ok("mWakefulness=Awake"),
            "PlaybackState": ok("state=PlaybackState {state=3, pos=1}"),
            "settings get": ok("off\n"),
        },
    )
    status = go(adbops.status(SHIELD))
    assert status["smarttube_version"] == "30.1" and "Window" in status["foreground"]
    health = go(adbops.health({**SHIELD, "paired_tv": "lg-a"}))
    assert health["awake"] is True and health["playing"] is True
    assert health["private_dns_mode"] == "off"


def test_status_offline_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, {"get-state": (1, b"", "offline")})
    assert go(adbops.status(SHIELD)) == {"adb_state": "offline"}
    assert go(adbops.health(SHIELD)) == {"adb_state": "offline"}


def png(color: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4000, 2000), (color, color, color)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.parametrize(
    "color,fire,note",
    [
        (255, False, None),
        (0, False, "all-black frame"),
        (0, True, "Fire OS blanks DRM"),
    ],
)
def test_shrink(color: int, fire: bool, note: str | None) -> None:
    jpeg, got = adbops.shrink(png(color), fire)
    assert max(Image.open(io.BytesIO(jpeg)).size) <= adbops.SCREENSHOT_MAX_PX
    assert (got is None) if note is None else (got is not None and note in got)


def test_screenshot(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, {"get-state": ok("device"), "screencap": (0, png(200), "")})
    jpeg, note = go(adbops.screenshot(SHIELD))
    assert jpeg.startswith(b"\xff\xd8") and note is None
    install(monkeypatch, {"get-state": ok("device"), "screencap": (0, b"nope", "")})
    with pytest.raises(DeviceError, match="screencap failed"):
        go(adbops.screenshot(SHIELD))


def test_send_key(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(monkeypatch, {"get-state": ok("device")})
    assert go(adbops.send_key(SHIELD, "HOME")) == {"sent": "HOME"}
    assert "input keyevent KEYCODE_HOME" in fake.calls[-1]
    install(monkeypatch, {"get-state": ok("device"), "keyevent": (1, b"", "bad")})
    with pytest.raises(DeviceError, match="keyevent failed"):
        go(adbops.send_key(SHIELD, "HOME"))


def test_launch_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = "org.example.app"
    install(
        monkeypatch, {"get-state": ok("device"), "monkey": ok("Events injected: 1")}
    )
    assert go(adbops.launch(SHIELD, pkg))["via"] == "monkey"
    install(
        monkeypatch,
        {
            "get-state": ok("device"),
            "monkey": ok("No activities found"),
            "resolve-activity": ok("priority=0\norg.example.app/org.example.Main\n"),
            "am start": ok("Starting"),
        },
    )
    assert (
        go(adbops.launch(SHIELD, pkg))["component"]
        == "org.example.app/org.example.Main"
    )
    install(
        monkeypatch,
        {
            "get-state": ok("device"),
            "monkey": ok("none"),
            "resolve-activity": ok("No activity found"),
        },
    )
    with pytest.raises(DeviceError, match="no launchable"):
        go(adbops.launch(SHIELD, pkg))
    install(
        monkeypatch,
        {
            "get-state": ok("device"),
            "monkey": ok("none"),
            "resolve-activity": ok("a.b/c.D"),
            "am start": ok("Error: boom"),
        },
    )
    with pytest.raises(DeviceError, match="am start failed"):
        go(adbops.launch(SHIELD, pkg))


def test_find_remote_and_reboot(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, {"get-state": ok("device"), "am start": ok("Starting")})
    assert go(adbops.find_remote(SHIELD)) == {"launched": adbops.REMOTE_LOCATOR}
    install(monkeypatch, {"get-state": ok("device"), "am start": ok("Error type 3")})
    with pytest.raises(DeviceError):
        go(adbops.find_remote(SHIELD))
    install(monkeypatch, {"get-state": ok("device")})
    assert go(adbops.reboot(SHIELD)) == {"rebooting": True}
    install(monkeypatch, {"get-state": ok("device"), "reboot": (1, b"", "denied")})
    with pytest.raises(DeviceError, match="reboot failed"):
        go(adbops.reboot(SHIELD))


def test_clean_toast_and_toast(monkeypatch: pytest.MonkeyPatch) -> None:
    assert adbops.clean_toast("hi <b>there</b>; `rm`") == "hi bthere/b; rm"
    for bad in ("", "<>", "x" * 201):
        with pytest.raises(DeviceError):
            adbops.clean_toast(bad)
    fake = install(monkeypatch, {"get-state": ok("device")})
    assert go(adbops.toast(SHIELD, "hello"))["toast"] == "hello"
    assert "cmd notification" in fake.calls[-1]


def test_app_label_and_list_apps(monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = "org.example.app"
    install(
        monkeypatch,
        {
            "get-state": ok("device"),
            "query-activities": ok("org.example.app/org.example.Main\n"),
            "pm path": ok("package:/data/app/x/base.apk\n"),
            "pull": (1, b"", "no"),
        },
    )
    assert go(adbops.app_label("s", pkg)) == appcache.app_title(pkg)
    assert go(adbops.app_label("s", "not a package")) == appcache.app_title(
        "not a package"
    )
    apps = go(adbops.list_apps(SHIELD))
    assert [a["id"] for a in apps] == [pkg]


def test_app_label_uses_apk_name(monkeypatch: pytest.MonkeyPatch) -> None:
    install(
        monkeypatch,
        {"pm path": ok("package:/data/app/x/base.apk\n"), "pull": ok("1 file pulled")},
    )
    monkeypatch.setattr(adbops, "apk_label", lambda _path: "Pretty Name")
    assert go(adbops.app_label("s", "org.example.app")) == appcache.app_title(
        "org.example.app", "Pretty Name"
    )


def test_save_undo_writes_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(adbops, "UNDO_DIR", tmp_path)
    install(
        monkeypatch,
        {
            "get-state": ok("device"),
            "pm list packages -d": ok("package:com.a.b\n"),
            "resolve-activity": ok("pkg/.Home\n"),
            "settings get": ok("1\n"),
        },
    )
    saved = json.loads(Path(go(adbops.save_undo(SHIELD, "test"))).read_text())
    assert (
        saved["disabled_packages"] == ["com.a.b"]
        and saved["home_activity"] == "pkg/.Home"
    )
    assert saved["settings"]["global/private_dns_mode"] == "1"
