"""Input discovery, naming, and resolution: parsers fed captured dump shapes, no device calls."""

import json
import subprocess
from typing import Any

import pytest
from conftest import DEVICES

import adbops
import inputs

# Shape of a Fire OS TV's `dumpsys tv_input` grep (AOSP TvInputManagerService.dump,
# TvInputHardwareInfo.toString, HdmiDeviceInfo.toString), with neutral package names.
ANDROID_DUMP = """\
    com.example.tuner/.TunerService: info: TvInputInfo{id=com.example.tuner/.TunerService, pkg=com.example.tuner, service=com.example.tuner.TunerService}; state: 0
    com.example.tis/.HdmiInputService/HDMI100008: info: TvInputInfo{id=com.example.tis/.HdmiInputService/HDMI100008, pkg=com.example.tis, service=com.example.tis.HdmiInputService}; state: 0
    com.example.tis/.HdmiInputService/HW3: info: TvInputInfo{id=com.example.tis/.HdmiInputService/HW3, pkg=com.example.tis, service=com.example.tis.HdmiInputService}; state: 1
    com.example.tis/.AnalogInputService/HW1: info: TvInputInfo{id=com.example.tis/.AnalogInputService/HW1, pkg=com.example.tis, service=com.example.tis.AnalogInputService}; state: 0
    com.example.tis/.HdmiInputService/HW2: info: TvInputInfo{id=com.example.tis/.HdmiInputService/HW2, pkg=com.example.tis, service=com.example.tis.HdmiInputService}; state: 0
    TvInputHardwareInfo {id=1, type=2, audio_type=0, audio_addr=, cable_connection_status=1}
    TvInputHardwareInfo {id=2, type=9, audio_type=0, audio_addr=, hdmi_port=1, cable_connection_status=2}
    TvInputHardwareInfo {id=3, type=9, audio_type=0, audio_addr=, hdmi_port=2, cable_connection_status=2}
    mDeviceInfo: CEC: logical_address: 0x00 device_type: 0 vendor_id: 1 display_name: Example TV power_status: 0 physical_address: 0x0000 port_id: -1
      CEC: logical_address: 0x08 device_type: 4 vendor_id: 2 display_name: Media Box power_status: 0 physical_address: 0x1000 port_id: 1
"""

# Shape of webOS ssap tv/getExternalInputList devices.
WEBOS_LIST = [
    {"id": "HDMI_1", "label": "HDMI 1", "port": 1, "connected": False, "subList": []},
    {
        "id": "HDMI_2",
        "label": "HDMI2",
        "port": 2,
        "appId": "com.webos.app.hdmi2",
        "connected": True,
        "subList": [{"id": "SIMPLINK", "osdName": "Media Box"}],
    },
    {
        "id": "HDMI_3",
        "label": "Game Console",
        "port": 3,
        "subList": [{"id": "URCU", "brandName": "CONSOLE", "labelName": "Console"}],
    },
]

FIRE = {"name": "tv-a", "type": "firetv", "capabilities": ["inputs"]}
LG = {"name": "tv-b", "type": "webos", "capabilities": ["inputs"]}


def test_android_parse_one_input_per_hdmi_port_named_by_cec() -> None:
    found = inputs.parse_android(ANDROID_DUMP)
    assert [i["id"] for i in found] == [
        "com.example.tis/.HdmiInputService/HW2",
        "com.example.tis/.HdmiInputService/HW3",
    ]
    first = found[0]
    assert first["port"] == 1 and first["label"] == "Media Box"
    assert first["cec_id"] == "com.example.tis/.HdmiInputService/HDMI100008"
    assert found[1]["label"] == "HDMI 2" and found[1]["state"] == "standby"


def test_android_parse_streaming_device_lists_nothing() -> None:
    dump = (
        "    tv.example.app/.TifService: info: TvInputInfo{id=tv.example.app/.TifService, "
        "pkg=tv.example.app, service=tv.example.app.TifService}; state: 0\n"
    )
    assert inputs.parse_android(dump) == []
    assert inputs.parse_android("") == []


def test_android_cec_input_without_a_port_input_stands_alone() -> None:
    dump = "\n".join(line for line in ANDROID_DUMP.splitlines() if "/HW2" not in line)
    found = inputs.parse_android(dump)
    assert found[0]["id"].endswith("/HDMI100008") and found[0]["port"] == 1


def test_webos_names_come_from_label_port_and_connected_device() -> None:
    found = inputs.from_webos(WEBOS_LIST)
    assert found[1]["names"] == ["HDMI2", "HDMI 2", "Media Box"]
    assert found[2]["device"] == "Console"
    assert inputs.from_webos([{"label": "no id"}]) == []


