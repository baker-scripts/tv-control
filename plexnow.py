"""What a Plex client on a TV is playing, read from the Plex server's sessions through Tautulli.

The fleet already reads Plex this way (Tautulli `get_activity`). TAUTULLI_URL and TAUTULLI_API_KEY
come from the environment; the key goes in the X-Api-Key header, never in a URL, argv, or log line.
"""

import asyncio
import json
import os
import re
import urllib.request
from typing import Any

PLEX_PREFIX = "com.plexapp."
TIMEOUT_S = 8
NOTHING = "Plex open, nothing playing"


class PlexLookupError(Exception):
    """The session read failed; the text never carries the URL or the key."""


def is_plex(pkg: str | None) -> bool:
    return bool(pkg) and str(pkg).startswith(PLEX_PREFIX)


def _fetch_sessions() -> list[dict[str, Any]]:
    url = os.environ.get("TAUTULLI_URL", "").rstrip("/")
    key = os.environ.get("TAUTULLI_API_KEY", "")
    if not url or not key:
        raise PlexLookupError("not configured")
    if not url.startswith(("http://", "https://")):
        raise PlexLookupError("bad TAUTULLI_URL")
    req = urllib.request.Request(
        f"{url}/api/v2?cmd=get_activity", headers={"X-Api-Key": key}
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            data = json.loads(resp.read())
    except (OSError, ValueError) as exc:
        raise PlexLookupError(type(exc).__name__) from exc
    response = data.get("response") or {}
    if response.get("result") != "success":
        raise PlexLookupError("Tautulli refused the read")
    sessions = (response.get("data") or {}).get("sessions") or []
    return [s for s in sessions if isinstance(s, dict)]


def _norm(text: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").casefold())


def match_session(
    sessions: list[dict[str, Any]], names: list[str | None]
) -> dict[str, Any] | None:
    """The session whose Plex player name equals one of the device's names; a playing one wins."""
    wanted = {_norm(n) for n in names if _norm(n)}
    hits = [s for s in sessions if _norm(s.get("player")) in wanted]
    playing = next((s for s in hits if s.get("state") == "playing"), None)
    return playing or (hits[0] if hits else None)


def _number(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clock(ms: Any) -> str | None:
    millis = _number(ms)
    if millis is None:
        return None
    hours, rest = divmod(millis // 1000, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def describe(session: dict[str, Any]) -> dict[str, Any]:
    """Title, show, episode, state, and position for one session."""
    kind = session.get("media_type")
    title = session.get("title") or session.get("full_title") or ""
    show = session.get("grandparent_title") or None
    episode = None
    if kind == "episode":
        season = _number(session.get("parent_media_index"))
        number = _number(session.get("media_index"))
        episode = (
            (f"S{season:02d}" if season is not None else "")
            + (f"E{number:02d}" if number is not None else "")
        ) or None
        label = " - ".join(p for p in (show, episode, title) if p)
    elif kind == "track":
        label = " - ".join(p for p in (show, title) if p)
    else:
        label = session.get("full_title") or title
    return {
        "title": title or None,
        "show": show if kind == "episode" else None,
        "episode": episode,
        "label": label or None,
        "state": str(session.get("state") or "unknown"),
        "position": _clock(session.get("view_offset")),
        "duration": _clock(session.get("duration")),
        "player": session.get("player"),
    }


def summary(info: dict[str, Any]) -> str:
    where = ""
    if info["position"] and info["duration"]:
        where = f", {info['position']} of {info['duration']}"
    return f"Plex: {info['label'] or 'unknown title'} ({info['state']}{where})"


async def now_playing(names: list[str | None]) -> dict[str, Any]:
    """Fields for a status body: now_playing text and plex_session, or a plain reason."""
    try:
        sessions = await asyncio.to_thread(_fetch_sessions)
    except PlexLookupError as exc:
        return {"now_playing": f"Plex open (now-playing lookup unavailable: {exc})"}
    session = match_session(sessions, names)
    if session is None:
        return {"now_playing": NOTHING, "plex_session": None}
    info = describe(session)
    return {"now_playing": summary(info), "plex_session": info}
