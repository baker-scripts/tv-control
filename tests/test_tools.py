"""Behavior tests with mocks only: no test touches a real device, WoL relay, or clock."""

import asyncio
import contextlib
from typing import Any

import adbops
import appcache
import inventory
import pytest
import sleeptimer
import tools
import webos
from inventory import DeviceError

SHIELD = {
    "name": "shield-a",
    "type": "shield",
    "ip": "192.0.2.1",
    "tailnet_fqdn": "shield-a.example.ts.net",
    "port": 5555,
    "capabilities": ["find_remote", "power", "sleep_timer"],
}
LG = {
    "name": "lg-a",
    "type": "webos",
    "ip": "192.0.2.2",
    "site": "home",
    "mac": "00:00:5e:00:53:01",
    "capabilities": ["wake", "power", "inputs", "sleep_timer"],
}


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setattr(appcache, "CACHE_FILE", tmp_path / "cache.json")
    monkeypatch.setattr(sleeptimer, "TIMERS_FILE", tmp_path / "timers.json")
    devices = {d["name"]: d for d in (SHIELD, LG)}

    async def get_device(name: str) -> dict[str, Any]:
        if name not in devices:
            raise DeviceError(f"unknown device {name!r}; call list_devices")
        return devices[name]

    async def denied() -> list[str]:
        return ["netflix"]

    async def quick() -> list[dict[str, Any]]:
        return [
            {"label": "Plex", "id": ["com.plexapp.android"], "title": ["plex"]},
            {
                "label": "YouTube TV",
                "id": ["youtube.leanback.ytv.v1"],
                "title": ["youtube tv"],
            },
        ]

    monkeypatch.setattr(tools, "get_device", get_device)
    monkeypatch.setattr(tools, "load_denied_apps", denied)
    monkeypatch.setattr(tools, "load_quick_apps", quick)

    async def no_device(*_: Any, **__: Any) -> Any:
        raise AssertionError("a test reached a real device call")

    for name in (
        "launch",
        "send_key",
        "send_key_fast",
        "find_remote",
        "toast",
        "list_apps",
    ):
        monkeypatch.setattr(adbops, name, no_device)
    for name in (
        "launch",
        "send_key",
        "list_apps",
    ):
        monkeypatch.setattr(webos, name, no_device)


def test_netflix_refused_on_every_device_type() -> None:
    for dev, app in ((SHIELD["name"], "com.netflix.ninja"), (LG["name"], "netflix")):
        body = run(tools.do_launch(dev, app))
        assert "denylist" in body["error"]
        assert body["cause"] and body["next_step"]


