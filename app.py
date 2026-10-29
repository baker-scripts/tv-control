"""tv-control: MCP server and web panel for the TVs in devices.yml.

One uvicorn process serves two ports, each on 127.0.0.1 and TS_IP:
  PANEL_PORT (8011): web panel and /healthz; a reverse proxy reaches it on TS_IP behind forward auth.
  MCP_PORT   (8012): MCP streamable HTTP at /mcp, bearer token required.

`python3 /app/app.py call <tool> key=value ...` calls a tool through the local MCP port with the
container's own token, so the /tv:* slash commands share the server's per-device locks.
"""

import asyncio
import base64
import contextlib
import hmac
import json
import logging
import os
import re
import socket
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import panel
import tools
import uvicorn
from inventory import run
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from tools import mcp

TS_IP = os.environ.get("TS_IP", "")
PANEL_PORT = int(os.environ.get("PANEL_PORT", "8011"))
MCP_PORT = int(os.environ.get("MCP_PORT", "8012"))
MCP_TOKEN = os.environ.get("TV_CONTROL_MCP_TOKEN", "")
SCREENSHOT_DIR = Path(os.environ.get("SCREENSHOT_DIR", "/config/screenshots"))
# Host path of the /config mount, used only to print where a CLI screenshot landed.
HOST_CONFIG_DIR = os.environ.get("HOST_CONFIG_DIR", "/config")
TAILNET_GRACE_S = 300
CLI_TIMEOUT_S = 960
TIMER_POLL_S = 10
# Linux IP_FREEBIND: bind TS_IP before tailscaled assigns it.
IP_FREEBIND = getattr(socket, "IP_FREEBIND", 15)

log = logging.getLogger("tv-control")
START = time.monotonic()


def tailnet_ip_assigned() -> bool:
    if not TS_IP:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((TS_IP, 0))
        except OSError:
            return False
    return True


async def healthz(_request: Request) -> JSONResponse:
    rc, out, _ = await run("adb", "version", timeout=5)
    version = next(
        (ln for ln in out.decode().splitlines() if ln.startswith("Version")), None
    )
    assigned = tailnet_ip_assigned()
    late = bool(TS_IP) and not assigned and time.monotonic() - START > TAILNET_GRACE_S
    body = {
        "adb_version": version if rc == 0 else None,
        "ts_ip": TS_IP or None,
        "tailnet_ip_assigned": assigned,
        "mcp_token_set": bool(MCP_TOKEN),
    }
    return JSONResponse(body, status_code=503 if late or rc != 0 else 200)


def bearer_guard(app: Any) -> Any:
    expected = f"Bearer {MCP_TOKEN}".encode()

    async def guarded(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            if not MCP_TOKEN:
                return await JSONResponse(
                    {"error": "TV_CONTROL_MCP_TOKEN not set"}, 503
                )(scope, receive, send)
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, expected):
                resp = JSONResponse(
                    {"error": "unauthorized"},
                    401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                return await resp(scope, receive, send)
        await app(scope, receive, send)

    return guarded


async def timer_loop() -> None:
    while True:
        try:
            for device in await tools.fire_sleep_timers():
                log.info("sleep timer fired for %s", device)
        except Exception:
            log.exception("sleep timer sweep failed")
        await asyncio.sleep(TIMER_POLL_S)


def build_app() -> Any:
    hosts = [f"{h}:{MCP_PORT}" for h in ("127.0.0.1", "localhost", TS_IP) if h]
    mcp_app = bearer_guard(
        mcp.streamable_http_app(
            transport_security=TransportSecuritySettings(allowed_hosts=hosts)
        )
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        # Mounted MCP apps never run their own lifespan; the top app must enter it.
        timers = asyncio.create_task(timer_loop())
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            timers.cancel()

    web = Starlette(
        routes=[Route("/healthz", healthz), *panel.ROUTES],
        middleware=[Middleware(panel.SecurityHeaders)],
        lifespan=lifespan,
    )

    async def by_port(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if (
            scope["type"] != "lifespan"
            and (scope.get("server") or (None, None))[1] == MCP_PORT
        ):
            await mcp_app(scope, receive, send)
            return
        await web(scope, receive, send)

    return by_port


def make_socket(host: str, port: int, freebind: bool) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if freebind:
        sock.setsockopt(socket.IPPROTO_IP, IP_FREEBIND, 1)
    sock.bind((host, port))
    return sock


def serve() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    socks = [make_socket("127.0.0.1", p, False) for p in (PANEL_PORT, MCP_PORT)]
    if TS_IP:
        socks += [make_socket(TS_IP, p, True) for p in (PANEL_PORT, MCP_PORT)]
        if not tailnet_ip_assigned():
            log.warning(
                "TS_IP %s not assigned yet; bound with IP_FREEBIND, accepting once tailscaled is up",
                TS_IP,
            )
    else:
        log.warning("TS_IP unset; serving on 127.0.0.1 only")
    if not MCP_TOKEN:
        log.warning("TV_CONTROL_MCP_TOKEN unset; /mcp answers 503")
    server = uvicorn.Server(
        uvicorn.Config(build_app(), log_level="info", proxy_headers=False)
    )
    server.run(sockets=socks)


def _save_image(device: str, data: str) -> str:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", device) or "screenshot"
    name = f"{safe}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.jpg"
    (SCREENSHOT_DIR / name).write_bytes(base64.b64decode(data))
    return f"{HOST_CONFIG_DIR}/screenshots/{name}"


async def call(tool: str, args: dict[str, str]) -> int:
    from mcp.client.client import Client
    from mcp.client.streamable_http import (  # type: ignore[attr-defined]  # name is re-exported, not in __all__
        create_mcp_http_client,
        streamable_http_client,
    )

    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {MCP_TOKEN}"})
    transport = streamable_http_client(
        f"http://127.0.0.1:{MCP_PORT}/mcp", http_client=http
    )
    async with http, Client(transport, read_timeout_seconds=CLI_TIMEOUT_S) as client:
        result = await client.call_tool(tool, args)
    if result.structured_content is not None:
        print(json.dumps(result.structured_content, indent=2))
    else:
        for item in result.content:
            if item.type == "image":
                print(
                    f"screenshot saved: {_save_image(args.get('device', ''), item.data)}"
                )
            elif item.type == "text":
                print(item.text)
    return 1 if result.is_error else 0


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "call":
        pairs = [a.split("=", 1) for a in sys.argv[3:]]
        if any(len(p) != 2 for p in pairs):
            sys.exit("usage: app.py call <tool> key=value ...")
        sys.exit(asyncio.run(call(sys.argv[2], dict(pairs))))
    serve()


if __name__ == "__main__":
    main()
