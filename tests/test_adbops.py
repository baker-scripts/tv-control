"""adbops against a scripted `adb`: every subprocess call is answered by FakeAdb, no device is contacted."""

import asyncio
import gzip
import io
import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

import adbops
import appcache
from inventory import DeviceError

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
    monkeypatch.setattr(adbops, "_ready", {})
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


BEDROOM = {
    "name": "bed-a",
    "type": "shield",
    "ip": "192.0.2.7",
    "tailnet_ip": "203.0.113.8",
    "tailnet_fqdn": "bed-a.example.ts.net",
}


def test_lan_down_falls_back_to_tailnet_ip_and_remembers_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install(
        monkeypatch,
        {
            "192.0.2.7": (1, b"", "Host is unreachable"),
            "-s 203.0.113.8:5555 get-state": ok("device\n"),
        },
    )
    monkeypatch.setattr(adbops, "_path_at", {})
    assert go(adbops.adb_ready(BEDROOM)) == ("203.0.113.8:5555", "device")
    assert adbops.serial_of(BEDROOM) == "203.0.113.8:5555"
    assert adbops.candidates(BEDROOM)[0] == "203.0.113.8:5555"
    fake.calls.clear()
    go(adbops.adb_ready(BEDROOM, fresh=True))
    assert "192.0.2.7" not in fake.calls[0]
    monkeypatch.setattr(adbops, "PATH_MEMORY_S", 0)
    assert adbops.candidates(BEDROOM)[0] == "192.0.2.7:5555"


def test_offline_error_names_every_path_tried(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, {"get-state": (1, b"", "no devices")})
    monkeypatch.setattr(adbops, "_path_at", {})
    with pytest.raises(DeviceError) as err:
        go(adbops.require_device(BEDROOM))
    text = str(err.value)
    assert "adb state is 'offline'" in text
    for host in ("192.0.2.7:5555", "203.0.113.8:5555", "bed-a.example.ts.net"):
        assert host in text


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
            "dumpsys power": ok("mWakefulness=Awake"),
            "dumpsys media_session": ok("state=PlaybackState {state=3, pos=1}"),
            "settings get": ok("off\n"),
        },
    )
    status = go(adbops.status(SHIELD))
    assert status["smarttube_version"] == "30.1" and "Window" in status["foreground"]
    health = go(adbops.health({**SHIELD, "paired_tv": "lg-a"}))
    assert health["awake"] is True and health["playing"] is True
    assert health["private_dns_mode"] == "off"


def test_unpaired_shield_health_reads_native_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install(
        monkeypatch,
        {
            "get-state": ok("device"),
            "dumpsys power": ok("mWakefulness=Awake"),
            "dumpsys media_session": ok("state=PlaybackState {state=3, pos=1}"),
        },
    )
    health = go(adbops.health(SHIELD))
    assert health["awake"] is True and health["playing"] is True
    shell_calls = [c for c in fake.calls if " shell " in c]
    assert len(shell_calls) == 2 + len(adbops.HEALTH_READS) + 3
    assert all(
        " shell dumpsys " in c or " shell settings get " in c for c in shell_calls
    )


@pytest.mark.parametrize(
    "dump", ["", "unsupported", "mWakefulness=Unknown", "mWakefulness=Awakeish"]
)
def test_unknown_power_is_not_asleep(dump: str) -> None:
    assert adbops.is_awake(dump) is None


@pytest.mark.parametrize(
    "dump",
    [
        "",
        "unsupported",
        "state=PlaybackState {state=30}",
        "state=PlaybackState {state=bad}",
        "state=PlaybackState {state=7}",
    ],
)
def test_unknown_playback_is_not_idle(dump: str) -> None:
    assert adbops.is_playing(dump) is None


@pytest.mark.parametrize("state", [0, 1, 2])
def test_recognized_inactive_playback(state: int) -> None:
    assert adbops.is_playing(f"state=PlaybackState {{state={state}, pos=1}}") is False


@pytest.mark.parametrize("state", [3, 4, 5, 6, 8, 9, 10, 11])
def test_active_playback_is_protected(state: int) -> None:
    assert adbops.is_playing(f"state=PlaybackState {{state={state}, pos=1}}") is True