def test_launch_allowed_app_reaches_adb(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(dev: dict[str, Any], pkg: str) -> dict[str, Any]:
        return {"launched": pkg}

    monkeypatch.setattr(adbops, "launch", fake)
    assert run(tools.do_launch("shield-a", "com.plexapp.android"))["launched"]


def test_find_remote_needs_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(dev: dict[str, Any]) -> dict[str, Any]:
        return {"launched": "locator"}

    monkeypatch.setattr(adbops, "find_remote", fake)
    assert run(tools.do_find_remote("shield-a"))["launched"] == "locator"
    assert "capability" in run(tools.do_find_remote("lg-a"))["error"]


def test_errors_are_actionable() -> None:
    unauth = DeviceError("adb state is 'unauthorized', not device").body()
    assert "Always allow" in unauth["next_step"]
    offline = DeviceError("adb state is 'offline', not device").body()
    assert "LAN" in offline["next_step"]
    assert set(unauth) == {"error", "cause", "next_step"}


def test_pending_device_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    async def load() -> list[dict[str, Any]]:
        return [{"name": "p", "ip": "", "status": "pending"}]

    monkeypatch.setattr(inventory, "load_devices", load)
    with pytest.raises(DeviceError, match="pending"):
        run(inventory.get_device("p"))
    assert not inventory.is_active({"ip": "192.0.2.4", "status": "pending"})


def test_adb_falls_back_to_tailnet_name(monkeypatch: pytest.MonkeyPatch) -> None:
    tried = []

    async def ready_one(dev: dict[str, Any], serial: str) -> tuple[str, str]:
        tried.append(serial)
        return serial, "device" if "example.ts.net" in serial else "offline"

    monkeypatch.setattr(adbops, "_ready_one", ready_one)
    serial, state = run(adbops.adb_ready(SHIELD))
    assert tried == ["192.0.2.1:5555", "shield-a.example.ts.net:5555"]
    assert (serial, state) == ("shield-a.example.ts.net:5555", "device")


def test_toast_text_is_sanitized() -> None:
    assert adbops.clean_toast('it\'s "fine"; $(x) `y`') == "its fine; (x) y"
    with pytest.raises(DeviceError):
        adbops.clean_toast("'\"`")


def test_sleep_timer_fires_on_fake_clock() -> None:
    fired: list[str] = []

    async def fake_fire(device: str) -> None:
        fired.append(device)

    t0 = 1_000_000.0
    assert run(tools.do_sleep_timer("shield-a", 15, now=t0))["remaining_s"] == 900
    assert (
        run(tools.do_sleep_timer("shield-a", None, now=t0 + 600))["remaining_s"] == 300
    )
    assert run(sleeptimer.fire_due(t0 + 899, fake_fire)) == []
    assert run(sleeptimer.fire_due(t0 + 900, fake_fire)) == ["shield-a"]
    assert fired == ["shield-a"]
    assert (
        run(tools.do_sleep_timer("shield-a", None, now=t0 + 901))["remaining_s"] is None
    )


def test_sleep_timer_cancel_and_bad_preset() -> None:
    run(tools.do_sleep_timer("lg-a", 30, now=0))
    assert run(tools.do_sleep_timer("lg-a", 0, now=1))["remaining_s"] is None
    assert "error" in run(tools.do_sleep_timer("lg-a", 7, now=0))


def test_sleep_timer_fire_uses_sleep_not_reboot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = []

    async def send_key(dev: dict[str, Any], key: str) -> dict[str, Any]:
        keys.append(key)
        return {"sent": key}

    monkeypatch.setattr(adbops, "send_key", send_key)
    run(tools.power_off_for_timer("shield-a"))
    assert keys == ["SLEEP"]


def test_cache_served_first_and_when_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    appcache.put(
        "shield-a", "apps", [{"id": "com.plexapp.android", "title": "Plex"}], now=0
    )

    async def boom(dev: dict[str, Any]) -> list[str]:
        raise DeviceError("adb state is 'offline', not device")

    monkeypatch.setattr(adbops, "list_apps", boom)
    monkeypatch.setattr(appcache, "is_fresh", lambda e, now=None: False)
    body = run(tools.do_list_apps("shield-a"))
    assert body["apps"][0]["id"] == "com.plexapp.android"
    assert "serving cache" in body["warning"]
    assert body["stale"] is True


def test_background_mode_replies_from_cache_then_refreshes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    appcache.put("shield-a", "apps", [{"id": "x.y", "title": "x.y"}], now=0)
    monkeypatch.setattr(appcache, "is_fresh", lambda e, now=None: False)
    calls = []

    async def fake_refresh(device: str) -> dict[str, Any]:
        calls.append(device)
        return {}

    monkeypatch.setattr(tools, "refresh_lists", fake_refresh)

    async def go() -> dict[str, Any]:
        body = await tools.do_list_apps("shield-a", background=True)
        await asyncio.gather(*tools._bg_tasks)
        return body

    body = run(go())
    assert body["refreshing"] is True
    assert calls == ["shield-a"]


def test_refresh_stores_apps_and_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def apps(dev: dict[str, Any]) -> list[dict[str, str]]:
        return [{"id": "netflix", "title": "Netflix"}]

    async def inputs(dev: dict[str, Any]) -> list[dict[str, str]]:
        return [{"id": "HDMI_1", "label": "PC"}]

    monkeypatch.setattr(webos, "list_apps", apps)
    monkeypatch.setattr(webos, "list_inputs", inputs)
    assert run(tools.refresh_lists("lg-a"))["refreshed"] == ["apps", "inputs"]
    assert run(tools.do_list_inputs("lg-a"))["inputs"][0]["id"] == "HDMI_1"


def test_quick_apps_only_installed_and_never_denied() -> None:
    apps = [
        {"id": "com.plexapp.android", "title": "com.plexapp.android"},
        {"id": "netflix", "title": "Netflix"},
        {"id": "youtube.leanback.ytv.v1", "title": "YouTube TV"},
    ]
    wanted = run(tools.load_quick_apps())
    got = appcache.quick_apps(apps, wanted, ["netflix"])
    assert [b["label"] for b in got] == ["Plex", "YouTube TV"]
    assert appcache.quick_apps(apps[1:2], wanted, ["netflix"]) == []


def test_friendly_titles_sort_and_upgrade_legacy_cache() -> None:
    apps = appcache.friendly_apps(
        [
            {
                "id": "com.google.android.youtube.tvunplugged",
                "title": "com.google.android.youtube.tvunplugged",
            },
            {"id": "com.plexapp.android", "title": "com.plexapp.android"},
            {"id": "other.id", "title": "A device label"},
            {"id": "unknown.id", "title": ""},
        ]
    )
    assert [a["title"] for a in apps] == [
        "A device label",
        "Plex",
        "Unknown app",
        "YouTube TV",
    ]


def test_android_label_reads_apk_without_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    calls = []

    async def shell(serial: str, command: str) -> str:
        assert command == "pm path example.app"
        return (
            "package:/data/app/example/base.apk\npackage:/data/app/example/split.apk\n"
        )

    async def adb(serial: str, *args: str, **kw: Any) -> Any:
        calls.append(args)
        return 0, b"", ""

    monkeypatch.setattr(adbops, "shell", shell)
    monkeypatch.setattr(adbops, "adb", adb)
    monkeypatch.setattr(adbops, "apk_label", lambda path: "Example TV")
    monkeypatch.setattr(
        adbops.asyncio, "to_thread", AsyncMock(side_effect=lambda fn, *args: fn(*args))
    )
    assert run(adbops.app_label("mock", "example.app")) == "Example TV"
    assert calls[0][:2] == ("pull", "/data/app/example/base.apk")


def test_android_label_failure_uses_curated_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setattr(adbops, "shell", AsyncMock(side_effect=DeviceError("offline")))
    assert run(adbops.app_label("mock", "com.plexapp.android")) == "Plex"


def test_configured_inputs_override_labels_and_flag_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    dev = {**LG, "inputs": {"HDMI_2": "Shield", "HDMI_9": "Missing"}}
    monkeypatch.setattr(tools, "get_device", AsyncMock(return_value=dev))
    appcache.put("lg-a", "inputs", [{"id": "HDMI_2", "label": "Set top box"}])
    body = run(tools.do_list_inputs("lg-a"))
    assert body["inputs"][0]["label"] == "Shield"
    assert "Unknown configured input" in body["inputs"][1]["error"]


def test_fire_configured_input_is_visible_but_never_guessed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        tools,
        "get_device",
        AsyncMock(
            return_value={
                "name": "fire",
                "type": "firetv",
                "inputs": {"hdmi1": "Shield"},
            }
        ),
    )
    body = run(tools.do_list_inputs("fire"))
    assert body["inputs"][0]["label"] == "Shield"
    assert "not verified" in body["inputs"][0]["error"]
    assert "unsupported_on_device" in run(tools.do_set_input("fire", "hdmi1"))["error"]


