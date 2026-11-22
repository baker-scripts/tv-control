"""open_app, go_home, and play_title with a fake screen: no device is contacted."""

import asyncio
from typing import Any

import adbops
import pytest
import tools
from inventory import DeviceError

SHIELD = {"name": "shield-a", "type": "shield", "ip": "192.0.2.1", "port": 5555}
LG = {"name": "lg-a", "type": "webos", "ip": "192.0.2.2"}
PLEX = "com.plexapp.android"
DREAM = "com.neilturner.aerialviews"
MID = "a" * 40


def go(coro: Any) -> Any:
    return asyncio.run(coro)


class Screen:
    """Fake device: launches and VIEW intents change the package in front after `lag` reads."""

    def __init__(self, front: str = tools.SHIELD_HOME_APP, lag: int = 1) -> None:
        self.front = front
        self.pending: str | None = None
        self.lag = lag
        self.state = "none"
        self.play_after_view = "playing"
        self.calls: list[str] = []
        # Screensaver: while dreaming the dream app stays in front and launches wait behind it.
        self.awake = True
        self.wakes = True

    async def current_screen(
        self, dev: dict[str, Any], focus: bool = True
    ) -> dict[str, Any]:
        self.calls.append("screen")
        if self.pending and self.awake:
            self.lag -= 1
            if self.lag <= 0:
                self.front, self.pending = self.pending, None
        front = self.front if self.awake else DREAM
        sessions = [{"package": front, "state": self.state, "title": "The Matrix"}]
        return {
            "package": front,
            "awake": self.awake,
            "wakefulness": "Awake" if self.awake else "Dreaming",
            "sessions": sessions,
            "playing": self.state == "playing",
        }

    async def send_key(self, dev: dict[str, Any], key: str) -> dict[str, Any]:
        self.calls.append(f"key {key}")
        if key == "WAKEUP" and self.wakes:
            self.awake = True
        return {"sent": key}

    async def launch(self, dev: dict[str, Any], pkg: str) -> dict[str, Any]:
        self.calls.append(f"launch {pkg}")
        self.pending = pkg
        return {"launched": pkg, "via": "monkey"}

    async def view(self, dev: dict[str, Any], uri: str, pkg: str) -> dict[str, Any]:
        self.calls.append(f"view {uri} {pkg}")
        self.pending = pkg
        self.state = self.play_after_view
        return {"started": uri}


@pytest.fixture
def screen(monkeypatch: pytest.MonkeyPatch) -> Screen:
    fake = Screen()
    devices = {d["name"]: d for d in (SHIELD, LG)}

    async def get_device(name: str) -> dict[str, Any]:
        if name not in devices:
            raise DeviceError(f"unknown device {name!r}")
        return devices[name]

    async def denied() -> list[str]:
        return ["netflix"]

    async def screen_fn(dev: dict[str, Any], focus: bool = True) -> dict[str, Any]:
        return await fake.current_screen(dev, focus)

    monkeypatch.setattr(tools, "get_device", get_device)
    monkeypatch.setattr(tools, "load_denied_apps", denied)
    monkeypatch.setattr(adbops, "current_screen", screen_fn)
    monkeypatch.setattr(adbops, "launch", fake.launch)
    monkeypatch.setattr(adbops, "view_uri", fake.view)
    monkeypatch.setattr(adbops, "send_key", fake.send_key)
    monkeypatch.setattr(tools, "CONFIRM_POLL_S", 0)
    monkeypatch.setattr(tools, "_opened", {})
    return fake


def test_open_app_confirms_the_app_is_in_front(screen: Screen) -> None:
    body = go(tools.do_open_app("shield-a", PLEX))
    assert body["confirmed"] is True and body["package"] == PLEX
    assert tools._opened["shield-a"][0] == PLEX
    assert f"launch {PLEX}" in screen.calls
    assert "key WAKEUP" not in screen.calls


def test_open_app_wakes_a_dreaming_device_before_the_launch(screen: Screen) -> None:
    screen.awake = False
    body = go(tools.do_open_app("shield-a", PLEX))
    assert body["confirmed"] is True and body["package"] == PLEX
    assert screen.calls.index("key WAKEUP") < screen.calls.index(f"launch {PLEX}")


def test_confirm_wait_is_longer_after_a_wake(
    screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "CONFIRM_TIMEOUT_S", 0)
    monkeypatch.setattr(tools, "WOKE_CONFIRM_TIMEOUT_S", 5)
    screen.awake = False
    screen.lag = 3
    assert go(tools.do_open_app("shield-a", PLEX))["confirmed"] is True


