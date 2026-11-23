"""tv_action, capabilities, and the write gate on every write verb. Mocks only: no device call."""

import asyncio
import inspect
from typing import Any

import pytest
from test_inputs import ANDROID_DUMP, WEBOS_LIST

import adbops
import appcache
import sleeptimer
import tools
import webos
from inventory import DeviceError

BOX = {
    "name": "box-a",
    "type": "shield",
    "ip": "192.0.2.1",
    "paired_tv": "tv-b",
    "capabilities": ["find_remote", "power", "sleep_timer"],
}
TV = {
    "name": "tv-b",
    "type": "webos",
    "ip": "192.0.2.2",
    "capabilities": ["wake", "power", "inputs"],
    "inputs": {"HDMI_2": "Living room box"},
    "input_aliases": {"console box": "HDMI_3"},
    # A ceiling only: listing find_remote on a webOS TV never makes it available.
}
FIRE = {
    "name": "tv-c",
    "type": "firetv",
    "ip": "192.0.2.3",
    "tv_control": "best-effort",
    "capabilities": ["sleep_timer"],
    "inputs": {"hdmi1": "Living room box"},
    "input_keys": {"hdmi1": "TV_INPUT_HDMI_1"},
}
DEVICES = {d["name"]: d for d in (BOX, TV, FIRE)}

# Every registered tool is one of these three kinds; a new tool fails the test until classified.
READ_VERBS = {
    "list_devices",
    "device_status",
    "current_screen",
    "what_is_on",
    "screenshot",
    "list_apps",
    "list_inputs",
    "health",
    "scan_site",
    "capabilities",
}
# Pairing prompts the operator starts with someone at the TV, and dry-run-only scripts.
SETUP_VERBS = {"webos_pair", "pair_start", "pair_finish", "apply_baseline", "lockdown"}


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> list[tuple[str, Any]]:
    monkeypatch.setattr(appcache, "CACHE_FILE", tmp_path / "cache.json")
    monkeypatch.setattr(sleeptimer, "TIMERS_FILE", tmp_path / "timers.json")
    writes: list[tuple[str, Any]] = []

    async def get_device(name: str) -> dict[str, Any]:
        if name not in DEVICES:
            raise DeviceError(f"unknown device {name!r}; call list_devices")
        return DEVICES[name]

    async def load_devices() -> list[dict[str, Any]]:
        return list(DEVICES.values())

    async def denied() -> list[str]:
        return ["netflix"]

    async def idle(dev: dict[str, Any]) -> bool:
        return False

    def recorder(name: str) -> Any:
        async def write(*args: Any, **_: Any) -> dict[str, Any]:
            writes.append((name, args[1:]))
            return {"done": name}

        return write

    async def android_inputs(dev: dict[str, Any]) -> list[dict[str, Any]]:
        import inputs

        return inputs.parse_android(ANDROID_DUMP) if dev["type"] == "firetv" else []

    async def webos_inputs(dev: dict[str, Any]) -> list[dict[str, Any]]:
        import inputs

        return inputs.from_webos(WEBOS_LIST)

    monkeypatch.setattr(tools, "get_device", get_device)
    monkeypatch.setattr(tools, "load_devices", load_devices)
    monkeypatch.setattr(tools, "load_denied_apps", denied)
    monkeypatch.setattr(tools, "device_busy", idle)

    async def no_apps(dev: dict[str, Any]) -> list[dict[str, str]]:
        return []

    async def no_adb_apps(dev: dict[str, Any]) -> tuple[list[dict[str, str]], bool]:
        return [], False

    monkeypatch.setattr(adbops, "list_apps", no_adb_apps)
    monkeypatch.setattr(webos, "list_apps", no_apps)
    monkeypatch.setattr(adbops, "list_inputs", android_inputs)
    monkeypatch.setattr(webos, "list_inputs", webos_inputs)
    for mod, names in (
        (adbops, ("launch", "send_key", "send_key_fast", "set_input", "set_input_key")),
        (webos, ("launch", "set_input", "send_key", "wake")),
    ):
        for name in names:
            monkeypatch.setattr(mod, name, recorder(f"{mod.__name__}.{name}"))
    for name in ("do_open_app", "do_play_title"):
        monkeypatch.setattr(tools, name, recorder(f"tools.{name}"))
    return writes


def tool_names() -> set[str]:
    return {t.name for t in run(tools.mcp.list_tools())}


def test_every_registered_tool_is_classified() -> None:
    write = tool_names() - READ_VERBS - SETUP_VERBS
    assert READ_VERBS | SETUP_VERBS <= tool_names()
    assert {"tv_action", "set_input", "launch_app", "toast"} <= write


