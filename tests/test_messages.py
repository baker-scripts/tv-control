"""Message policy: duration, quiet hours, narration depth, sender rules, and the per-TV history."""

from pathlib import Path
from typing import Any

import pytest
from conftest import read_policy

import messages
from inventory import DeviceError

GRANDPARENTS = {"name": "b-shield", "type": "shield", "household": "grandparents"}
FAMILY = {"name": "a-shield", "type": "shield", "household": "family"}


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("Hi", 6),
        ("one two three four five", 6),
        (" ".join(["w"] * 20), 8),
        (" ".join(["w"] * 21), 9),
        (" ".join(["w"] * 200), 20),
    ],
)
def test_duration_is_about_two_seconds_per_five_words_within_6_to_20(
    policy: dict[str, Any], text: str, seconds: int
) -> None:
    assert messages.seconds_for(text, policy) == seconds


@pytest.mark.parametrize(
    ("minutes", "quiet"),
    [
        (21 * 60 + 59, False),
        (22 * 60, True),
        (23 * 60 + 30, True),
        (0, True),
        (6 * 60 + 59, True),
        (7 * 60, False),
        (12 * 60, False),
    ],
)
def test_quiet_hours_wrap_midnight(
    policy: dict[str, Any], minutes: int, quiet: bool
) -> None:
    assert messages.in_quiet_hours(minutes, policy) is quiet


def test_parse_device_clock() -> None:
    assert messages.parse_clock("22:05\n") == 22 * 60 + 5
    assert messages.parse_clock("garbage") is None


def test_narration_depth_is_grandparents_every_step_and_start_end_elsewhere(
    policy: dict[str, Any],
) -> None:
    assert messages.narration_mode(GRANDPARENTS, policy) == "every_step"
    assert messages.narration_mode(FAMILY, policy) == "start_end"
    assert messages.narration_mode({"name": "x"}, policy) == "start_end"


def test_a_device_narration_key_overrides_its_household(policy: dict[str, Any]) -> None:
    assert (
        messages.narration_mode({**GRANDPARENTS, "narration": "off"}, policy) == "off"
    )
    with pytest.raises(DeviceError, match="narration"):
        messages.narration_mode({**GRANDPARENTS, "narration": "loud"}, policy)


def test_example_policy_names_only_known_kinds() -> None:
    policy = read_policy()
    assert set(policy["unrequested_kinds"]) == set(messages.KINDS)


def test_assistant_identity_comes_from_config() -> None:
    policy = read_policy()
    assert messages.senders(policy) == ("operator", "assistant")
    assert messages.intro_line(policy) == "Hi, this is TV Helper. I will navigate now."
    assert messages.senders({}) == ("operator", "assistant")
    custom = {"assistant": {"name": "Robin", "sender": "robin"}}
    assert messages.intro_line(custom) == "Hi, this is Robin. I will navigate now."
    assert (
        check(sender="robin", requested=True, household="family", policy=custom) == ""
    )
    with pytest.raises(DeviceError, match="not a valid sender"):
        messages.senders({"assistant": {"sender": "operator"}})


def check(**kw: Any) -> str:
    args: dict[str, Any] = {
        "dev": FAMILY,
        "sender": "operator",
        "requested": False,
        "kind": "",
        "household": "",
    }
    args["policy"] = read_policy()
    return messages.check_sender(**{**args, **kw})


def test_operator_may_push_to_any_tv_and_defaults_to_operator_push() -> None:
    assert check() == "operator_push"
    assert check(dev=GRANDPARENTS, requested=True) == ""


def test_assistant_needs_the_households_request_or_a_named_kind() -> None:
    check(sender="assistant", requested=True, household="family")
    check(sender="assistant", kind="media_ready", household="family")
    check(sender="assistant", kind="reply_waiting", household="family")
    with pytest.raises(DeviceError, match="wrong_household"):
        check(sender="assistant", requested=True, household="grandparents")
    with pytest.raises(DeviceError, match="wrong_household"):
        check(sender="assistant", requested=True)
    with pytest.raises(DeviceError, match="not_allowed"):
        check(sender="assistant", household="family")
    with pytest.raises(DeviceError, match="not_allowed"):
        check(sender="assistant", kind="weather", household="family")
    with pytest.raises(DeviceError, match="not_allowed"):
        check(sender="assistant", kind="operator_push", household="family")


def test_unknown_sender_and_bad_kind_are_refused() -> None:
    with pytest.raises(DeviceError, match="not_allowed"):
        check(sender="ha")
    with pytest.raises(DeviceError, match="not_allowed"):
        check(kind="Bad Kind!", requested=True)


@pytest.mark.parametrize(
    "image", ["https://img.example/p.jpg", "http://192.0.2.1/x.png", "mdi:movie"]
)
def test_image_accepts_documented_url_or_mdi(image: str) -> None:
    assert messages.check_image(image) == image


@pytest.mark.parametrize("image", ["data:image/png;base64,AAAA", "javascript:x", "a b"])
def test_image_refuses_anything_else(image: str) -> None:
    with pytest.raises(DeviceError, match="image"):
        messages.check_image(image)
    assert messages.check_image("") == ""


def test_history_keeps_the_last_50_newest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(messages, "LOG_DIR", tmp_path)
    for i in range(55):
        messages.record("tv", {"text": f"m{i}", "shown": i % 2 == 0}, now=1000 + i)
    rows = messages.history("tv")
    assert len(rows) == 50
    assert rows[0]["text"] == "m54" and rows[-1]["text"] == "m5"
    assert rows[0]["time"] == 1054 and rows[0]["shown"] is True
    assert messages.history("other") == []


def test_history_ignores_a_bad_log_and_unsafe_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(messages, "LOG_DIR", tmp_path / "log")
    messages.record("tv", {"text": "x"}, now=1)
    (tmp_path / "log" / "tv.json").write_text("{not json")
    assert messages.history("tv") == []
    messages.record("x/evil", {"text": "x"}, now=1)
    assert sorted(p.name for p in (tmp_path / "log").iterdir()) == ["tv.json"]
    assert messages.history("x/evil") == []