def test_device_that_stays_asleep_reports_not_awake(
    screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "WAKE_TIMEOUT_S", 0)
    screen.awake = False
    screen.wakes = False
    body = go(tools.do_open_app("shield-a", PLEX))
    assert "not_awake" in body["error"]
    assert f"launch {PLEX}" not in screen.calls


@pytest.mark.parametrize(
    "call",
    [
        lambda: tools.do_go_home("shield-a"),
        lambda: tools.do_play_title("shield-a", MID, "1"),
        lambda: tools.do_navigate("shield-a", "DPAD_DOWN", tools.SHIELD_HOME_APP),
    ],
)
def test_navigation_verbs_wake_a_dreaming_device_first(
    screen: Screen, call: Any
) -> None:
    screen.awake = False
    body = go(call())
    assert "error" not in body, body
    assert screen.calls[1] == "key WAKEUP"


def test_open_app_errors_when_the_app_never_comes_to_front(
    screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "CONFIRM_TIMEOUT_S", 0)
    screen.lag = 99
    body = go(tools.do_open_app("shield-a", PLEX))
    assert "not_confirmed" in body["error"] and tools.SHIELD_HOME_APP in body["error"]


def test_open_app_refuses_netflix_before_any_device_call(screen: Screen) -> None:
    body = go(tools.do_open_app("shield-a", "com.netflix.ninja"))
    assert "denylist" in body["error"] and screen.calls == []


def test_open_app_is_android_only(screen: Screen) -> None:
    assert "unsupported" in go(tools.do_open_app("lg-a", "plex"))["error"]


def test_go_home_opens_projectivy_and_confirms(screen: Screen) -> None:
    screen.front = PLEX
    body = go(tools.do_go_home("shield-a"))
    assert body["confirmed"] is True and body["package"] == tools.SHIELD_HOME_APP


def test_play_title_builds_the_deep_link_and_waits_for_playback(screen: Screen) -> None:
    body = go(tools.do_play_title("shield-a", MID, "12345"))
    uri = f"plex://server://{MID}/com.plexapp.plugins.library/library/metadata/12345?autoPlay=1"
    assert screen.calls[:2] == ["screen", f"view {uri} {PLEX}"]
    assert body["playing"] is True and body["title"] == "The Matrix"


def test_play_title_errors_when_playback_never_starts(
    screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "PLAY_TIMEOUT_S", 0)
    screen.play_after_view = "paused"
    body = go(tools.do_play_title("shield-a", MID, "12345"))
    assert "not_playing" in body["error"]


@pytest.mark.parametrize(
    ("mid", "key"),
    [("bad'id", "1"), (MID, "12a"), ("", "1"), (MID, ""), (MID + "/x", "1")],
)
def test_play_title_validates_ids(screen: Screen, mid: str, key: str) -> None:
    body = go(tools.do_play_title("shield-a", mid, key))
    assert "error" in body and screen.calls == []


@pytest.mark.parametrize(
    "call",
    [
        lambda: tools.open_app("shield-a", PLEX),
        lambda: tools.go_home("shield-a"),
        lambda: tools.play_title("shield-a", MID, "1"),
    ],
)
def test_navigation_tools_keep_the_in_use_refusal(
    monkeypatch: pytest.MonkeyPatch, screen: Screen, call: Any
) -> None:
    async def busy(device: str, force: bool) -> dict[str, Any] | None:
        return None if force else {"device": device, "error": "in_use: watching"}

    monkeypatch.setattr(tools, "in_use_refusal", busy)
    assert "in_use" in go(call())["error"]
    assert screen.calls == []


def test_view_uri_quotes_the_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_run(*argv: str, **_: Any) -> tuple[int, bytes, str]:
        calls.append(" ".join(argv))
        return 0, b"device" if "get-state" in argv else b"Starting: Intent", ""

    monkeypatch.setattr(adbops, "run", fake_run)
    monkeypatch.setattr(adbops, "_active", {})
    monkeypatch.setattr(adbops, "_ready", {})
    go(adbops.view_uri(SHIELD, "plex://a?b=1", PLEX))
    assert calls[-1].endswith(
        "shell am start -a android.intent.action.VIEW -d 'plex://a?b=1' -p com.plexapp.android"
    )
    with pytest.raises(DeviceError, match="invalid deep link"):
        go(adbops.view_uri(SHIELD, "plex://a';reboot", PLEX))
