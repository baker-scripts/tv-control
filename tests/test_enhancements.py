"""Offline and wrong-address alerts, favorites, and the Home Assistant volume route. Mocks only."""

import asyncio
import io
import urllib.error
from pathlib import Path
from typing import Any, Self

import pytest

import adbops
import appcache
import haremote
import panel
import sleeptimer
import tools
from inventory import DeviceError

SHIELD: dict[str, Any] = {
    "name": "shield-a",
    "type": "shield",
    "ip": "192.0.2.1",
    "site": "home",
    "port": 5555,
    "capabilities": ["sleep_timer"],
}
SOUNDBAR: dict[str, Any] = {
    **SHIELD,
    "name": "bar-shield",
    "volume_via": "ha_remote",
    "ha_remote": {
        "entity": "remote.soundbar",
        "commands": {"up": "vol_up", "down": "vol_down", "mute": "mute"},
    },
}
DEVICES: dict[str, dict[str, Any]] = {}


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(appcache, "CACHE_FILE", tmp_path / "cache.json")
    monkeypatch.setattr(sleeptimer, "TIMERS_FILE", tmp_path / "timers.json")
    monkeypatch.setattr(adbops, "_addrs", {})
    DEVICES.clear()
    DEVICES.update({d["name"]: d for d in (SHIELD, SOUNDBAR)})

    async def get_device(name: str) -> dict[str, Any]:
        if name not in DEVICES:
            raise DeviceError(f"unknown device {name!r}")
        return DEVICES[name]

    async def denied() -> list[str]:
        return ["netflix"]

    monkeypatch.setattr(tools, "get_device", get_device)
    monkeypatch.setattr(tools, "load_denied_apps", denied)
    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)


# ---------- offline and wrong-address alert ----------

IP_OUT = (
    "1: lo    inet 127.0.0.1/8 scope host lo\n"
    "3: wlan0    inet 192.0.2.40/24 brd 192.0.2.255 scope global wlan0\n"
    "9: tun0    inet 100.100.1.2/32 scope global tun0\n"
)


def test_lan_addresses_skip_loopback_and_the_tailnet_range() -> None:
    assert adbops.lan_addresses(IP_OUT) == ["192.0.2.40"]
    assert adbops.lan_addresses("") == []


def _read_returns(monkeypatch: pytest.MonkeyPatch, text: str) -> list[str]:
    calls: list[str] = []

    async def read(_: str, command: str) -> str:
        calls.append(command)
        return text

    monkeypatch.setattr(adbops, "read", read)
    return calls


def test_ip_alert_flags_a_device_that_holds_a_different_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _read_returns(monkeypatch, IP_OUT)
    alert = run(adbops.ip_alert(SHIELD, "192.0.2.40:5555"))
    assert alert is not None
    assert "192.0.2.40" in alert and "devices.yml says 192.0.2.1" in alert


def test_ip_alert_is_quiet_when_the_address_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _read_returns(monkeypatch, IP_OUT.replace("192.0.2.40", "192.0.2.1"))
    assert run(adbops.ip_alert(SHIELD, "192.0.2.1:5555")) is None


def test_ip_alert_names_a_fallback_path_when_the_device_reports_no_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _read_returns(monkeypatch, "")
    alert = run(adbops.ip_alert(SHIELD, "shield-a.example.net:5555"))
    assert alert is not None and "192.0.2.1 is not answering" in alert
    assert run(adbops.ip_alert({**SHIELD, "ip": ""}, "x:5555")) is None


def test_ip_alert_reads_the_device_once_per_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _read_returns(monkeypatch, IP_OUT)
    run(adbops.ip_alert(SHIELD, "192.0.2.40:5555"))
    run(adbops.ip_alert(SHIELD, "192.0.2.40:5555"))
    assert len(calls) == 1


def test_ip_alert_survives_a_failed_read(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*_: Any) -> str:
        raise DeviceError("closed")

    monkeypatch.setattr(adbops, "read", boom)
    assert run(adbops.ip_alert(SHIELD, "192.0.2.1:5555")) is None


# ---------- Home Assistant volume ----------


