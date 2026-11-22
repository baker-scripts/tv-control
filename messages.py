"""On-screen message policy and the per-TV message history.

The policy is data: the `messages:` block in devices.yml (title, corner, duration, quiet hours, the
unrequested kinds, narration depth, the assistant identity). Nothing here sends to a device. The assistant
writes the words; tv-control shows what it is given.
"""

import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any

from inventory import DeviceError

LOG_DIR = Path(os.environ.get("MESSAGE_LOG_DIR", "/config/messages"))
HISTORY_LIMIT = 50
OPERATOR = "operator"
DEFAULT_ASSISTANT = {"name": "your TV assistant", "sender": "assistant"}
KINDS = ("media_ready", "reply_waiting", "operator_push")
NARRATION_MODES = ("every_step", "start_end", "off")
KIND_RE = re.compile(r"^[a-z_]{1,32}$")
CLOCK_RE = re.compile(r"(\d{1,2}):(\d{2})")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# TvOverlay documents image as an MDI icon, an image URL, or Base64; Base64 is refused to keep a message small.
IMAGE_RE = re.compile(r"^(https?://\S{1,500}|mdi:[a-z0-9-]{1,64})$")


def _refuse(message: str, next_step: str) -> DeviceError:
    return DeviceError(
        message,
        cause="The message policy in devices.yml (messages:) does not allow this.",
        next_step=next_step,
    )


def assistant(policy: dict[str, Any]) -> dict[str, str]:
    """messages.assistant: the name shown in the narration intro and the sender id it calls with."""
    given = policy.get("assistant") or {}
    found = {k: str(given.get(k) or v) for k, v in DEFAULT_ASSISTANT.items()}
    if not NAME_RE.fullmatch(found["sender"]) or found["sender"] == OPERATOR:
        raise DeviceError(
            f"messages.assistant.sender {found['sender']!r} is not a valid sender id"
        )
    return found


def senders(policy: dict[str, Any]) -> tuple[str, str]:
    return (OPERATOR, assistant(policy)["sender"])


def intro_line(policy: dict[str, Any]) -> str:
    return f"Hi, this is {assistant(policy)['name']}. I will navigate now."


def seconds_for(text: str, policy: dict[str, Any]) -> int:
    """About seconds_per_5_words per 5 words, clamped to floor_s..cap_s."""
    rule = policy["duration"]
    raw = math.ceil(len(text.split()) * rule["seconds_per_5_words"] / 5)
    return int(min(rule["cap_s"], max(rule["floor_s"], raw)))


def parse_clock(text: str) -> int | None:
    """Minutes since midnight from a device clock read such as 22:05, else None."""
    match = CLOCK_RE.search(text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    return hour * 60 + minute if hour < 24 and minute < 60 else None


def _minutes(value: str) -> int:
    parsed = parse_clock(str(value))
    if parsed is None:
        raise DeviceError(f"messages.quiet_hours has a bad time {value!r}; use HH:MM")
    return parsed


def in_quiet_hours(minutes: int, policy: dict[str, Any]) -> bool:
    start, end = (_minutes(policy["quiet_hours"][k]) for k in ("start", "end"))
    if start <= end:
        return start <= minutes < end
    return minutes >= start or minutes < end


def narration_mode(dev: dict[str, Any], policy: dict[str, Any]) -> str:
    """The device's own narration key, else its household's, else the default."""
    rule = policy["narration"]
    mode = (
        dev.get("narration")
        or (rule.get("households") or {}).get(dev.get("household"))
        or rule["default"]
    )
    if mode not in NARRATION_MODES:
        raise DeviceError(f"narration must be one of {NARRATION_MODES}, not {mode!r}")
    return str(mode)


def check_sender(
    dev: dict[str, Any],
    policy: dict[str, Any],
    sender: str,
    requested: bool,
    kind: str,
    household: str,
) -> str:
    """Refuse a message the policy does not allow; return the kind to record.

    The operator may send to any TV. The assistant (messages.assistant.sender) may send only to the
    household that asked (requested=true) or one of unrequested_kinds, and only with household= naming the
    TV's household. operator_push is the operator's alone. An operator message with no request and no kind
    is an operator_push.
    """
    allowed_senders = senders(policy)
    bot = allowed_senders[1]
    if sender not in allowed_senders:
        raise _refuse(
            f"not_allowed: sender must be one of {allowed_senders}",
            f"Pass sender={OPERATOR} or sender={bot}.",
        )
    if kind and not KIND_RE.fullmatch(kind):
        raise _refuse("not_allowed: kind is lowercase letters and _", "Fix kind=.")
    if sender == OPERATOR and not requested and not kind:
        kind = "operator_push"
    allowed = list(policy.get("unrequested_kinds") or [])
    if not requested and kind not in allowed:
        raise _refuse(
            f"not_allowed: an unrequested message needs kind={allowed}",
            "Send requested=true for a reply to a request, or use a named kind.",
        )
    if kind == "operator_push" and sender != OPERATOR:
        raise _refuse(
            "not_allowed: operator_push is for the operator only",
            f"Send sender={OPERATOR} only when the operator asked for the push.",
        )
    if sender == bot and household != dev.get("household"):
        raise _refuse(
            f"wrong_household: {dev['name']} belongs to {dev.get('household')!r}, not {household!r}",
            "The assistant sends only to the household that made the request.",
        )
    return kind


def check_image(image: str) -> str:
    if image and not IMAGE_RE.fullmatch(image):
        raise _refuse(
            "not_allowed: image must be an http(s) URL or mdi:<name>",
            "Use a poster URL or an mdi: icon name.",
        )
    return image


def _log(device: str) -> Path | None:
    return LOG_DIR / f"{device}.json" if NAME_RE.fullmatch(device) else None


def _load(path: Path) -> list[dict[str, Any]]:
    try:
        rows = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return rows if isinstance(rows, list) else []


def record(device: str, entry: dict[str, Any], now: float | None = None) -> None:
    """Append one message (time, text, sender, kind, shown) and keep the last HISTORY_LIMIT."""
    path = _log(device)
    if path is None:
        return
    rows = [
        *_load(path),
        {"time": int(now if now is not None else time.time()), **entry},
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows[-HISTORY_LIMIT:]))
    tmp.replace(path)


def history(device: str) -> list[dict[str, Any]]:
    """The last HISTORY_LIMIT messages for one TV, newest first."""
    path = _log(device)
    return list(reversed(_load(path))) if path else []