@pytest.mark.parametrize(
    ("query", "want"),
    [
        ("HDMI_2", "HDMI_2"),
        ("hdmi 2", "HDMI_2"),
        ("HDMI2", "HDMI_2"),
        ("media box", "HDMI_2"),
        ("Console", "HDMI_3"),
        ("game console", "HDMI_3"),
        ("Living room box", "HDMI_2"),
        ("console box", "HDMI_3"),
    ],
)
def test_resolve_spoken_names(query: str, want: str) -> None:
    dev = {
        **LG,
        "inputs": {"HDMI_2": "Living room box"},
        "input_aliases": {"console box": "HDMI_3"},
    }
    found = inputs.with_config(dev, inputs.from_webos(WEBOS_LIST))
    assert inputs.resolve(dev, query, found)["id"] == want


def test_alias_to_a_name_resolves_through_names() -> None:
    dev = {**LG, "input_aliases": {"cable box": "media box"}}
    found = inputs.with_config(dev, inputs.from_webos(WEBOS_LIST))
    assert inputs.resolve(dev, "Cable Box", found)["id"] == "HDMI_2"


def test_ambiguous_name_returns_candidates() -> None:
    raw = [*WEBOS_LIST, {"id": "HDMI_4", "label": "Media Box", "port": 4}]
    found = inputs.with_config(LG, inputs.from_webos(raw))
    with pytest.raises(inputs.InputRefusal) as err:
        inputs.resolve(LG, "media box", found)
    body = err.value.body()
    assert body["error"].startswith("ambiguous_input")
    assert {c["id"] for c in body["candidates"]} == {"HDMI_2", "HDMI_4"}


@pytest.mark.parametrize("query", ["HDMI 9", "projector", ""])
def test_unlisted_name_is_refused_with_candidates(query: str) -> None:
    found = inputs.with_config(LG, inputs.from_webos(WEBOS_LIST))
    with pytest.raises(inputs.InputRefusal) as err:
        inputs.resolve(LG, query, found)
    body = err.value.body()
    assert body["error"].startswith("unknown_input")
    assert len(body["candidates"]) == 3


def test_configured_input_the_device_does_not_list_is_refused() -> None:
    dev = {**LG, "inputs": {"HDMI_9": "Old box"}}
    found = inputs.with_config(dev, inputs.from_webos(WEBOS_LIST))
    assert found[-1]["error"].startswith("Unknown configured input HDMI_9")
    with pytest.raises(inputs.InputRefusal, match="does not list it"):
        inputs.resolve(dev, "HDMI_9", found)


def test_no_inputs_points_at_the_paired_tv() -> None:
    dev = {"name": "box-a", "type": "shield", "paired_tv": "tv-b"}
    with pytest.raises(inputs.InputRefusal, match="switch tv-b instead"):
        inputs.resolve(dev, "HDMI 1", inputs.with_config(dev, []))


def test_old_fire_config_keys_and_ids_become_aliases() -> None:
    dev = {
        **FIRE,
        "inputs": {"hdmi1": "Living room box"},
        "input_keys": {"hdmi1": "TV_INPUT_HDMI_1"},
        "input_ids": {"hdmi2": "com.example.tis/.HdmiInputService/HW3"},
    }
    found = inputs.with_config(dev, inputs.parse_android(ANDROID_DUMP))
    assert inputs.resolve(dev, "hdmi1", found)["key"] == "hdmi1"
    assert inputs.resolve(dev, "living room box", found)["port"] == 1
    assert inputs.resolve(dev, "hdmi2", found)["id"].endswith("/HW3")
    assert all("error" not in i for i in found)


def test_example_inventory_input_config_loads() -> None:
    devices: list[dict[str, Any]] = json.loads(
        subprocess.run(
            ["yq", "-o=json", ".devices", str(DEVICES)],
            capture_output=True,
            check=True,
        ).stdout
    )
    configured = [d for d in devices if inputs.configured_keys(d)]
    assert configured
    for dev in configured:
        listed = inputs.with_config(dev, [])
        assert {i["id"] for i in listed} == set(inputs.configured_keys(dev))
        assert all("error" in i for i in listed)


def test_input_key_accepts_the_keycode_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[Any, ...]] = []

    async def ready(dev: dict[str, Any]) -> str:
        return "ser"

    async def awake(serial: str, cmd: str) -> str:
        return "mWakefulness=Awake"

    async def fake_adb(serial: str, *args: str, **_: Any) -> Any:
        sent.append(args)
        return 0, b"", ""

    import asyncio

    monkeypatch.setattr(adbops, "require_device", ready)
    monkeypatch.setattr(adbops, "read", awake)
    monkeypatch.setattr(adbops, "adb", fake_adb)
    body = asyncio.run(adbops.set_input_key(FIRE, "KEYCODE_TV_INPUT_HDMI_2"))
    assert body["input_set"] == "TV_INPUT_HDMI_2"
    assert sent[0][-1] == "KEYCODE_TV_INPUT_HDMI_2"
