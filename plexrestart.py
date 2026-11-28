"""Ask the host to restart Plex through one unix socket.

The host side is a systemd socket unit that runs a single sudoers-allowed command and answers
`restarted`, `busy` (Plex maintenance running), or `failed`. This process holds no container-runtime access.
"""

import asyncio
import os
from typing import Any

SOCKET = os.environ.get("PLEX_RESTART_SOCKET", "")
TIMEOUT_S = 90.0


def enabled() -> bool:
    return bool(SOCKET)


async def restart(path: str | None = None) -> dict[str, Any]:
    target = path or SOCKET
    try:
        reader, writer = await asyncio.open_unix_connection(target)
    except OSError as exc:
        return {
            "error": f"unavailable: Plex restart socket not reachable ({exc.strerror})"
        }
    try:
        reply = await asyncio.wait_for(reader.read(64), TIMEOUT_S)
    except TimeoutError:
        return {"error": f"timeout: no answer within {TIMEOUT_S:.0f} s"}
    except OSError as exc:
        return {"error": f"unavailable: {exc.strerror}"}
    finally:
        writer.close()
    if reply.strip() == b"restarted":
        return {"restarted": True}
    if reply.strip() == b"busy":
        return {
            "busy": True,
            "error": "busy: Plex maintenance is running",
            "next_step": "Wait for the Plex maintenance job to finish, then try again.",
        }
    return {"error": "failed: the host could not restart Plex"}
