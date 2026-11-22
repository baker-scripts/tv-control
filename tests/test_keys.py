"""Fast key path: no device lock, Remote v2 first, persistent adb shell fallback. Mocks only."""

import asyncio
from pathlib import Path
from typing import Any, ClassVar

import pytest
from androidtvremote2 import CannotConnect

import adbops
import inventory
import tools
import tvremote
from inventory import DeviceError

SHIELD = {"name": "shield-a", "type": "shield", "ip": "192.0.2.1", "port": 5555}
FIRE = {"name": "fire-a", "type": "firetv", "ip": "192.0.2.3", "port": 5555}


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeRemote:
    instances: ClassVar[list["FakeRemote"]] = []
    fail_connect = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.keys: list[str] = []
        self.reconnecting = False
        self.disconnected = False
        self.callbacks: list[Any] = []
        FakeRemote.instances.append(self)

    async def async_connect(self) -> None:
        if FakeRemote.fail_connect:
            raise CannotConnect

    def add_is_available_updated_callback(self, callback: Any) -> None:
        self.callbacks.append(callback)

    def set_available(self, ok: bool) -> None:
        for callback in self.callbacks:
            callback(ok)

    current_app = "com.example.app"
    texts: list[str]

    def send_text(self, text: str) -> None:
        if not hasattr(self, "texts"):
            self.texts = []
        self.texts.append(text)

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
    for table in (
        tvremote._remotes,
        tvremote._pairing,
        tvremote._retry_at,
        tvremote._available,
    ):
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


def test_warm_opens_one_connection_only_when_paired() -> None:
    async def warm_twice() -> None:
        tvremote.warm(SHIELD)
        await asyncio.gather(*tvremote._warming)
        tvremote.warm(SHIELD)
        await asyncio.gather(*tvremote._warming)

    run(warm_twice())
    assert FakeRemote.instances == []
    pair(SHIELD)
    FakeRemote.instances.clear()
    run(warm_twice())
    assert len(FakeRemote.instances) == 1
    assert "shield-a" in tvremote._remotes


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


def test_volume_keys_use_remote_when_paired() -> None:
    pair(SHIELD)
    for key in ("VOLUME_UP", "VOLUME_DOWN", "VOLUME_MUTE"):
        assert tvremote.should_use_remote(SHIELD, key)
        assert run(tvremote.send_key(SHIELD, key))["via"] == "remote"


def test_volume_keys_fall_back_to_adb_when_unpaired() -> None:
    assert run(tvremote.send_key(SHIELD, "VOLUME_UP")) is None
    assert not tvremote.should_use_remote(SHIELD, "VOLUME_UP")


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


def test_unavailable_connection_falls_back_then_resumes_after_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair(SHIELD)
    FakeRemote.instances.clear()
    assert run(tvremote.send_key(SHIELD, "HOME"))["via"] == "remote"
    remote = FakeRemote.instances[-1]
    remote.set_available(False)
    monkeypatch.setattr(tvremote, "RECONNECT_WAIT_S", 0.1)
    assert run(tvremote.send_key(SHIELD, "HOME")) is None
    assert remote.keys == ["HOME"]
    assert not remote.disconnected

    async def scenario() -> Any:
        task = asyncio.create_task(tvremote.send_key(SHIELD, "BACK"))
        await asyncio.sleep(0.05)
        remote.set_available(True)
        return await task

    monkeypatch.setattr(tvremote, "RECONNECT_WAIT_S", 1.0)
    assert run(scenario())["via"] == "remote"
    assert remote.keys == ["HOME", "BACK"]
    assert len(FakeRemote.instances) == 1


def test_send_failure_reports_adb_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    pair(SHIELD)
    run(tvremote.send_key(SHIELD, "HOME"))

    def boom(self: FakeRemote, key: str) -> None:
        raise tvremote.ConnectionClosed("closed")

    monkeypatch.setattr(FakeRemote, "send_key_command", boom)

    async def fast(dev: dict[str, Any], key: str) -> dict[str, Any]:
        return {"sent": key, "via": "adb shell"}

    monkeypatch.setattr(adbops, "send_key_fast", fast)
    assert run(tools.do_key("shield-a", "BACK"))["via"] == "adb-fallback"
    assert FakeRemote.instances[-1].disconnected


