"""scan, inventory, and app glue. No device is contacted: sockets are loopback, subprocesses are `echo`/`sleep`."""

import asyncio
import base64
import ipaddress
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import app
import inventory
import pytest
import scan
from inventory import DeviceError


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_run_captures_stdout_and_times_out() -> None:
    rc, out, _ = run(inventory.run("echo", "hi"))
    assert (rc, out.strip()) == (0, b"hi")
    rc, _, err = run(inventory.run("sleep", "5", timeout=0.1))
    assert rc == 124 and "timeout" in err


def test_yq_failure_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bad = tmp_path / "bad.yml"
    bad.write_text("a: [unclosed")
    monkeypatch.setattr(inventory, "DEVICES_FILE", bad)
    with pytest.raises(DeviceError):
        run(inventory.load_devices())


@pytest.mark.parametrize(
    "sites,err",
    [
        ({}, "no routed LAN"),
        ({"x": {"cidr": "8.8.8.0/24"}}, "private IPv4"),
        ({"x": {"cidr": "10.0.0.0/8"}}, "private IPv4"),
    ],
)
def test_site_network_rejects(
    monkeypatch: pytest.MonkeyPatch, sites: dict[str, Any], err: str
) -> None:
    monkeypatch.setattr(scan, "load_sites", AsyncMock(return_value=sites))
    with pytest.raises(DeviceError, match=err):
        run(scan.site_network("x"))


def test_site_network_accepts_private_24(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        scan, "load_sites", AsyncMock(return_value={"x": {"cidr": "198.51.100.0/24"}})
    )
    assert str(run(scan.site_network("x"))) == "198.51.100.0/24"


def test_open_probe_sees_listener_and_closed_port() -> None:
    async def go() -> tuple[bool, bool]:
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        sem = asyncio.Semaphore(2)
        seen = await scan._open("127.0.0.1", port, sem)
        server.close()
        await server.wait_closed()
        return seen, await scan._open("127.0.0.1", port, sem)

    assert run(go()) == (True, False)


def test_scan_site_splits_known_and_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    net = ipaddress.ip_network("192.0.2.0/30")
    monkeypatch.setattr(scan, "site_network", AsyncMock(return_value=net))
    monkeypatch.setattr(
        scan,
        "load_devices",
        AsyncMock(return_value=[{"name": "k", "ip": "192.0.2.1"}]),
    )

    async def fake_open(ip: str, port: int, _sem: asyncio.Semaphore) -> bool:
        return port in (5555, 3001) and ip in ("192.0.2.1", "192.0.2.2")

    monkeypatch.setattr(scan, "_open", fake_open)
    out = run(scan.scan_site("x"))
    assert out["known_hits"] == ["192.0.2.1"]
    assert [(u["ip"], u["guessed_type"]) for u in out["unknown"]] == [
        ("192.0.2.2", "webos")
    ]


def stage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **over: Any) -> Any:
    net = ipaddress.ip_network("192.0.2.0/24")
    monkeypatch.setattr(scan, "ENROLL_DIR", tmp_path)
    monkeypatch.setattr(scan, "site_network", AsyncMock(return_value=net))
    monkeypatch.setattr(scan, "load_devices", AsyncMock(return_value=[{"name": "old"}]))
    req = {
        "site": "x",
        "name": "new-tv",
        "ip": "192.0.2.5",
        "port": 5555,
        "type": "shield",
        "config": "simple",
        **over,
    }
    return run(scan.stage_enroll(req))


def test_stage_enroll_writes_snippet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = stage(monkeypatch, tmp_path)
    assert "type: shield" in out["snippet"] and "config: simple" in out["snippet"]
    assert Path(out["staged"]).read_text() == out["snippet"]


def test_stage_enroll_webos_has_no_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = stage(monkeypatch, tmp_path, type="webos", port=3001, config="")
    assert "config:" not in out["snippet"]


