"""App policy and name resolution: every rule with neutral fixtures, no device call."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

import adbops
import appcache
import apppolicy
import inventory
import tools
import webos
from inventory import DeviceError

SHIELD = {"name": "shield-a", "type": "shield", "ip": "192.0.2.1"}
LOCKED = {
    "name": "shield-locked",
    "type": "shield",
    "ip": "192.0.2.5",
    "allowed_apps": ["com.plexapp.android", "org.smarttube.stable"],
}
TV = {"name": "tv-a", "type": "webos", "ip": "192.0.2.2"}
FEEDER = {"name": "shield-b", "type": "shield", "ip": "192.0.2.3", "paired_tv": "tv-a"}
FIRE = {"name": "fire-a", "type": "firetv", "ip": "192.0.2.4"}
DEVICES = {d["name"]: d for d in (SHIELD, LOCKED, TV, FEEDER, FIRE)}

APPS = [
    {"id": "com.plexapp.android", "title": "Plex"},
    {"id": "org.example.plexdl", "title": "Plex Downloader"},
    {"id": "org.smarttube.stable", "title": "SmartTube"},
    {"id": "com.google.android.youtube.tv", "title": "YouTube"},
    {"id": "com.google.android.youtube.tvunplugged", "title": "YouTube TV"},
    {"id": "com.netflix.ninja", "title": "TV"},
    {"id": "org.example.player", "title": "Netflix!"},
    {"id": "org.example.store", "title": "Play Store"},
    {"id": "com.android.vending", "title": "Shop"},
]


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def inventory_mock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    monkeypatch.setattr(appcache, "CACHE_FILE", tmp_path / "cache.json")
    for name in ("shield-a", "shield-locked"):
        appcache.put(name, "apps", APPS)
    appcache.put("tv-a", "apps", [{"id": "com.webos.app.hdmi1", "title": "HDMI 1"}])
    launched: list[str] = []

    async def get_device(name: str) -> dict[str, Any]:
        if name not in DEVICES:
            raise DeviceError(f"unknown device {name!r}; call list_devices")
        return DEVICES[name]

    async def load_devices() -> list[dict[str, Any]]:
        return list(DEVICES.values())

    async def denied() -> list[str]:
        return ["netflix"]

    async def fake_launch(dev: dict[str, Any], pkg: str) -> dict[str, Any]:
        launched.append(pkg)
        return {"launched": pkg}

    async def no_device(*_: Any, **__: Any) -> Any:
        raise AssertionError("a test reached a real device call")

    monkeypatch.setattr(tools, "get_device", get_device)
    monkeypatch.setattr(tools, "load_devices", load_devices)
    monkeypatch.setattr(tools, "load_denied_apps", denied)
    monkeypatch.setattr(adbops, "launch", fake_launch)
    monkeypatch.setattr(webos, "launch", fake_launch)
    for name in ("send_key", "send_key_fast", "set_input", "set_input_key"):
        monkeypatch.setattr(adbops, name, no_device)
    monkeypatch.setattr(webos, "set_input", no_device)
    return launched


def test_normalize_casefolds_and_drops_punctuation() -> None:
    assert apppolicy.normalize("  Disney+ ") == "disney"
    assert apppolicy.normalize("YouTube-TV!") == "youtube tv"


@pytest.mark.parametrize(
    "app", ["com.netflix.ninja", "org.example.player", "Netflix", "netflix!"]
)
def test_netflix_caught_by_id_and_by_label(app: str, inventory_mock: list[str]) -> None:
    body = run(tools.do_launch("shield-a", app))
    assert "denylist" in body["error"]
    assert inventory_mock == []


@pytest.mark.parametrize(
    "app", ["com.android.vending", "Play Store", "org.example.store"]
)
def test_protected_target_refused_by_id_and_label(
    app: str, inventory_mock: list[str]
) -> None:
    body = run(tools.do_launch("shield-a", app))
    assert body["error"].startswith("protected_target")
    assert inventory_mock == []


def test_force_never_skips_the_app_policy(inventory_mock: list[str]) -> None:
    for app in ("com.netflix.ninja", "com.android.vending", "com.amazon.venezia"):
        assert "error" in run(tools.launch_app("shield-a", app, force=True))
    assert inventory_mock == []


def test_allow_list_device_refuses_an_installed_unlisted_app(
    inventory_mock: list[str],
) -> None:
    for app in ("YouTube", "com.google.android.youtube.tv"):
        body = run(tools.do_launch("shield-locked", app))
        assert body["error"].startswith("allowlist")
    assert inventory_mock == []
    assert run(tools.do_launch("shield-locked", "plex"))["launched"] == (
        "com.plexapp.android"
    )


def test_allow_list_does_not_need_the_app_in_the_cached_list(
    inventory_mock: list[str],
) -> None:
    # list_apps (query-activities) omits disabled and hidden packages; the id is checked itself.
    appcache.put("shield-locked", "apps", [])
    assert (
        "allowlist" in run(tools.do_launch("shield-locked", "org.example.x"))["error"]
    )
    assert run(tools.do_launch("shield-locked", "org.smarttube.stable"))["launched"]


@pytest.mark.parametrize("raw", [[], None, "com.plexapp.android"])
def test_empty_or_malformed_allow_list_opens_nothing(raw: Any) -> None:
    dev = {"name": "x", "allowed_apps": raw}
    assert apppolicy.refusal(dev, "com.plexapp.android", ["Plex"], []) is not None


def test_ambiguous_name_is_refused_with_candidates(inventory_mock: list[str]) -> None:
    body = run(tools.do_launch("shield-a", "you"))
    assert body["error"].startswith("ambiguous_app")
    ids = {c["id"] for c in body["candidates"]}
    assert ids == {
        "com.google.android.youtube.tv",
        "com.google.android.youtube.tvunplugged",
    }
    assert inventory_mock == []


def test_unknown_name_is_refused_with_candidates() -> None:
    body = run(tools.do_launch("shield-a", "no such app"))
    assert body["error"].startswith("unknown_app")
    assert body["candidates"]


def test_look_alike_label_is_not_matched_by_prefix_on_an_allow_list_device(
    inventory_mock: list[str],
) -> None:
    body = run(tools.do_launch("shield-locked", "plex d"))
    assert body["error"].startswith("unknown_app")
    assert {c["id"] for c in body["candidates"]} <= set(LOCKED["allowed_apps"])
    assert inventory_mock == []


def test_exact_label_shared_with_a_look_alike_prefers_the_allowed_app(
    inventory_mock: list[str],
) -> None:
    appcache.put(
        "shield-locked",
        "apps",
        [*APPS, {"id": "org.example.fake", "title": "Plex"}],
    )
    assert run(tools.do_launch("shield-locked", "Plex"))["launched"] == (
        "com.plexapp.android"
    )
    appcache.put(
        "shield-a", "apps", [*APPS, {"id": "org.example.fake", "title": "Plex"}]
    )
    assert run(tools.do_launch("shield-a", "Plex"))["error"].startswith("ambiguous_app")


def test_unique_prefix_and_package_id_resolve(inventory_mock: list[str]) -> None:
    run(tools.do_launch("shield-a", "smart"))
    run(tools.do_launch("shield-a", "org.example.notcached"))
    assert inventory_mock == ["org.smarttube.stable", "org.example.notcached"]


def test_alias_resolves_to_the_installed_id(
    monkeypatch: pytest.MonkeyPatch, inventory_mock: list[str]
) -> None:
    async def aliases() -> dict[str, Any]:
        return {
            "YouTube TV!": [
                "youtube.leanback.ytv.v1",
                "com.google.android.youtube.tvunplugged",
            ],
            "tube": "org.smarttube.stable",
        }

    monkeypatch.setattr(tools, "load_app_aliases", aliases)
    run(tools.do_launch("shield-a", "youtube tv"))
    run(tools.do_launch("shield-a", "Tube"))
    assert inventory_mock == [
        "com.google.android.youtube.tvunplugged",
        "org.smarttube.stable",
    ]


def test_panel_and_quick_apps_show_only_what_the_policy_opens() -> None:
    body = run(tools.launchable_apps("shield-locked", {"apps": APPS}))
    assert [a["id"] for a in body["apps"]] == [
        "com.plexapp.android",
        "org.smarttube.stable",
    ]
    open_ids = {
        a["id"] for a in run(tools.launchable_apps("shield-a", {"apps": APPS}))["apps"]
    }
    assert not open_ids & {
        "com.netflix.ninja",
        "org.example.player",
        "org.example.store",
        "com.android.vending",
    }


def test_quick_apps_drop_apps_off_the_allow_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def quick() -> list[dict[str, Any]]:
        return [
            {"label": "Plex", "id": ["com.plexapp.android"]},
            {"label": "YouTube", "id": ["com.google.android.youtube.tv"]},
        ]

    monkeypatch.setattr(tools, "load_quick_apps", quick)
    labels = [a["label"] for a in run(tools.do_quick_apps("shield-locked"))["apps"]]
    assert labels == ["Plex"]


def _feeder_state(monkeypatch: pytest.MonkeyPatch, focus: str, state: str) -> None:
    async def fake_require(dev: dict[str, Any]) -> str:
        return dev["name"]

    async def fake_read(serial: str, command: str) -> str:
        if serial != "shield-b":
            raise DeviceError("adb state is 'offline', not device")
        if "mWakefulness" in command:
            return "mWakefulness=Awake"
        if "media_session" in command:
            return f"PlaybackState {{state={state},"
        return focus

    async def tv_idle(dev: dict[str, Any]) -> list[str]:
        return []

    monkeypatch.setattr(adbops, "require_device", fake_require)
    monkeypatch.setattr(adbops, "read", fake_read)
    monkeypatch.setattr(webos, "play_states", tv_idle)


@pytest.mark.parametrize(
    ("focus", "state"),
    [
        (tools.SHIELD_HOME_APP, "3"),
        ("mCurrentFocus=Window{1 u0 org.smarttube.stable/A}", "2"),
    ],
)
def test_input_switch_and_launch_on_a_tv_refused_while_its_feeder_is_in_use(
    monkeypatch: pytest.MonkeyPatch, inventory_mock: list[str], focus: str, state: str
) -> None:
    _feeder_state(monkeypatch, focus, state)
    for coro in (
        tools.set_input("tv-a", "HDMI_1"),
        tools.launch_app("tv-a", "com.webos.app.hdmi1"),
    ):
        body = run(coro)
        assert body["error"].startswith("in_use") and "shield-b" in body["error"]
    assert inventory_mock == []


def test_feeder_on_its_home_screen_does_not_block_the_tv(
    monkeypatch: pytest.MonkeyPatch, inventory_mock: list[str]
) -> None:
    _feeder_state(monkeypatch, tools.SHIELD_HOME_APP, "1")
    assert run(tools.launch_app("tv-a", "com.webos.app.hdmi1"))["launched"]
    _feeder_state(monkeypatch, tools.SHIELD_HOME_APP, "3")
    assert run(tools.launch_app("tv-a", "com.webos.app.hdmi1", force=True))["launched"]


def test_webos_paused_media_counts_as_in_use(monkeypatch: pytest.MonkeyPatch) -> None:
    states = {"value": ["paused"]}

    async def play_states(dev: dict[str, Any]) -> list[str]:
        return states["value"]

    monkeypatch.setattr(webos, "play_states", play_states)
    assert "in_use" in run(tools.in_use_refusal("tv-a", False))["error"]
    states["value"] = ["unloaded"]
    assert run(tools.in_use_refusal("tv-a", False)) is None


def test_fire_os_paused_in_an_app_counts_as_in_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads = {"focus": "mCurrentFocus=Window{1 u0 org.example.app/Main}"}

    async def fake_require(dev: dict[str, Any]) -> str:
        return "s"

    async def fake_read(serial: str, command: str) -> str:
        if "mWakefulness" in command:
            return "mWakefulness=Awake"
        if "media_session" in command:
            return "PlaybackState {state=2,"
        return reads["focus"]

    monkeypatch.setattr(adbops, "require_device", fake_require)
    monkeypatch.setattr(adbops, "read", fake_read)
    assert "in_use" in run(tools.in_use_refusal("fire-a", False))["error"]
    for idle in (tools.FIRE_LAUNCHER, adbops.PASSTHROUGH_PKG):
        reads["focus"] = f"mCurrentFocus=Window{{1 u0 {idle}/Main}}"
        assert run(tools.in_use_refusal("fire-a", False)) is None


def test_webos_play_states_without_a_pairing_key_reads_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(webos, "WEBOS_KEYS_FILE", tmp_path / "none.json")
    assert run(webos.play_states(TV)) == []


OLD_FILE = """\
devices:
  - name: box-a
    site: home
    ip: 192.0.2.30
    type: shield
denied_apps: [netflix]
quick_apps:
  - label: Plex
    id: [com.plexapp.android]
"""
NEW_FILE = (
    OLD_FILE.replace(
        "    type: shield\n",
        "    type: shield\n    allowed_apps: [com.plexapp.android]\n    future_key: 1\n",
    )
    + "app_aliases:\n  films: com.plexapp.android\nfuture_block: {a: 1}\n"
)


@pytest.mark.parametrize(
    ("text", "aliases"), [(OLD_FILE, {}), (NEW_FILE, {"films": "com.plexapp.android"})]
)
def test_old_and_newer_devices_files_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str, aliases: dict[str, Any]
) -> None:
    path = tmp_path / "devices.yml"
    path.write_text(text)
    monkeypatch.setattr(inventory, "DEVICES_FILE", path)
    dev = run(inventory.get_device("box-a"))
    assert run(inventory.load_app_aliases()) == aliases
    assert run(inventory.load_denied_apps()) == ["netflix"]
    allowed = apppolicy.allow_list(dev)
    assert (
        allowed is None
        if text == OLD_FILE
        else allowed == frozenset({"com.plexapp.android"})
    )
