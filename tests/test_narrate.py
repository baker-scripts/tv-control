"""narrate and narrate=true on navigation verbs, with fake messages and a fake screen."""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from conftest import read_policy
from test_navigation import MID, PLEX, Screen
from test_navigation import screen as screen  # noqa: PLC0414

import adbops
import messages
import tools
from inventory import DeviceError

FIRE = {
    "name": "fire-a",
    "type": "firetv",
    "ip": "192.0.2.3",
    "household": "grandparents",
}


def go(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def shown(
    monkeypatch: pytest.MonkeyPatch,
    screen: Screen,
    policy: dict[str, Any],
    tmp_path: Path,
) -> list[str]:
    lines: list[str] = []
    devices = {
        "shield-a": {
            "name": "shield-a",
            "type": "shield",
            "ip": "192.0.2.1",
            "household": "grandparents",
        },
        "family-shield": {
            "name": "family-shield",
            "type": "shield",
            "ip": "192.0.2.4",
            "household": "family",
        },
        "lg-a": {"name": "lg-a", "type": "webos", "ip": "192.0.2.2"},
        "fire-a": FIRE,
    }

    async def get_device(name: str) -> dict[str, Any]:
        return devices[name]

    async def load_policy() -> dict[str, Any]:
        return policy

    async def toast(dev: dict[str, Any], message: str, **_: Any) -> dict[str, Any]:
        lines.append(message)
        screen.calls.append("toast")
        return {"toast": message, "shown": True}

    monkeypatch.setattr(tools, "get_device", get_device)
    monkeypatch.setattr(tools, "load_message_policy", load_policy)
    monkeypatch.setattr(tools.messages, "LOG_DIR", tmp_path / "messages")
    monkeypatch.setattr(adbops, "toast", toast)
    monkeypatch.setattr(tools, "NARRATE_GAP_S", 0)
    return lines


def test_narrate_starts_with_the_assistant_intro(shown: list[str]) -> None:
    body = go(tools.do_narrate("shield-a", "Opening Plex | Picking your show"))
    assert shown == [
        messages.intro_line(read_policy()),
        "Opening Plex",
        "Picking your show",
    ]
    assert body["shown"] == shown
    assert (
        messages.intro_line(read_policy())
        == "Hi, this is TV Helper. I will navigate now."
    )


def test_narrate_speech_is_reported_unavailable(shown: list[str]) -> None:
    body = go(tools.do_narrate("shield-a", "Opening Plex", speak=True))
    assert body["speech"].startswith("speech not available on this device")


def test_narrate_stops_when_a_message_is_not_shown(
    shown: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail(dev: dict[str, Any], message: str, **_: Any) -> dict[str, Any]:
        raise DeviceError("not_shown: no overlay window")

    monkeypatch.setattr(adbops, "toast", fail)
    assert "not_shown" in go(tools.do_narrate("shield-a", "x"))["error"]


def test_narrate_refuses_empty_steps(shown: list[str]) -> None:
    assert "error" in go(tools.do_narrate("shield-a", " | "))


def test_open_app_narrates_before_acting_with_shield_button_words(
    shown: list[str],
    screen: Screen,
) -> None:
    body = go(tools.do_open_app("shield-a", PLEX, narrate=True))
    assert body["confirmed"] is True
    assert shown[0] == messages.intro_line(read_policy())
    assert "Plex" in shown[1]
    assert "the circle button, just under the big ring on the right side" in shown[1]
    assert "the round button in the middle of the ring" in shown[1]
    assert screen.calls.index("toast") < screen.calls.index(f"launch {PLEX}")
    assert body["narration"] == shown


def test_go_home_narration_names_the_circle_button(shown: list[str]) -> None:
    go(tools.do_go_home("shield-a", narrate=True))
    assert "circle button" in shown[1]


def test_non_shield_narration_names_no_button(
    shown: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def send_key(dev: dict[str, Any], key: str) -> dict[str, Any]:
        return {"sent": key}

    monkeypatch.setattr(adbops, "send_key", send_key)
    go(tools.do_go_home("fire-a", narrate=True))
    assert "button" not in shown[1].lower() and "home" in shown[1].lower()


def test_play_title_narrates(shown: list[str]) -> None:
    body = go(tools.do_play_title("shield-a", MID, "1", narrate=True))
    assert body["playing"] is True and "Plex" in shown[1]


def test_failed_narration_skips_the_action(
    shown: list[str],
    screen: Screen,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(dev: dict[str, Any], message: str, **_: Any) -> dict[str, Any]:
        raise DeviceError("overlay_app_missing: install TvOverlay")

    monkeypatch.setattr(adbops, "toast", fail)
    body = go(tools.do_open_app("shield-a", PLEX, narrate=True))
    assert "overlay_app_missing" in body["error"]
    assert not any(c.startswith("launch") for c in screen.calls)


def test_narration_text_passes_the_message_filter() -> None:
    for line in [
        messages.intro_line(read_policy()),
        tools.END_LINE,
        *tools.SHIELD_BUTTONS.values(),
    ]:
        assert adbops.clean_toast(line) == line


def test_grandparents_household_gets_every_step_then_an_end_message(
    shown: list[str],
) -> None:
    body = go(tools.do_open_app("shield-a", PLEX, narrate=True))
    assert len(shown) == 3 and shown[-1] == tools.END_LINE
    assert body["narration_mode"] == "every_step"


def test_other_households_get_one_start_and_one_end_message_without_button_words(
    shown: list[str],
    screen: Screen,
) -> None:
    body = go(tools.do_open_app("family-shield", PLEX, narrate=True))
    assert body["confirmed"] is True and body["narration_mode"] == "start_end"
    assert len(shown) == 2 and shown[1] == tools.END_LINE
    assert "Plex" in shown[0] and "button" not in shown[0]
    assert screen.calls.index("toast") < screen.calls.index(f"launch {PLEX}")


def test_the_end_message_follows_a_confirmed_action_only(
    shown: list[str],
    screen: Screen,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tools, "PLAY_TIMEOUT_S", 0)
    screen.pending = None
    body = go(tools.do_play_title("family-shield", MID, "1", narrate=True))
    assert body["playing"] is True and shown[-1] == tools.END_LINE
    shown.clear()
    screen.play_after_view = "none"
    body = go(tools.do_play_title("family-shield", MID, "1", narrate=True))
    assert "error" in body and tools.END_LINE not in shown


def test_a_missed_end_message_is_reported_and_the_action_stands(
    shown: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def second_fails(
        dev: dict[str, Any], message: str, **_: Any
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise DeviceError("not_shown: no overlay window")
        return {"toast": message, "shown": True}

    monkeypatch.setattr(adbops, "toast", second_fails)
    body = go(tools.do_open_app("family-shield", PLEX, narrate=True))
    assert body["confirmed"] is True and "not_shown" in body["narration_error"]


def test_narration_off_shows_nothing_but_still_acts(
    shown: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def get_device(name: str) -> dict[str, Any]:
        return {
            "name": name,
            "type": "shield",
            "ip": "192.0.2.9",
            "household": "family",
            "narration": "off",
        }

    monkeypatch.setattr(tools, "get_device", get_device)
    body = go(tools.do_open_app("quiet-shield", PLEX, narrate=True))
    assert body["confirmed"] is True and shown == []
    assert body["narration_mode"] == "off"
    assert go(tools.do_narrate("quiet-shield", "One | Two"))["shown"] == []


def test_narrate_verb_in_start_end_shows_the_intro_and_the_last_step(
    shown: list[str],
) -> None:
    body = go(tools.do_narrate("family-shield", "Opening Plex | Picking | All set"))
    assert shown == [messages.intro_line(read_policy()), "All set"]
    assert body["narration_mode"] == "start_end"
