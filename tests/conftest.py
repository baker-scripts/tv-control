"""Shared fixtures: the message policy from devices.example.yml (or DEVICES_FILE), so tests read real config."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEVICES = Path(os.environ.get("DEVICES_FILE", ROOT / "devices.example.yml"))


def read_policy() -> dict[str, Any]:
    out = subprocess.run(
        ["yq", "-o=json", ".messages", str(DEVICES)],
        capture_output=True,
        check=True,
    ).stdout
    policy: dict[str, Any] = json.loads(out)
    return policy


@pytest.fixture
def policy() -> dict[str, Any]:
    return read_policy()


@pytest.fixture(autouse=True)
def no_alias_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests mock the inventory; app_aliases defaults to none instead of reading DEVICES_FILE."""
    import tools

    async def none() -> dict[str, Any]:
        return {}

    monkeypatch.setattr(tools, "load_app_aliases", none)
