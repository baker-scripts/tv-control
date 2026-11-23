"""App launch policy: one place that decides whether automation may open an app, and which app a name means.

Every verb that opens an app (launch_app, open_app, play_title, and the panel's launch and open)
resolves the request to a package id and label here, then checks it against three rules:
denied_apps (id and label), the device's allowed_apps list when it has one, and the protected
targets (stores, settings, account sign-in). Nothing here touches a device.
"""

import re
import unicodedata
from typing import Any

from inventory import DeviceError

# Launch targets automation never opens. Package ids match the protected lists of the companion
# fleet scripts and the curated Settings ids in appcache; labels catch a renamed or sideloaded copy.
PROTECTED_IDS = frozenset(
    {
        "com.android.vending",  # Google Play Store
        "com.amazon.venezia",  # Amazon Appstore
        "com.android.settings",
        "com.android.tv.settings",
        "com.amazon.tv.settings",
        "com.webos.app.settings",
        "com.google.android.gms",  # Google account sign-in
        "com.google.android.tungsten.setupwraith",  # Android TV setup and sign-in
        "com.amazon.tv.oobe",  # Fire OS setup and Amazon sign-in
    }
)
PROTECTED_LABELS = frozenset(
    {
        "play store",
        "google play store",
        "appstore",
        "amazon appstore",
        "settings",
        "accounts",
        "accounts and sign in",
    }
)
PKG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
MAX_CANDIDATES = 10


def normalize(text: str) -> str:
    """Casefold, drop punctuation and symbols, collapse spaces: 'Disney+ ' -> 'disney'."""
    kept = (
        ch if unicodedata.category(ch)[0] in "LN" else " "
        for ch in unicodedata.normalize("NFKC", text).casefold()
    )
    return " ".join("".join(kept).split())


class AppRefusal(DeviceError):
    """The policy refused an app request; candidates (id and title) help the caller pick again."""

    def __init__(
        self, message: str, candidates: list[dict[str, str]] | None = None
    ) -> None:
        super().__init__(message)
        self.candidates = candidates or []

    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = dict(super().body())
        if self.candidates:
            body["candidates"] = self.candidates[:MAX_CANDIDATES]
        return body


def allow_list(dev: dict[str, Any]) -> frozenset[str] | None:
    """The device's allowed_apps ids, or None when the key is absent (no allow list)."""
    if "allowed_apps" not in dev:
        return None
    raw = dev.get("allowed_apps")
    if raw is None:
        return frozenset()
    if not isinstance(raw, list):
        # Fail closed: a malformed list allows nothing rather than everything.
        return frozenset()
    return frozenset(str(x).strip().casefold() for x in raw if str(x).strip())


def refusal(
    dev: dict[str, Any], app_id: str, labels: list[str | None], denied: list[str]
) -> str | None:
    """Why automation may not open app_id on dev, or None. labels: every name known for the app."""
    low_id = app_id.casefold()
    names = [normalize(x) for x in labels if x]
    for word in denied:
        key = normalize(str(word))
        if not key:
            continue
        if str(word).casefold() in low_id or any(key in n for n in names):
            return f"{app_id!r} refused: matches denylist entry {word!r}"
    if low_id in PROTECTED_IDS or any(n in PROTECTED_LABELS for n in names):
        return (
            f"protected_target: {app_id!r} is a store, settings, or sign-in app; "
            "automation never opens it"
        )
    allowed = allow_list(dev)
    if allowed is not None and low_id not in allowed:
        return f"allowlist: {app_id!r} is not in allowed_apps for {dev['name']}"
    return None


def visible(
    dev: dict[str, Any], apps: list[dict[str, Any]], denied: list[str]
) -> list[dict[str, Any]]:
    """The apps (id plus title or label) automation may open on dev."""
    return [
        app
        for app in apps
        if refusal(
            dev,
            str(app.get("id", "")),
            [app.get("title"), app.get("label")],
            denied,
        )
        is None
    ]


def _candidates(apps: list[dict[str, str]]) -> list[dict[str, str]]:
    return [{"id": a["id"], "title": a.get("title") or a["id"]} for a in apps]


def resolve(
    dev: dict[str, Any],
    query: str,
    installed: list[dict[str, str]],
    aliases: dict[str, Any],
) -> str:
    """The app id a spoken name or id means on dev.

    Order: exact label, exact id, app_aliases entry, a package id as given, unique label prefix.
    A prefix only matches apps on the allow list when the device has one. Ambiguous or unknown
    names raise AppRefusal with candidates.
    """
    raw = query.strip()
    if not raw:
        raise AppRefusal("unknown_app: no app named; pass a name or package id")
    want = normalize(raw)
    allowed = allow_list(dev)

    def prefer_allowed(hits: list[dict[str, str]]) -> list[dict[str, str]]:
        if allowed is None or len(hits) < 2:
            return hits
        return [a for a in hits if a["id"].casefold() in allowed] or hits

    by_label = prefer_allowed(
        [a for a in installed if want and normalize(a.get("title") or "") == want]
    )
    if len(by_label) == 1:
        return by_label[0]["id"]
    if len(by_label) > 1:
        raise AppRefusal(
            f"ambiguous_app: {raw!r} matches {len(by_label)} apps; pass the package id",
            _candidates(by_label),
        )
    by_id = [a for a in installed if a["id"].casefold() == raw.casefold()]
    if by_id:
        return by_id[0]["id"]
    target = aliases.get(want)
    if target is not None:
        ids = [str(t) for t in (target if isinstance(target, list) else [target])]
        have = {a["id"] for a in installed}
        hit = next((i for i in ids if i in have), None)
        if hit:
            return hit
        if len(ids) == 1 and not installed:
            return ids[0]
        raise AppRefusal(
            f"unknown_app: alias {raw!r} names {', '.join(ids)}, none installed here",
            _candidates(installed),
        )
    if PKG_RE.match(raw):
        return raw
    pool = [a for a in installed if allowed is None or a["id"].casefold() in allowed]
    by_prefix = [
        a for a in pool if want and normalize(a.get("title") or "").startswith(want)
    ]
    if len(by_prefix) == 1:
        return by_prefix[0]["id"]
    if len(by_prefix) > 1:
        raise AppRefusal(
            f"ambiguous_app: {raw!r} matches {len(by_prefix)} apps; pass a fuller name or the package id",
            _candidates(by_prefix),
        )
    if dev.get("type") == "webos" and " " not in raw:
        # webOS app ids have no dots; the TV checks the id against its own list.
        return raw
    note = "" if installed else " (no cached app list; call list_apps)"
    raise AppRefusal(
        f"unknown_app: no app matches {raw!r}{note}", _candidates(pool or installed)
    )


def merged_aliases(top: dict[str, Any], dev: dict[str, Any]) -> dict[str, Any]:
    """Top-level app_aliases, then the device's own app_aliases over them; keys normalized."""
    out: dict[str, Any] = {}
    for source in (top, dev.get("app_aliases")):
        if isinstance(source, dict):
            out |= {
                normalize(str(k)): v for k, v in source.items() if normalize(str(k))
            }
    return out
