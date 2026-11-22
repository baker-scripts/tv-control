"""Key-sending verbs read current_screen first and stop on a mismatch: fake screen, no device."""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from conftest import read_policy

import adbops
import messages
import tools
from inventory import DeviceError

SHIELD = {
    "name": "shield-a",
    "type": "shield",
    "ip": "192.0.2.1",
    "household": "grandparents",
}
PLEX = "com.plexapp.android"


def go(coro: Any) -> Any:
    return asyncio.run(coro)


class Device:
    def __init__(self) -> None:
        self.fronts = [PLEX]
        self.focus: dict[str, Any] | None = {
            "text": "Continue Watching",
            "content_desc": "The Matrix",
            "resource_id": "com.plexapp.android:id/card",
            "bounds": [1, 2, 3, 4],
        }
        self.log: list[str] = []
        self.shown: list[str] = []

    async def current_screen(
        self, dev: dict[str, Any], focus: bool = True
    ) -> dict[str, Any]:
        self.log.append("screen+focus" if focus else "screen")
        front = self.fronts.pop(0) if len(self.fronts) > 1 else self.fronts[0]
        body: dict[str, Any] = {"package": front, "awake": True, "sessions": []}
        if focus:
            body["focus"] = self.focus
        return body

    async def send_key(self, dev: dict[str, Any], key: str) -> dict[str, Any]:
        self.log.append(f"key {key}")
        return {"sent": key}

    async def toast(
        self, dev: dict[str, Any], message: str, **_: Any
    ) -> dict[str, Any]:
        self.log.append("toast")
        self.shown.append(message)
        return {"shown": True}


@pytest.fixture
def device(
    monkeypatch: pytest.MonkeyPatch, policy: dict[str, Any], tmp_path: Path
) -> Device:
    fake = Device()

    async def load_policy() -> dict[str, Any]:
        return policy

    monkeypatch.setattr(tools, "load_message_policy", load_policy)
    monkeypatch.setattr(tools.messages, "LOG_DIR", tmp_path / "messages")

    async def get_device(name: str) -> dict[str, Any]:
        if name != "shield-a":
            raise DeviceError("unknown device")
        return SHIELD

    async def no_refusal(device: str, force: bool) -> None:
        return None

    monkeypatch.setattr(tools, "get_device", get_device)
    monkeypatch.setattr(tools, "in_use_refusal", no_refusal)
    monkeypatch.setattr(adbops, "current_screen", fake.current_screen)
    monkeypatch.setattr(adbops, "send_key", fake.send_key)
    monkeypatch.setattr(adbops, "toast", fake.toast)
    monkeypatch.setattr(tools, "NARRATE_GAP_S", 0)
    return fake


def test_navigate_checks_the_screen_before_every_key(device: Device) -> None:
    body = go(tools.do_navigate("shield-a", "down, down, ok", PLEX, "Matrix"))
    assert device.log == [
        "screen",
        "screen+focus",
        "key DPAD_DOWN",
        "screen",
        "key DPAD_DOWN",
        "screen",
        "key DPAD_CENTER",
        "screen+focus",
    ]
    assert body["sent"] == ["DPAD_DOWN", "DPAD_DOWN", "DPAD_CENTER"]
    assert body["screen"]["package"] == PLEX


def test_navigate_stops_when_another_app_is_in_front(device: Device) -> None:
    device.fronts = [tools.SHIELD_HOME_APP]
    body = go(tools.do_navigate("shield-a", "ok", PLEX))
    assert "screen_mismatch" in body["error"] and tools.SHIELD_HOME_APP in body["error"]
    assert not any(e.startswith("key") for e in device.log)


def test_navigate_stops_mid_sequence_when_the_app_changes(device: Device) -> None:
    device.fronts = [PLEX, PLEX, "org.smarttube.stable"]
    body = go(tools.do_navigate("shield-a", "down, ok", PLEX))
    assert "screen_mismatch" in body["error"]
    assert [e for e in device.log if e.startswith("key")] == ["key DPAD_DOWN"]


@pytest.mark.parametrize(
    "focus", [None, {"text": "Settings", "content_desc": "", "resource_id": ""}]
)
def test_navigate_stops_on_a_focus_mismatch(device: Device, focus: Any) -> None:
    device.focus = focus
    body = go(tools.do_navigate("shield-a", "ok", PLEX, "Matrix"))
    assert "screen_mismatch" in body["error"] and "focus" in body["error"]
    assert not any(e.startswith("key") for e in device.log)


@pytest.mark.parametrize(
    "keys", ["", "HOME", "SLEEP", "VOLUME_UP", ",".join(["down"] * 11), "bogus"]
)
def test_navigate_refuses_bad_key_lists(device: Device, keys: str) -> None:
    assert "error" in go(tools.do_navigate("shield-a", keys, PLEX))
    assert device.log == []


def test_navigate_needs_expect_app(device: Device) -> None:
    assert "expect_app" in go(tools.do_navigate("shield-a", "ok", ""))["error"]


def test_navigate_narration_names_select_on_a_shield(device: Device) -> None:
    body = go(tools.do_navigate("shield-a", "down, ok", PLEX, narrate=True))
    assert device.shown[0] == messages.intro_line(read_policy())
    assert "the round button in the middle of the ring" in device.shown[1]
    assert device.log.index("toast") < device.log.index("key DPAD_DOWN")
    assert body["narration"] == device.shown
    assert device.shown[-1] == tools.END_LINE and body["narration_mode"] == "every_step"


def test_navigate_narration_is_start_and_end_outside_grandparents_household(
    device: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(SHIELD, "household", "family")
    body = go(tools.do_navigate("shield-a", "down, ok", PLEX, narrate=True))
    assert len(device.shown) == 2 and device.shown[1] == tools.END_LINE
    assert "button" not in device.shown[0] and "select it" in device.shown[0]
    assert body["narration_mode"] == "start_end"


def test_send_key_with_expect_app_checks_first(
    device: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def do_key(device_name: str, key: str) -> dict[str, Any]:
        device.log.append(f"key {key}")
        return {"sent": key}

    monkeypatch.setattr(tools, "do_key", do_key)
    device.fronts = [tools.SHIELD_HOME_APP]
    body = go(tools.send_key("shield-a", "OK", expect_app=PLEX))
    assert "screen_mismatch" in body["error"]
    assert not any(e.startswith("key") for e in device.log)
    device.fronts = [PLEX]
    assert go(tools.send_key("shield-a", "OK", expect_app=PLEX)) == {"sent": "OK"}


def test_send_key_without_expectations_is_unchanged(
    device: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def do_key(device_name: str, key: str) -> dict[str, Any]:
        return {"sent": key}

    monkeypatch.setattr(tools, "do_key", do_key)
    assert go(tools.send_key("shield-a", "OK")) == {"sent": "OK"}
    assert device.log == []