def test_fast_key_state_follows_pairing_and_rejected_cert() -> None:
    assert tvremote.fast_key_state(FIRE) == "unsupported"
    assert tvremote.fast_key_state(SHIELD) == "needs_pairing"
    pair(SHIELD)
    assert tvremote.fast_key_state(SHIELD) == "ready"
    tvremote._auth_failed.add("shield-a")
    assert tvremote.fast_key_state(SHIELD) == "needs_pairing"
    assert run(tvremote.send_key(SHIELD, "HOME")) is None
    run(tvremote.start_pair(SHIELD))
    run(tvremote.finish_pair(SHIELD, "1234"))
    assert tvremote.fast_key_state(SHIELD) == "ready"


def test_rejected_cert_on_connect_marks_pairing_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair(SHIELD)

    async def refuse(self: FakeRemote) -> None:
        raise tvremote.InvalidAuth

    monkeypatch.setattr(FakeRemote, "async_connect", refuse)
    assert run(tvremote.send_key(SHIELD, "HOME")) is None
    assert tvremote.fast_key_state(SHIELD) == "needs_pairing"


def test_bad_pin_reports_error_and_stays_unpaired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run(tvremote.start_pair(SHIELD))

    async def reject(self: FakeRemote, code: str) -> None:
        raise tvremote.InvalidAuth

    monkeypatch.setattr(FakeRemote, "async_finish_pairing", reject)
    with pytest.raises(DeviceError):
        run(tvremote.finish_pair(SHIELD, "0000"))
    assert not tvremote.is_paired(SHIELD)


def test_pair_start_and_finish_verbs_drive_pairing() -> None:
    assert run(tools.pair_start("shield-a"))["pairing"] == "started"
    assert run(tools.pair_finish("shield-a", "1234"))["paired"] is True
    assert tvremote.is_paired(SHIELD)


def test_send_text_uses_remote_when_paired() -> None:
    pair(SHIELD)
    FakeRemote.instances.clear()
    assert run(tools.do_text("shield-a", "hello"))["via"] == "remote"
    assert FakeRemote.instances[0].texts == ["hello"]
    assert tvremote.remote_app("shield-a") == "com.example.app"


def test_send_text_falls_back_to_adb_input_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[str] = []

    async def require(dev: dict[str, Any]) -> str:
        return "192.0.2.1:5555"

    async def shell(serial: str, command: str) -> str:
        ran.append(command)
        return ""

    monkeypatch.setattr(adbops, "require_device", require)
    monkeypatch.setattr(adbops, "shell", shell)
    assert run(tools.do_text("shield-a", "two words"))["via"] == "adb input text"
    assert ran == ["input text 'two%swords'"]
    pair(SHIELD)
    FakeRemote.instances.clear()
    tvremote._retry_at["shield-a"] = float("inf")
    assert run(tools.do_text("shield-a", "x"))["via"] == "adb input text"


def test_send_text_rejects_shell_metacharacters() -> None:
    with pytest.raises(DeviceError):
        run(adbops.send_text(SHIELD, "a'; reboot #"))


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
    assert run(tools.do_key("shield-a", "BACK"))["via"] == "remote"
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


def test_screenshot_and_status_do_not_block_presses(
    monkeypatch: pytest.MonkeyPatch, policy: dict[str, Any], tmp_path: Path
) -> None:
    async def slow_shot(dev: dict[str, Any]) -> tuple[bytes, None]:
        await asyncio.sleep(0.3)
        return b"x", None

    async def toast(dev: dict[str, Any], message: str, **kw: Any) -> dict[str, Any]:
        return {"toast": message}

    async def load_policy() -> dict[str, Any]:
        return policy

    async def current_screen(dev: dict[str, Any], focus: bool = True) -> dict[str, Any]:
        return {"awake": True}

    monkeypatch.setattr(adbops, "screenshot", slow_shot)
    monkeypatch.setattr(adbops, "toast", toast)
    monkeypatch.setattr(adbops, "current_screen", current_screen)
    monkeypatch.setattr(tools, "load_message_policy", load_policy)
    monkeypatch.setattr(tools.messages, "LOG_DIR", tmp_path / "messages")

    async def scenario() -> dict[str, Any]:
        shot = asyncio.create_task(tools.do_screenshot("fire-a"))
        await asyncio.sleep(0.05)
        result = await asyncio.wait_for(
            tools.do_toast("fire-a", "hi", requested=True), 0.2
        )
        await shot
        return result

    assert run(scenario())["toast"] == "hi"