def _ha(
    monkeypatch: pytest.MonkeyPatch, state: tuple[int, Any], post: int = 200
) -> list[tuple[str, dict[str, Any]]]:
    monkeypatch.setenv("HA_URL", "http://ha.example:8123/")
    monkeypatch.setenv("HA_TOKEN", "t")
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(haremote, "_get", lambda url, token: state)

    def fake_post(url: str, token: str, payload: dict[str, Any]) -> tuple[int, Any]:
        posted.append((url, payload))
        return post, None

    monkeypatch.setattr(haremote, "_post", fake_post)
    return posted


def test_ha_volume_sends_the_learned_command(monkeypatch: pytest.MonkeyPatch) -> None:
    posted = _ha(monkeypatch, (200, {"state": "on"}))
    body = run(tools.do_key("bar-shield", "VOLUME_UP"))
    assert body["via"] == "ha_remote:remote.soundbar" and body["sent"] == "VOLUME_UP"
    assert posted == [
        (
            "http://ha.example:8123/api/services/remote/send_command",
            {"entity_id": "remote.soundbar", "command": ["vol_up"]},
        )
    ]


def test_ha_volume_passes_the_learned_device_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted = _ha(monkeypatch, (200, {"state": "on"}))
    dev = {**SOUNDBAR, "ha_remote": {**SOUNDBAR["ha_remote"], "device": "vizio"}}
    run(haremote.send_key(dev, "VOLUME_MUTE"))
    assert posted[0][1]["device"] == "vizio" and posted[0][1]["command"] == ["mute"]


def test_ha_volume_never_falls_back_to_another_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_other_route(*_: Any) -> Any:
        raise AssertionError("must not use the Shield remote or ADB")

    monkeypatch.setattr(tools.tvremote, "send_key", no_other_route)
    monkeypatch.setattr(tools.adbops, "send_key_fast", no_other_route)
    body = run(tools.do_key("bar-shield", "VOLUME_DOWN"))
    assert "Home Assistant is not configured" in body["error"]


@pytest.mark.parametrize(
    "state,needle",
    [
        ((404, None), "does not exist"),
        ((200, {"state": "unavailable"}), "not available"),
        ((500, None), "not available"),
    ],
)
def test_ha_volume_fails_safe_when_the_entity_is_missing(
    monkeypatch: pytest.MonkeyPatch, state: tuple[int, Any], needle: str
) -> None:
    posted = _ha(monkeypatch, state)
    body = run(tools.do_key("bar-shield", "VOLUME_UP"))
    assert needle in body["error"] and body["next_step"]
    assert posted == []


def test_ha_volume_reports_a_failed_send(monkeypatch: pytest.MonkeyPatch) -> None:
    _ha(monkeypatch, (200, {"state": "on"}), post=500)
    assert "answered 500" in run(tools.do_key("bar-shield", "VOLUME_UP"))["error"]


@pytest.mark.parametrize(
    "block",
    [
        None,
        {"entity": "light.x", "commands": {}},
        {"entity": "remote.x", "commands": {"up": "a", "down": "b"}},
    ],
)
def test_ha_config_errors_refuse_before_any_call(block: Any) -> None:
    dev = {**SOUNDBAR, "ha_remote": block}
    assert haremote.config_error(dev)
    with pytest.raises(DeviceError, match="ha_remote"):
        run(haremote.send_key(dev, "VOLUME_UP"))
    route, why = tools.volume_path(dev)
    assert route is None and why


def test_ha_volume_route_is_named_in_the_volume_path() -> None:
    route, why = tools.volume_path(SOUNDBAR)
    assert why is None and route is not None and "remote.soundbar" in route


def test_ha_transport_errors_are_device_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: Any, **__: Any) -> Any:
        raise urllib.error.URLError("down")

    monkeypatch.setattr(haremote.urllib.request, "urlopen", refuse)
    with pytest.raises(DeviceError, match="did not answer"):
        haremote._get("http://ha.example", "t")


def test_ha_http_error_status_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*_: Any, **__: Any) -> Any:
        raise urllib.error.HTTPError("u", 404, "nf", None, io.BytesIO(b""))  # type: ignore[arg-type]

    monkeypatch.setattr(haremote.urllib.request, "urlopen", missing)
    assert haremote._get("http://ha.example", "t") == (404, None)


