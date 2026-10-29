"""Static guards for tv-control. Run: uv run --locked pytest"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


SOURCES = sorted(ROOT.glob("*.py"))


def test_no_adb_disconnect() -> None:
    # adb disconnect drops the device for every other client of the shared ADB server.
    assert SOURCES
    for src in SOURCES:
        assert not re.search(r"""["']disconnect["']""", src.read_text()), src.name


def test_no_power_off_reboot_or_data_wipe() -> None:
    for src in SOURCES:
        text = src.read_text()
        assert not re.search(r"""["']reboot["'],\s*["']-p["']""", text), src.name
        assert "reboot -p" not in text.replace("`reboot -p`", ""), src.name
        assert not re.search(r"pm (clear|uninstall)", text), src.name


def test_mcp_tools_never_run_real_lockdown_or_baseline() -> None:
    tools = (ROOT / "tools.py").read_text()
    mcp_part = tools.split("# ---------- MCP ----------", 1)[1]
    assert "real=True" not in mcp_part
    assert "do_baseline(device, real=False)" in mcp_part
    assert "do_lockdown(device, real=False)" in mcp_part


def test_panel_real_runs_need_typed_confirm() -> None:
    panel = (ROOT / "panel.py").read_text()
    assert 'data.get("confirm") != name' in panel