def _dummy_args(fn: Any) -> dict[str, Any]:
    args: dict[str, Any] = {}
    for name, param in inspect.signature(fn).parameters.items():
        if param.default is not inspect.Parameter.empty:
            continue
        args[name] = "box-a" if name == "device" else "x"
    return args


def test_every_write_verb_calls_write_gate(
    monkeypatch: pytest.MonkeyPatch, isolated: list[tuple[str, Any]]
) -> None:
    gate_calls: list[str] = []

    async def gate(device: str, force: bool, **_: Any) -> dict[str, Any]:
        gate_calls.append(device)
        return {"device": device, "error": "gate_test"}

    monkeypatch.setattr(tools, "write_gate", gate)
    write = sorted(tool_names() - READ_VERBS - SETUP_VERBS)
    for name in write:
        fn = getattr(tools, name)
        before = len(gate_calls)
        body = run(fn(**_dummy_args(fn)))
        assert body == {"device": "box-a", "error": "gate_test"}, name
        assert len(gate_calls) == before + 1, f"{name} skipped write_gate"
    for action in tools.ACTIONS:
        assert run(tools.tv_action("box-a", action, "x"))["error"] == "gate_test"
    assert isolated == []


@pytest.mark.parametrize(
    ("device", "action", "target", "would"),
    [
        ("box-a", "open_app", "com.plexapp.android", {"app": "com.plexapp.android"}),
        ("tv-b", "switch_input", "console box", {"input": "HDMI_3"}),
        ("tv-b", "switch_input", "media box", {"input": "HDMI_2"}),
        ("tv-c", "switch_input", "HDMI 1", {"route": "keyevent TV_INPUT_HDMI_1"}),
        ("tv-c", "switch_input", "hdmi 2", {"route": "passthrough intent"}),
        ("box-a", "play_title", "abcdef0123:42:5000", {"app": "com.plexapp.android"}),
        ("box-a", "wake", "", {"route": "WAKEUP key"}),
        ("tv-b", "wake", "", {"route": "Wake-on-LAN"}),
        ("box-a", "sleep", "", {"route": "SLEEP key (standby)"}),
        ("tv-b", "volume", "down", {"key": "VOLUME_DOWN", "route": "webos"}),
    ],
)
def test_shadow_reports_the_plan_and_writes_nothing(
    isolated: list[tuple[str, Any]],
    device: str,
    action: str,
    target: str,
    would: dict[str, Any],
) -> None:
    body = run(tools.tv_action(device, action, target, shadow=True))
    assert body["shadow"] is True, body
    assert would.items() <= body["would"].items()
    assert isolated == []


def test_real_action_runs_the_planned_write(isolated: list[tuple[str, Any]]) -> None:
    body = run(tools.tv_action("tv-b", "switch_input", "Console"))
    assert body["action"] == "switch_input" and body["label"] == "Game Console"
    assert isolated == [("webos.set_input", ("HDMI_3",))]
    isolated.clear()
    run(tools.tv_action("tv-c", "switch_input", "living room box"))
    assert isolated == [("adbops.set_input_key", ("TV_INPUT_HDMI_1",))]
    isolated.clear()
    appcache.put("box-a", "apps", [{"id": "com.plexapp.android", "title": "Plex"}])
    run(tools.tv_action("box-a", "open_app", "plex"))
    assert isolated[0][0] == "tools.do_open_app"


@pytest.mark.parametrize(
    ("device", "action", "target", "error"),
    [
        ("box-a", "reboot", "", "unknown_action"),
        ("box-a", "open_app", "com.netflix.ninja", "denylist"),
        ("box-a", "open_app", "com.android.vending", "protected_target"),
        ("tv-b", "switch_input", "projector", "unknown_input"),
        ("tv-b", "switch_input", "", "unknown_input"),
        ("box-a", "switch_input", "HDMI 1", "unsupported_on_device"),
        ("tv-c", "switch_input", "HDMI 4", "unknown_input"),
        ("box-a", "play_title", "not-a-title", "play_title target"),
        ("box-a", "play_title", "abcdef0123:42:soon", "view_offset_ms"),
        ("tv-b", "play_title", "abcdef0123:42", "Android only"),
        ("box-a", "volume", "louder", "volume target"),
        ("tv-c", "volume", "up", "best-effort"),
        ("tv-c", "wake", "", "'wake' capability"),
        ("tv-c", "sleep", "", "'power' capability"),
        ("nope", "wake", "", "unknown device"),
    ],
)
def test_refusals(
    isolated: list[tuple[str, Any]],
    device: str,
    action: str,
    target: str,
    error: str,
) -> None:
    for shadow in (True, False):
        body = run(tools.tv_action(device, action, target, shadow=shadow))
        assert error in body["error"], body
    assert isolated == []


