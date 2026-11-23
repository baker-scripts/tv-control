"""Leak guard: no personal or site-specific value in tracked files.

The checks live in scripts/check_pii.py, which pre-commit and CI also run. Real names come from
PII_TERMS or PII_TERMS_FILE, never from this repo, so these tests use generic shapes and a fake term.
Examples use the documentation ranges (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24) and
example.com / example.ts.net names.
"""

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_pii.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_pii", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


check = load()
TERMS = [
    (
        "listed term",
        re.compile(r"(?<![A-Za-z0-9])zorgblat(?![A-Za-z0-9])", re.IGNORECASE),
    )
]


def ip(*parts: int) -> str:
    return ".".join(map(str, parts))


@pytest.mark.parametrize(
    "sample",
    [
        ip(10, 1, 2, 3),
        ip(172, 20, 0, 4),
        ip(192, 168, 5, 6),
        ip(100, 101, 102, 103),
        ip(8, 8, 4, 4),
        "mac 3c:22:fb:" + "01:02:03",
        "a." + "b@" + "corp.test",
        "(312) " + "555-0142",
        "12 Oak " + "Street",
        "baker" + "flix.app",
        "/opt/" + "dockergit/x",
        "tv." + "internal",
        "tail" + "1234ab.ts.net",
        "device Zorgblat-room",
    ],
)
def test_patterns_catch_leak_shapes(sample: str) -> None:
    assert check.line_hits(sample, TERMS), sample


@pytest.mark.parametrize(
    "clean",
    [
        "192.0.2.1",
        "198.51.100.0/24",
        "203.0.113.9",
        "127.0.0.1:8011",
        "x.example.ts.net",
        "user@relay.example.com",
        "00:00:5e:00:53:01",
        "aa:bb:cc:dd:ee:ff",
        "zorgblats and unzorgblat",
    ],
)
def test_documentation_values_pass(clean: str) -> None:
    assert not check.line_hits(clean, TERMS), clean


def test_terms_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PII_TERMS", "# c\nZorgblat\nre:\\bfoo\\d\\b\n")
    terms, have = check.load_terms()
    assert have and len(terms) == 2
    assert check.line_hits("see foo7", terms)


def test_no_list_falls_back_to_generic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("PII_TERMS", raising=False)
    monkeypatch.setenv("PII_TERMS_FILE", str(tmp_path / "none.txt"))
    terms, have = check.load_terms()
    assert terms == [] and not have


def test_no_personal_values_in_tracked_files() -> None:
    terms, _ = check.load_terms()
    found = check.scan(check.tracked(), terms)
    assert not found, found