def test_ha_ok_response_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    class Resp:
        status = 200

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"state": "on"}'

    monkeypatch.setattr(haremote.urllib.request, "urlopen", lambda *_, **__: Resp())
    assert haremote._post("http://ha.example", "t", {"a": 1}) == (200, {"state": "on"})


# ---------- favorites ----------


def test_default_favorites_are_the_installed_big_three_plus_a_timer() -> None:
    items = [
        {"id": i, "title": i}
        for i in ("com.plexapp.android", "org.smarttube.stable", "com.other")
    ]
    appcache.put("shield-a", "apps", items)
    body = run(tools.do_favorites("shield-a"))
    kinds = [(f["kind"], f.get("app") or f.get("minutes")) for f in body["favorites"]]
    assert kinds == [
        ("app", "com.plexapp.android"),
        ("app", "org.smarttube.stable"),
        ("sleep", 30),
    ]
    assert body["skipped"] == []


def test_configured_favorites_replace_the_defaults() -> None:
    DEVICES["shield-a"] = {
        **SHIELD,
        "capabilities": ["sleep_timer", "inputs"],
        "favorites": [
            {"kind": "app", "app": "com.plexapp.android", "label": "Movies"},
            {"kind": "input", "input": "HDMI_2"},
            {"kind": "sleep", "minutes": 60, "label": "Bedtime"},
        ],
    }
    body = run(tools.do_favorites("shield-a"))
    assert [(f["kind"], f["label"]) for f in body["favorites"]] == [
        ("app", "Movies"),
        ("input", "HDMI_2"),
        ("sleep", "Bedtime"),
    ]


def test_bad_favorites_are_dropped_with_a_reason() -> None:
    DEVICES["shield-a"] = {
        **SHIELD,
        "favorites": [
            {"kind": "app", "app": "com.netflix.ninja", "label": "Netflix"},
            {"kind": "app", "app": "com.example.hidden", "label": "Netflix Kids"},
            {"kind": "app"},
            {"kind": "input", "input": "HDMI_1"},
            {"kind": "sleep", "minutes": 7},
            {"kind": "sleep", "minutes": "x"},
            {"kind": "bogus"},
            "plex",
        ],
    }
    body = run(tools.do_favorites("shield-a"))
    assert body["favorites"] == []
    assert len(body["skipped"]) == 8
    assert "netflix is never offered" in body["skipped"][0]["why"]


def test_empty_or_malformed_favorites_give_no_buttons() -> None:
    values: list[Any] = [[], "plex"]
    for value in values:
        DEVICES["shield-a"] = {**SHIELD, "favorites": value}
        assert run(tools.do_favorites("shield-a"))["favorites"] == []


def test_favorites_respect_the_allowed_apps_list() -> None:
    DEVICES["shield-a"] = {
        **SHIELD,
        "allowed_apps": ["com.plexapp.android"],
        "favorites": [
            {"kind": "app", "app": "com.plexapp.android"},
            {"kind": "app", "app": "org.smarttube.stable"},
        ],
    }
    body = run(tools.do_favorites("shield-a"))
    assert [f["app"] for f in body["favorites"]] == ["com.plexapp.android"]
    assert "allowlist" in body["skipped"][0]["why"]


def test_unknown_device_favorites_answer_an_error() -> None:
    assert "error" in run(tools.do_favorites("nope"))


def test_favorites_are_a_read_for_site_users_only() -> None:
    assert "favorites" in panel.USER_GET_VERBS
    assert "favorites" not in panel.USER_POST_VERBS


def test_card_has_alert_and_favorites_slots_and_the_dialog_line() -> None:
    card = panel._card({"name": "mock-tv", "type": "firetv", "site": "home"})
    for marker in (
        'class="reach-alert"',
        'class="ip-alert"',
        'class="notice-now"',
        'class="row favorites"',
    ):
        assert marker in card


def test_js_wires_favorites_alerts_and_the_dialog_line() -> None:
    js = (Path(__file__).resolve().parents[1] / "static" / "panel.js").read_text()
    assert "request(card,'favorites')" in js
    assert "reachAlert(card," in js
    assert "'On now: '+nowText(st.now)" in js