@pytest.mark.parametrize(
    "over",
    [
        {"ip": "203.0.113.1"},
        {"ip": "nope"},
        {"port": 22},
        {"name": "A"},
        {"type": "toaster"},
        {"name": "old"},
        {"config": "weird"},
    ],
)
def test_stage_enroll_rejects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, over: dict[str, Any]
) -> None:
    with pytest.raises(DeviceError):
        stage(monkeypatch, tmp_path, **over)


def test_tailnet_ip_assigned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "TS_IP", "")
    assert app.tailnet_ip_assigned() is False
    monkeypatch.setattr(app, "TS_IP", "127.0.0.1")
    assert app.tailnet_ip_assigned() is True
    monkeypatch.setattr(app, "TS_IP", "203.0.113.9")
    assert app.tailnet_ip_assigned() is False


@pytest.mark.parametrize(
    "rc,late,status", [(0, False, 200), (1, False, 503), (0, True, 503)]
)
def test_healthz(
    monkeypatch: pytest.MonkeyPatch, rc: int, late: bool, status: int
) -> None:
    monkeypatch.setattr(
        app, "run", AsyncMock(return_value=(rc, b"Version 35.0.2\n", ""))
    )
    monkeypatch.setattr(app, "TS_IP", "203.0.113.9" if late else "")
    monkeypatch.setattr(app, "START", -10_000.0)
    resp = run(app.healthz(None))  # type: ignore[arg-type]
    assert resp.status_code == status
    assert json.loads(resp.body)["mcp_token_set"] is False


def asgi(headers: list[tuple[bytes, bytes]]) -> tuple[int, bool]:
    sent: list[dict[str, Any]] = []
    reached: list[bool] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        reached.append(True)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b""}

    guard = app.bearer_guard(inner)
    run(guard({"type": "http", "headers": headers}, receive, send))
    return (sent[0]["status"] if sent else 0), bool(reached)


def test_bearer_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "MCP_TOKEN", "")
    assert asgi([]) == (503, False)
    monkeypatch.setattr(app, "MCP_TOKEN", "s3cret")
    assert asgi([(b"authorization", b"Bearer wrong")]) == (401, False)
    assert asgi([(b"authorization", b"Bearer s3cret")]) == (0, True)


def test_by_port_routes_mcp_port_through_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app, "MCP_TOKEN", "")
    web = app.build_app()
    seen: list[int] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            seen.append(message["status"])

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/mcp",
        "headers": [],
        "query_string": b"",
        "server": ("127.0.0.1", app.MCP_PORT),
    }
    run(web(scope, receive, send))
    assert seen == [503]


def test_save_image_sanitises_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(app, "SCREENSHOT_DIR", tmp_path)
    shown = app._save_image("../evil tv", base64.b64encode(b"jpg").decode())
    name = shown.rsplit("/", 1)[1]
    assert name.startswith("___evil_tv-") and (tmp_path / name).read_bytes() == b"jpg"


def test_make_socket_binds_loopback() -> None:
    sock = app.make_socket("127.0.0.1", 0, False)
    assert sock.getsockname()[0] == "127.0.0.1"
    sock.close()


def test_main_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["app.py", "call", "t", "novalue"])
    with pytest.raises(SystemExit, match="usage"):
        app.main()


def test_main_call_exits_with_result(monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, Any] = {}

    async def fake_call(tool: str, args: dict[str, str]) -> int:
        called.update(tool=tool, args=args)
        return 3

    monkeypatch.setattr(app, "call", fake_call)
    monkeypatch.setattr(sys, "argv", ["app.py", "call", "tool", "a=b"])
    with pytest.raises(SystemExit) as exc:
        app.main()
    assert exc.value.code == 3 and called == {"tool": "tool", "args": {"a": "b"}}


def test_main_serves_without_call(monkeypatch: pytest.MonkeyPatch) -> None:
    served: list[bool] = []
    monkeypatch.setattr(app, "serve", lambda: served.append(True))
    monkeypatch.setattr(sys, "argv", ["app.py"])
    app.main()
    assert served == [True]
