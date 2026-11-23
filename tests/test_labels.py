"""App names: curated override, device label, package name; HDMI passthrough titles; Plex now-playing."""

import asyncio
import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

import adbops
import appcache
import plexnow
import tools
from inventory import DeviceError

FIRE = {"name": "fire-a", "type": "firetv", "ip": "192.0.2.3", "label": "Fire A"}
SHIELD = {
    "name": "shield-a",
    "label": "Shield A",
    "type": "shield",
    "ip": "192.0.2.1",
    "paired_tv": "fire-a",
}

# Trimmed from a live Fire OS TV with a Shield on HDMI 1.
HDMI_DUMP = """\
    mDeviceInfo: CEC: logical_address: 0x00 device_type: 0 vendor_id: 7173227 display_name: My Fire TV power_status: 0 physical_address: 0x0000 port_id: -1
    mActiveRoutingPath: 0x1000
      CEC: logical_address: 0x00 device_type: 0 vendor_id: 7173227 display_name: My Fire TV power_status: 0 physical_address: 0x0000 port_id: -1
      CEC: logical_address: 0x08 device_type: 4 vendor_id: 1099 display_name: SHIELD power_status: 0 physical_address: 0x1000 port_id: 1
"""


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(appcache, "CACHE_FILE", tmp_path / "cache.json")
    monkeypatch.setattr(adbops, "serial_of", lambda dev: "s")


# Chain 1: curated override > device label > package name


def test_curated_name_overrides_device_label() -> None:
    assert appcache.app_title("com.plexapp.android", "Plex Media") == "Plex"


def test_device_label_used_when_not_curated() -> None:
    assert appcache.app_title("org.example.app", "Example") == "Example"


@pytest.mark.parametrize("label", [None, "", "  ", "org.example.app", "@string/x"])
def test_package_name_when_device_has_no_label(label: str | None) -> None:
    assert appcache.app_title("org.example.app", label) == "org.example.app"
    assert "Unknown" not in appcache.app_title("org.example.app", label)


def test_label_cache_found_is_permanent_and_miss_retries() -> None:
    appcache.put_label("d", "a.b", "Alpha", now=0)
    appcache.put_label("d", "c.d", None, now=0)
    far = appcache.LABEL_MISS_RETRY_S + 1
    assert appcache.cached_label("d", "a.b", now=far) == (True, "Alpha")
    assert appcache.cached_label("d", "c.d", now=1) == (True, None)
    assert appcache.cached_label("d", "c.d", now=far) == (False, None)
    assert appcache.cached_label("other", "a.b") == (False, None)


def test_label_cache_does_not_disturb_app_lists() -> None:
    appcache.put("d", "apps", [{"id": "a.b", "title": "A"}])
    appcache.put_label("d", "a.b", "Alpha")
    entry = appcache.get("d", "apps")
    assert entry is not None and entry["items"] == [{"id": "a.b", "title": "A"}]


def test_device_label_reads_once_then_serves_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read = AsyncMock(return_value="Example")
    monkeypatch.setattr(adbops, "read_label", read)
    assert run(tools.device_label(FIRE, "org.example.app", None)) == "Example"
    assert run(tools.device_label(FIRE, "org.example.app", None)) == "Example"
    assert read.await_count == 1


def test_device_label_skips_device_when_app_list_has_a_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read = AsyncMock()
    monkeypatch.setattr(adbops, "read_label", read)
    assert run(tools.device_label(FIRE, "a.b", "From list")) == "From list"
    read.assert_not_awaited()


def test_device_without_label_caches_the_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    read = AsyncMock(return_value=None)
    monkeypatch.setattr(adbops, "read_label", read)
    assert run(tools.device_label(FIRE, "a.b", "a.b")) is None
    assert run(tools.device_label(FIRE, "a.b", "a.b")) is None
    assert read.await_count == 1


def test_read_label_is_none_when_pull_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    async def shell(serial: str, command: str) -> str:
        return "package:/data/app/x/base.apk\n"

    async def adb(serial: str, *args: str, **_: Any) -> tuple[int, bytes, str]:
        return 1, b"", "no"

    monkeypatch.setattr(adbops, "shell", shell)
    monkeypatch.setattr(adbops, "adb", adb)
    assert run(adbops.read_label("s", "org.example.app")) is None


def test_app_fields_unlabelled_package_shows_package_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adbops, "read_label", AsyncMock(return_value=None))
    body = run(tools.app_fields(FIRE, "org.example.app"))
    assert body == {"app_title": "org.example.app"}


def test_app_fields_device_error_still_names_the_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adbops, "read_label", AsyncMock(side_effect=DeviceError("x")))
    assert run(tools.app_fields(FIRE, "org.example.app")) == {
        "app_title": "org.example.app"
    }


