"""toast under the message policy: senders, requested or not, quiet hours, sleep, history. No device is contacted."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

import adbops
import messages
import tools
import webos
from inventory import DeviceError

FAMILY = {
    "name": "family-shield",
    "type": "shield",
    "ip": "192.0.2.1",
    "household": "family",
}
GRANDPARENTS = {
    "name": "grandparents-shield",
    "type": "shield",
    "ip": "192.0.2.2",
    "household": "grandparents",
}
LG = {"name": "family-lg", "type": "webos", "ip": "192.0.2.3", "household": "family"}


def go(coro: Any) -> Any:
    return asyncio.run(coro)


class Rig:
    def __init__(self) -> None:
        self.awake = True
        self.clock: int | None = 12 * 60
        self.sent: list[dict[str, Any]] = []
        self.woke = 0
        self.webos: list[str] = []
        self.confirmed = True


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, policy: dict[str, Any]) -> Rig:
    state = Rig()
    devices = {d["name"]: d for d in (FAMILY, GRANDPARENTS, LG)}

    async def get_device(name: str) -> dict[str, Any]:
        return devices[name]

    async def load_policy() -> dict[str, Any]:
        return policy

    async def toast(dev: dict[str, Any], message: str, **kw: Any) -> dict[str, Any]:
        state.sent.append({"device": dev["name"], "message": message, **kw})
        return {"toast": message, "shown": state.confirmed}

    async def current_screen(dev: dict[str, Any], focus: bool = True) -> dict[str, Any]:
        return {"awake": state.awake}

    async def wake_first(dev: dict[str, Any]) -> bool:
        state.woke += 1
        state.awake = True
        return True

    async def minutes(dev: dict[str, Any]) -> int | None:
        return state.clock

    async def webos_toast(dev: dict[str, Any], message: str) -> dict[str, Any]:
        state.webos.append(message)
        return {"toast": message}

    monkeypatch.setattr(tools, "get_device", get_device)
    monkeypatch.setattr(tools, "load_message_policy", load_policy)
    monkeypatch.setattr(tools, "_wake_first", wake_first)
    monkeypatch.setattr(adbops, "toast", toast)
    monkeypatch.setattr(adbops, "current_screen", current_screen)
    monkeypatch.setattr(adbops, "device_minutes", minutes)
    monkeypatch.setattr(webos, "toast", webos_toast)
    monkeypatch.setattr(messages, "LOG_DIR", tmp_path)
    return state


def test_a_requested_message_uses_the_policy_title_corner_and_length_duration(
    rig: Rig,
) -> None:
    body = go(
        tools.do_toast(
            "family-shield", "Opening Plex now.", "assistant", True, "", "family"
        )
    )
    assert body["shown"] is True
    sent = rig.sent[0]
    assert sent["title"] == "TV Helper" and sent["corner"] == "top_end"
    assert sent["seconds"] == 6 and sent["image"] == ""
    long = " ".join(["word"] * 40)
    go(tools.do_toast("family-shield", long, "assistant", True, "", "family"))
    assert rig.sent[1]["seconds"] == 16


def test_a_requested_message_wakes_a_sleeping_tv_and_shows_at_any_hour(
    rig: Rig,
) -> None:
    rig.awake, rig.clock = False, 23 * 60 + 30
    body = go(
        tools.do_toast("family-shield", "Here you go.", "assistant", True, "", "family")
    )
    assert body["shown"] is True and rig.woke == 1


def test_an_unrequested_message_to_a_sleeping_tv_is_dropped_and_not_shown(
    rig: Rig,
) -> None:
    rig.awake = False
    body = go(
        tools.do_toast(
            "family-shield",
            "Your movie is ready.",
            "assistant",
            False,
            "media_ready",
            "family",
        )
    )
    assert body["shown"] is False and body["dropped"] == "asleep"
    assert "not_shown" in body["error"]
    assert rig.sent == [] and rig.woke == 0


def test_an_unrequested_message_in_quiet_hours_is_dropped(rig: Rig) -> None:
    rig.clock = 22 * 60
    body = go(
        tools.do_toast(
            "family-shield",
            "Reply waiting.",
            "assistant",
            False,
            "reply_waiting",
            "family",
        )
    )
    assert body["dropped"] == "quiet_hours" and body["shown"] is False
    rig.clock = 7 * 60
    assert go(
        tools.do_toast(
            "family-shield",
            "Reply waiting.",
            "assistant",
            False,
            "reply_waiting",
            "family",
        )
    )["shown"]


def test_an_unrequested_message_with_an_unreadable_clock_is_dropped(rig: Rig) -> None:
    rig.clock = None
    body = go(tools.do_toast("family-shield", "Hello.", "operator"))
    assert body["dropped"] == "quiet_hours"


def test_operator_push_is_the_default_for_the_operator_and_reaches_any_tv(
    rig: Rig,
) -> None:
    body = go(tools.do_toast("grandparents-shield", "Dinner at six."))
    assert body["shown"] is True
    assert messages.history("grandparents-shield")[0]["kind"] == "operator_push"


def test_assistant_to_another_household_is_refused_and_nothing_is_sent(
    rig: Rig,
) -> None:
    body = go(
        tools.do_toast("grandparents-shield", "Hello.", "assistant", True, "", "family")
    )
    assert body["error"].startswith("wrong_household")
    assert rig.sent == [] and rig.woke == 0
    row = messages.history("grandparents-shield")[0]
    assert row["shown"] is False and row["sender"] == "assistant"


def test_assistant_unrequested_kind_outside_the_list_is_refused(rig: Rig) -> None:
    body = go(
        tools.do_toast(
            "family-shield", "Weather.", "assistant", False, "weather", "family"
        )
    )
    assert body["error"].startswith("not_allowed") and rig.sent == []


def test_a_message_the_tv_did_not_confirm_is_never_reported_shown(rig: Rig) -> None:
    rig.confirmed = False
    body = go(
        tools.do_toast("family-shield", "Hello.", "assistant", True, "", "family")
    )
    assert body.get("shown") is not True
    assert messages.history("family-shield")[0]["shown"] is False


def test_a_device_error_is_returned_and_recorded_as_not_shown(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail(dev: dict[str, Any], message: str, **kw: Any) -> dict[str, Any]:
        raise DeviceError("not_shown: TvOverlay answered HTTP 500")

    monkeypatch.setattr(adbops, "toast", fail)
    body = go(
        tools.do_toast("family-shield", "Hello.", "assistant", True, "", "family")
    )
    assert "not_shown" in body["error"]
    assert messages.history("family-shield")[0]["shown"] is False


def test_a_poster_image_is_passed_to_tvoverlay_and_bad_images_are_refused(
    rig: Rig,
) -> None:
    poster = "https://img.example/poster.jpg"
    go(
        tools.do_toast(
            "family-shield",
            "Your movie is ready.",
            "assistant",
            False,
            "media_ready",
            "family",
            poster,
        )
    )
    assert rig.sent[0]["image"] == poster
    body = go(
        tools.do_toast(
            "family-shield", "x", "operator", False, "", "", "data:image/png;base64,AA"
        )
    )
    assert body["error"].startswith("not_allowed") and len(rig.sent) == 1


def test_history_records_time_text_sender_kind_and_the_panel_user(rig: Rig) -> None:
    go(tools.do_toast("family-shield", "Hello there.", actor="pat"))
    row = messages.history("family-shield")[0]
    assert row["text"] == "Hello there." and row["sender"] == "pat"
    assert row["kind"] == "operator_push" and row["shown"] is True and row["time"] > 0


def test_webos_shows_a_requested_message_unconfirmed_and_drops_an_unrequested_one(
    rig: Rig,
) -> None:
    body = go(tools.do_toast("family-lg", "Hello.", "assistant", True, "", "family"))
    assert rig.webos == ["Hello."] and "shown" not in body
    assert messages.history("family-lg")[0]["shown"] is False
    body = go(tools.do_toast("family-lg", "Hello.", "operator"))
    assert body["dropped"] == "unsupported" and body["shown"] is False
    assert rig.webos == ["Hello."]


def test_the_toast_tool_is_allowed_over_playback(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def never(device: str, force: bool) -> dict[str, Any]:
        raise AssertionError("toast must not refuse for in_use")

    monkeypatch.setattr(tools, "in_use_refusal", never)
    body = go(
        tools.toast(
            "family-shield",
            "Hello.",
            sender="assistant",
            requested=True,
            household="family",
        )
    )
    assert body["shown"] is True