@pytest.mark.parametrize("command", ["dumpsys power", "dumpsys media_session"])
def test_health_state_command_failure_is_not_idle(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    install(
        monkeypatch,
        {"get-state": ok("device"), command: (1, b"", "unsupported service")},
    )
    with pytest.raises(DeviceError, match="unsupported service"):
        go(adbops.health(SHIELD))


@pytest.mark.parametrize("state", ["offline", "unauthorized"])
def test_status_offline_short_circuits(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    fake = install(monkeypatch, {"get-state": (1, b"", state)})
    assert go(adbops.status(SHIELD)) == {"adb_state": state}
    assert go(adbops.health(SHIELD)) == {"adb_state": state}
    assert not any(" shell " in c for c in fake.calls)


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


def frame(color: int, pixels: int = adbops.FRAME_PIXEL_BYTES) -> bytes:
    return gzip.compress(adbops.FRAME_HEADER + bytes([color]) * pixels, 1)


def test_screenshot_uses_scaled_frame_on_shield(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install(
        monkeypatch, {"get-state": ok("device"), "screenrecord": (0, frame(200), "")}
    )
    jpeg, note = go(adbops.screenshot(SHIELD))
    assert note is None
    assert Image.open(io.BytesIO(jpeg)).size == (1280, 720)
    assert not any("screencap" in c for c in fake.calls)


@pytest.mark.parametrize(
    "reply",
    [
        (0, frame(0), ""),
        (0, frame(200, adbops.FRAME_PIXEL_BYTES - 3), ""),
        (0, gzip.compress(b"x" * 20), ""),
        (0, b"not gzip", ""),
        (0, b"", ""),
        (1, frame(200), "failed"),
    ],
)
def test_screenshot_falls_back_to_screencap(
    monkeypatch: pytest.MonkeyPatch, reply: tuple[int, bytes, str]
) -> None:
    fake = install(
        monkeypatch,
        {
            "get-state": ok("device"),
            "screenrecord": reply,
            "screencap": (0, png(200), ""),
        },
    )
    jpeg, note = go(adbops.screenshot(SHIELD))
    assert jpeg.startswith(b"\xff\xd8")
    assert note is None
    assert any("screencap -p" in c for c in fake.calls)


def test_screenshot_keeps_fire_on_screencap(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(
        monkeypatch, {"get-state": ok("device"), "screencap": (0, png(200), "")}
    )
    go(adbops.screenshot(FIRE))
    assert not any("screenrecord" in c for c in fake.calls)


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
    install(monkeypatch, {"get-state": ok("device")})
    with pytest.raises(DeviceError, match="overlay_app_missing"):
        go(adbops.toast(SHIELD, "hello"))


def test_app_label_and_list_apps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(appcache, "CACHE_FILE", tmp_path / "cache.json")
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
    assert go(adbops.read_label("s", pkg)) is None
    apps, partial = go(adbops.list_apps(SHIELD))
    assert [a["id"] for a in apps] == [pkg]
    assert partial is False


def test_app_label_uses_apk_name(monkeypatch: pytest.MonkeyPatch) -> None:
    install(
        monkeypatch,
        {"pm path": ok("package:/data/app/x/base.apk\n"), "pull": ok("1 file pulled")},
    )
    monkeypatch.setattr(adbops, "apk_label", lambda _path: "Pretty Name")
    assert go(adbops.read_label("s", "org.example.app")) == "Pretty Name"
    assert appcache.app_title("org.example.app", "Pretty Name")


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


def probes(fake: FakeAdb) -> int:
    return sum("get-state" in c for c in fake.calls)


def test_pool_skips_probe_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(monkeypatch, {"get-state": ok("device\n")})
    go(adbops.require_device(SHIELD))
    go(adbops.require_device(SHIELD))
    go(adbops.send_key(SHIELD, "HOME"))
    assert probes(fake) == 1
    assert sum("keyevent" in c for c in fake.calls) == 1


def test_pool_reprobes_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(monkeypatch, {"get-state": ok("device\n")})
    go(adbops.require_device(SHIELD))
    name = SHIELD["name"]
    serial, stamp = adbops._ready[str(name)]
    adbops._ready[str(name)] = (serial, stamp - adbops.READY_TTL_S - 1)
    go(adbops.require_device(SHIELD))
    assert probes(fake) == 2


def test_pool_fresh_always_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(monkeypatch, {"get-state": ok("device\n")})
    go(adbops.adb_ready(SHIELD))
    go(adbops.adb_ready(SHIELD, fresh=True))
    go(adbops.status(SHIELD))
    assert probes(fake) == 3


def test_pool_dropped_after_failed_command(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(
        monkeypatch,
        {"keyevent": (1, b"", "error: device offline"), "get-state": ok("device\n")},
    )
    go(adbops.require_device(SHIELD))
    with pytest.raises(DeviceError):
        go(adbops.send_key(SHIELD, "HOME"))
    assert SHIELD["name"] not in adbops._ready
    go(adbops.require_device(SHIELD))
    assert probes(fake) == 2


def test_pool_not_filled_for_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, {"get-state": (1, b"", "device offline")})
    assert go(adbops.adb_ready(SHIELD))[1] == "offline"
    assert adbops._ready == {}


@pytest.mark.parametrize(
    "states,chip",
    [
        ([], "idle"),
        (["stopped"], "idle"),
        (["none", "paused"], "paused"),
        (["paused", "buffering"], "buffering"),
        (["connecting"], "buffering"),
        (["paused", "playing"], "playing"),
        (["buffering", "playing"], "playing"),
        (["fast_forwarding"], "playing"),
        (["error"], "idle"),
        ([None], "idle"),
    ],
)
def test_playback_state_chip(states: list[str | None], chip: str) -> None:
    assert adbops.playback_state([{"state": s} for s in states]) == chip


def test_status_reports_playback_chip(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = (
        "      package=com.plexapp.android\n"
        "      active=true\n"
        "      state=PlaybackState {state=6, position=0}\n"
    )
    install(
        monkeypatch,
        {
            "get-state": ok("device"),
            "mCurrentFocus": ok("mCurrentFocus=Window{x u0 pkg/act}\n"),
            "dumpsys media_session": ok(sessions),
        },
    )
    assert go(adbops.status(SHIELD))["playback"] == "buffering"


def test_status_playback_read_failure_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    install(
        monkeypatch,
        {
            "get-state": ok("device"),
            "dumpsys media_session": (1, b"", "closed"),
        },
    )
    assert go(adbops.status(SHIELD))["playback"] is None