# Chain 2: HDMI passthrough


def test_parse_hdmi_reads_port_and_cec_name() -> None:
    assert adbops.parse_hdmi(HDMI_DUMP) == {"hdmi_port": 1, "hdmi_device": "SHIELD"}


def test_parse_hdmi_without_routing_path() -> None:
    assert adbops.parse_hdmi("") == {"hdmi_port": None, "hdmi_device": None}
    assert adbops.parse_hdmi("mActiveRoutingPath: 0x0000")["hdmi_port"] is None


def test_parse_hdmi_device_behind_a_hub_matches_the_port() -> None:
    dump = (
        "mActiveRoutingPath: 0x2300\n"
        "CEC: logical_address: 0x04 device_type: 1 vendor_id: 1 display_name: Streamer "
        "power_status: 0 physical_address: 0x2300 port_id: 2\n"
    )
    assert adbops.parse_hdmi(dump) == {"hdmi_port": 2, "hdmi_device": "Streamer"}


def test_hdmi_title_names_the_paired_shield() -> None:
    assert tools.hdmi_title(FIRE, [FIRE, SHIELD], 1, "SHIELD") == "HDMI 1 (Shield A)"


def test_hdmi_title_uses_cec_name_for_other_devices() -> None:
    assert tools.hdmi_title(FIRE, [FIRE, SHIELD], 3, "PS5") == "HDMI 3 (PS5)"


def test_hdmi_title_with_two_paired_shields_keeps_cec_name() -> None:
    other = {**SHIELD, "name": "shield-b", "label": "Shield B"}
    assert tools.hdmi_title(FIRE, [FIRE, SHIELD, other], 1, "SHIELD") == (
        "HDMI 1 (SHIELD)"
    )


def test_hdmi_title_without_reports_is_still_plain() -> None:
    assert tools.hdmi_title(FIRE, [FIRE], 2, None) == "HDMI 2"
    assert tools.hdmi_title(FIRE, [FIRE], None, None) == "HDMI input"


def test_app_fields_passthrough_reports_hdmi_and_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        adbops, "hdmi_input", AsyncMock(return_value=adbops.parse_hdmi(HDMI_DUMP))
    )
    monkeypatch.setattr(tools, "load_devices", AsyncMock(return_value=[FIRE, SHIELD]))
    body = run(tools.app_fields(FIRE, adbops.PASSTHROUGH_PKG))
    assert body["app_title"] == "HDMI 1 (Shield A)"
    assert body["hdmi_port"] == 1


def test_app_fields_passthrough_read_failure_falls_back_to_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adbops, "hdmi_input", AsyncMock(side_effect=DeviceError("x")))
    body = run(tools.app_fields(FIRE, adbops.PASSTHROUGH_PKG))
    assert body["app_title"] == adbops.PASSTHROUGH_PKG


# Chain 3: Plex now playing

EPISODE = {
    "player": "Shield A",
    "state": "playing",
    "media_type": "episode",
    "title": "Pilot",
    "full_title": "Show - Pilot",
    "grandparent_title": "Show",
    "parent_media_index": "2",
    "media_index": "3",
    "view_offset": "754000",
    "duration": "3130000",
}


def test_describe_episode_movie_and_track() -> None:
    ep = plexnow.describe(EPISODE)
    assert ep["show"] == "Show" and ep["episode"] == "S02E03" and ep["title"] == "Pilot"
    assert (
        plexnow.summary(ep) == "Plex: Show - S02E03 - Pilot (playing, 12:34 of 52:10)"
    )
    movie = plexnow.describe(
        {
            "media_type": "movie",
            "title": "Film",
            "full_title": "Film",
            "state": "paused",
        }
    )
    assert plexnow.summary(movie) == "Plex: Film (paused)"
    track = plexnow.describe(
        {
            "media_type": "track",
            "title": "Song",
            "grandparent_title": "Band",
            "state": "playing",
        }
    )
    assert track["label"] == "Band - Song"


def test_clock_handles_hours_and_junk() -> None:
    assert plexnow._clock("7605984") == "2:06:45"
    assert plexnow._clock("") is None


def test_match_session_by_any_device_name_prefers_playing() -> None:
    other = {**EPISODE, "player": "Elsewhere"}
    paused = {**EPISODE, "state": "paused", "title": "Old"}
    assert plexnow.match_session([other], ["shield-a", "Shield A"]) is None
    assert plexnow.match_session([other, EPISODE], [None, "shield a"]) is EPISODE
    assert plexnow.match_session([paused, EPISODE], ["Shield-A"]) is EPISODE
    assert plexnow.match_session([paused], ["Shield-A"]) is paused


