"""Sleep timers stored in the config volume so they survive a closed browser or a restart.

Every function takes now (epoch seconds) so tests drive a fake clock. The server loop calls
fire_due() every few seconds; a timer that came due while the container was down fires on start.
"""

import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

TIMERS_FILE = Path(os.environ.get("SLEEP_TIMERS_FILE", "/config/sleep-timers.json"))
PRESETS_MIN = (15, 30, 60, 90)


def _read() -> dict[str, float]:
    try:
        data: dict[str, float] = json.loads(TIMERS_FILE.read_text())
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write(data: dict[str, float]) -> None:
    TIMERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TIMERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(TIMERS_FILE)


def set_timer(device: str, minutes: int, now: float) -> float:
    if minutes not in PRESETS_MIN:
        raise ValueError(f"minutes must be one of {PRESETS_MIN}; 0 cancels")
    data = _read()
    data[device] = now + minutes * 60
    _write(data)
    return data[device]


def cancel(device: str) -> bool:
    data = _read()
    had = data.pop(device, None) is not None
    _write(data)
    return had


def remaining(device: str, now: float) -> int | None:
    fires_at = _read().get(device)
    return None if fires_at is None else max(0, int(fires_at - now))


async def fire_due(now: float, fire: Callable[[str], Awaitable[object]]) -> list[str]:
    """Remove every due timer, then call fire(device) for each. Returns the devices fired."""
    data = _read()
    due = sorted(d for d, at in data.items() if at <= now)
    if due:
        _write({d: at for d, at in data.items() if d not in due})
    for device in due:
        await fire(device)
    return due
