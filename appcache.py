"""Server-side cache of each device's app list and LG inputs, kept in the config volume.

The panel and the quick-app buttons read this first, so they load without touching the device
and still work when it is offline. Entries older than TTL_S are served stale and refreshed.
"""

import json
import os
import time
from pathlib import Path
from typing import Any

CACHE_FILE = Path(os.environ.get("APP_CACHE_FILE", "/config/app-cache.json"))
TTL_S = 24 * 3600

FALLBACK_LABELS = {
    "com.plexapp.android": "Plex",
    "org.smarttube.stable": "SmartTube",
    "org.smarttube.beta": "SmartTube",
    "com.google.android.youtube.tv": "YouTube",
    "com.google.android.youtube.tvunplugged": "YouTube TV",
    "youtube.leanback.v4": "YouTube",
    "youtube.leanback.ytv.v1": "YouTube TV",
    "com.amazon.avod.thirdpartyclient": "Prime Video",
    "com.amazon.firebat": "Prime Video",
    "com.disney.disneyplus": "Disney+",
    "com.wbd.stream": "Max",
    "com.hbo.hbonow": "Max",
    "com.hulu.livingroomplus": "Hulu",
    "com.peacocktv.peacockandroid": "Peacock",
    "com.cbs.ott": "Paramount+",
    "com.cbs.app": "Paramount+",
    "com.spotify.tv.android": "Spotify",
    "com.spocky.projengmenu": "Projectivy",
    "com.android.tv.settings": "Settings",
    "com.amazon.tv.settings": "Settings",
    "com.android.settings": "Settings",
    "com.android.tv": "Live TV",
    "com.amazon.tv.livetv": "Live TV",
    "com.webos.app.livetv": "Live TV",
    "com.webos.app.settings": "Settings",
    "plex": "Plex",
    "hulu": "Hulu",
    "spotify-beehive": "Spotify",
}


def app_title(app_id: str, title: str | None = None) -> str:
    if title and title.strip() and title != app_id and not title.startswith("@"):
        return title.strip()
    return FALLBACK_LABELS.get(app_id, "Unknown app")


def friendly_apps(items: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(
        ({**a, "title": app_title(a["id"], a.get("title"))} for a in items),
        key=lambda a: (a["title"].casefold(), a["id"]),
    )


def _read() -> dict[str, Any]:
    try:
        data: dict[str, Any] = json.loads(CACHE_FILE.read_text())
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get(device: str, kind: str) -> dict[str, Any] | None:
    return (_read().get(device) or {}).get(kind)


def put(device: str, kind: str, items: list[Any], now: float | None = None) -> None:
    data = _read()
    data.setdefault(device, {})[kind] = {
        "items": items,
        "fetched_at": time.time() if now is None else now,
    }
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(CACHE_FILE)


def is_fresh(entry: dict[str, Any], now: float | None = None) -> bool:
    return bool((time.time() if now is None else now) - entry["fetched_at"] < TTL_S)


def quick_apps(
    apps: list[dict[str, str]], wanted: list[dict[str, Any]], denied: list[str]
) -> list[dict[str, str]]:
    """Return the quick_apps buttons whose app is installed, never a denied one."""
    out = []
    for want in wanted:
        ids = {i.lower() for i in want.get("id") or []}
        titles = [t.lower() for t in want.get("title") or []]
        for app in apps:
            low_id, low_title = app["id"].lower(), app["title"].lower()
            if low_id in ids or any(t in low_title for t in titles):
                if not any(d in low_id or d in low_title for d in denied if d):
                    out.append({"label": want["label"], "id": app["id"]})
                break
    return out
