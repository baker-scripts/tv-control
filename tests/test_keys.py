"""Fast key path: no device lock, Remote v2 first, persistent adb shell fallback. Mocks only."""

import asyncio
from pathlib import Path
from typing import Any

import adbops
import inventory
import pytest
import tools
import tvremote
from androidtvremote2 import CannotConnect
from inventory import DeviceError

SHIELD = {"name": "shield-a", "type": "shield", "ip": "192.0.2.1", "port": 5555}
FIRE = {"name": "fire-a", "type": "firetv", "ip": "192.0.2.3", "port": 5555}


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeRemote:
    instances: list["FakeRemote"] = []
    fail_connect = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.keys: list[str] = []
        self.reconnecting = False
        self.disconnected = False
        FakeRemote.instances.append(self)

    async def async_connect(self) -> None:
        if FakeRemote.fail_connect:
            raise CannotConnect

    def keep_reconnecting(self) -> None:
        self.reconnecting = True

    def send_key_command(self, key: str) -> None:
        self.keys.append(key)

    def disconnect(self) -> None:
        self.disconnected = True

    async def async_generate_cert_if_missing(self) -> bool:
        return True

    async def async_start_pairing(self) -> None:
        return None

    async def async_finish_pairing(self, code: str) -> None:
        self.code = code


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    FakeRemote.instances = []
    FakeRemote.fail_connect = False
    monkeypatch.setattr(tvremote, "AndroidTVRemote", FakeRemote)
    monkeypatch.setattr(tvremote, "REMOTE_DIR", tmp_path / "remote")
    for table in (tvremote._remotes, tvremote._pairing, tvremote._retry_at):
        table.clear()
    adbops._key_shells.clear()
    devices = {d["name"]: d for d in (SHIELD, FIRE)}

    async def get_device(name: str) -> dict[str, Any]:
        return devices[name]

    monkeypatch.setattr(tools, "get_device", get_device)


def pair(dev: dict[str, Any]) -> None:
    run(tvremote.start_pair(dev))
    run(tvremote.finish_pair(dev, " 1234 "))


def test_unpaired_shield_returns_none() -> None:
    assert run(tvremote.send_key(SHIELD, "DPAD_UP")) is None
    assert FakeRemote.instances == []


def test_pairing_flow_marks_device_and_trims_code() -> None:
    assert run(tvremote.start_pair(SHIELD))["pairing"] == "started"
    assert run(tvremote.finish_pair(SHIELD, " 1234 "))["paired"] is True
    assert tvremote.is_paired(SHIELD)
    assert FakeRemote.instances[0].code == "1234"
    with pytest.raises(DeviceError):
        run(tvremote.finish_pair(SHIELD, "1"))


def test_fire_tv_cannot_pair_remote() -> None:
    with pytest.raises(DeviceError):
        run(tvremote.start_pair(FIRE))


def test_paired_shield_reuses_one_connection() -> None:
    pair(SHIELD)
    FakeRemote.instances.clear()
    assert run(tvremote.send_key(SHIELD, "DPAD_UP"))["via"] == "remote"
    assert run(tvremote.send_key(SHIELD, "BACK"))["via"] == "remote"
    assert len(FakeRemote.instances) == 1
    assert FakeRemote.instances[0].keys == ["DPAD_UP", "BACK"]
    assert FakeRemote.instances[0].reconnecting


def test_power_keys_stay_on_adb() -> None:
    pair(SHIELD)
    assert run(tvremote.send_key(SHIELD, "SLEEP")) is None
    assert run(tvremote.send_key(SHIELD, "WAKEUP")) is None


def test_connect_failure_falls_back_and_backs_off() -> None:
    pair(SHIELD)
    FakeRemote.instances.clear()
    FakeRemote.fail_connect = True
    assert run(tvremote.send_key(SHIELD, "HOME")) is None
    FakeRemote.fail_connect = False
    assert run(tvremote.send_key(SHIELD, "HOME")) is None
    assert len(FakeRemote.instances) == 1
    tvremote._retry_at.clear()
    assert run(tvremote.send_key(SHIELD, "HOME"))["via"] == "remote"


def test_do_key_does_not_wait_for_device_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []

    async def fast(dev: dict[str, Any], key: str) -> dict[str, Any]:
        sent.append(key)
        return {"sent": key}

    monkeypatch.setattr(adbops, "send_key_fast", fast)

    async def scenario() -> dict[str, Any]:
        async with inventory.device_lock("fire-a"):
            return await asyncio.wait_for(tools.do_key("fire-a", "up"), 1)

    assert run(scenario()) == {"device": "fire-a", "sent": "DPAD_UP"}
    assert sent == ["DPAD_UP"]


def test_do_key_prefers_remote_then_adb(monkeypatch: pytest.MonkeyPatch) -> None:
    pair(SHIELD)
    used: list[str] = []

    async def fast(dev: dict[str, Any], key: str) -> dict[str, Any]:
        used.append("adb")
        return {"sent": key, "via": "adb shell"}

    monkeypatch.setattr(adbops, "send_key_fast", fast)
    assert run(tools.do_key("shield-a", "HOME"))["via"] == "remote"
    assert run(tools.do_key("shield-a", "SLEEP"))["via"] == "adb shell"
    assert used == ["adb"]


class FakeStdin:
    def __init__(self, broken: bool = False) -> None:
        self.lines: list[bytes] = []
        self.broken = broken

    def write(self, data: bytes) -> None:
        if self.broken:
            raise BrokenPipeError
        self.lines.append(data)

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return False


class FakeProc:
    def __init__(self, broken: bool = False) -> None:
        self.stdin = FakeStdin(broken)
        self.returncode = None


def test_adb_key_shell_is_persistent_and_replaced_when_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    procs = [FakeProc(broken=True), FakeProc(), FakeProc()]
    spawned: list[tuple[Any, ...]] = []

    async def spawn(*argv: Any, **kwargs: Any) -> FakeProc:
        spawned.append(argv)
        return procs[len(spawned) - 1]

    async def require(dev: dict[str, Any]) -> str:
        return "192.0.2.3:5555"

    monkeypatch.setattr(adbops.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(adbops, "require_device", require)
    assert run(adbops.send_key_fast(FIRE, "DPAD_UP"))["sent"] == "DPAD_UP"
    assert len(spawned) == 2
    assert procs[1].stdin.lines == [b"input keyevent KEYCODE_DPAD_UP\n"]
    run(adbops.send_key_fast(FIRE, "BACK"))
    assert len(spawned) == 2
    assert procs[1].stdin.lines[-1] == b"input keyevent KEYCODE_BACK\n"
    assert spawned[0][:4] == ("adb", "-s", "192.0.2.3:5555", "shell")


def test_adb_key_shell_rejects_unknown_key() -> None:
    with pytest.raises(DeviceError):
        run(adbops.send_key_fast(FIRE, "REBOOT; rm -rf /"))