def test_now_playing_nothing_playing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plexnow, "_fetch_sessions", list)
    assert run(plexnow.now_playing(["Shield A"])) == {
        "now_playing": "Plex open, nothing playing",
        "plex_session": None,
    }


def test_now_playing_matches_the_device(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plexnow, "_fetch_sessions", lambda: [EPISODE])
    body = run(plexnow.now_playing(["Shield A"]))
    assert body["now_playing"].startswith("Plex: Show - S02E03 - Pilot (playing")
    assert body["plex_session"]["position"] == "12:34"


def test_now_playing_lookup_failure_is_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> list[dict[str, Any]]:
        raise plexnow.PlexLookupError("URLError")

    monkeypatch.setattr(plexnow, "_fetch_sessions", boom)
    body = run(plexnow.now_playing(["Shield A"]))
    assert body == {
        "now_playing": "Plex open (now-playing lookup unavailable: URLError)"
    }


def test_fetch_requires_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAUTULLI_URL", raising=False)
    monkeypatch.delenv("TAUTULLI_API_KEY", raising=False)
    with pytest.raises(plexnow.PlexLookupError, match="not configured"):
        plexnow._fetch_sessions()
    monkeypatch.setenv("TAUTULLI_URL", "file:///etc/passwd")
    monkeypatch.setenv("TAUTULLI_API_KEY", "k")
    with pytest.raises(plexnow.PlexLookupError, match="bad TAUTULLI_URL"):
        plexnow._fetch_sessions()


def test_fetch_sends_the_key_in_a_header_only(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_urlopen(req: Any, timeout: float) -> io.BytesIO:
        seen["url"], seen["headers"] = req.full_url, dict(req.header_items())
        payload = {"response": {"result": "success", "data": {"sessions": [EPISODE]}}}
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setenv("TAUTULLI_URL", "http://192.0.2.9:8181/tautulli/")
    monkeypatch.setenv("TAUTULLI_API_KEY", "sentinel-key")
    monkeypatch.setattr(plexnow.urllib.request, "urlopen", fake_urlopen)
    assert plexnow._fetch_sessions() == [EPISODE]
    assert seen["url"] == "http://192.0.2.9:8181/tautulli/api/v2?cmd=get_activity"
    assert "sentinel-key" not in seen["url"]
    assert seen["headers"]["X-api-key"] == "sentinel-key"


def test_fetch_error_text_has_no_url_or_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(req: Any, timeout: float) -> Any:
        raise OSError("connect to http://192.0.2.9 failed")

    monkeypatch.setenv("TAUTULLI_URL", "http://192.0.2.9:8181")
    monkeypatch.setenv("TAUTULLI_API_KEY", "sentinel-key")
    monkeypatch.setattr(plexnow.urllib.request, "urlopen", refuse)
    with pytest.raises(plexnow.PlexLookupError) as err:
        plexnow._fetch_sessions()
    assert str(err.value) == "OSError"


def test_app_fields_plex_adds_now_playing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adbops, "read", AsyncMock(return_value="Living Room\n"))
    monkeypatch.setattr(plexnow, "_fetch_sessions", lambda: [EPISODE])
    body = run(tools.app_fields(SHIELD, "com.plexapp.android"))
    assert body["app_title"] == "Plex"
    assert body["now_playing"].startswith("Plex: Show - S02E03")


def test_app_fields_plex_device_name_can_match(monkeypatch: pytest.MonkeyPatch) -> None:
    session = {**EPISODE, "player": "Living Room"}
    monkeypatch.setattr(adbops, "read", AsyncMock(return_value="Living Room\n"))
    monkeypatch.setattr(plexnow, "_fetch_sessions", lambda: [session])
    body = run(tools.app_fields({**SHIELD, "label": None}, "com.plexapp.android"))
    assert body["plex_session"]["title"] == "Pilot"


def test_app_fields_plex_nothing_playing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adbops, "read", AsyncMock(return_value="null\n"))
    monkeypatch.setattr(plexnow, "_fetch_sessions", list)
    body = run(tools.app_fields(SHIELD, "com.plexapp.android"))
    assert body["now_playing"] == "Plex open, nothing playing"


def test_non_plex_app_gets_no_plex_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> list[dict[str, Any]]:
        raise AssertionError("must not call Tautulli")

    monkeypatch.setattr(plexnow, "_fetch_sessions", boom)
    body = run(tools.app_fields(SHIELD, "org.smarttube.stable"))
    assert body == {"app_title": "SmartTube"}
    assert plexnow.is_plex("com.plexapp.plex") and not plexnow.is_plex(None)
