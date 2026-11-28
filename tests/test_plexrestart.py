"""Restart Plex: admin-only, confirmed, rate limited, logged by user. No real socket or container is touched."""

import asyncio
import json
import logging
import os
import socket
import tempfile
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request
from test_panel import DEVICES, PROXY_KEY, call, headers

import panel
import plexrestart


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(panel, "PROXY_KEY", PROXY_KEY)
    monkeypatch.setattr(panel, "load_devices", AsyncMock(return_value=DEVICES))
    monkeypatch.setattr(panel, "_last_power", {})
    monkeypatch.setattr(panel, "_last_plex_restart", None)
    monkeypatch.setattr(panel.plexrestart, "SOCKET", "/run/unused.sock")


def test_non_admin_is_denied_and_nothing_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    run = AsyncMock(return_value={"restarted": True})
    monkeypatch.setattr(panel.plexrestart, "restart", run)
    for groups in ("tv-site-home", "tv-site-home|tv-site-rpm", "", "tv-admins"):
        resp = call(
            "restartplex",
            {"confirm": "plex"},
            name="plex",
            hdrs=headers(groups=groups),
        )
        assert resp.status_code == 403
    run.assert_not_awaited()


def test_branch_checks_admin_even_if_verb_reaches_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = AsyncMock(return_value={"restarted": True})
    monkeypatch.setattr(panel.plexrestart, "restart", run)
    monkeypatch.setattr(
        panel, "USER_POST_VERBS", panel.USER_POST_VERBS | {"restartplex"}
    )
    monkeypatch.setattr(
        panel, "_visible_devices", AsyncMock(return_value=[{"name": "plex"}])
    )
    resp = call(
        "restartplex",
        {"confirm": "plex"},
        name="plex",
        hdrs=headers(groups="tv-site-home"),
    )
    assert resp.status_code == 403
    run.assert_not_awaited()


def test_missing_proxy_key_and_csrf_header_are_refused() -> None:
    assert (
        call(
            "restartplex", {"confirm": "plex"}, name="plex", hdrs=headers(key=None)
        ).status_code
        == 403
    )
    no_csrf = [h for h in headers() if h[0] != b"x-tv-panel"]
    assert (
        call("restartplex", {"confirm": "plex"}, name="plex", hdrs=no_csrf).status_code
        == 403
    )


def test_admin_needs_the_typed_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    run = AsyncMock(return_value={"restarted": True})
    monkeypatch.setattr(panel.plexrestart, "restart", run)
    assert call("restartplex", {}, name="plex").status_code == 400
    assert call("restartplex", {"confirm": "mock-tv"}, name="plex").status_code == 400
    run.assert_not_awaited()


def test_only_the_plex_target_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    run = AsyncMock(return_value={"restarted": True})
    monkeypatch.setattr(panel.plexrestart, "restart", run)
    assert call("restartplex", {"confirm": "plex"}, name="sonarr").status_code == 404
    run.assert_not_awaited()


def test_admin_restart_logs_the_user_and_rate_limits(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    run = AsyncMock(return_value={"restarted": True})
    monkeypatch.setattr(panel.plexrestart, "restart", run)
    lines: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: lines.append(record.getMessage())  # type: ignore[method-assign]
    panel.audit.addHandler(handler)
    try:
        resp = call("restartplex", {"confirm": "plex"}, name="plex")
        again = call("restartplex", {"confirm": "plex"}, name="plex")
    finally:
        panel.audit.removeHandler(handler)
    assert resp.status_code == 200 and json.loads(resp.body) == {"restarted": True}
    assert again.status_code == 429 and "Retry-After" in again.headers
    run.assert_awaited_once()
    assert any(
        "plex-restart" in line
        and 'user="tester"' in line
        and "result=restarted" in line
        for line in lines
    )


def test_unconfigured_socket_answers_503_without_a_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(panel.plexrestart, "SOCKET", "")
    resp = call("restartplex", {"confirm": "plex"}, name="plex")
    assert resp.status_code == 503


def test_button_is_rendered_for_admin_only() -> None:
    def page(groups: str) -> str:
        request = Request({"type": "http", "headers": headers(groups=groups)})
        return asyncio.run(panel.index(request)).body.decode()

    assert 'id="restart-plex"' in page("tv-admin")
    assert 'id="plex-confirm"' in page("tv-admin")
    assert "restart-plex" not in page("tv-site-home")
    assert "plex-confirm" not in page("tv-site-home")


def serve(path: str, reply: bytes) -> threading.Thread:
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(path)
    srv.listen(1)

    def run() -> None:
        conn, _ = srv.accept()
        conn.sendall(reply)
        conn.close()
        srv.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


@pytest.mark.parametrize(
    "reply,ok",
    [(b"restarted\n", True), (b"failed\n", False), (b"", False), (b"junk", False)],
)
def test_client_reads_the_unit_reply(reply: bytes, ok: bool) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "r.sock")
        thread = serve(path, reply)
        out = asyncio.run(plexrestart.restart(path))
        thread.join(2)
    assert ("restarted" in out) is ok
    assert ok or "error" in out


def test_client_reports_a_missing_socket() -> None:
    out = asyncio.run(plexrestart.restart("/nonexistent/restart.sock"))
    assert out["error"].startswith("unavailable")


def test_module_never_touches_docker_directly() -> None:
    text = Path(plexrestart.__file__).read_text()
    assert "docker" not in text.lower().replace("docker socket", "")
    assert "subprocess" not in text