def test_foreground_uses_cached_label(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    appcache.put("shield-a", "apps", [{"id": "com.plexapp.android", "title": "Plex"}])
    monkeypatch.setattr(
        adbops,
        "status",
        AsyncMock(
            return_value={
                "adb_state": "device",
                "foreground": "mCurrentFocus=Window{abc u0 com.plexapp.android/.MainActivity}",
            }
        ),
    )
    body = run(tools.do_status("shield-a"))
    assert body["app_title"] == "Plex"
    assert body["app_id"] == "com.plexapp.android"


def test_webos_current_input_and_live_config_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    dev = {**LG, "inputs": {"HDMI_2": "Shield", "HDMI_9": "Missing"}}
    tv = SimpleNamespace(
        tv_state=SimpleNamespace(
            is_on=True,
            is_screen_on=True,
            power_state={"state": "Active"},
            current_app_id="com.webos.app.hdmi2",
            volume=10,
            muted=False,
        ),
        get_inputs=AsyncMock(
            return_value=[
                {"id": "HDMI_2", "label": "HDMI 2", "appId": "com.webos.app.hdmi2"}
            ]
        ),
    )

    async def call(dev: dict[str, Any], fn: Any) -> Any:
        return await fn(tv)

    monkeypatch.setattr(webos, "is_paired", lambda name: True)
    monkeypatch.setattr(webos, "call", call)
    body = run(webos.status(dev))
    assert body["current_input"] == "HDMI_2"
    assert body["input_errors"] == {"HDMI_9": "Unknown configured input HDMI_9"}
    tv.get_inputs.assert_awaited_once()


class FakeTv:
    def __init__(self, fail: bool) -> None:
        self.fail = fail
        self.shown: list[str] = []

    async def send_message(self, text: str) -> None:
        if self.fail:
            raise TimeoutError
        self.shown.append(text)


def _patch_client(monkeypatch: pytest.MonkeyPatch, fails: int) -> list[FakeTv]:
    made: list[FakeTv] = []

    @contextlib.asynccontextmanager
    async def client(dev: dict[str, Any], key: str | None, timeout: float = 0) -> Any:
        tv = FakeTv(fail=len(made) < fails)
        made.append(tv)
        yield tv

    monkeypatch.setattr(webos, "client", client)
    monkeypatch.setattr(webos, "read_keys", lambda: {"lg-a": "k"})
    monkeypatch.setattr(webos, "RETRY_DELAY_S", 0)
    return made


def test_webos_reconnects_once_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    made = _patch_client(monkeypatch, fails=1)
    assert run(webos.toast(LG, "tv-control test")) == {"toast": "tv-control test"}
    assert len(made) == 2 and made[1].shown == ["tv-control test"]


def test_webos_gives_up_after_second_timeout() -> None:
    mp = pytest.MonkeyPatch()
    made = _patch_client(mp, fails=2)
    with pytest.raises(DeviceError) as err:
        run(webos.toast(LG, "x"))
    mp.undo()
    assert len(made) == 2
    assert "timed out" in str(err.value) and err.value.next_step


def test_set_input_has_no_default_and_validates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def inputs(dev: dict[str, Any]) -> list[dict[str, str]]:
        return [{"id": "HDMI_1", "label": "PC"}]

    monkeypatch.setattr(webos, "list_inputs", inputs)
    with pytest.raises(DeviceError, match="explicit"):
        run(webos.set_input(LG, ""))
    with pytest.raises(DeviceError, match="not an input"):
        run(webos.set_input(LG, "HDMI_4"))


def test_wake_without_relay_key_is_actionable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    async def sites() -> dict[str, Any]:
        return {"home": {"wol_relay": "relay@wol.example"}}

    monkeypatch.setattr(webos, "load_sites", sites)
    monkeypatch.setattr(webos, "WOL_KEY", tmp_path / "missing")
    with pytest.raises(DeviceError) as err:
        run(webos.wake(LG))
    assert "key" in str(err.value) and err.value.next_step


def test_wake_sends_mac_through_relay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    key = tmp_path / "id"
    key.write_text("k")
    seen: list[tuple[str, ...]] = []

    async def sites() -> dict[str, Any]:
        return {"home": {"wol_relay": "relay@wol.example"}}

    async def fake_run(*argv: str, timeout: float = 0) -> tuple[int, bytes, str]:
        seen.append(argv)
        return 0, b"", ""

    monkeypatch.setattr(webos, "load_sites", sites)
    monkeypatch.setattr(webos, "WOL_KEY", key)
    monkeypatch.setattr(webos, "run", fake_run)
    assert run(webos.wake(LG))["wol_sent"] == LG["mac"]
    assert seen[0][0] == "ssh" and seen[0][-3:] == (
        "relay@wol.example",
        "wol",
        LG["mac"],
    )


def test_health_all_skips_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    async def load() -> list[dict[str, Any]]:
        return [SHIELD, {"name": "p", "ip": "192.0.2.5", "status": "pending"}]

    seen: list[str] = []

    async def health(name: str) -> dict[str, Any]:
        seen.append(name)
        return {}

    monkeypatch.setattr(tools, "load_devices", load)
    monkeypatch.setattr(tools, "do_health", health)
    run(tools.health("all"))
    assert seen == ["shield-a"]


def test_panel_card_has_timer_presets_and_touch_layout() -> None:
    import panel

    card = panel._card(SHIELD)
    for minutes in (15, 30, 60, 90):
        assert f'data-min="{minutes}"' in card
    assert 'data-act="findremote"' in card and 'class="quick"' in card
    css = (panel.STATIC / "panel.css").read_text()
    assert "min-height:48px" in css and "viewport" in panel.PAGE
    assert "prefers-color-scheme:dark" in css
    lg = panel._card({**LG, "ip": "192.0.2.2"})
    assert 'class="inputs row"' in lg and 'data-act="findremote"' not in lg


def test_fire_tv_timer_needs_no_power_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fire_device(name: str) -> dict[str, Any]:
        return {"name": name, "type": "firetv", "capabilities": []}

    seen = []

    async def key(dev: dict[str, Any], value: str) -> dict[str, Any]:
        seen.append(value)
        return {}

    monkeypatch.setattr(tools, "get_device", fire_device)
    monkeypatch.setattr(adbops, "send_key", key)
    assert run(tools.do_sleep_timer("fire", 15, now=0))["remaining_s"] == 900
    run(tools.power_off_for_timer("fire"))
    assert seen == ["SLEEP"]


def test_preview_cache_throttles_concurrent_requests_and_resizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io
    from unittest.mock import AsyncMock

    from PIL import Image

    source = io.BytesIO()
    Image.new("RGB", (1280, 720)).save(source, format="JPEG")

    async def capture(_: str) -> tuple[bytes, None]:
        await asyncio.sleep(0)
        return source.getvalue(), None

    action = AsyncMock(side_effect=capture)
    monkeypatch.setattr(tools, "do_screenshot", action)
    monkeypatch.setattr(tools, "_preview_cache", {})
    now = [100.0]
    monkeypatch.setattr(tools.time, "monotonic", lambda: now[0])

    async def scenario() -> None:
        results = await asyncio.gather(
            *(tools.do_preview(SHIELD["name"]) for _ in range(4))
        )
        assert all(result == results[0] for result in results)
        action.assert_awaited_once()
        with Image.open(io.BytesIO(results[0][0])) as image:
            assert image.size == (480, 270) and image.format == "JPEG"
        now[0] += 3.99
        assert await tools.do_preview(SHIELD["name"]) == results[0]
        assert action.await_count == 1
        now[0] += 0.01
        await tools.do_preview(SHIELD["name"])
        assert action.await_count == 2

    run(scenario())


def test_preview_failures_throttled_and_webos_never_captured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    action = AsyncMock(return_value=(None, "offline"))
    monkeypatch.setattr(tools, "do_screenshot", action)
    monkeypatch.setattr(tools, "_preview_cache", {})

    async def scenario() -> None:
        assert await tools.do_preview(SHIELD["name"]) == (None, "offline", None)
        assert await tools.do_preview(SHIELD["name"]) == (None, "offline", None)
        action.assert_awaited_once()
        assert (await tools.do_preview(LG["name"]))[0] is None
        action.assert_awaited_once()

    run(scenario())


PAIRED = {**SHIELD, "paired_tv": "lg-a"}


def _paired_health(
    monkeypatch: pytest.MonkeyPatch, shield: dict[str, Any], tv: dict[str, Any]
) -> dict[str, Any]:
    async def get(name: str) -> dict[str, Any]:
        return PAIRED

    async def health(dev: dict[str, Any]) -> dict[str, Any]:
        return {"adb_state": "device", **shield}

    async def status(name: str) -> dict[str, Any]:
        return tv

    monkeypatch.setattr(tools, "get_device", get)
    monkeypatch.setattr(adbops, "health", health)
    monkeypatch.setattr(tools, "do_status", status)
    return run(tools.do_health("shield-a"))


def test_paired_tv_unreachable_while_playing_is_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _paired_health(
        monkeypatch, {"awake": True, "playing": True}, {"error": "timed out"}
    )
    assert out["status"] == "fault: paired TV unreachable while Shield plays"


@pytest.mark.parametrize(
    "shield", [{"awake": True, "playing": False}, {"awake": False, "playing": True}]
)
def test_paired_tv_unreachable_otherwise_is_standby(
    monkeypatch: pytest.MonkeyPatch, shield: dict[str, Any]
) -> None:
    assert (
        _paired_health(monkeypatch, shield, {"adb_state": "offline"})["status"]
        == "standby"
    )


def test_paired_tv_reachable_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _paired_health(
        monkeypatch, {"awake": True, "playing": True}, {"webos_state": "connected"}
    )
    assert out["status"] == "ok"


def test_playback_and_wakefulness_parsers() -> None:
    assert adbops.is_playing("state=PlaybackState {state=3, position=5}")
    assert not adbops.is_playing("state=PlaybackState {state=2, position=5}")
    assert not adbops.is_playing("state=PlaybackState {state=30}")
    assert adbops.is_awake("  mWakefulness=Awake")
    assert not adbops.is_awake("  mWakefulness=Asleep")


def test_firetv_set_input_uses_passthrough_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    dev = {
        "name": "fire",
        "type": "firetv",
        "inputs": {"hdmi1": "Shield"},
        "input_ids": {"hdmi1": "com.example.tv/.Hdmi/HW5"},
    }
    monkeypatch.setattr(tools, "get_device", AsyncMock(return_value=dev))
    calls: list[tuple[Any, ...]] = []

    async def fake_adb(serial: str, *args: str, **_: Any) -> Any:
        calls.append(args)
        return 0, b"Starting: Intent", ""

    async def fake_read(serial: str, cmd: str) -> str:
        return "mWakefulness=Awake" if "power" in cmd else "  mResumedActivity: x"

    async def ready(d: dict[str, Any]) -> str:
        return "ser"

    monkeypatch.setattr(adbops, "adb", fake_adb)
    monkeypatch.setattr(adbops, "read", fake_read)
    monkeypatch.setattr(adbops, "require_device", ready)
    body = run(tools.do_set_input("fire", "hdmi1"))
    assert body["input_set"] == "com.example.tv/.Hdmi/HW5"
    assert (
        calls[0][-1]
        == "content://android.media.tv/passthrough/com.example.tv%2F.Hdmi%2FHW5"
    )
    assert "error" in run(tools.do_set_input("fire", "hdmi9"))
    with pytest.raises(DeviceError):
        adbops.passthrough_uri("x; reboot")

    async def asleep(serial: str, cmd: str) -> str:
        return "mWakefulness=Asleep"

    monkeypatch.setattr(adbops, "read", asleep)
    calls.clear()
    assert "asleep" in run(tools.do_set_input("fire", "hdmi1"))["error"]
    assert not calls


def test_offline_apps_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        tools,
        "refresh_lists",
        AsyncMock(return_value={"error": "adb state is 'offline', not device"}),
    )
    assert run(tools.do_list_apps("shield-a"))["offline"] is True
    appcache.put("shield-a", "apps", [{"id": "com.plexapp.android", "title": "Plex"}])
    body = run(tools.do_list_apps("shield-a", refresh=True))
    assert body["offline"] is True and body["apps"]


def test_panel_js_shows_offline_message() -> None:
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] / "static" / "panel.js").read_text()
    assert js.count("Offline \\u2013 apps unavailable") == 2
