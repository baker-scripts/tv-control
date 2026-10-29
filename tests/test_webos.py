"""webos against a fake WebOsClient, and the MCP tool wrappers against patched do_* verbs."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
import pytest
import tools
import webos
from inventory import DeviceError

DEV = {"name": "lg-a", "type": "webos", "ip": "192.0.2.2", "site": "home"}


class FakeTv:
    instances: list["FakeTv"] = []
    fail: type[BaseException] | None = None

    def __init__(self, ip: str, key: str | None) -> None:
        self.client_key = "new-key"  # pragma: allowlist secret
        self.sent: list[tuple[str, Any]] = []
        self.tv_state = SimpleNamespace(
            is_on=True,
            is_screen_on=True,
            power_state={"state": "Active"},
            current_app_id="hdmi2",
            apps={"plex": {"title": "Plex"}, "abc": {}},
            media_state=True,
            muted=False,
            volume=7,
        )
        FakeTv.instances.append(self)

    async def connect(self) -> None:
        if FakeTv.fail:
            raise FakeTv.fail()

    async def disconnect(self) -> None:
        return None

    async def get_inputs(self) -> list[dict[str, str]]:
        return [{"id": "HDMI_2", "label": "Shield", "appId": "hdmi2"}]

    async def button(self, name: str) -> None:
        self.sent.append(("button", name))

    async def volume_up(self) -> None:
        self.sent.append(("volume_up", None))

    async def volume_down(self) -> None:
        self.sent.append(("volume_down", None))

    async def set_mute(self, value: bool) -> None:
        self.sent.append(("mute", value))

    async def power_off(self) -> None:
        self.sent.append(("power_off", None))

    async def set_input(self, input_id: str) -> None:
        self.sent.append(("input", input_id))

    async def launch_app(self, app_id: str) -> None:
        self.sent.append(("launch", app_id))

    async def send_message(self, text: str) -> None:
        self.sent.append(("toast", text))


@pytest.fixture(autouse=True)
def fake_tv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    FakeTv.instances, FakeTv.fail = [], None
    monkeypatch.setattr(webos, "WebOsClient", FakeTv)
    monkeypatch.setattr(webos, "WEBOS_KEYS_FILE", tmp_path / "keys.json")
    monkeypatch.setattr(webos, "RETRY_DELAY_S", 0)


def go(coro: Any) -> Any:
    return asyncio.run(coro)


def paired() -> None:
    webos.write_key("lg-a", "k")


def test_keys_roundtrip() -> None:
    assert webos.read_keys() == {} and not webos.is_paired("lg-a")
    paired()
    assert webos.is_paired("lg-a")


def test_unpaired_call_is_refused_and_status_says_so() -> None:
    with pytest.raises(DeviceError, match="pairing pending"):
        go(webos.launch(DEV, "plex"))
    assert go(webos.status(DEV))["webos_state"] == "not_paired"


def test_pair_stores_key_and_reports_failure() -> None:
    assert go(webos.pair(DEV)) == {"paired": True}
    assert webos.read_keys()["lg-a"] == "new-key"
    FakeTv.fail = OSError
    with pytest.raises(DeviceError, match="pairing failed"):
        go(webos.pair(DEV))


def test_status_reports_unknown_configured_input() -> None:
    paired()
    body = go(webos.status({**DEV, "inputs": {"HDMI_2": "Shield", "HDMI_9": "X"}}))
    assert body["current_input"] == "HDMI_2" and body["volume"] == 7
    assert list(body["input_errors"]) == ["HDMI_9"]


def test_status_unreachable_after_retry() -> None:
    paired()
    FakeTv.fail = TimeoutError
    body = go(webos.status(DEV))
    assert body["webos_state"] == "unreachable" and len(FakeTv.instances) == 2


def test_ws_close_frame_is_retried() -> None:
    paired()
    FakeTv.fail = aiohttp.WSMessageTypeError
    body = go(webos.status(DEV))
    assert body["webos_state"] == "unreachable" and len(FakeTv.instances) == 2


def test_call_wraps_unexpected_errors() -> None:
    paired()
    FakeTv.fail = ValueError
    with pytest.raises(DeviceError, match="ValueError"):
        go(webos.toast(DEV, "hi"))


@pytest.mark.parametrize(
    "key,sent",
    [
        ("HOME", ("button", "HOME")),
        ("MEDIA_PLAY_PAUSE", ("button", "PAUSE")),
        ("VOLUME_UP", ("volume_up", None)),
        ("VOLUME_DOWN", ("volume_down", None)),
        ("VOLUME_MUTE", ("mute", True)),
        ("SLEEP", ("power_off", None)),
    ],
)
def test_send_key(key: str, sent: tuple[str, Any]) -> None:
    paired()
    assert go(webos.send_key(DEV, key)) == {"sent": key}
    assert FakeTv.instances[-1].sent == [sent]


def test_send_key_unmapped_and_wakeup_routes_to_wol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paired()
    with pytest.raises(DeviceError, match="unsupported_on_device"):
        go(webos.send_key(DEV, "NOPE"))
    wake = AsyncMock(return_value={"wol_sent": "x"})
    monkeypatch.setattr(webos, "wake", wake)
    assert go(webos.send_key(DEV, "WAKEUP")) == {"wol_sent": "x"}


def test_lists_and_switching() -> None:
    paired()
    assert [a["title"] for a in go(webos.list_apps(DEV))] == ["abc", "Plex"]
    assert go(webos.list_inputs(DEV))[0]["label"] == "Shield"
    assert go(webos.set_input(DEV, "HDMI_2")) == {"input": "HDMI_2"}
    for bad in ("", "HDMI_9"):
        with pytest.raises(DeviceError):
            go(webos.set_input(DEV, bad))
    assert go(webos.launch(DEV, "plex")) == {"launched": "plex"}
    with pytest.raises(DeviceError, match="not in the TV's app list"):
        go(webos.launch(DEV, "ghost"))
    assert go(webos.toast(DEV, " hello ")) == {"toast": "hello"}
    for bad in ("", "x" * 500):
        with pytest.raises(DeviceError):
            go(webos.toast(DEV, bad))


def test_wake_preconditions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(DeviceError, match="no valid mac"):
        go(webos.wake(DEV))
    dev = {**DEV, "mac": "00:00:5e:00:53:01"}
    sites: dict[str, Any] = {}
    monkeypatch.setattr(webos, "load_sites", AsyncMock(side_effect=lambda: sites))
    with pytest.raises(DeviceError, match="no wol_relay"):
        go(webos.wake(dev))
    sites["home"] = {"wol_relay": "relay@wol.example"}
    monkeypatch.setattr(webos, "WOL_KEY", tmp_path / "missing-key")
    with pytest.raises(DeviceError, match="key missing"):
        go(webos.wake(dev))


def test_wake_runs_ssh_relay(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_text("k")
    monkeypatch.setattr(webos, "WOL_KEY", key)
    monkeypatch.setattr(
        webos,
        "load_sites",
        AsyncMock(return_value={"home": {"wol_relay": "relay@wol.example"}}),
    )
    ssh = AsyncMock(return_value=(0, b"", ""))
    monkeypatch.setattr(webos, "run", ssh)
    dev = {**DEV, "mac": "00:00:5e:00:53:01"}
    assert go(webos.wake(dev)) == {
        "wol_sent": "00:00:5e:00:53:01",
        "via": "relay@wol.example",
    }
    assert ssh.await_args.args[-2:] == ("wol", "00:00:5e:00:53:01")
    ssh.return_value = (255, b"", "refused")
    with pytest.raises(DeviceError, match="refused"):
        go(webos.wake(dev))


WRAPPERS = [
    ("webos_pair", "do_pair", {"device": "d"}),
    ("send_key", "do_key", {"device": "d", "key": "HOME"}),
    ("list_apps", "do_list_apps", {"device": "d"}),
    ("list_inputs", "do_list_inputs", {"device": "d"}),
    ("set_input", "do_set_input", {"device": "d", "input_id": "i"}),
    ("wake", "do_wake", {"device": "d"}),
    ("power", "do_power", {"device": "d", "state": "on"}),
    ("find_remote", "do_find_remote", {"device": "d"}),
    ("sleep_timer", "do_sleep_timer", {"device": "d"}),
    ("launch_app", "do_launch", {"device": "d", "app": "a"}),
    ("toast", "do_toast", {"device": "d", "message": "m"}),
    ("reboot", "do_reboot", {"device": "d"}),
    ("scan_site", "do_scan", {"site": "s"}),
    ("apply_baseline", "do_baseline", {"device": "d"}),
    ("lockdown", "do_lockdown", {"device": "d"}),
    ("health", "do_health", {"device": "d"}),
]


@pytest.mark.parametrize("wrapper,verb,args", WRAPPERS)
def test_mcp_wrappers_delegate(
    monkeypatch: pytest.MonkeyPatch, wrapper: str, verb: str, args: dict[str, Any]
) -> None:
    action = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(tools, verb, action)
    monkeypatch.setattr(tools, "load_devices", AsyncMock(return_value=[]))
    assert go(getattr(tools, wrapper)(**args)) == {"ok": True}
    action.assert_awaited_once()


def test_health_all_fans_out(monkeypatch: pytest.MonkeyPatch) -> None:
    action = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(tools, "do_health", action)
    devices = [{"name": "a", "ip": "192.0.2.1"}, {"name": "b", "ip": "192.0.2.2"}]
    monkeypatch.setattr(tools, "load_devices", AsyncMock(return_value=devices))
    assert go(tools.health("all")) == {"devices": [{"ok": True}, {"ok": True}]}