def test_app_policy_refuses_even_with_force(isolated: list[tuple[str, Any]]) -> None:
    body = run(tools.tv_action("box-a", "open_app", "com.netflix.ninja", force=True))
    assert "denylist" in body["error"]
    assert isolated == []


def test_ambiguous_input_returns_candidates(
    monkeypatch: pytest.MonkeyPatch, isolated: list[tuple[str, Any]]
) -> None:
    import inputs

    async def twins(dev: dict[str, Any]) -> list[dict[str, Any]]:
        return inputs.from_webos(
            [*WEBOS_LIST, {"id": "HDMI_4", "label": "Media Box", "port": 4}]
        )

    monkeypatch.setattr(webos, "list_inputs", twins)
    body = run(tools.tv_action("tv-b", "switch_input", "media box"))
    assert body["error"].startswith("ambiguous_input")
    assert len(body["candidates"]) == 2
    assert isolated == []


def test_in_use_refuses_unless_forced(
    monkeypatch: pytest.MonkeyPatch, isolated: list[tuple[str, Any]]
) -> None:
    async def busy(dev: dict[str, Any]) -> bool:
        return dev["name"] == "box-a"

    monkeypatch.setattr(tools, "device_busy", busy)
    # The box feeds tv-b (paired_tv), so an input switch on the TV is refused too.
    for action, device in (("wake", "box-a"), ("switch_input", "tv-b")):
        body = run(tools.tv_action(device, action, "media box", shadow=True))
        assert body["error"].startswith("in_use"), body
    assert run(tools.tv_action("box-a", "wake", force=True))["action"] == "wake"


def test_set_input_verb_resolves_spoken_names(isolated: list[tuple[str, Any]]) -> None:
    assert run(tools.set_input("tv-b", "Living room box"))["label"] == "Living room box"
    assert isolated == [("webos.set_input", ("HDMI_2",))]


def test_capabilities_derive_from_platform_and_config_only_removes() -> None:
    tv = run(tools.do_capabilities("tv-b"))
    assert tv["verbs"]["find_remote"]["ok"] is False
    assert tv["verbs"]["open_app"]["ok"] is False
    assert tv["verbs"]["launch_app"]["ok"] is True
    assert tv["actions"]["switch_input"]["ok"] is True
    assert tv["actions"]["play_title"]["ok"] is False
    assert tv["volume"]["path"] == "webos"
    assert {i["id"] for i in tv["inputs"]} == {"HDMI_1", "HDMI_2", "HDMI_3"}
    box = run(tools.do_capabilities("box-a"))
    assert box["verbs"]["find_remote"]["ok"] is True
    assert box["actions"]["switch_input"]["ok"] is False
    assert box["actions"]["volume"]["ok"] is True
    fire = run(tools.do_capabilities("tv-c"))
    assert fire["verbs"]["power"]["ok"] is False
    assert "'power' capability" in fire["actions"]["sleep"]["why"]
    assert fire["actions"]["volume"]["ok"] is False
    assert fire["actions"]["switch_input"]["ok"] is True
    assert "capabilities" not in fire["verbs"]


def test_capabilities_lists_only_apps_the_policy_allows() -> None:
    appcache.put(
        "box-a",
        "apps",
        [
            {"id": "com.plexapp.android", "title": "Plex"},
            {"id": "com.netflix.ninja", "title": "Netflix"},
            {"id": "com.android.vending", "title": "Google Play Store"},
        ],
    )
    apps = run(tools.do_capabilities("box-a"))["apps"]
    assert [a["id"] for a in apps] == ["com.plexapp.android"]


def test_toast_passes_the_gate_without_the_in_use_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []

    async def gate(device: str, force: bool, **kw: Any) -> None:
        seen.append(kw)

    async def fake_toast(*_: Any, **__: Any) -> dict[str, Any]:
        return {"shown": True}

    monkeypatch.setattr(tools, "write_gate", gate)
    monkeypatch.setattr(tools, "do_toast", fake_toast)
    assert run(tools.toast("box-a", "hello"))["shown"] is True
    assert seen == [{"app": None, "paired": False, "in_use": False}]


def test_wake_on_a_fire_device_sends_wakeup_not_wol(
    monkeypatch: pytest.MonkeyPatch, isolated: list[tuple[str, Any]]
) -> None:
    DEVICES["tv-d"] = {**FIRE, "name": "tv-d", "capabilities": ["wake"]}
    try:
        run(tools.do_wake("tv-d"))
    finally:
        del DEVICES["tv-d"]
    assert isolated == [("adbops.send_key", ("WAKEUP",))]
