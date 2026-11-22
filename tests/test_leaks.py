"""Leak guard: no site-specific value in tracked files. Site values belong in devices.yml and env.

Examples use the documentation ranges (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24) and
example.com / example.ts.net names, which none of these patterns match.
"""

import ipaddress
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {"uv.lock", "LICENSE"}
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
# RFC 1918 and the CGNAT range Tailscale uses. Written as tuples, and the word patterns split,
# so this file does not match its own patterns.
PRIVATE = [
    ipaddress.ip_network(".".join(map(str, net)) + f"/{bits}")
    for net, bits in (
        ((10, 0, 0, 0), 8),
        ((172, 16, 0, 0), 12),
        ((192, 168, 0, 0), 16),
        ((100, 64, 0, 0), 10),
    )
]
WORDS = [
    re.compile("baker" + "flix", re.I),
    re.compile(re.escape("/opt/" + "dockergit")),
    re.compile(r"\b[a-z0-9-]+\.inter" + r"nal\b", re.I),
    re.compile(r"\btail[0-9a-f]{4,}\.ts\.net\b", re.I),
]


def tracked() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
        ).stdout
        names = [n for n in out.decode().split("\0") if n]
    except (OSError, subprocess.CalledProcessError):
        names = [
            str(p.relative_to(ROOT))
            for p in ROOT.rglob("*")
            if p.is_file() and ".git" not in p.parts and ".venv" not in p.parts
        ]
    return [ROOT / n for n in names if Path(n).name not in SKIP]


def private_ips(text: str) -> list[str]:
    found = []
    for raw in IPV4.findall(text):
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if any(ip in net for net in PRIVATE):
            found.append(raw)
    return found


def leaks(text: str) -> list[str]:
    return private_ips(text) + [m.group(0) for w in WORDS for m in w.finditer(text)]


def test_patterns_catch_known_leak_shapes() -> None:
    def ip(*parts: int) -> str:
        return ".".join(map(str, parts))

    samples = [
        ip(10, 1, 2, 3),
        ip(172, 20, 0, 4),
        ip(192, 168, 5, 6),
        ip(100, 101, 102, 103),
        "baker" + "flix.app",
        "/opt/" + "dockergit/x",
        "tv." + "internal",
        "tail" + "1234ab.ts.net",
    ]
    for sample in samples:
        assert leaks(sample), sample
    for clean in ("192.0.2.1", "198.51.100.0/24", "203.0.113.9", "x.example.ts.net"):
        assert not leaks(clean), clean


def test_no_site_specific_values_in_tracked_files() -> None:
    files = tracked()
    assert files
    bad = {}
    for path in files:
        try:
            text = path.read_text()
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        if hits := leaks(text):
            bad[str(path.relative_to(ROOT))] = sorted(set(hits))
    assert not bad, f"move these values to devices.yml or env: {bad}"
